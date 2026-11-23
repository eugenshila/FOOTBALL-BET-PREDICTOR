"""Backups for Verifix: database + part photos + logins + shop profile.

Why this module exists
----------------------
Inventory and uploaded media are git-ignored shop data, so Git never backs
them up — and ``uploads/`` (where every part's photo lives) is ignored too.
Losing the laptop used to mean losing the stock list *and* every photo
attached to a part. This module packages everything the shop needs into one
timestamped backup:

* the inventory database (SQLite file copy, or a ``pg_dump`` custom-format
  dump for PostgreSQL) — this is what links each part to its photo via
  ``parts.image_path``;
* ``uploads/`` as a zip — every photo attached to a part, plus the
  ``.image-review`` manifests;
* ``data/local-auth.db`` — the local login accounts (separate from the
  inventory database, and easy to forget);
* ``data/shop_profile.json`` and ``data/catalog.json`` — shop identity and
  the reference catalogue.

Before packaging, the photo links are verified: every ``image_path`` stored
on a part is checked against the files actually in ``uploads/``. Missing
photos are reported in the manifest instead of silently restoring a
database full of broken image links.

Backups can be kept locally (USB drive / second folder) and/or uploaded to
any S3-compatible bucket (AWS S3, Cloudflare R2, Backblaze B2, Wasabi, …)
with boto3. Nothing here prints secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config

APP_NAME = "verifix"
BACKUP_DIR_NAME = "verifix-backup"
MANIFEST_NAME = "manifest.json"
DB_ARTIFACT_SQLITE = "inventory.db"
DB_ARTIFACT_POSTGRES = "inventory.dump"
UPLOADS_ARTIFACT = "uploads.zip"
DATA_FILES_ARTIFACT = "data-files.zip"

# Files inside data/ that are shop data worth keeping (never .env secrets).
DATA_FILES = ("local-auth.db", "shop_profile.json", "catalog.json")


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


# ── paths & settings ────────────────────────────────────────────────────────

def _data_dir() -> Path:
    return Path(config.DATA_DIR)


def _upload_dir() -> Path:
    return Path(config.UPLOAD_DIR)


def _db_path() -> Path:
    return Path(config.DB_PATH)


def _database_url() -> str:
    return (config.settings.database_url or "").strip()


def _is_postgres(database_url: str | None = None) -> bool:
    url = _database_url() if database_url is None else database_url
    return url.startswith(("postgres://", "postgresql://"))


def cloud_settings() -> dict[str, str]:
    """S3-compatible bucket settings from the environment (never printed)."""
    return {
        "bucket": os.getenv("CLOUD_BACKUP_BUCKET", "").strip(),
        "prefix": os.getenv("CLOUD_BACKUP_PREFIX", "verifix/backups").strip("/"),
        "region": os.getenv("CLOUD_BACKUP_REGION", "us-east-1").strip(),
        "endpoint": os.getenv("CLOUD_BACKUP_ENDPOINT", "").strip(),
        "keep": os.getenv("CLOUD_BACKUP_KEEP", "30").strip(),
    }


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── photo link verification ─────────────────────────────────────────────────

def _resolve_photo(raw: str, upload_dir: Path) -> tuple[Path, str] | None:
    """Where a stored ``image_path`` points, and which kind of photo it is.

    Returns ``(path, "uploads")`` for shop photos (these must be in the
    backup) or ``(path, "seed")`` for the demo photos bundled inside the app
    (those ship with the code, so they cannot be lost). ``None`` when the
    path points somewhere else entirely.
    """
    if not raw:
        return None
    value = raw.replace("\\", "/")
    if value.startswith("/media/uploads/"):
        candidate = upload_dir / value.removeprefix("/media/uploads/")
    elif value.startswith("uploads/"):
        candidate = upload_dir / value.removeprefix("uploads/")
    else:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = config.BASE_DIR / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for root, kind in ((upload_dir.resolve(), "uploads"), (Path(config.SEED_DIR).resolve(), "seed")):
        if resolved.is_relative_to(root):
            return resolved, kind
    return None


def _part_image_rows(
    database_url: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[tuple[int, str]], int | None]:
    """Return ([(part_id, image_path)], part_count) or raise BackupError."""
    url = _database_url() if database_url is None else database_url
    if _is_postgres(url):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise BackupError(
                "Photo check needs psycopg for PostgreSQL. Install it with "
                "'pip install -r requirements-postgres.txt'."
            ) from exc
        try:
            with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, image_path FROM parts")
                    rows = [(int(r["id"]), r["image_path"] or "") for r in cur.fetchall()]
                    cur.execute("SELECT COUNT(*) AS n FROM parts")
                    count = int(cur.fetchone()["n"])
        except Exception as exc:  # noqa: BLE001 - reported, backup continues
            raise BackupError(f"Cannot read parts from PostgreSQL: {exc}") from exc
        return rows, count
    path = _db_path() if db_path is None else Path(db_path)
    if not path.exists():
        raise BackupError(f"Inventory database not found at {path}. Start the app once first.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, image_path FROM parts").fetchall()
            count = int(conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Cannot read parts from {path}: {exc}") from exc
    return [(int(r[0]), r[1] or "") for r in rows], count


def photo_link_report(
    upload_dir: Path | None = None,
    database_url: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Check every part's photo is really present in the uploads folder.

    Returns a dict with ``checked``, ``parts_with_photo``, ``photos_found``,
    ``missing`` (list of "part <id>: <path>") and ``orphan_files`` (photos
    in uploads/ that no part references). Never raises: an unreadable
    database yields ``checked=False`` so the backup can still proceed.
    """
    upload_dir = _upload_dir() if upload_dir is None else Path(upload_dir)
    report: dict = {
        "checked": False,
        "reason": "",
        "parts_with_photo": 0,
        "photos_found": 0,
        "missing": [],
        "orphan_files": 0,
        "part_count": None,
    }
    try:
        rows, count = _part_image_rows(database_url, db_path)
    except BackupError as exc:
        report["reason"] = str(exc)
        return report
    report["checked"] = True
    report["part_count"] = count

    referenced: set[Path] = set()
    missing: list[str] = []
    found = 0
    report["bundled"] = 0
    for part_id, raw in rows:
        if not raw:
            continue
        located = _resolve_photo(raw, upload_dir)
        if located is not None and located[0].is_file():
            if located[1] == "seed":
                # Demo photo that ships with the app — nothing to back up.
                report["bundled"] += 1
                continue
            report["parts_with_photo"] += 1
            found += 1
            referenced.add(located[0])
        else:
            report["parts_with_photo"] += 1
            missing.append(f"part {part_id}: {raw}")
    report["photos_found"] = found
    report["missing"] = missing

    if upload_dir.is_dir():
        on_disk = {
            p.resolve()
            for p in upload_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        }
        report["orphan_files"] = len(on_disk - referenced)
    return report


# ── packaging ───────────────────────────────────────────────────────────────

def _snapshot_sqlite(dest: Path, source: Path | None = None) -> None:
    """Consistent copy of the live SQLite file via the backup API."""
    source = _db_path() if source is None else Path(source)
    if not source.exists():
        raise BackupError(f"Inventory database not found at {source}. Start the app once first.")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _pg_env(database_url: str) -> dict[str, str]:
    """PostgreSQL credentials as env vars (never on a command line)."""
    parsed = urlparse(database_url)
    env = dict(os.environ)
    env["PGHOST"] = parsed.hostname or "localhost"
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGDATABASE"] = unquote(parsed.path.lstrip("/"))
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env


def _find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    override = os.getenv(f"{name.upper().replace('-', '_')}_PATH", "").strip()
    if override and Path(override).exists():
        return override
    return None


def _snapshot_postgres(dest: Path) -> None:
    pg_dump = _find_tool("pg_dump")
    if not pg_dump:
        raise BackupError(
            "pg_dump was not found. Add PostgreSQL's bin folder to PATH or set "
            "PG_DUMP_PATH in .env (see docs/CLOUD_BACKUPS.md)."
        )
    result = subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", "--no-acl", "--file", str(dest)],
        env=_pg_env(_database_url()),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BackupError(f"pg_dump failed: {result.stderr.strip()[:500]}")


def _zip_uploads(dest: Path, upload_dir: Path) -> int:
    """Zip the whole uploads tree (photos + .image-review manifests)."""
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        if upload_dir.is_dir():
            for path in sorted(upload_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, Path("uploads") / path.relative_to(upload_dir))
                    count += 1
    return count


def _zip_data_files(dest: Path, data_dir: Path) -> list[str]:
    included: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in DATA_FILES:
            source = data_dir / name
            if source.is_file():
                archive.write(source, Path("data") / name)
                included.append(name)
    return included


def build_local_backup(
    dest_root: Path | None = None,
    *,
    upload_dir: Path | None = None,
    data_dir: Path | None = None,
    db_path: Path | None = None,
    database_url: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Create one timestamped backup folder and return its manifest.

    Layout::

        <dest_root>/verifix-backup-<timestamp>/
            inventory.db | inventory.dump   (database, incl. photo links)
            uploads.zip                     (every part photo)
            data-files.zip                  (logins, shop profile, catalogue)
            manifest.json                   (sizes, checksums, photo check)
    """
    dest_root = Path(dest_root) if dest_root is not None else config.BASE_DIR / "backups"
    upload_dir = _upload_dir() if upload_dir is None else Path(upload_dir)
    data_dir = _data_dir() if data_dir is None else Path(data_dir)
    url = _database_url() if database_url is None else database_url
    stamp = timestamp or _timestamp_now()

    backup_dir = dest_root / f"{BACKUP_DIR_NAME}-{stamp}"
    if backup_dir.exists():
        raise BackupError(f"Backup folder already exists: {backup_dir}")
    backup_dir.mkdir(parents=True)

    try:
        engine = "postgres" if _is_postgres(url) else "sqlite"
        db_artifact = DB_ARTIFACT_POSTGRES if engine == "postgres" else DB_ARTIFACT_SQLITE
        if engine == "postgres":
            _snapshot_postgres(backup_dir / db_artifact)
        else:
            _snapshot_sqlite(backup_dir / db_artifact, db_path)

        upload_count = _zip_uploads(backup_dir / UPLOADS_ARTIFACT, upload_dir)
        data_included = _zip_data_files(backup_dir / DATA_FILES_ARTIFACT, data_dir)
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)  # no half-finished backups
        raise

    files = []
    for name in (db_artifact, UPLOADS_ARTIFACT, DATA_FILES_ARTIFACT):
        path = backup_dir / name
        files.append({"name": name, "size": path.stat().st_size, "sha256": sha256_of(path)})

    manifest = {
        "app": APP_NAME,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "files": files,
        "photo_check": photo_link_report(upload_dir, url, db_path),
        "upload_file_count": upload_count,
        "data_files": data_included,
        "backup_dir": str(backup_dir),
    }
    (backup_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def summarize(manifest: dict) -> str:
    """One human-readable summary line block for CLI output."""
    check = manifest.get("photo_check", {})
    lines = [
        f"Backup: {manifest.get('backup_dir', '')}",
        f"Engine: {manifest.get('engine', '?')} · "
        f"parts: {check.get('part_count') if check.get('part_count') is not None else '?'} · "
        f"upload files: {manifest.get('upload_file_count', 0)}",
    ]
    if check.get("checked"):
        line = (
            f"Photos: {check.get('photos_found', 0)}/{check.get('parts_with_photo', 0)} "
            "part photos present in uploads/"
        )
        if check.get("bundled"):
            line += f" (+{check['bundled']} demo photos bundled with the app)"
        lines.append(line)
        for item in check.get("missing", [])[:10]:
            lines.append(f"  ! missing photo — {item}")
        if check.get("missing"):
            lines.append("  (fix these in Inventory → Edit → photo, then back up again)")
    else:
        lines.append(f"Photos: could not verify ({check.get('reason', 'unknown')})")
    for upload in manifest.get("uploads", []):
        lines.append(f"Cloud ({upload.get('destination')}): {upload.get('target')}")
    for note in manifest.get("notes", []):
        lines.append(note)
    for error in manifest.get("errors", []):
        lines.append(f"! {error}")
    return "\n".join(lines)


# ── cloud upload (S3-compatible, via boto3) ─────────────────────────────────

def _s3_client(settings: dict[str, str]):
    try:
        import boto3
    except ImportError as exc:
        raise BackupError(
            "Cloud upload needs boto3. Install it with: pip install boto3"
        ) from exc
    kwargs: dict = {"region_name": settings["region"] or "us-east-1"}
    if settings["endpoint"]:
        kwargs["endpoint_url"] = settings["endpoint"]
    return boto3.client("s3", **kwargs)


def _backup_prefix(settings: dict[str, str], stamp: str) -> str:
    return f"{settings['prefix']}/{BACKUP_DIR_NAME}-{stamp}"


def upload_backup(
    manifest: dict,
    *,
    settings: dict[str, str] | None = None,
    keep: int | None = None,
) -> str:
    """Upload one local backup folder to the configured bucket. Returns key prefix."""
    settings = cloud_settings() if settings is None else settings
    if not settings["bucket"]:
        raise BackupError(
            "Set CLOUD_BACKUP_BUCKET in .env first (see docs/CLOUD_BACKUPS.md)."
        )
    if not (os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")):
        raise BackupError(
            "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env first. "
            "Never commit .env to Git."
        )
    backup_dir = Path(manifest["backup_dir"])
    stamp = backup_dir.name.removeprefix(f"{BACKUP_DIR_NAME}-")
    prefix = _backup_prefix(settings, stamp)
    client = _s3_client(settings)
    for entry in manifest["files"]:
        client.upload_file(
            str(backup_dir / entry["name"]),
            settings["bucket"],
            f"{prefix}/{entry['name']}",
        )
    client.upload_file(
        str(backup_dir / MANIFEST_NAME),
        settings["bucket"],
        f"{prefix}/{MANIFEST_NAME}",
    )
    if keep is None:
        try:
            keep = int(settings.get("keep") or 30)
        except ValueError:
            keep = 30
    if keep > 0:
        prune_old_backups(client, settings, keep=keep)
    return prefix


def list_cloud_backups(
    *, settings: dict[str, str] | None = None
) -> list[str]:
    """Sorted backup timestamps stored in the bucket (oldest first)."""
    settings = cloud_settings() if settings is None else settings
    if not settings["bucket"]:
        raise BackupError("Set CLOUD_BACKUP_BUCKET in .env first.")
    client = _s3_client(settings)
    paginator = client.get_paginator("list_objects_v2")
    stamps: set[str] = set()
    for page in paginator.paginate(Bucket=settings["bucket"], Prefix=settings["prefix"]):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = Path(key).name
            if name == MANIFEST_NAME:
                stamps.add(Path(key).parent.name.removeprefix(f"{BACKUP_DIR_NAME}-"))
    return sorted(stamps)


def prune_old_backups(client, settings: dict[str, str], *, keep: int) -> int:
    """Delete all but the newest ``keep`` backups. Returns deleted count."""
    paginator = client.get_paginator("list_objects_v2")
    by_stamp: dict[str, list[str]] = {}
    for page in paginator.paginate(Bucket=settings["bucket"], Prefix=settings["prefix"]):
        for obj in page.get("Contents", []):
            stamp = Path(obj["Key"]).parent.name
            by_stamp.setdefault(stamp, []).append(obj["Key"])
    stamps = sorted(by_stamp)
    excess = stamps[: max(0, len(stamps) - keep)]
    deleted = 0
    for stamp in excess:
        for key in by_stamp[stamp]:
            client.delete_object(Bucket=settings["bucket"], Key=key)
            deleted += 1
    return deleted


# ── restore ─────────────────────────────────────────────────────────────────

def _verify_manifest(backup_dir: Path) -> dict:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise BackupError(f"No {MANIFEST_NAME} in {backup_dir} — not a Verifix backup.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest.get("files", []):
        path = backup_dir / entry["name"]
        if not path.exists():
            raise BackupError(f"Backup is incomplete: missing {entry['name']}")
        if sha256_of(path) != entry["sha256"]:
            raise BackupError(f"Backup file failed its checksum: {entry['name']}")
    return manifest


def _safety_copy(
    dest_root: Path,
    *,
    upload_dir: Path,
    data_dir: Path,
    db_path: Path,
    database_url: str,
) -> Path:
    """Copy the current state aside before a restore overwrites it."""
    safety = dest_root / f"pre-restore-{_timestamp_now()}"
    safety.mkdir(parents=True, exist_ok=True)
    if _is_postgres(database_url):
        try:
            _snapshot_postgres(safety / DB_ARTIFACT_POSTGRES)
        except BackupError:
            pass  # a dead database has nothing to save; restore continues
    else:
        if db_path.exists():
            shutil.copy2(db_path, safety / DB_ARTIFACT_SQLITE)
    if upload_dir.is_dir():
        shutil.copytree(upload_dir, safety / "uploads", dirs_exist_ok=True)
    for name in DATA_FILES:
        source = data_dir / name
        if source.is_file():
            (safety / "data").mkdir(exist_ok=True)
            shutil.copy2(source, safety / "data" / name)
    return safety


def _extract_uploads(archive_path: Path, upload_dir: Path) -> None:
    """Extract uploads.zip (stored under ``uploads/``) into upload_dir safely."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    root = upload_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            rel = member.removeprefix("uploads/")
            target = (upload_dir / rel).resolve()
            if not target.is_relative_to(root):
                continue  # zip-slip guard: never write outside uploads/
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))


def restore_backup(
    backup_dir: Path,
    *,
    upload_dir: Path | None = None,
    data_dir: Path | None = None,
    db_path: Path | None = None,
    database_url: str | None = None,
    force: bool = False,
    safety_root: Path | None = None,
) -> dict:
    """Restore a local backup folder. Stop the app first.

    The current database, uploads and data files are copied to a
    ``pre-restore-<timestamp>`` safety folder before anything is
    overwritten. Existing targets require ``force=True``.
    """
    backup_dir = Path(backup_dir)
    upload_dir = _upload_dir() if upload_dir is None else Path(upload_dir)
    data_dir = _data_dir() if data_dir is None else Path(data_dir)
    db_target = _db_path() if db_path is None else Path(db_path)
    url = _database_url() if database_url is None else database_url
    manifest = _verify_manifest(backup_dir)
    targets_exist = (
        (not _is_postgres(url) and db_target.exists())
        or upload_dir.is_dir() and any(upload_dir.iterdir())
        or any((data_dir / name).exists() for name in DATA_FILES)
    )
    if targets_exist and not force:
        raise BackupError(
            "Restore would overwrite existing data. Stop the app, then pass "
            "--force (a safety copy of the current state is kept first)."
        )

    safety_root = Path(safety_root) if safety_root is not None else config.BASE_DIR / "backups"
    safety = _safety_copy(
        safety_root, upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_target, database_url=url,
    )

    engine = manifest.get("engine", "postgres" if _is_postgres(url) else "sqlite")
    if engine == "postgres":
        pg_restore = _find_tool("pg_restore")
        if not pg_restore:
            raise BackupError(
                "pg_restore was not found. Add PostgreSQL's bin folder to PATH "
                "or set PG_RESTORE_PATH in .env."
            )
        dump = backup_dir / DB_ARTIFACT_POSTGRES
        result = subprocess.run(
            [pg_restore, "--clean", "--if-exists", "--no-owner", "--no-acl",
             "-d", url, str(dump)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "ERROR" in result.stderr:
            raise BackupError(f"pg_restore failed: {result.stderr.strip()[:500]}")
    else:
        source = backup_dir / DB_ARTIFACT_SQLITE
        db_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, db_target)

    uploads_zip = backup_dir / UPLOADS_ARTIFACT
    if uploads_zip.exists():
        _extract_uploads(uploads_zip, upload_dir)
    data_zip = backup_dir / DATA_FILES_ARTIFACT
    if data_zip.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(data_zip) as archive:
            for member in archive.namelist():
                if member.startswith("data/") and not member.endswith("/"):
                    target = data_dir / Path(member).name
                    target.write_bytes(archive.read(member))

    manifest["safety_copy"] = str(safety)
    return manifest


def download_cloud_backup(
    stamp: str,
    *,
    settings: dict[str, str] | None = None,
    dest_root: Path | None = None,
) -> Path:
    """Download one cloud backup to a local folder. Returns the folder."""
    settings = cloud_settings() if settings is None else settings
    if not settings["bucket"]:
        raise BackupError("Set CLOUD_BACKUP_BUCKET in .env first.")
    client = _s3_client(settings)
    prefix = _backup_prefix(settings, stamp)
    dest_root = Path(dest_root) if dest_root is not None else Path(tempfile.mkdtemp(prefix="verifix-restore-"))
    dest_root.mkdir(parents=True, exist_ok=True)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings["bucket"], Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            target = dest_root / Path(key).name
            client.download_file(settings["bucket"], key, str(target))
    return dest_root


# ── Google Drive (via rclone) ───────────────────────────────────────────────
# Google Drive has no S3 API, so it cannot use the boto3 path above. rclone
# speaks Google Drive natively (handles OAuth, retries and resumable uploads),
# so the backup folder is simply copied to a Drive folder. rclone also covers
# OneDrive, Dropbox, MEGA and local USB drives with the same settings.

def rclone_settings() -> dict[str, str]:
    """rclone remote from the environment, e.g. ``gdrive:VerifixBackups``."""
    return {
        "remote": os.getenv("CLOUD_BACKUP_RCLONE_REMOTE", "").strip().rstrip("/"),
        "keep": os.getenv("CLOUD_BACKUP_KEEP", "30").strip(),
    }


def _rclone_binary() -> str:
    found = shutil.which("rclone")
    if found:
        return found
    override = os.getenv("RCLONE_PATH", "").strip()
    if override and Path(override).exists():
        return override
    raise BackupError(
        "rclone was not found. Install it from https://rclone.org/downloads/ "
        "and add it to PATH, or set RCLONE_PATH in .env to rclone.exe."
    )


def _run_rclone(args: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [_rclone_binary(), *args, "--log-level", "ERROR"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise BackupError(f"rclone failed ({' '.join(args[:2])}): {detail}")
    return result


def _rclone_dir(settings: dict[str, str], stamp: str) -> str:
    return f"{settings['remote']}/{BACKUP_DIR_NAME}-{stamp}"


def upload_to_rclone(
    manifest: dict,
    *,
    settings: dict[str, str] | None = None,
    keep: int | None = None,
) -> str:
    """Copy one local backup folder to the rclone remote (e.g. Google Drive)."""
    settings = rclone_settings() if settings is None else settings
    if not settings["remote"]:
        raise BackupError(
            "Set CLOUD_BACKUP_RCLONE_REMOTE in .env first (e.g. gdrive:VerifixBackups)."
        )
    backup_dir = Path(manifest["backup_dir"])
    stamp = backup_dir.name.removeprefix(f"{BACKUP_DIR_NAME}-")
    target = _rclone_dir(settings, stamp)
    _run_rclone(["copy", str(backup_dir), target])
    if keep is None:
        try:
            keep = int(settings.get("keep") or 30)
        except ValueError:
            keep = 30
    if keep > 0:
        prune_rclone_backups(settings, keep=keep)
    return target


def list_rclone_backups(*, settings: dict[str, str] | None = None) -> list[str]:
    """Sorted backup timestamps on the rclone remote (oldest first)."""
    settings = rclone_settings() if settings is None else settings
    if not settings["remote"]:
        raise BackupError("Set CLOUD_BACKUP_RCLONE_REMOTE in .env first.")
    try:
        result = _run_rclone(["lsf", "--dirs-only", settings["remote"]])
    except BackupError:
        return []  # the backup folder does not exist yet
    stamps = set()
    for line in result.stdout.splitlines():
        name = line.strip().rstrip("/")
        if name.startswith(f"{BACKUP_DIR_NAME}-"):
            stamps.add(name.removeprefix(f"{BACKUP_DIR_NAME}-"))
    return sorted(stamps)


def prune_rclone_backups(settings: dict[str, str], *, keep: int) -> int:
    """Delete all but the newest ``keep`` backups on the remote.

    ``--drive-use-trash=false`` is passed so old backups really free up space
    instead of sitting in the Drive bin for 30 days.
    """
    stamps = list_rclone_backups(settings=settings)
    excess = stamps[: max(0, len(stamps) - keep)]
    for stamp in excess:
        _run_rclone(
            ["purge", _rclone_dir(settings, stamp), "--drive-use-trash=false"]
        )
    return len(excess)


def download_rclone_backup(
    stamp: str,
    *,
    settings: dict[str, str] | None = None,
    dest_root: Path | None = None,
) -> Path:
    """Copy one backup from the rclone remote into a local folder."""
    settings = rclone_settings() if settings is None else settings
    if not settings["remote"]:
        raise BackupError("Set CLOUD_BACKUP_RCLONE_REMOTE in .env first.")
    dest_root = (
        Path(dest_root)
        if dest_root is not None
        else Path(tempfile.mkdtemp(prefix="verifix-restore-")) / f"{BACKUP_DIR_NAME}-{stamp}"
    )
    dest_root.mkdir(parents=True, exist_ok=True)
    _run_rclone(["copy", _rclone_dir(settings, stamp), str(dest_root)])
    return dest_root


# ── one-command orchestration (used by the CLI and scripts) ─────────────────

def run_backup(
    dest_root: Path | None = None,
    *,
    local_only: bool = False,
    keep: int | None = None,
) -> dict:
    """Local backup, then every configured cloud destination.

    A failing cloud destination never discards the local backup; the reason
    is returned in ``errors`` so the run still reports failure loudly.
    """
    manifest = build_local_backup(dest_root)
    manifest["uploads"] = []
    manifest["errors"] = []
    manifest["notes"] = []

    if local_only:
        manifest["notes"].append("Cloud: skipped (--local-only)")
        return manifest

    s3 = cloud_settings()
    drive = rclone_settings()
    configured = False

    if s3["bucket"]:
        configured = True
        try:
            target = upload_backup(manifest, settings=s3, keep=keep)
            manifest["uploads"].append({"destination": "S3", "target": target})
        except BackupError as exc:
            manifest["errors"].append(f"S3 upload failed: {exc}")

    if drive["remote"]:
        configured = True
        try:
            target = upload_to_rclone(manifest, settings=drive, keep=keep)
            manifest["uploads"].append({"destination": "Google Drive", "target": target})
        except BackupError as exc:
            manifest["errors"].append(f"Google Drive upload failed: {exc}")

    if not configured:
        manifest["notes"].append(
            "Cloud: no destination configured yet — this backup is on this "
            "computer only. See docs/CLOUD_BACKUPS.md"
        )
    return manifest


def list_available_backups(source: str = "auto") -> list[dict]:
    """Backups available to restore, as ``{source, timestamp}`` entries."""
    entries: list[dict] = []
    if source in ("auto", "s3"):
        s3 = cloud_settings()
        if s3["bucket"]:
            for stamp in list_cloud_backups(settings=s3):
                entries.append({"source": "s3", "timestamp": stamp})
        elif source == "s3":
            raise BackupError("CLOUD_BACKUP_BUCKET is not set in .env.")
    if source in ("auto", "gdrive"):
        drive = rclone_settings()
        if drive["remote"]:
            for stamp in list_rclone_backups(settings=drive):
                entries.append({"source": "gdrive", "timestamp": stamp})
        elif source == "gdrive":
            raise BackupError("CLOUD_BACKUP_RCLONE_REMOTE is not set in .env.")
    return entries


def verify_backup(
    *,
    source: str = "auto",
    local_dir: Path | None = None,
    timestamp: str = "",
) -> dict:
    """Download a backup and check it — without changing anything on this PC.

    Every file is re-hashed against the manifest, so this answers the only
    question that matters: "if my laptop died tonight, is this backup good?"
    """
    if local_dir is not None:
        backup_dir, origin, stamp = Path(local_dir), "local", local_dir.name
    else:
        entries = list_available_backups(source)
        if not entries:
            raise BackupError(
                "No cloud backups found. Run a backup first, or check .env."
            )
        if timestamp:
            matches = [e for e in entries if e["timestamp"] == timestamp]
            if not matches:
                raise BackupError(f"No cloud backup with timestamp {timestamp}.")
            entry = matches[0]
        else:
            entry = entries[-1]
        origin, stamp = entry["source"], entry["timestamp"]
        if origin == "s3":
            backup_dir = download_cloud_backup(stamp)
        else:
            backup_dir = download_rclone_backup(stamp)

    manifest = _verify_manifest(backup_dir)
    return {
        "source": origin,
        "timestamp": stamp,
        "backup_dir": str(backup_dir),
        "created_at": manifest.get("created_at", ""),
        "engine": manifest.get("engine", ""),
        "upload_file_count": manifest.get("upload_file_count", 0),
        "photo_check": manifest.get("photo_check", {}),
        "files": manifest.get("files", []),
    }


def summarize_verification(result: dict) -> str:
    """Plain-language 'is my backup good?' report."""
    check = result.get("photo_check", {})
    lines = [
        f"Checked {result.get('source')} backup {result.get('timestamp')}",
        f"OK: all {len(result.get('files', []))} files are present and undamaged "
        "(checksums match)",
        f"Taken: {result.get('created_at', 'unknown')}",
        f"Inventory engine: {result.get('engine', '?')} · "
        f"photo files in backup: {result.get('upload_file_count', 0)}",
    ]
    if check.get("checked"):
        lines.append(
            f"Photos: {check.get('photos_found', 0)}/{check.get('parts_with_photo', 0)} "
            "part photos were in uploads/ at backup time"
        )
        for item in check.get("missing", [])[:10]:
            lines.append(f"  ! was already missing — {item}")
        if check.get("missing"):
            lines.append("  (these parts had no photo file even before the backup)")
    lines.append("Nothing on this computer was changed.")
    return "\n".join(lines)


def restore_from_any(
    *,
    source: str = "auto",
    local_dir: Path | None = None,
    timestamp: str = "",
    force: bool = False,
) -> dict:
    """Restore from a local folder or the newest backup in the cloud."""
    if local_dir is not None:
        backup_dir, origin = Path(local_dir), "local"
    else:
        entries = list_available_backups(source)
        if not entries:
            raise BackupError(
                "No cloud backups found. Check .env, or pass --local-dir to "
                "restore from a folder on this computer."
            )
        if timestamp:
            matches = [e for e in entries if e["timestamp"] == timestamp]
            if not matches:
                raise BackupError(f"No cloud backup with timestamp {timestamp}.")
            entry = matches[0]
        else:
            entry = entries[-1]
        origin = entry["source"]
        if origin == "s3":
            backup_dir = download_cloud_backup(entry["timestamp"])
        else:
            backup_dir = download_rclone_backup(entry["timestamp"])

    manifest = restore_backup(backup_dir, force=force)
    manifest["restored_from"] = origin
    manifest["source_dir"] = str(backup_dir)
    return manifest

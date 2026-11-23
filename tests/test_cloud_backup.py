"""Cloud/local backups: photos attached to parts must survive a round trip.

These tests use a throwaway SQLite database and uploads folder so they run
with no server, no bucket and no network.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app import backup as backup_mod

PARTS_SCHEMA = """
CREATE TABLE parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_name TEXT NOT NULL,
    image_path TEXT DEFAULT ''
);
"""


def _make_shop(tmp_path: Path):
    """Create a fake shop: db with 3 parts, 2 photos on disk, 1 missing."""
    data_dir = tmp_path / "data"
    upload_dir = tmp_path / "uploads"
    data_dir.mkdir()
    upload_dir.mkdir()
    db_path = data_dir / "inventory.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(PARTS_SCHEMA)
    conn.execute(
        "INSERT INTO parts (part_name, image_path) VALUES (?, ?)",
        ("PCV Valve", "/media/uploads/pcv.jpg"),
    )
    conn.execute(
        "INSERT INTO parts (part_name, image_path) VALUES (?, ?)",
        ("Wheel Hub", "uploads/hub.jpg"),
    )
    conn.execute(
        "INSERT INTO parts (part_name, image_path) VALUES (?, ?)",
        ("Rocker Arm", "/media/uploads/gone.jpg"),  # file does not exist
    )
    conn.commit()
    conn.close()
    (upload_dir / "pcv.jpg").write_bytes(b"fake-jpeg-pcv")
    (upload_dir / "hub.jpg").write_bytes(b"fake-jpeg-hub")
    (upload_dir / "orphan.jpg").write_bytes(b"no part points here")
    (data_dir / "local-auth.db").write_bytes(b"fake-auth-db")
    (data_dir / "shop_profile.json").write_text('{"shop_name": "Verifix"}')
    return db_path, upload_dir, data_dir


def test_photo_link_report_finds_missing_and_orphans(tmp_path):
    db_path, upload_dir, _ = _make_shop(tmp_path)
    report = backup_mod.photo_link_report(upload_dir, "", db_path)
    assert report["checked"] is True
    assert report["part_count"] == 3
    assert report["parts_with_photo"] == 3
    assert report["photos_found"] == 2
    assert report["missing"] == ["part 3: /media/uploads/gone.jpg"]
    assert report["orphan_files"] == 1


def test_photo_link_report_unreadable_db_does_not_raise(tmp_path):
    report = backup_mod.photo_link_report(tmp_path / "uploads", "", tmp_path / "nope.db")
    assert report["checked"] is False
    assert "not found" in report["reason"]


def test_build_local_backup_packages_photos_and_logins(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    dest = tmp_path / "backups"
    manifest = backup_mod.build_local_backup(
        dest, upload_dir=upload_dir, data_dir=data_dir, db_path=db_path,
        database_url="", timestamp="20261123-230000",
    )
    backup_dir = dest / "verifix-backup-20261123-230000"
    assert backup_dir.is_dir()
    assert (backup_dir / "inventory.db").exists()
    assert (backup_dir / "uploads.zip").exists()
    assert (backup_dir / "data-files.zip").exists()

    with zipfile.ZipFile(backup_dir / "uploads.zip") as archive:
        names = archive.namelist()
    assert "uploads/pcv.jpg" in names
    assert "uploads/hub.jpg" in names

    with zipfile.ZipFile(backup_dir / "data-files.zip") as archive:
        names = archive.namelist()
    assert "data/local-auth.db" in names
    assert "data/shop_profile.json" in names

    saved = json.loads((backup_dir / "manifest.json").read_text())
    assert saved["photo_check"]["photos_found"] == 2
    assert saved["photo_check"]["missing"] == ["part 3: /media/uploads/gone.jpg"]
    for entry in saved["files"]:
        assert backup_mod.sha256_of(backup_dir / entry["name"]) == entry["sha256"]


def test_sqlite_snapshot_is_a_consistent_copy(tmp_path):
    db_path, _, _ = _make_shop(tmp_path)
    dest = tmp_path / "snapshot.db"
    backup_mod._snapshot_sqlite(dest, db_path)
    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 3
    finally:
        conn.close()


def test_restore_refuses_to_overwrite_without_force(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    dest = tmp_path / "backups"
    backup_mod.build_local_backup(
        dest, upload_dir=upload_dir, data_dir=data_dir, db_path=db_path,
        database_url="", timestamp="20261123-230000",
    )
    backup_dir = dest / "verifix-backup-20261123-230000"
    with pytest.raises(backup_mod.BackupError, match="--force"):
        backup_mod.restore_backup(
            backup_dir, upload_dir=upload_dir, data_dir=data_dir,
            db_path=db_path, database_url="", force=False,
        )


def test_restore_roundtrip_brings_back_parts_and_photos(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    dest = tmp_path / "backups"
    backup_mod.build_local_backup(
        dest, upload_dir=upload_dir, data_dir=data_dir, db_path=db_path,
        database_url="", timestamp="20261123-230000",
    )
    backup_dir = dest / "verifix-backup-20261123-230000"

    # Simulate data loss: delete everything.
    db_path.unlink()
    (upload_dir / "pcv.jpg").unlink()
    (upload_dir / "hub.jpg").unlink()
    (data_dir / "local-auth.db").unlink()

    manifest = backup_mod.restore_backup(
        backup_dir, upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", force=True,
        safety_root=tmp_path / "safety",
    )
    assert manifest["safety_copy"]
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 3
    finally:
        conn.close()
    assert (upload_dir / "pcv.jpg").read_bytes() == b"fake-jpeg-pcv"
    assert (upload_dir / "hub.jpg").read_bytes() == b"fake-jpeg-hub"
    assert (data_dir / "local-auth.db").read_bytes() == b"fake-auth-db"


def test_restore_rejects_corrupt_backup(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    dest = tmp_path / "backups"
    backup_mod.build_local_backup(
        dest, upload_dir=upload_dir, data_dir=data_dir, db_path=db_path,
        database_url="", timestamp="20261123-230000",
    )
    backup_dir = dest / "verifix-backup-20261123-230000"
    (backup_dir / "uploads.zip").write_bytes(b"tampered")
    with pytest.raises(backup_mod.BackupError, match="checksum"):
        backup_mod.restore_backup(
            backup_dir, upload_dir=upload_dir, data_dir=data_dir,
            db_path=db_path, database_url="", force=True,
        )


# ── Google Drive via rclone ────────────────────────────────────────────────
# Google Drive has no S3 API, so the backup folder is copied with rclone.
# These tests stub rclone to prove the right commands and arguments are used.

class FakeRclone:
    """Records rclone calls and mimics copy/lsf/purge against a fake remote."""

    def __init__(self, root: Path, *, fail: bool = False):
        self.root = root
        self.calls: list[list[str]] = []
        self.fail = fail

    @staticmethod
    def _remote_arg(args) -> str:
        """The gdrive: argument, ignoring flags (lsf puts a flag first)."""
        return next(a for a in args if a.startswith("gdrive:"))

    def __call__(self, args, *, timeout=3600):
        self.calls.append(args)
        if self.fail:
            raise backup_mod.BackupError(
                "rclone failed (copy): directory not found"
            )
        if args[0] == "copy":
            source, target = args[1], args[2]
            src = Path(source) if not source.startswith("gdrive:") else (
                self.root / source.removeprefix("gdrive:")
            )
            dst = Path(target) if not target.startswith("gdrive:") else (
                self.root / target.removeprefix("gdrive:")
            )
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src)
                    (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dst / rel).write_bytes(item.read_bytes())
        elif args[0] == "lsf":
            remote = self.root / self._remote_arg(args).removeprefix("gdrive:")
            names = [f"{p.name}/" for p in remote.iterdir() if p.is_dir()] if remote.is_dir() else []
            result = type("R", (), {"returncode": 0, "stdout": "\n".join(names), "stderr": ""})()
            return result
        elif args[0] == "purge":
            target = self.root / self._remote_arg(args).removeprefix("gdrive:")
            shutil.rmtree(target, ignore_errors=True)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    remote_root = tmp_path / "drive"
    remote_root.mkdir()
    stub = FakeRclone(remote_root)
    monkeypatch.setattr(backup_mod, "_run_rclone", stub)
    monkeypatch.setenv("CLOUD_BACKUP_RCLONE_REMOTE", "gdrive:VerifixBackups")
    monkeypatch.setenv("CLOUD_BACKUP_KEEP", "3")
    monkeypatch.delenv("CLOUD_BACKUP_BUCKET", raising=False)
    return stub


def test_rclone_settings_reads_env(monkeypatch):
    monkeypatch.setenv("CLOUD_BACKUP_RCLONE_REMOTE", "gdrive:VerifixBackups/")
    monkeypatch.setenv("CLOUD_BACKUP_KEEP", "7")
    settings = backup_mod.rclone_settings()
    assert settings["remote"] == "gdrive:VerifixBackups"
    assert settings["keep"] == "7"


def test_missing_rclone_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("RCLONE_PATH", raising=False)
    with pytest.raises(backup_mod.BackupError, match="rclone was not found"):
        backup_mod._rclone_binary()


def test_rclone_path_override(tmp_path, monkeypatch):
    fake = tmp_path / "rclone"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)
    monkeypatch.setenv("RCLONE_PATH", str(fake))
    assert backup_mod._rclone_binary() == str(fake)


def test_upload_to_rclone_copies_backup_folder(tmp_path, fake_rclone):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    dest = tmp_path / "backups"
    manifest = backup_mod.build_local_backup(
        dest, upload_dir=upload_dir, data_dir=data_dir, db_path=db_path,
        database_url="", timestamp="20261123-230000",
    )
    target = backup_mod.upload_to_rclone(manifest, keep=0)
    assert target == "gdrive:VerifixBackups/verifix-backup-20261123-230000"
    assert fake_rclone.calls[0][0] == "copy"
    remote_dir = tmp_path / "drive" / "VerifixBackups" / "verifix-backup-20261123-230000"
    assert (remote_dir / "manifest.json").exists()
    with zipfile.ZipFile(remote_dir / "uploads.zip") as archive:
        assert "uploads/pcv.jpg" in archive.namelist()


def test_upload_sends_permanent_delete_flag_when_pruning(tmp_path, fake_rclone):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    for i in range(5):
        backup_mod.build_local_backup(
            tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
            db_path=db_path, database_url="", timestamp=f"2026112{i}-230000",
        )
    remote = tmp_path / "drive" / "VerifixBackups"
    for i in range(5):
        (remote / f"verifix-backup-2026112{i}-230000").mkdir(parents=True)
    latest = backup_mod.build_local_backup(
        tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", timestamp="20261125-230000",
    )
    backup_mod.upload_to_rclone(latest, keep=3)
    purges = [c for c in fake_rclone.calls if c[0] == "purge"]
    assert len(purges) == 3  # 6 backups, keep newest 3
    assert all("--drive-use-trash=false" in call for call in purges)
    assert "gdrive:VerifixBackups/verifix-backup-20261125-230000" not in [c[1] for c in purges]
    assert len(backup_mod.list_rclone_backups()) == 3


def test_list_rclone_backups_when_folder_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_BACKUP_RCLONE_REMOTE", "gdrive:Nothing")

    def boom(args, *, timeout=3600):
        raise backup_mod.BackupError("rclone failed (lsf): directory not found")

    monkeypatch.setattr(backup_mod, "_run_rclone", boom)
    assert backup_mod.list_rclone_backups() == []


def test_run_backup_uploads_to_drive_and_keeps_local_copy(tmp_path, fake_rclone, monkeypatch):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    monkeypatch.setattr(backup_mod, "_upload_dir", lambda: upload_dir)
    monkeypatch.setattr(backup_mod, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(backup_mod, "_db_path", lambda: db_path)
    monkeypatch.setattr(backup_mod, "_database_url", lambda: "")

    manifest = backup_mod.run_backup(tmp_path / "backups")
    assert [u["destination"] for u in manifest["uploads"]] == ["Google Drive"]
    assert manifest["errors"] == []
    assert (tmp_path / "backups" / Path(manifest["backup_dir"]).name / "uploads.zip").exists()


def test_failed_drive_upload_still_keeps_local_backup_and_reports(tmp_path, monkeypatch):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    monkeypatch.setattr(backup_mod, "_upload_dir", lambda: upload_dir)
    monkeypatch.setattr(backup_mod, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(backup_mod, "_db_path", lambda: db_path)
    monkeypatch.setattr(backup_mod, "_database_url", lambda: "")
    monkeypatch.setenv("CLOUD_BACKUP_RCLONE_REMOTE", "gdrive:VerifixBackups")
    monkeypatch.delenv("CLOUD_BACKUP_BUCKET", raising=False)
    monkeypatch.setattr(backup_mod, "_run_rclone", FakeRclone(tmp_path, fail=True))

    manifest = backup_mod.run_backup(tmp_path / "backups")
    assert manifest["errors"] and "Google Drive" in manifest["errors"][0]
    assert Path(manifest["backup_dir"]).is_dir()
    assert "Google Drive upload failed" in backup_mod.summarize(manifest)


def test_no_destination_configured_is_a_clear_note(tmp_path, monkeypatch):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    monkeypatch.setattr(backup_mod, "_upload_dir", lambda: upload_dir)
    monkeypatch.setattr(backup_mod, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(backup_mod, "_db_path", lambda: db_path)
    monkeypatch.setattr(backup_mod, "_database_url", lambda: "")
    monkeypatch.delenv("CLOUD_BACKUP_BUCKET", raising=False)
    monkeypatch.delenv("CLOUD_BACKUP_RCLONE_REMOTE", raising=False)

    manifest = backup_mod.run_backup(tmp_path / "backups")
    assert manifest["errors"] == []
    assert any("no destination configured" in n for n in manifest["notes"])


def test_restore_from_newest_drive_backup(tmp_path, fake_rclone, monkeypatch):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    manifest = backup_mod.build_local_backup(
        tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", timestamp="20261123-230000",
    )
    backup_mod.upload_to_rclone(manifest, keep=0)

    # Data loss again
    db_path.unlink()
    (upload_dir / "pcv.jpg").unlink()
    (upload_dir / "hub.jpg").unlink()

    monkeypatch.setattr(backup_mod, "_upload_dir", lambda: upload_dir)
    monkeypatch.setattr(backup_mod, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(backup_mod, "_db_path", lambda: db_path)
    monkeypatch.setattr(backup_mod, "_database_url", lambda: "")
    monkeypatch.setattr(
        backup_mod, "download_rclone_backup",
        lambda stamp, **kw: tmp_path / "restore" / f"verifix-backup-{stamp}",
    )
    (tmp_path / "restore").mkdir()
    import shutil as _shutil
    _shutil.copytree(
        tmp_path / "drive" / "VerifixBackups" / "verifix-backup-20261123-230000",
        tmp_path / "restore" / "verifix-backup-20261123-230000",
    )

    restored = backup_mod.restore_from_any(source="gdrive", force=True)
    assert restored["restored_from"] == "gdrive"
    assert (upload_dir / "pcv.jpg").read_bytes() == b"fake-jpeg-pcv"
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 3
    finally:
        conn.close()


def test_restore_with_no_backups_anywhere_explains_itself(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUD_BACKUP_BUCKET", raising=False)
    monkeypatch.delenv("CLOUD_BACKUP_RCLONE_REMOTE", raising=False)
    with pytest.raises(backup_mod.BackupError, match="No cloud backups found"):
        backup_mod.restore_from_any()


def test_bundled_demo_photos_are_not_reported_missing(tmp_path):
    """Demo photos ship inside the app, so they must not raise false alarms."""
    data_dir = tmp_path / "data"
    upload_dir = tmp_path / "uploads"
    data_dir.mkdir()
    upload_dir.mkdir()
    seed_photo = Path(backup_mod.config.SEED_DIR) / "pcv-valve.jpg"
    if not seed_photo.exists():
        pytest.skip("no bundled seed photo in this checkout")

    db_path = data_dir / "inventory.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(PARTS_SCHEMA)
    conn.execute(
        "INSERT INTO parts (part_name, image_path) VALUES (?, ?)",
        ("PCV Valve", str(seed_photo)),
    )
    conn.commit()
    conn.close()

    report = backup_mod.photo_link_report(upload_dir, "", db_path)
    assert report["bundled"] == 1
    assert report["missing"] == []
    assert report["parts_with_photo"] == 0  # nothing shop-owned to verify
    assert "bundled with the app" in backup_mod.summarize({
        "engine": "sqlite", "upload_file_count": 0, "photo_check": report,
    })


# ── verify: prove a backup is good without restoring it ────────────────────

def test_verify_local_backup_reports_health(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    manifest = backup_mod.build_local_backup(
        tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", timestamp="20261123-230000",
    )
    result = backup_mod.verify_backup(local_dir=Path(manifest["backup_dir"]))
    assert result["engine"] == "sqlite"
    assert len(result["files"]) == 3
    assert result["photo_check"]["photos_found"] == 2
    text = backup_mod.summarize_verification(result)
    assert "present and undamaged" in text
    assert "Nothing on this computer was changed" in text


def test_verify_refuses_a_damaged_backup(tmp_path):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    manifest = backup_mod.build_local_backup(
        tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", timestamp="20261123-230000",
    )
    (Path(manifest["backup_dir"]) / "inventory.db").write_bytes(b"corrupted")
    with pytest.raises(backup_mod.BackupError, match="checksum"):
        backup_mod.verify_backup(local_dir=Path(manifest["backup_dir"]))


def test_verify_newest_drive_backup(tmp_path, fake_rclone, monkeypatch):
    db_path, upload_dir, data_dir = _make_shop(tmp_path)
    manifest = backup_mod.build_local_backup(
        tmp_path / "backups", upload_dir=upload_dir, data_dir=data_dir,
        db_path=db_path, database_url="", timestamp="20261123-230000",
    )
    backup_mod.upload_to_rclone(manifest, keep=0)
    monkeypatch.setattr(
        backup_mod, "download_rclone_backup",
        lambda stamp, **kw: tmp_path / "drive" / "VerifixBackups"
        / f"verifix-backup-{stamp}",
    )
    result = backup_mod.verify_backup(source="gdrive")
    assert result["source"] == "gdrive"
    assert result["timestamp"] == "20261123-230000"
    assert result["photo_check"]["photos_found"] == 2


def test_verify_with_nothing_configured_is_clear(monkeypatch):
    monkeypatch.delenv("CLOUD_BACKUP_BUCKET", raising=False)
    monkeypatch.delenv("CLOUD_BACKUP_RCLONE_REMOTE", raising=False)
    with pytest.raises(backup_mod.BackupError, match="No cloud backups found"):
        backup_mod.verify_backup()

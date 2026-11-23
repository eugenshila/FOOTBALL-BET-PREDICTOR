#!/usr/bin/env python3
"""Back up Verifix (database + part photos + logins) locally and to the cloud.

Cross-platform replacement for ``backup_to_cloud.ps1``, for Task Scheduler,
cron or systemd. Works with SQLite or PostgreSQL, and uploads to whichever
cloud destination is configured — S3-compatible storage (AWS S3, Cloudflare
R2, Backblaze B2, Wasabi) and/or Google Drive via rclone.

Usage:
    python scripts/backup_to_cloud.py                 # local + every configured cloud
    python scripts/backup_to_cloud.py --local-only    # local folder only
    python scripts/backup_to_cloud.py --local-dir D   # local folder D instead of ./backups
    python scripts/backup_to_cloud.py --dry-run       # show what would be backed up

Settings live in .env (see docs/CLOUD_BACKUPS.md). Secrets are never printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup as backup_mod  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up Verifix database + part photos")
    parser.add_argument("--local-dir", default="", help="local backup folder (default: ./backups)")
    parser.add_argument("--local-only", action="store_true", help="skip the cloud upload")
    parser.add_argument("--keep", type=int, default=None, help="cloud backups to keep (default: CLOUD_BACKUP_KEEP or 30)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be backed up, write nothing")
    args = parser.parse_args(argv)

    if args.dry_run:
        report = backup_mod.photo_link_report()
        s3 = backup_mod.cloud_settings()
        drive = backup_mod.rclone_settings()
        print("Destinations: local ./backups", end="")
        if s3["bucket"]:
            print(f" + S3 bucket {s3['bucket']}", end="")
        if drive["remote"]:
            print(f" + rclone {drive['remote']}", end="")
        print()
        print(f"Engine: {'postgres' if backup_mod._is_postgres() else 'sqlite'}")
        print(f"Uploads folder: {backup_mod._upload_dir()}")
        print(f"Data files: {[n for n in backup_mod.DATA_FILES if (backup_mod._data_dir() / n).exists()]}")
        print(backup_mod.summarize({
            "backup_dir": "(dry run)", "engine": "sqlite",
            "upload_file_count": 0, "photo_check": report,
        }))
        return 0

    try:
        manifest = backup_mod.run_backup(
            Path(args.local_dir) if args.local_dir else None,
            local_only=args.local_only,
            keep=args.keep,
        )
    except backup_mod.BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(backup_mod.summarize(manifest))
    return 1 if manifest.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())

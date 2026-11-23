#!/usr/bin/env python3
"""Back up Verifix (database + part photos + logins) to the cloud.

Cross-platform replacement for ``backup_to_cloud.ps1``: works on Windows,
macOS and Linux, with SQLite or PostgreSQL. The PowerShell script still
works on Windows; this one is what Task Scheduler / cron / systemd should
call on other systems, and what ``python -m app.cli backup`` calls.

Usage:
    python scripts/backup_to_cloud.py                 # local + cloud (if configured)
    python scripts/backup_to_cloud.py --local-only    # local folder only, no cloud
    python scripts/backup_to_cloud.py --local-dir D   # local folder D instead of ./backups
    python scripts/backup_to_cloud.py --dry-run       # show what would be backed up

Cloud settings come from .env (see docs/CLOUD_BACKUPS.md):
    CLOUD_BACKUP_BUCKET, CLOUD_BACKUP_PREFIX, CLOUD_BACKUP_REGION,
    CLOUD_BACKUP_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Cloud upload needs boto3:  pip install boto3
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
        print(f"Engine: {'postgres' if backup_mod._is_postgres() else 'sqlite'}")
        print(f"Uploads folder: {backup_mod._upload_dir()}")
        print(f"Data files: {[n for n in backup_mod.DATA_FILES if (backup_mod._data_dir() / n).exists()]}")
        print(backup_mod.summarize({"backup_dir": "(dry run)", "engine": "sqlite", "upload_file_count": 0, "photo_check": report}))
        return 0

    try:
        manifest = backup_mod.build_local_backup(Path(args.local_dir) if args.local_dir else None)
        print(backup_mod.summarize(manifest))
        if not args.local_only:
            prefix = backup_mod.upload_backup(manifest, keep=args.keep)
            print(f"Cloud: uploaded to {prefix}")
        else:
            print("Cloud: skipped (--local-only)")
    except backup_mod.BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

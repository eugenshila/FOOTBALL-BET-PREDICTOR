#!/usr/bin/env python3
"""Restore Verifix from a cloud or local backup. Stop the app first!

Usage:
    python scripts/restore_from_cloud.py --list            # show cloud backups
    python scripts/restore_from_cloud.py                   # restore newest cloud backup
    python scripts/restore_from_cloud.py --timestamp 20261123-230000
    python scripts/restore_from_cloud.py --local-dir ./backups/verifix-backup-20261123-230000
    python scripts/restore_from_cloud.py --force           # overwrite existing data

A safety copy of the current database, uploads and data files is written to
./backups/pre-restore-<timestamp>/ before anything is overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup as backup_mod  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore Verifix from a backup")
    parser.add_argument("--list", action="store_true", help="list cloud backups and exit")
    parser.add_argument("--timestamp", default="", help="cloud backup timestamp (default: newest)")
    parser.add_argument("--local-dir", default="", help="restore from a local backup folder instead of the cloud")
    parser.add_argument("--force", action="store_true", help="overwrite existing data (safety copy is kept)")
    args = parser.parse_args(argv)

    try:
        if args.list:
            stamps = backup_mod.list_cloud_backups()
            if not stamps:
                print("No cloud backups found.")
            for stamp in stamps:
                print(stamp)
            return 0

        if args.local_dir:
            backup_dir = Path(args.local_dir)
        else:
            stamps = backup_mod.list_cloud_backups()
            if not stamps:
                print("No cloud backups found. Run a backup first.", file=sys.stderr)
                return 1
            stamp = args.timestamp or stamps[-1]
            print(f"Downloading cloud backup {stamp} …")
            backup_dir = backup_mod.download_cloud_backup(stamp)

        manifest = backup_mod.restore_backup(backup_dir, force=args.force)
        print(f"Restored from {backup_dir}")
        print(f"Safety copy of previous state: {manifest.get('safety_copy', '')}")
        print("Start the app again to use the restored data.")
    except backup_mod.BackupError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

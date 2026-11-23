"""Cloud/local backups: photos attached to parts must survive a round trip.

These tests use a throwaway SQLite database and uploads folder so they run
with no server, no bucket and no network.
"""

from __future__ import annotations

import json
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

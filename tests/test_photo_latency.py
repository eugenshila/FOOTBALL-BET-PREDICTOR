"""Fast local lookup preserves safety, stock, and the existing result shape."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app import db, photo_match, vision
from app.config import UPLOAD_DIR
from app.models import IdentificationReport


def _create(client, name, oem, image=""):
    result = client.post("/api/parts", json={"part_name": name, "oem_part_number": oem, "quantity": 4, "image_path": image})
    assert result.status_code == 201, result.text
    return result.json()


def test_shared_reference_scored_once_and_stock_dates_preserved(client, sample_parts, monkeypatch):
    a = _create(client, "Speed First", "SPEED-ONE", "assets/seed/wheel-hub.jpg")
    b = _create(client, "Speed Second", "SPEED-TWO", "assets/seed/wheel-hub.jpg")
    try:
        original_list = db.list_parts
        monkeypatch.setattr(photo_match.db, "list_parts", lambda **kwargs: [db.get_part(a["id"]), db.get_part(b["id"])])
        monkeypatch.setattr(photo_match, "catalogue_parts", lambda: [])
        calls = []
        distance = photo_match._distance
        def count_distance(query, ref):
            calls.append(1)
            return distance(query, ref)
        monkeypatch.setattr(photo_match, "_distance", count_distance)
        result = photo_match.suggestions(sample_parts["wheel-hub"].read_bytes())
        assert len(calls) == 1
        assert {c["part"].id for c in result} == {a["id"], b["id"]}
        for candidate in result:
            assert candidate["part"].last_received_at is not None
            assert candidate["part"].quantity == 4
        assert original_list(limit=1)  # ordinary inventory lookup still includes receipt timestamps
    finally:
        client.delete(f"/api/parts/{a['id']}")
        client.delete(f"/api/parts/{b['id']}")


def test_scanned_photo_thumbnail_is_created_only_on_demand(client, sample_parts, monkeypatch):
    part = _create(client, "Lazy Thumb", "SPEED-THUMB")
    try:
        candidate = {"part": db.get_part(part["id"]), "distance": 0.01, "reference_url": "/assets/seed/wheel-hub.jpg"}
        monkeypatch.setattr(photo_match, "suggestions", lambda image: [candidate])
        response = client.post("/api/identify", files={"file": ("wheel.jpg", sample_parts["wheel-hub"].read_bytes(), "image/jpeg")})
        assert response.status_code == 200, response.text
        filename = response.json()["image_url"].rsplit("/", 1)[-1]
        photo = UPLOAD_DIR / filename
        thumb = photo.with_name(f"{photo.stem}.thumb.jpg")
        assert photo.exists() and not thumb.exists()
        assert client.get(f"/api/thumbs/{filename}").status_code == 200
        assert thumb.exists()
        assert client.get(f"/api/parts/{part['id']}").json()["quantity"] == 4
    finally:
        client.delete(f"/api/parts/{part['id']}")


def test_slow_external_fallback_does_not_block_other_requests(client, sample_parts, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(photo_match, "suggestions", lambda image: [])
    def slow_provider(image, mime):
        started.set()
        assert release.wait(5), "test provider was not released"
        return IdentificationReport(provider="test-provider")
    monkeypatch.setattr(vision, "identify", slow_provider)
    image = sample_parts["wheel-hub"].read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(client.post, "/api/identify", files={"file": ("wheel.jpg", image, "image/jpeg")})
        try:
            assert started.wait(3)
            stats = pool.submit(client.get, "/api/stats")
            assert stats.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert scan.result(timeout=5).status_code == 200

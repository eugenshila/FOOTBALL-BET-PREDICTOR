"""End-to-end API tests: identification, inventory, cards, and the guard rails."""

from __future__ import annotations

import io

from PIL import Image


def _upload(client, path):
    with open(path, "rb") as handle:
        return client.post(
            "/api/identify", files={"file": (path.name, handle, "image/jpeg")}
        )


# ── health & seed ─────────────────────────────────────────────────────────
def test_health_reports_provider(client):
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["vision_provider"] == "offline"
    assert "offline" in payload["providers_available"]


def test_demo_catalogue_is_seeded(client):
    parts = client.get("/api/parts").json()
    assert len(parts) >= 4
    names = {p["part_name"] for p in parts}
    assert "PCV Valve" in names
    assert all(p["verified"] for p in parts)


def test_stats_shape(client):
    stats = client.get("/api/stats").json()
    assert stats["parts"] >= 4
    assert stats["units"] > 0
    assert stats["inventory_value"] > 0


# ── identification ────────────────────────────────────────────────────────
def test_identify_returns_three_section_report(client, sample_parts):
    response = _upload(client, sample_parts["wheel-hub"])
    assert response.status_code == 200
    body = response.json()

    report = body["report"]
    assert report["part_name"]
    assert report["provider"] == "offline"
    assert body["scan_id"]

    markdown = body["report_markdown"]
    assert "### PART IDENTIFICATION REPORT" in markdown
    assert "### WAREHOUSE & INVENTORY DETAILS" in markdown
    assert "### WHATSAPP PROMOTIONAL POST GENERATOR" in markdown
    assert "WhatsApp to order" in markdown


def test_identify_matches_existing_stock_record(client, sample_parts):
    body = _upload(client, sample_parts["wheel-hub"]).json()
    matched = body["matched_part"]
    assert matched is not None
    assert "Wheel Hub" in matched["part_name"]
    assert body["candidates"]


def test_identify_rejects_non_image(client):
    response = client.post(
        "/api/identify",
        files={"file": ("notes.txt", io.BytesIO(b"not an image"), "text/plain")},
    )
    assert response.status_code == 415


def test_identify_rejects_corrupt_image_with_image_mime(client):
    response = client.post(
        "/api/identify",
        files={"file": ("broken.jpg", io.BytesIO(b"\xff\xd8garbage"), "image/jpeg")},
    )
    assert response.status_code == 415


def test_identify_rejects_empty_upload(client):
    response = client.post(
        "/api/identify", files={"file": ("empty.jpg", io.BytesIO(b""), "image/jpeg")}
    )
    assert response.status_code in {415, 422}


def test_identify_handles_photo_with_busy_background(client):
    """A real-world snapshot (no studio backdrop) must not crash the matcher."""
    image = Image.effect_noise((640, 480), 90).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    buffer.seek(0)
    response = client.post(
        "/api/identify", files={"file": ("noise.jpg", buffer, "image/jpeg")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["needs_manual_review"] is True


# ── inventory CRUD & stock -------------------------------------------------
def _new_part(**overrides):
    payload = {
        "part_name": "Thermostat Housing",
        "oem_part_number": "5184651AH",
        "alternate_numbers": ["5184651AG"],
        "fitment": [
            {"make": "Jeep", "model": "Wrangler JK", "engine": "3.6L Pentastar V6", "years": "2012-2018"}
        ],
        "condition": "New",
        "bin_location": "D4-A2",
        "quantity": 3,
        "unit_price": 4350,
        "currency": "KES",
        "category": "Cooling",
        "technical_notes": ["Bleed the cooling system after fitting."],
        "image_path": "assets/seed/hp-fuel-pump.jpg",
        "verified": True,
    }
    payload.update(overrides)
    return payload


def test_create_read_update_delete_part(client):
    created = client.post("/api/parts", json=_new_part()).json()
    part_id = created["id"]
    assert created["quantity"] == 3

    fetched = client.get(f"/api/parts/{part_id}").json()
    assert fetched["bin_location"] == "D4-A2"

    patched = client.patch(
        f"/api/parts/{part_id}", json={"bin_location": "D9-B1", "unit_price": 4600}
    ).json()
    assert patched["bin_location"] == "D9-B1"
    assert patched["unit_price"] == 4600

    assert client.delete(f"/api/parts/{part_id}").status_code == 204
    assert client.get(f"/api/parts/{part_id}").status_code == 404


def test_duplicate_oem_is_rejected(client):
    first = client.post("/api/parts", json=_new_part(oem_part_number="DUPE-001")).json()
    response = client.post("/api/parts", json=_new_part(oem_part_number="DUPE-001"))
    assert response.status_code == 409
    assert str(first["id"]) in response.json()["detail"]
    client.delete(f"/api/parts/{first['id']}")


def test_stock_adjustment_and_history(client):
    part_id = client.post("/api/parts", json=_new_part(oem_part_number="STOCK-1")).json()["id"]

    sold = client.post(
        f"/api/parts/{part_id}/adjust", json={"part_id": part_id, "delta": -2, "reason": "sold"}
    ).json()
    assert sold["quantity"] == 1

    movements = client.get(f"/api/parts/{part_id}/movements").json()
    assert movements[0]["delta"] == -2
    assert movements[0]["reason"] == "sold"
    assert movements[-1]["reason"] == "initial_stock"

    client.delete(f"/api/parts/{part_id}")


def test_stock_cannot_go_negative(client):
    part_id = client.post("/api/parts", json=_new_part(oem_part_number="NEG-1")).json()["id"]
    response = client.post(
        f"/api/parts/{part_id}/adjust", json={"part_id": part_id, "delta": -99}
    )
    assert response.status_code == 422
    client.delete(f"/api/parts/{part_id}")


def test_lookup_by_cross_reference(client):
    body = client.get("/api/lookup", params={"number": "ha590358"}).json()
    assert "Wheel Hub" in body["part_name"]


def test_lookup_unknown_number(client):
    assert client.get("/api/lookup", params={"number": "NOPE-123"}).status_code == 404


def test_search_filters_by_vehicle(client):
    results = client.get("/api/parts", params={"q": "Durango"}).json()
    assert results
    assert all("Durango" in str(p["fitment"]) for p in results)


# ── promo cards ------------------------------------------------------------
def test_generate_all_card_formats(client):
    part_id = client.get("/api/parts").json()[0]["id"]
    response = client.post(
        "/api/cards", json={"part_id": part_id, "formats": ["story", "square", "wide", "bin_label"]}
    )
    assert response.status_code == 200
    body = response.json()

    expected = {"story": (1080, 1920), "square": (1080, 1080), "wide": (1200, 900), "bin_label": (600, 300)}
    assert {c["format"] for c in body["cards"]} == set(expected)
    for card in body["cards"]:
        assert (card["width"], card["height"]) == expected[card["format"]]
        assert card["url"].startswith("/media/exports/")

    assert "+254 700 000 000" in body["caption"]
    assert "*" in body["caption"]  # WhatsApp bold markers present


def test_generated_card_file_is_downloadable(client):
    part_id = client.get("/api/parts").json()[0]["id"]
    card = client.post("/api/cards", json={"part_id": part_id, "formats": ["square"]}).json()["cards"][0]

    served = client.get(card["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"

    with Image.open(io.BytesIO(served.content)) as image:
        assert image.size == (1080, 1080)


def test_download_endpoint_blocks_path_traversal(client):
    response = client.get("/api/cards/download/..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in {400, 404}


def test_unverified_part_cannot_be_published(client):
    part_id = client.post("/api/parts", json=_new_part(oem_part_number="UNVER-1", verified=False)).json()["id"]
    response = client.post("/api/cards", json={"part_id": part_id, "formats": ["square"]})
    assert response.status_code == 409
    assert "verified" in response.json()["detail"].lower()

    client.patch(f"/api/parts/{part_id}", json={"verified": True})
    assert client.post(
        "/api/cards", json={"part_id": part_id, "formats": ["square"]}
    ).status_code == 200
    client.delete(f"/api/parts/{part_id}")


def test_out_of_stock_part_still_renders(client):
    part_id = client.post(
        "/api/parts", json=_new_part(oem_part_number="EMPTY-1", quantity=0)
    ).json()["id"]
    body = client.post("/api/cards", json={"part_id": part_id, "formats": ["square"]}).json()
    assert body["part"]["quantity"] == 0
    client.delete(f"/api/parts/{part_id}")


def test_inline_part_preview(client):
    body = client.post(
        "/api/cards",
        json={
            "part": {
                "part_name": "Serpentine Belt Tensioner",
                "oem_part_number": "53034046AB",
                "unit_price": 7400,
                "currency": "KES",
                "image_path": "assets/seed/rocker-arm.jpg",
                "verified": True,
                "quantity": 5,
            },
            "formats": ["wide"],
        },
    ).json()
    assert body["part"]["id"] is None
    assert body["cards"][0]["format"] == "wide"


def test_unknown_card_format_is_rejected(client):
    part_id = client.get("/api/parts").json()[0]["id"]
    response = client.post("/api/cards", json={"part_id": part_id, "formats": ["billboard"]})
    assert response.status_code == 422


def test_card_for_missing_part(client):
    assert client.post("/api/cards", json={"part_id": 999999, "formats": ["square"]}).status_code == 404


# ── stored report ----------------------------------------------------------
def test_stored_part_report_has_all_sections(client):
    part_id = client.get("/api/parts").json()[0]["id"]
    body = client.get(f"/api/parts/{part_id}/report").json()
    assert "### PART IDENTIFICATION REPORT" in body["markdown"]
    assert body["caption"]


def test_scan_history_is_recorded(client, sample_parts):
    before = len(client.get("/api/scans").json())
    _upload(client, sample_parts["pcv-valve"])
    after = client.get("/api/scans").json()
    assert len(after) == before + 1
    assert after[0]["image_url"].startswith("/media/uploads/")

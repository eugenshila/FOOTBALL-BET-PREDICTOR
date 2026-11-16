"""Trail Vault image review: suggestions are read-only until approved per row."""
import pytest

from app import cards
from app.image_review import local_image


@pytest.fixture
def review_part(client):
    response = client.post("/api/parts", json={
        "part_name": "Replacement PCV Valve",
        "oem_part_number": "TV-REVIEW-PCV",
        "quantity": 7,
        "unit_price": 1200,
        "fitment_text": "Jeep Wrangler 3.6L 2012-2018",
    })
    assert response.status_code == 201
    part = response.json()
    yield part
    client.delete(f"/api/parts/{part['id']}")


def test_candidates_do_not_modify_stock(client, review_part):
    part_id = review_part["id"]
    result = client.get(f"/api/parts/{part_id}/image-candidates").json()
    assert result["requires_approval"] is True
    assert result["source"] == "local catalogue"
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert client.get(candidate["image_url"]).status_code == 200
    assert client.get(f"/api/parts/{part_id}").json() == review_part


def test_approval_changes_only_photo_and_timestamp(client, review_part):
    part_id = review_part["id"]
    candidate = client.get(f"/api/parts/{part_id}/image-candidates").json()["candidates"][0]
    response = client.post(f"/api/parts/{part_id}/image-approval", json={"candidate_id": candidate["candidate_id"]})
    assert response.status_code == 200
    updated = response.json()
    assert updated["image_path"] == candidate["image_path"]
    for field in review_part:
        if field not in {"image_path", "updated_at"}:
            assert updated[field] == review_part[field]
    assert updated["verified"] is False
    # Repeated approvals are harmless; an image is not an EPC verification.
    assert client.post(f"/api/parts/{part_id}/image-approval", json={"candidate_id": candidate["candidate_id"]}).status_code == 200
    assert client.post("/api/cards", json={"part_id": part_id, "formats": ["square"]}).status_code == 409


def test_invalid_candidate_does_not_write(client, review_part):
    response = client.post(f"/api/parts/{review_part['id']}/image-approval", json={"candidate_id": "not-a-candidate"})
    assert response.status_code == 409
    assert client.get(f"/api/parts/{review_part['id']}").json() == review_part


def test_missing_part_for_image_review(client):
    assert client.get("/api/parts/999999999/image-candidates").status_code == 404
    assert client.post("/api/parts/999999999/image-approval", json={"candidate_id": "missing"}).status_code == 404


@pytest.mark.parametrize("path", ["https://example.com/part.jpg", "/etc/passwd", "assets/seed/../../README.md", "assets/seed/missing.jpg", "/media/uploads/../../README.md"])
def test_candidates_reject_remote_missing_or_unsafe_paths(path):
    assert local_image(path) is None


def test_browser_import_defers_photos_until_review(client):
    raw = "Part Name,OEM,Qty,Image\nReview PCV Valve,TV-CSV-REVIEW,2,assets/seed/pcv-valve.jpg\n"
    files = {"file": ("stock.csv", raw, "text/csv")}
    preview = client.post("/api/inventory/import?dry_run=true&review_images=true", files=files).json()
    assert preview["created"] == 1
    assert client.get("/api/parts?q=TV-CSV-REVIEW").json() == []
    applied = client.post("/api/inventory/import?dry_run=false&review_images=true", files=files).json()
    part_id = applied["results"][0]["part_id"]
    try:
        part = client.get(f"/api/parts/{part_id}").json()
        assert part["image_path"] == ""
        assert part["verified"] is False
        assert client.get(f"/api/parts/{part_id}/image-candidates").json()["candidates"]
    finally:
        client.delete(f"/api/parts/{part_id}")


def test_deferred_import_keeps_existing_photo(client, review_part):
    part_id = review_part["id"]
    client.patch(f"/api/parts/{part_id}", json={"image_path": "assets/seed/pcv-valve.jpg"})
    raw = "Part Name,OEM,Image\nReplacement PCV Valve,TV-REVIEW-PCV,assets/seed/wheel-hub.jpg\n"
    result = client.post("/api/inventory/import?dry_run=false&update_existing=true&review_images=true",
                         files={"file": ("stock.csv", raw, "text/csv")}).json()
    assert result["updated"] == 1
    assert client.get(f"/api/parts/{part_id}").json()["image_path"] == "assets/seed/pcv-valve.jpg"


def test_yellow_card_cta_has_dark_text():
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1080, 1080))
    from app.models import InventoryPart
    cards._cta_band(ImageDraw.Draw(image), cards.CardContext(part=InventoryPart(part_name="Test")), cards.FORMATS["square"], 45, 180)
    assert image.getpixel((5, 1000)) == cards.ACCENT
    assert any(pixel == cards.INK for row in range(900, 1080) for pixel in [image.getpixel((150, row))])

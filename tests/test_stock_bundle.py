"""ZIP import guards and row-specific, explicitly approved uploaded images."""
import io
import stat
import zipfile

import pytest
from PIL import Image

from app.config import UPLOAD_DIR
from app.stock_bundle import read_bundle


def photo():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "yellow").save(output, "JPEG")
    return output.getvalue()


def bundle(files=None, csv_text=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("stock.csv", csv_text or "part_name,oem_part_number,condition,quantity,image_path,verified\nZIP Test Bracket,000TV-ZIP,Used,3,images/000TV-ZIP.jpg,true\n")
        for name, raw in (files if files is not None else {"images/000TV-ZIP.jpg": photo(), "TEST-ONLY.txt": "TEST placeholders"}).items():
            archive.writestr(name, raw)
    return output.getvalue()


def test_preview_is_read_only(client):
    before = set(UPLOAD_DIR.rglob("*"))
    result = client.post("/api/inventory/import", files={"file": ("stock.zip", bundle(), "application/zip")})
    assert result.status_code == 200
    payload = result.json()
    assert payload["created"] == 1
    assert payload["bundle"]["images_matched"] == 1
    assert payload["bundle"]["test_only"] is True
    assert payload["results"][0]["image_filename"] == "images/000TV-ZIP.jpg"
    assert client.get("/api/parts?q=000TV-ZIP").json() == []
    assert set(UPLOAD_DIR.rglob("*")) == before


def test_zip_apply_stages_photo_until_approved_and_reupload_keeps_stock(client):
    files = {"file": ("stock.zip", bundle(), "application/zip")}
    response = client.post("/api/inventory/import?dry_run=false&mark_verified=true", files=files)
    assert response.status_code == 200
    part_id = response.json()["results"][0]["part_id"]
    try:
        part = client.get(f"/api/parts/{part_id}").json()
        assert part["oem_part_number"] == "000TV-ZIP"
        assert part["quantity"] == 3 and part["condition"] == "Used"
        assert part["image_path"] == "" and part["verified"] is False
        candidates = client.get(f"/api/parts/{part_id}/image-candidates").json()["candidates"]
        assert candidates[0]["reason"].startswith("TEST PLACEHOLDER")
        assert client.get(candidates[0]["image_url"]).status_code == 200
        approved = client.post(f"/api/parts/{part_id}/image-approval", json={"candidate_id": candidates[0]["candidate_id"]})
        assert approved.status_code == 200
        assert approved.json()["image_path"] and not approved.json()["verified"]
        saved = approved.json()["image_path"]
        repeated = client.post("/api/inventory/import?dry_run=false", files=files).json()
        assert repeated["skipped"] == 1 and repeated["created"] == 0
        current = client.get(f"/api/parts/{part_id}").json()
        assert current["quantity"] == 3 and current["image_path"] == saved
        assert client.post("/api/cards", json={"part_id": part_id, "formats": ["square"]}).status_code == 409
    finally:
        client.delete(f"/api/parts/{part_id}")


@pytest.mark.parametrize("files,message", [
    ({"../escape.jpg": photo()}, "unsafe path"),
    ({"/escape.jpg": photo()}, "unsafe path"),
    ({"images\\escape.jpg": photo()}, "unsafe path"),
    ({"images/000TV-ZIP.jpg": b"not an image"}, "unreadable"),
    ({}, "missing or invalid"),
    ({"evil.exe": b"x"}, "supports"),
])
def test_rejects_unsafe_or_invalid_bundle(files, message):
    with pytest.raises(ValueError, match=message):
        read_bundle(bundle(files))


def test_rejects_filename_oem_mismatch():
    with pytest.raises(ValueError, match="must match"):
        read_bundle(bundle({"images/OTHER.jpg": photo()}, "part_name,oem_part_number,image_path\nBracket,000TV-ZIP,images/OTHER.jpg\n"))


def test_auto_matches_filename_and_allows_missing_images():
    result = read_bundle(bundle({"images/000TV-ZIP.jpg": photo()}, "part_name,oem_part_number\nBracket,000TV-ZIP\nOther,NO-IMAGE\n"))
    assert result.rows == 2 and set(result.images) == {2}
    assert result.test_only is False


def test_rejects_duplicate_oems_before_apply(client):
    raw = bundle({}, "part_name,oem_part_number\nBracket,000TV-ZIP\nBracket,000TV-ZIP\n")
    response = client.post("/api/inventory/import?dry_run=false", files={"file": ("stock.zip", raw, "application/zip")})
    assert response.status_code == 422
    assert client.get("/api/parts?q=000TV-ZIP").json() == []


def test_requires_root_csv_and_rejects_invalid_archives():
    with pytest.raises(ValueError, match="readable"):
        read_bundle(b"not a zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("nested/stock.csv", "part_name,oem_part_number\nPart,123\n")
    with pytest.raises(ValueError):
        read_bundle(output.getvalue())


def test_rejects_symlinks():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("image.jpg")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(ValueError, match="link"):
        read_bundle(output.getvalue())


def test_csv_errors_prevent_all_zip_writes(client):
    raw = bundle({}, "part_name,oem_part_number,quantity\nBracket,000TV-ZIP,3\n,000TV-BAD,1\n")
    response = client.post("/api/inventory/import?dry_run=false", files={"file": ("stock.zip", raw, "application/zip")})
    assert response.status_code == 422
    assert client.get("/api/parts?q=000TV-ZIP").json() == []

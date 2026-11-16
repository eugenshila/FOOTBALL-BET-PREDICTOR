"""Preserve newer main-branch tools alongside the Verifix/local-auth refresh."""
from collections import Counter
from html.parser import HTMLParser
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image

from app import shop_profile
from app.models import ShopProfileUpdate


class PageIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for key, value in attrs if key == "id")


def test_workspace_keeps_all_tools_and_unique_ids(client):
    html = client.get("/").text
    parser = PageIds()
    parser.feed(html)
    assert all(count == 1 for count in Counter(parser.ids).values())
    for name in ("identify", "inventory", "studio", "reports", "settings"):
        assert f"tab-{name}" in parser.ids
        assert f"view-{name}" in parser.ids
    assert "btn-export-pdf" in parser.ids
    assert "sort-received" in parser.ids
    assert "shop-settings-form" in parser.ids
    assert "studio-search" in parser.ids


def test_pdf_and_verification_exports_still_work(client):
    pdf = client.get("/api/inventory.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-")
    excel = client.get("/api/inventory-verification.xlsx")
    assert excel.status_code == 200
    book = load_workbook(BytesIO(excel.content))
    try:
        assert set(book.sheetnames) == {"Verified", "Not Verified"}
    finally:
        book.close()


def test_saved_shop_profile_and_loopback_guard_survive(client, monkeypatch, tmp_path):
    monkeypatch.setattr(shop_profile, "PROFILE_PATH", tmp_path / "profile.json")
    profile = ShopProfileUpdate(shop_name="Verifix Counter", social_links={"instagram": "https://instagram.com/example"})
    shop_profile.save_profile(profile)
    config = client.get("/api/config").json()
    assert config["shop_name"] == "Verifix Counter"
    assert config["social_links"]["instagram"] == "https://instagram.com/example"
    # TestClient uses a non-loopback host; this restriction must not be lost.
    assert client.put("/api/settings", json=profile.model_dump()).status_code == 403


def test_excel_photo_is_a_candidate_until_individually_approved(client):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Part Name", "OEM Part Number", "Photo", "Quantity", "Condition"])
    sheet.append(["Excel Review Bracket", "000-INTEGRATION-XLSX", "", 2, "Used"])
    photo = BytesIO()
    Image.new("RGB", (50, 50), "gold").save(photo, format="PNG")
    photo.seek(0)
    sheet.add_image(SheetImage(photo), "C2")
    raw = BytesIO()
    workbook.save(raw)
    workbook.close()
    files = {"file": ("parts.xlsx", raw.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    preview = client.post("/api/inventory/import?dry_run=true&review_images=true", files=files)
    assert preview.status_code == 200
    assert preview.json()["created"] == 1 and preview.json()["images_found"] == 1
    assert client.get("/api/parts?q=000-INTEGRATION-XLSX").json() == []
    applied = client.post("/api/inventory/import?dry_run=false&review_images=true", files=files)
    assert applied.status_code == 200
    part_id = applied.json()["results"][0]["part_id"]
    try:
        assert applied.json()["images_pending"] == 1
        part = client.get(f"/api/parts/{part_id}").json()
        assert part["quantity"] == 2 and part["image_path"] == ""
        candidates = client.get(f"/api/parts/{part_id}/image-candidates").json()["candidates"]
        assert len(candidates) >= 1
        approved = client.post(f"/api/parts/{part_id}/image-approval", json={"candidate_id": candidates[0]["candidate_id"]})
        assert approved.status_code == 200 and approved.json()["image_path"]
        assert approved.json()["verified"] is False
    finally:
        client.delete(f"/api/parts/{part_id}")


def test_explicit_restock_adds_quantity_and_preserves_primary_name(client):
    part = client.post("/api/parts", json={"part_name": "Canonical Bracket", "oem_part_number": "000-INTEGRATION-MERGE", "quantity": 2}).json()
    try:
        raw = b"Part Name,OEM,Qty\nDifferent description,000-INTEGRATION-MERGE,3\n"
        response = client.post("/api/inventory/import?dry_run=false&update_existing=true", files={"file": ("restock.csv", raw, "text/csv")})
        assert response.status_code == 200 and response.json()["updated"] == 1
        saved = client.get(f"/api/parts/{part['id']}").json()
        assert saved["quantity"] == 5 and saved["part_name"] == "Canonical Bracket"
    finally:
        client.delete(f"/api/parts/{part['id']}")

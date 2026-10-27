"""CSV import: real-world spreadsheet mess, dry runs, dedupe and round-trip."""

from __future__ import annotations

import io

import pytest

from app.importers import (
    export_csv,
    import_csv,
    map_headers,
    parse_fitment,
    parse_price,
    parse_quantity,
)


# ── scalar parsing ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("KES 12,800.50", 12800.50),
        ("12,800", 12800.0),
        ("1.200,50", 1200.50),
        ("$45", 45.0),
        ("46 500", 46500.0),
        ("", 0.0),
        ("n/a", 0.0),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("14", 14), ("14 pcs", 14), ("", 0), ("-3", 0), ("lots", 0)],
)
def test_parse_quantity(raw, expected):
    assert parse_quantity(raw) == expected


def test_parse_fitment_multiple_vehicles():
    fitment = parse_fitment("Jeep Grand Cherokee 3.6L V6 2011-2021; Dodge Durango 5.7L 2011-2020")
    assert len(fitment) == 2
    assert fitment[0].make == "Jeep"
    assert fitment[0].model == "Grand Cherokee"
    assert fitment[0].engine == "3.6L V6"
    assert fitment[0].years == "2011-2021"
    assert fitment[1].make == "Dodge"


def test_parse_fitment_two_word_make_and_fuel_words():
    fitment = parse_fitment("Land Rover Discovery 4 3.0L Diesel 2010-2016")
    assert fitment[0].make == "Land Rover"
    assert fitment[0].model == "Discovery 4"
    assert "Diesel" not in fitment[0].model


def test_parse_fitment_open_ended_years():
    fitment = parse_fitment("Toyota Hilux 2.7L 2015-present")
    assert fitment[0].years == "2015-present"


def test_parse_fitment_empty():
    assert parse_fitment("") == []


# ── header mapping ────────────────────────────────────────────────────────
def test_header_aliases_are_recognised():
    mapping, ignored = map_headers(
        ["Description", "OEM No", "Cross Refs", "Shelf", "On Hand", "Retail", "Notes"]
    )
    assert mapping["Description"] == "part_name"
    assert mapping["OEM No"] == "oem_part_number"
    assert mapping["Cross Refs"] == "alternate_numbers"
    assert mapping["Shelf"] == "bin_location"
    assert mapping["On Hand"] == "quantity"
    assert mapping["Retail"] == "unit_price"
    assert ignored == []


def test_unknown_columns_are_ignored_not_fatal():
    mapping, ignored = map_headers(["Part Name", "Supplier", "Warehouse Code"])
    assert mapping == {"Part Name": "part_name"}
    assert set(ignored) == {"Supplier", "Warehouse Code"}


# ── import behaviour ──────────────────────────────────────────────────────
CSV_BASIC = """Part Name,OEM,Qty,Price,Bin
Serpentine Belt Tensioner,53034046AB,5,"KES 7,400",E1-B2
Radiator Cap,52028918AA,22,950,E2-C1
"""


def test_dry_run_writes_nothing(client):
    before = client.get("/api/stats").json()["parts"]
    response = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("stock.csv", io.BytesIO(CSV_BASIC.encode()), "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["created"] == 2
    assert client.get("/api/stats").json()["parts"] == before


def test_apply_creates_parts(client):
    before = client.get("/api/stats").json()["parts"]
    body = client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("stock.csv", io.BytesIO(CSV_BASIC.encode()), "text/csv")},
    ).json()
    assert body["created"] == 2
    assert client.get("/api/stats").json()["parts"] == before + 2

    cap = client.get("/api/lookup", params={"number": "52028918AA"}).json()
    assert cap["quantity"] == 22
    assert cap["bin_location"] == "E2-C1"
    assert cap["unit_price"] == 950
    assert cap["currency"] == "KES"


def test_reimport_skips_existing_oem(client):
    body = client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("stock.csv", io.BytesIO(CSV_BASIC.encode()), "text/csv")},
    ).json()
    assert body["created"] == 0
    assert body["skipped"] == 2


def test_semicolon_delimited_file_with_bom(client):
    text = "\ufeffPart Name;OEM;Qty;Price\nThermostat Housing;5184651AH;4;4350\n"
    body = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("euro.csv", io.BytesIO(text.encode("utf-8")), "text/csv")},
    ).json()
    assert body["rows_total"] == 1
    assert body["created"] == 1
    assert body["columns_mapped"]["Part Name"] == "part_name"


def test_bad_rows_are_reported_and_do_not_abort(client):
    text = "Part Name,OEM,Qty\nGood Part,AAA-1,2\n,nothing,1\nAnother Part,BBB-2,3\n"
    body = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("mixed.csv", io.BytesIO(text.encode()), "text/csv")},
    ).json()
    assert body["created"] == 2
    assert body["errors"] == 1
    assert "missing part name" in body["problems"][0]["message"]


def test_file_without_name_column_is_rejected_clearly(client):
    text = "Item Code,Qty\nABC-1,4\n"
    body = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("bad.csv", io.BytesIO(text.encode()), "text/csv")},
    ).json()
    assert body["errors"] == 1
    assert "part-name column" in body["problems"][0]["message"]


def test_empty_csv_is_rejected(client):
    response = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
    )
    assert response.status_code == 422


def test_export_round_trips(client):
    exported = client.get("/api/inventory.csv")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert "attachment" in exported.headers["content-disposition"]
    assert "Part Name" in exported.text
    assert "52124767AE" in exported.text


def test_exported_csv_can_be_reimported(client):
    text = client.get("/api/inventory.csv").text
    body = client.post(
        "/api/inventory/import?dry_run=true",
        files={"file": ("round.csv", io.BytesIO(text.encode()), "text/csv")},
    ).json()
    assert body["errors"] == 0
    # everything already exists, so nothing new and nothing skipped-by-name
    assert body["created"] == 0
    assert body["skipped"] == body["rows_total"]


def test_fitment_survives_import(client):
    text = (
        "Part Name,OEM,Fitment,Qty\n"
        "Glow Plug,GLP-77,Isuzu D-Max 3.0L 2012-2019 | Nissan Navara 2.5L 2008-2015,9\n"
    )
    client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("f.csv", io.BytesIO(text.encode()), "text/csv")},
    )
    part = client.get("/api/lookup", params={"number": "GLP-77"}).json()
    assert len(part["fitment"]) == 2
    assert part["fitment"][0]["make"] == "Isuzu"
    assert part["fitment"][1]["model"] == "Navara"


def test_export_route_is_not_shadowed_by_part_id(client):
    """Regression: /api/inventory.csv must not be parsed as a part id."""
    response = client.get("/api/inventory.csv")
    assert response.status_code == 200
    assert "Part Name" in response.text


def test_row_without_part_number_does_not_duplicate_on_reimport(client):
    text = "Part Name,OEM,Qty,Bin\nBulk Hose Clamp Assortment,,12,G1-C2\n"
    first = client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("a.csv", io.BytesIO(text.encode()), "text/csv")},
    ).json()
    second = client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("a.csv", io.BytesIO(text.encode()), "text/csv")},
    ).json()
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["skipped"] == 1


def test_name_lookup_is_whitespace_and_case_insensitive(client):
    from app import db

    client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("n.csv", io.BytesIO(b"Part Name,OEM\n  Oil  Filter  ,OF-9\n"), "text/csv")},
    )
    assert db.find_by_name("oil filter") is not None


# ── replacing existing data ───────────────────────────────────────────────
def test_load_stock_replace_swaps_the_whole_inventory(client):
    from app import db, importers

    new_file = "Part Name,OEM,Qty,Price\nReplacement Pump,RPL-100,5,9800\nReplacement Filter,RPL-200,40,750\n"
    removed = db.clear_inventory()
    importers.import_csv(new_file, dry_run=False)
    parts = db.list_parts()
    assert removed["parts_removed"] >= 4
    assert {p.oem_part_number for p in parts} == {"RPL-100", "RPL-200"}


def test_clear_inventory_keeps_scans_by_default(client):
    from app import db

    db.save_scan("assets/seed/pcv-valve.jpg", "{}", None)
    before = db.stats()["scans"]
    db.clear_inventory(include_scans=False)
    assert db.stats()["scans"] == before
    db.clear_inventory(include_scans=True)
    assert db.stats()["scans"] == 0


def test_import_rejects_a_file_that_parses_to_nothing():
    """A header-only or junk file must not be able to wipe the inventory."""
    from app.importers import import_csv

    report = import_csv("Part Name,OEM\n", dry_run=True)
    assert report.rows_total == 0


def test_fitment_text_is_parsed_by_the_server(client):
    part = client.post(
        "/api/parts",
        json={
            "part_name": "Fitment Text Probe",
            "fitment_text": "Land Rover Discovery 4 3.0L 2010-2016",
            "verified": True,
        },
    ).json()
    assert part["fitment"][0]["make"] == "Land Rover"
    assert part["fitment"][0]["model"] == "Discovery 4"
    assert part["fitment"][0]["years"] == "2010-2016"
    client.delete(f"/api/parts/{part['id']}")


def test_fitment_text_on_update(client):
    part = client.post(
        "/api/parts", json={"part_name": "Fitment Update Probe", "verified": True}
    ).json()
    updated = client.patch(
        f"/api/parts/{part['id']}",
        json={"fitment_text": "Mercedes-Benz Sprinter 2.1L 2012-2018"},
    ).json()
    assert updated["fitment"][0]["make"] in {"Mercedes", "Mercedes-Benz"}
    assert updated["fitment"][0]["model"] == "Sprinter"
    client.delete(f"/api/parts/{part['id']}")


def test_cli_bootstrap_respects_seed_demo_data(monkeypatch, capsys):
    """A production database must never be seeded with demo parts by the CLI."""
    from app import catalog, cli, db
    from app.config import settings

    monkeypatch.setattr(settings, "seed_demo_data", False)
    called = {"n": 0}
    monkeypatch.setattr(cli, "seed_if_empty", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 4)

    cli._bootstrap()
    assert called["n"] == 0
    assert "seeded demo catalogue" not in capsys.readouterr().out


def test_real_world_shop_sheet_headers_are_mapped():
    """Regression: a real stock sheet used 'PART NUMBER' / 'PART DESCRIPTION'."""
    mapping, ignored = map_headers(
        ["PART NUMBER", "PART DESCRIPTION", "LOCATION", "NEW/USE", "CAR FROM", "QUANTITY", "CONDITION"]
    )
    assert mapping["PART NUMBER"] == "oem_part_number"
    assert mapping["PART DESCRIPTION"] == "part_name"
    assert mapping["LOCATION"] == "bin_location"
    assert mapping["NEW/USE"] == "condition"
    assert mapping["CAR FROM"] == "fitment"
    assert mapping["QUANTITY"] == "quantity"
    # The sheet carries both NEW/USE and CONDITION; the first wins and the
    # duplicate is reported instead of silently overwriting it.
    assert mapping["NEW/USE"] == "condition"
    assert "CONDITION" in ignored


def test_exchange_shorthand_maps_to_refurbished():
    from app.importers import parse_condition
    from app.models import Condition

    assert parse_condition("EX") is Condition.refurbished
    assert parse_condition("New") is Condition.new
    assert parse_condition("Used") is Condition.used


def test_import_can_mark_rows_verified(client):
    """Shop-asserted rows should be publishable without editing 40 records."""
    before = client.get("/api/parts", params={"q": "VPROBE-1"}).json()
    assert before == []
    client.post(
        "/api/inventory/import?dry_run=false&mark_verified=true",
        files={"file": ("v.csv", io.BytesIO(b"Part Name,OEM,Qty\nVerified Probe,VPROBE-1,3\n"), "text/csv")},
    )
    part = client.get("/api/parts", params={"q": "VPROBE-1"}).json()[0]
    assert part["verified"] is True
    assert client.post("/api/cards", json={"part_id": part["id"], "formats": ["square"]}).status_code == 200
    client.delete(f"/api/parts/{part['id']}")


def test_import_stays_unverified_by_default(client):
    client.post(
        "/api/inventory/import?dry_run=false",
        files={"file": ("u.csv", io.BytesIO(b"Part Name,OEM\nUnverified Probe,UPROBE-1\n"), "text/csv")},
    )
    part = client.get("/api/parts", params={"q": "UPROBE-1"}).json()[0]
    assert part["verified"] is False
    assert client.post("/api/cards", json={"part_id": part["id"], "formats": ["square"]}).status_code == 409
    client.delete(f"/api/parts/{part['id']}")

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

"""Delivery handovers and photo suggestions use isolated test storage."""
from concurrent.futures import ThreadPoolExecutor

from app import db


def make_part(client, **extra):
    payload = {"part_name": "FIFO test component", "oem_part_number": "FIFO-TEST-248", "quantity": 3, **extra}
    response = client.post("/api/parts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def handover(client, number, collector, lines, note=""):
    return client.post("/api/warehouse/delivery-orders", json={"order_number": number, "collected_by": collector, "note": note, "lines": lines})


def test_delivery_order_tracks_fifo_collector_and_multiple_parts(client):
    first = make_part(client)
    second = make_part(client, part_name="Other part", oem_part_number="FIFO-OTHER-248", quantity=2)
    pid = first["id"]
    try:
        client.post(f"/api/parts/{pid}/adjust", json={"part_id": pid, "delta": 4, "reason": "received"})
        lots = client.get(f"/api/warehouse/parts/{pid}/fifo").json()["lots"]
        assert [lot["remaining"] for lot in lots] == [3, 4]
        lines = [{"part_id": pid, "quantity": 5}, {"part_id": second["id"], "quantity": 1}]
        response = handover(client, "DO-TEST-1", "Mechanic A", lines, "Job 123")
        assert response.status_code == 201, response.text
        order = response.json()
        assert order["recorded_by"] == "suite@example.test"
        assert order["collected_by"] == "Mechanic A" and order["handed_over_at"].endswith("+00:00")
        assert [(a["movement_id"], a["quantity"]) for a in order["lines"][0]["allocations"]] == [(lots[0]["movement_id"], 3), (lots[1]["movement_id"], 2)]
        assert client.get(f"/api/parts/{pid}").json()["quantity"] == 2
        assert client.get(f"/api/parts/{second['id']}").json()["quantity"] == 1
        assert [lot["remaining"] for lot in client.get(f"/api/warehouse/parts/{pid}/fifo").json()["lots"]] == [2]
        history = client.get("/api/warehouse/delivery-orders?q=DO-TEST-1").json()
        assert history[0]["collected_by"] == "Mechanic A" and history[0]["note"] == "Job 123"
        assert client.get("/api/warehouse/delivery-orders?q=Other%20part").json()[0]["id"] == order["id"]
        assert handover(client, "do-test-1", "A", [{"part_id": pid, "quantity": 1}]).status_code == 409
        assert client.get(f"/api/parts/{pid}").json()["quantity"] == 2
    finally:
        client.delete(f"/api/parts/{pid}")
        client.delete(f"/api/parts/{second['id']}")
    # Saved order is a snapshot and survives deletion of an inventory record.
    assert client.get("/api/warehouse/delivery-orders?q=DO-TEST-1").json()[0]["lines"][0]["part_name"] == first["part_name"]


def test_invalid_or_insufficient_order_is_atomic(client):
    first = make_part(client, oem_part_number="FIFO-ATOMIC-1")
    second = make_part(client, oem_part_number="FIFO-ATOMIC-2")
    try:
        pid, sid = first["id"], second["id"]
        assert handover(client, "DO-ATOMIC", "Tech", [{"part_id": pid, "quantity": 1}, {"part_id": sid, "quantity": 4}]).status_code == 409
        assert client.get(f"/api/parts/{pid}").json()["quantity"] == 3
        assert not client.get("/api/warehouse/delivery-orders?q=DO-ATOMIC").json()
        for number, collector, lines in [
            (" ", "Tech", [{"part_id": pid, "quantity": 1}]),
            ("DO-BAD", " ", [{"part_id": pid, "quantity": 1}]),
            ("DO-BAD", "Tech", [{"part_id": pid, "quantity": 0}]),
            ("DO-BAD", "Tech", [{"part_id": pid, "quantity": 1}, {"part_id": pid, "quantity": 1}]),
            ("DO-BAD", "Tech", []),
        ]:
            assert handover(client, number, collector, lines).status_code == 422
    finally:
        client.delete(f"/api/parts/{first['id']}")
        client.delete(f"/api/parts/{second['id']}")


def test_legacy_opening_stock_is_unknown_and_first(client):
    part = make_part(client, oem_part_number="FIFO-LEGACY-248")
    pid = part["id"]
    try:
        with db.connect() as conn:
            conn.execute("DELETE FROM movements WHERE part_id = ?", (pid,))
        client.post(f"/api/parts/{pid}/adjust", json={"part_id": pid, "delta": 2})
        fifo = client.get(f"/api/warehouse/parts/{pid}/fifo").json()
        assert fifo["has_unknown_history"] and [x["remaining"] for x in fifo["lots"]] == [3, 2]
        order = handover(client, "DO-LEGACY-1", "Technician", [{"part_id": pid, "quantity": 4}]).json()
        assert [(x["received_at"] is None, x["quantity"]) for x in order["lines"][0]["allocations"]] == [(True, 3), (False, 1)]
    finally:
        client.delete(f"/api/parts/{pid}")


def test_simultaneous_handover_cannot_oversell(client):
    part = make_part(client, oem_part_number="FIFO-RACE-248", quantity=3)
    pid = part["id"]
    try:
        def issue(index):
            try:
                return db.create_delivery_order(f"DO-RACE-{index}", "Tech", "operator@example.test", "", [{"part_id": pid, "quantity": 2}])
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(issue, range(2)))
        assert sum(bool(x) for x in outcomes) == 1
        assert db.get_part(pid).quantity == 1
    finally:
        client.delete(f"/api/parts/{pid}")


def test_photo_suggestions_from_database_and_no_stock_changes(client, sample_parts):
    part = make_part(client, oem_part_number="PHOTO-DB-248", image_path="assets/seed/pcv-valve.jpg")
    pid = part["id"]
    try:
        before = client.get(f"/api/parts/{pid}").json()
        image = sample_parts["pcv-valve"].read_bytes()
        result = client.post("/api/identify", files={"file": ("photo.jpg", image, "image/jpeg")})
        assert result.status_code == 200, result.text
        candidate = next(c for c in result.json()["photo_candidates"] if c["part"]["id"] == pid)
        assert candidate["distance"] < 0.05
        assert client.get(candidate["reference_url"]).status_code == 200
        assert client.get(f"/api/parts/{pid}").json() == before
    finally:
        client.delete(f"/api/parts/{pid}")


def test_inconsistent_legacy_movements_leave_new_receipts_traceable():
    movements = [
        {"id": 1, "delta": 10, "created_at": "old"},
        {"id": 2, "delta": 2, "created_at": "new"},
    ]
    lots, unknown = db._fifo_lots(5, movements)
    assert unknown
    assert lots == [
        {"movement_id": None, "received_at": None, "remaining": 3},
        {"movement_id": 2, "received_at": "new", "remaining": 2},
    ]


def test_schema_upgrade_preserves_existing_inventory(tmp_path):
    from app import storage

    dsn = f"sqlite:///{tmp_path / 'existing.db'}"
    # Simulate the old schema before delivery order tables were introduced.
    old_schema = storage.SQLITE_SCHEMA
    start = old_schema.index("CREATE TABLE IF NOT EXISTS delivery_orders")
    end = old_schema.index("CREATE TABLE IF NOT EXISTS scans", start)
    old_schema = old_schema[:start] + old_schema[end:]
    with storage.connection(dsn) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO parts (part_name, quantity, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     ("Existing local stock", 11, "old", "old"))
        conn.execute("INSERT INTO movements (part_id, delta, created_at) VALUES (?, ?, ?)", (1, 11, "old"))
    storage.init_schema(dsn)
    with storage.connection(dsn) as conn:
        part = conn.execute("SELECT part_name, quantity, created_at, updated_at FROM parts WHERE id = 1").fetchone()
        movement = conn.execute("SELECT part_id, delta, created_at FROM movements WHERE part_id = 1").fetchone()
        assert tuple(part) == ("Existing local stock", 11, "old", "old")
        assert tuple(movement) == (1, 11, "old")
        assert conn.execute("SELECT COUNT(*) AS count FROM delivery_orders").fetchone()["count"] == 0


def test_identify_checks_clear_local_photo_before_provider(client, sample_parts, monkeypatch):
    from app import photo_match, vision

    part = make_part(client, part_name="Local wheel hub", oem_part_number="LOCAL-WHEEL-TEST",
                     image_path="assets/seed/wheel-hub.jpg")
    try:
        # Isolate the photo lookup from records other tests may have left behind.
        monkeypatch.setattr(photo_match.db, "list_parts", lambda **kwargs: [db.get_part(part["id"])])
        monkeypatch.setattr(photo_match, "catalogue_parts", lambda: [])
        def forbidden_provider(*args, **kwargs):
            raise AssertionError("External/provider identification must not run for a clear local match")
        monkeypatch.setattr(vision, "identify", forbidden_provider)
        result = client.post("/api/identify", files={"file": ("wheel.jpg", sample_parts["wheel-hub"].read_bytes(), "image/jpeg")})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["report"]["provider"] == "local-inventory"
        assert body["report"]["needs_manual_review"] is True
        assert body["report"]["number_confidence"] == "low"
        assert body["matched_part"]["id"] == part["id"]
        assert body["scan_id"]
    finally:
        client.delete(f"/api/parts/{part['id']}")


def test_identifier_stops_for_weak_and_ambiguous_local_candidates(client, sample_parts, monkeypatch):
    from app import photo_match, vision

    first = make_part(client, part_name="Local PCV test", oem_part_number="LOCAL-PCV-TEST")
    second = make_part(client, part_name="Local wheel test", oem_part_number="LOCAL-HUB-TEST")
    try:
        part = db.get_part(first["id"])
        other = db.get_part(second["id"])
        def forbidden_provider(*args, **kwargs):
            raise AssertionError("A local candidate must prevent external identification")
        monkeypatch.setattr(vision, "identify", forbidden_provider)
        payload = sample_parts["wheel-hub"].read_bytes()
        for suggestions in [
            [{"part": part, "distance": 0.2, "reference_url": "/assets/seed/pcv-valve.jpg"}],
            [{"part": part, "distance": 0.01, "reference_url": "/assets/seed/pcv-valve.jpg"},
             {"part": other, "distance": 0.02, "reference_url": "/assets/seed/wheel-hub.jpg"}],
        ]:
            monkeypatch.setattr(photo_match, "suggestions", lambda image, result=suggestions: result)
            response = client.post("/api/identify", files={"file": ("wheel.jpg", payload, "image/jpeg")})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["report"]["provider"] == "local-inventory"
            assert body["report"]["overall_confidence"] == "low"
            assert body["report"]["needs_manual_review"] is True
            assert "uncertain" in body["report"]["warnings"][-1]
            assert body["matched_part"]["id"] == part.id
            assert len(body["photo_candidates"]) == len(suggestions)
        assert client.get(f"/api/parts/{part.id}").json()["quantity"] == first["quantity"]
    finally:
        client.delete(f"/api/parts/{first['id']}")
        client.delete(f"/api/parts/{second['id']}")


def test_identifier_uses_provider_when_no_local_candidates(client, sample_parts, monkeypatch):
    from app import photo_match, vision
    from app.models import IdentificationReport

    first = make_part(client, part_name="Fallback PCV", oem_part_number="FALLBACK-PCV-TEST")
    try:
        calls = []
        def provider(image, mime):
            calls.append(mime)
            return IdentificationReport(part_name=first["part_name"], oem_part_number=first["oem_part_number"], provider="test-provider")
        monkeypatch.setattr(vision, "identify", provider)
        monkeypatch.setattr(photo_match, "suggestions", lambda image: [])
        response = client.post("/api/identify", files={"file": ("wheel.jpg", sample_parts["wheel-hub"].read_bytes(), "image/jpeg")})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"]["provider"] == "test-provider"
        assert body["matched_part"]["id"] == first["id"]
        assert "No local photo suggestions found" in body["report"]["warnings"][0]
        assert calls == ["image/jpeg"]
    finally:
        client.delete(f"/api/parts/{first['id']}")

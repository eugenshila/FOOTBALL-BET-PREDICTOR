"""Storage layer: dialect selection, SQL translation, and both engines.

The PostgreSQL cases run whenever ``TEST_DATABASE_URL`` is set (see RUNNING.md);
otherwise they are skipped so the default suite needs no server.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app import db, storage
from app.models import PartCreate, VehicleFitment

PG_DSN = os.getenv("TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_DSN, reason="set TEST_DATABASE_URL to run")


# ── dialect helpers (engine independent) ──────────────────────────────────
def test_dialect_detection():
    assert storage.dialect_of("postgresql://u:p@h/db") == storage.POSTGRES
    assert storage.dialect_of("postgres://u:p@h/db") == storage.POSTGRES
    assert storage.dialect_of("sqlite:///./data/x.db") == storage.SQLITE


def test_unsupported_scheme_is_explained():
    with pytest.raises(storage.StorageError) as excinfo:
        storage.dialect_of("mysql://u:p@h/db")
    assert "postgresql://" in str(excinfo.value)


def test_password_is_masked_in_diagnostics():
    masked = storage.mask_dsn("postgresql://partsid:supersecret@localhost:5432/partsid")
    assert "supersecret" not in masked
    assert masked == "postgresql://partsid:***@localhost:5432/partsid"
    assert storage.mask_dsn("sqlite:///./data/x.db").startswith("sqlite")


def test_sqlite_path_extraction():
    assert str(storage.sqlite_path_of("sqlite:///./data/x.db")) == "data/x.db"


def test_default_is_sqlite_when_no_url_is_configured(monkeypatch):
    """With DATABASE_URL unset the app must still run out of the box."""
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "")
    assert storage.resolve_dsn("").startswith("sqlite")
    assert storage.dialect_of(storage.resolve_dsn("")) == storage.SQLITE


def test_explicit_dsn_wins_over_the_environment(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://a:b@h/db")
    assert storage.resolve_dsn("sqlite:///./tmp/x.db") == "sqlite:///./tmp/x.db"


def test_placeholders_are_translated_per_dialect():
    from app.storage import Connection

    class FakeRaw:
        def __init__(self):
            self.sql = None

        def cursor(self):
            outer = self

            class C:
                def execute(self, sql, params):
                    outer.sql = sql

                def fetchone(self):
                    return {"id": 5}

            return C()

    raw = FakeRaw()
    pg = Connection(raw, storage.POSTGRES)
    pg.execute("SELECT * FROM parts WHERE id = ? AND bin = ?", (1, "A1"))
    assert raw.sql == "SELECT * FROM parts WHERE id = %s AND bin = %s"

    sqlite_raw = FakeRaw()
    Connection(sqlite_raw, storage.SQLITE).execute("SELECT * FROM parts WHERE id = ?", (1,))
    assert sqlite_raw.sql == "SELECT * FROM parts WHERE id = ?"


def test_engine_specific_sql_helpers():
    from app.storage import Connection

    class Null:
        pass

    assert Connection(Null(), storage.POSTGRES).like == "ILIKE"
    assert Connection(Null(), storage.SQLITE).like == "LIKE"
    assert "LOWER(part_name)" in Connection(Null(), storage.POSTGRES).name_order
    assert "COLLATE NOCASE" in Connection(Null(), storage.SQLITE).name_order


# ── behaviour that must hold on every engine ──────────────────────────────
@pytest.fixture()
def fresh_part(client):
    name = f"Storage Test Part {uuid.uuid4().hex[:8]}"
    part = client.post(
        "/api/parts",
        json={
            "part_name": name,
            "oem_part_number": f"ST-{uuid.uuid4().hex[:6].upper()}",
            "bin_location": "Z9-Z9",
            "quantity": 4,
            "unit_price": 1234.5,
            "verified": True,
        },
    ).json()
    yield part
    client.delete(f"/api/parts/{part['id']}")


def test_insert_returns_a_usable_id(fresh_part):
    assert isinstance(fresh_part["id"], int) and fresh_part["id"] > 0
    assert db.get_part(fresh_part["id"]) is not None


def test_ids_increment_across_tables(fresh_part):
    part_id = fresh_part["id"]
    scan_id = db.save_scan("assets/seed/pcv-valve.jpg", "{}", part_id)
    assert isinstance(scan_id, int) and scan_id > 0


def test_case_insensitive_search(client, fresh_part):
    """LIKE on SQLite, ILIKE on Postgres - both must match regardless of case."""
    hits = client.get("/api/parts", params={"q": fresh_part["part_name"].upper()}).json()
    assert any(p["id"] == fresh_part["id"] for p in hits)


def test_float_prices_survive_the_round_trip(fresh_part):
    assert fresh_part["unit_price"] == pytest.approx(1234.5)


def test_booleans_survive_the_round_trip(client, fresh_part):
    assert fresh_part["verified"] is True
    client.patch(f"/api/parts/{fresh_part['id']}", json={"verified": False})
    assert client.get(f"/api/parts/{fresh_part['id']}").json()["verified"] is False


def test_movements_cascade_on_delete(client):
    part = client.post(
        "/api/parts",
        json={"part_name": "Cascade Probe", "quantity": 2, "verified": True},
    ).json()
    db.adjust_stock(part["id"], -1, reason="sold")
    assert len(db.movements_for(part["id"])) == 2
    client.delete(f"/api/parts/{part['id']}")
    assert db.movements_for(part["id"]) == []


def test_fitment_json_survives_any_engine(client):
    part = client.post(
        "/api/parts",
        json={
            "part_name": "Fitment Probe",
            "fitment": [
                VehicleFitment(make="Jeep", model="Wrangler", engine="3.6L", years="2012-2018").model_dump()
            ],
            "verified": True,
        },
    ).json()
    fetched = client.get(f"/api/parts/{part['id']}").json()
    assert fetched["fitment"][0]["make"] == "Jeep"
    client.delete(f"/api/parts/{part['id']}")


# ── PostgreSQL specifics ──────────────────────────────────────────────────
@requires_pg
def test_suite_is_actually_running_on_postgres(dialect):
    assert dialect == storage.POSTGRES


@requires_pg
def test_postgres_uses_identity_columns(client):
    """Regression: lastrowid does not exist in Postgres; INSERT..RETURNING must."""
    part = client.post(
        "/api/parts", json={"part_name": "Identity Probe", "verified": True}
    ).json()
    assert part["id"] >= 1
    client.delete(f"/api/parts/{part['id']}")


@requires_pg
def test_postgres_server_version_is_reported():
    info = db.database_info()
    assert info["connected"] is True
    assert "PostgreSQL" in info["version"]


@requires_pg
def test_postgres_pool_reuses_connections():
    """A burst of operations must not exhaust or corrupt the pool."""
    for _ in range(12):
        assert db.stats()["parts"] >= 0
    assert db.database_info()["connected"] is True


@requires_pg
def test_postgres_health_reports_database(client):
    payload = client.get("/api/health").json()
    assert payload["database"]["dialect"] == storage.POSTGRES
    assert payload["database"]["connected"] is True
    assert "***" in payload["database"]["dsn"] or "@" not in payload["database"]["dsn"]


# ── .env loading ──────────────────────────────────────────────────────────
def test_dotenv_loader_reads_values_and_keeps_real_env(tmp_path, monkeypatch):
    from app.config import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "PARTSID_TEST_A=plain\n"
        'PARTSID_TEST_B="quoted value"\n'
        "PARTSID_TEST_C=from-file\n"
        "malformed line without equals\n"
        "\n"
    )
    monkeypatch.delenv("PARTSID_TEST_A", raising=False)
    monkeypatch.delenv("PARTSID_TEST_B", raising=False)
    monkeypatch.setenv("PARTSID_TEST_C", "from-env")

    _load_dotenv(env_file)

    import os

    assert os.environ["PARTSID_TEST_A"] == "plain"
    assert os.environ["PARTSID_TEST_B"] == "quoted value"
    assert os.environ["PARTSID_TEST_C"] == "from-env"  # real env wins


def test_dotenv_loader_ignores_missing_file(tmp_path):
    from app.config import _load_dotenv

    _load_dotenv(tmp_path / "nope.env")  # must not raise

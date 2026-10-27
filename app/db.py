"""SQLite persistence for inventory, stock movements and scan history.

Runs on SQLite by default and on PostgreSQL when ``DATABASE_URL`` points at one;
the engine-specific details live in :mod:`app.storage`, so everything here is
portable SQL. A connection-per-operation model keeps it safe under the threaded
dev server without a global lock.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage
from .models import (
    Condition,
    InventoryPart,
    PartCreate,
    PartUpdate,
    ScanRecord,
    VehicleFitment,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dsn(path: Path | str | None = None) -> str:
    """Allow per-call overrides (tests, alternate databases) without globals."""
    if isinstance(path, str) and "://" in path:
        return path
    return storage.resolve_dsn()


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[storage.Connection]:
    with storage.connection(_dsn(path)) as conn:
        yield conn


def _row_to_part(row: Any) -> InventoryPart:
    return InventoryPart(
        id=row["id"],
        part_name=row["part_name"],
        oem_part_number=row["oem_part_number"] or "",
        alternate_numbers=json.loads(row["alternate_numbers"] or "[]"),
        superseded_numbers=json.loads(row["superseded_numbers"] or "[]"),
        fitment=[VehicleFitment(**f) for f in json.loads(row["fitment"] or "[]")],
        condition=Condition(row["condition"] or "New"),
        bin_location=row["bin_location"] or "",
        quantity=int(row["quantity"] or 0),
        unit_price=float(row["unit_price"] or 0),
        currency=row["currency"] or "KES",
        category=row["category"] or "",
        technical_notes=json.loads(row["technical_notes"] or "[]"),
        image_path=row["image_path"] or "",
        verified=bool(row["verified"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def init_db(path: Path | str | None = None) -> str:
    """Create the schema if needed. Returns the dialect in use."""
    return storage.init_schema(_dsn(path))


def database_info() -> dict[str, Any]:
    return storage.describe()


def list_parts(
    query: str = "",
    *,
    in_stock_only: bool = False,
    limit: int = 200,
    path: Path | str | None = None,
) -> list[InventoryPart]:
    with connect(path) as conn:
        like = conn.like
        sql = "SELECT * FROM parts"
        clauses: list[str] = []
        params: list[object] = []
        if query:
            clauses.append(
                f"(part_name {like} ? OR oem_part_number {like} ? OR bin_location {like} ? "
                f"OR alternate_numbers {like} ? OR category {like} ? OR fitment {like} ?)"
            )
            needle = f"%{query}%"
            params.extend([needle] * 6)
        if in_stock_only:
            clauses.append("quantity > 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {conn.name_order} ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_part(r) for r in rows]


def get_part(part_id: int, path: Path | str | None = None) -> InventoryPart | None:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(row) if row else None


def _normalise_number(value: str) -> str:
    return (value or "").replace(" ", "").replace("-", "").upper()


def find_by_number(number: str, path: Path | str | None = None) -> InventoryPart | None:
    """Exact OEM or alternate-number lookup (case/space insensitive)."""
    needle = _normalise_number(number)
    if not needle:
        return None
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM parts").fetchall()
    for row in rows:
        part = _row_to_part(row)
        haystack = [part.oem_part_number, *part.alternate_numbers, *part.superseded_numbers]
        for candidate in haystack:
            if candidate and _normalise_number(candidate) == needle:
                return part
    return None


def find_by_name(name: str, path: Path | str | None = None) -> InventoryPart | None:
    """Case/space-insensitive exact match on the part name."""
    needle = " ".join((name or "").split()).lower()
    if not needle:
        return None
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM parts").fetchall()
    for row in rows:
        if " ".join((row["part_name"] or "").split()).lower() == needle:
            return _row_to_part(row)
    return None


def create_part(
    payload: PartCreate, *, path: Path | str | None = None, log_movement: bool = True
) -> InventoryPart:
    stamp = _now()
    with connect(path) as conn:
        cur = conn.insert(
            """INSERT INTO parts (part_name, oem_part_number, alternate_numbers, superseded_numbers,
                                  fitment, condition, bin_location, quantity, unit_price, currency,
                                  category, technical_notes, image_path, verified, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                " ".join(payload.part_name.split()),
                payload.oem_part_number.strip(),
                json.dumps(payload.alternate_numbers),
                json.dumps(payload.superseded_numbers),
                json.dumps([f.model_dump() for f in payload.fitment]),
                payload.condition.value if isinstance(payload.condition, Condition) else str(payload.condition),
                payload.bin_location.strip(),
                int(payload.quantity),
                float(payload.unit_price),
                payload.currency,
                payload.category,
                json.dumps(payload.technical_notes),
                payload.image_path,
                int(payload.verified),
                stamp,
                stamp,
            ),
        )
        part_id = int(cur.lastrowid or 0)
        if log_movement and payload.quantity:
            conn.execute(
                "INSERT INTO movements (part_id, delta, reason, note, created_at) VALUES (?,?,?,?,?)",
                (part_id, int(payload.quantity), "initial_stock", "created with stock", stamp),
            )
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(row)


def update_part(
    part_id: int, patch: PartUpdate, *, path: Path | str | None = None
) -> InventoryPart | None:
    existing = get_part(part_id, path)
    if existing is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    if not data:
        return existing

    qty_delta = 0
    if "quantity" in data and data["quantity"] is not None:
        qty_delta = int(data["quantity"]) - existing.quantity
        if data["quantity"] < 0:
            raise ValueError("quantity cannot be negative")
    if "unit_price" in data and data["unit_price"] is not None and data["unit_price"] < 0:
        raise ValueError("unit_price cannot be negative")

    columns: dict[str, object] = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "fitment":
            columns["fitment"] = json.dumps([VehicleFitment(**f).model_dump() for f in value])
        elif key in {"alternate_numbers", "superseded_numbers", "technical_notes"}:
            columns[key] = json.dumps(value)
        elif key == "condition":
            columns[key] = value.value if isinstance(value, Condition) else str(value)
        elif key == "verified":
            columns["verified"] = int(bool(value))
        elif key == "part_name":
            columns["part_name"] = " ".join(str(value).split())
        else:
            columns[key] = value
    if not columns and not qty_delta:
        return existing

    columns["updated_at"] = _now()
    assignments = ", ".join(f"{col} = ?" for col in columns)
    params = [*columns.values(), part_id]
    with connect(path) as conn:
        conn.execute(f"UPDATE parts SET {assignments} WHERE id = ?", params)
        if qty_delta:
            conn.execute(
                "INSERT INTO movements (part_id, delta, reason, note, created_at) VALUES (?,?,?,?,?)",
                (part_id, qty_delta, "manual_edit", "quantity edited", columns["updated_at"]),
            )
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(row)


def adjust_stock(
    part_id: int,
    delta: int,
    *,
    reason: str = "adjustment",
    note: str = "",
    path: Path | str | None = None,
) -> InventoryPart | None:
    stamp = _now()
    with connect(path) as conn:
        row = conn.execute("SELECT quantity FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            return None
        new_qty = int(row["quantity"]) + int(delta)
        if new_qty < 0:
            raise ValueError("stock cannot go negative")
        conn.execute("UPDATE parts SET quantity = ?, updated_at = ? WHERE id = ?", (new_qty, stamp, part_id))
        conn.execute(
            "INSERT INTO movements (part_id, delta, reason, note, created_at) VALUES (?,?,?,?,?)",
            (part_id, int(delta), reason, note, stamp),
        )
        updated = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    return _row_to_part(updated)


def delete_part(part_id: int, *, path: Path | str | None = None) -> bool:
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
        return cur.rowcount > 0


def movements_for(
    part_id: int, *, limit: int = 50, path: Path | str | None = None
) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM movements WHERE part_id = ? ORDER BY id DESC LIMIT ?",
            (part_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_scan(
    image_path: str,
    report_json: str,
    matched_part_id: int | None,
    *,
    path: Path | str | None = None,
) -> int:
    with connect(path) as conn:
        cur = conn.insert(
            "INSERT INTO scans (image_path, report_json, matched_part_id, created_at) VALUES (?,?,?,?)",
            (image_path, report_json, matched_part_id, _now()),
        )
        return int(cur.lastrowid or 0)


def list_scans(limit: int = 20, *, path: Path | str | None = None) -> list[ScanRecord]:
    from .models import IdentificationReport

    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out: list[ScanRecord] = []
    for row in rows:
        out.append(
            ScanRecord(
                id=row["id"],
                image_path=row["image_path"],
                report=IdentificationReport.model_validate_json(row["report_json"]),
                matched_part_id=row["matched_part_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )
    return out


def stats(*, path: Path | str | None = None) -> dict[str, object]:
    with connect(path) as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS parts, COALESCE(SUM(quantity),0) AS units, "
            "COALESCE(SUM(quantity*unit_price),0) AS value, "
            "COALESCE(SUM(CASE WHEN quantity <= 0 THEN 1 ELSE 0 END),0) AS out_of_stock "
            "FROM parts"
        ).fetchone()
        scans = conn.execute("SELECT COUNT(*) AS c FROM scans").fetchone()
    return {
        "parts": int(totals["parts"]),
        "units": int(totals["units"]),
        "inventory_value": round(float(totals["value"]), 2),
        "out_of_stock": int(totals["out_of_stock"]),
        "scans": int(scans["c"]),
    }

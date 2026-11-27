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
    image_path = row["image_path"] or ""
    # Stock sheets often contain Windows-style relative paths (uploads\photo.jpg).
    # Keep them portable so the UI and media URL mapper can serve the stored file.
    if image_path and "://" not in image_path:
        image_path = image_path.replace("\\", "/")
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
        image_path=image_path,
        verified=bool(row["verified"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_received_at=(
            datetime.fromisoformat(row["last_received_at"])
            if "last_received_at" in row.keys() and row["last_received_at"]
            else None
        ),
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
        sql = """SELECT parts.*,
                    (SELECT MAX(m.created_at) FROM movements m
                     WHERE m.part_id = parts.id AND m.delta > 0) AS last_received_at
                 FROM parts"""
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


def _lock_inventory(conn: storage.Connection) -> None:
    # SQLite must reserve the write lock before reading the balance. PostgreSQL
    # locks the relevant part row below instead.
    if conn.dialect == storage.SQLITE:
        conn.execute("BEGIN IMMEDIATE")


def _part_lock_query(conn: storage.Connection) -> str:
    return "SELECT * FROM parts WHERE id = ?" + (" FOR UPDATE" if conn.dialect == storage.POSTGRES else "")


def update_part(
    part_id: int, patch: PartUpdate, *, path: Path | str | None = None
) -> InventoryPart | None:
    with connect(path) as conn:
        _lock_inventory(conn)
        row = conn.execute(_part_lock_query(conn), (part_id,)).fetchone()
        if row is None:
            return None
        existing = _row_to_part(row)
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
    with connect(path) as conn:
        _lock_inventory(conn)
        stamp = _now()
        row = conn.execute(_part_lock_query(conn), (part_id,)).fetchone()
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


def _fifo_lots(quantity: int, movements: list) -> tuple[list[dict], bool]:
    """Replay the longest consistent history suffix, keeping legacy stock undated.

    Movements are ordered by their database ID. If an old, incomplete history
    cannot explain the current balance, preserve the longest recent suffix that
    can, and treat the rest as an unknown opening balance rather than inventing
    receipt dates. This also keeps future receipts usable for FIFO.
    """
    trailing = 0
    start = len(movements)
    for index in range(len(movements) - 1, -1, -1):
        next_trailing = trailing + int(movements[index]["delta"])
        if quantity - next_trailing < 0:
            break
        trailing = next_trailing
        start = index
    opening = quantity - trailing
    lots = ([{"movement_id": None, "received_at": None, "remaining": opening}] if opening else [])
    for movement in movements[start:]:
        delta = int(movement["delta"])
        if delta > 0:
            lots.append({"movement_id": movement["id"], "received_at": movement["created_at"], "remaining": delta})
        elif delta < 0:
            needed = -delta
            for lot in lots:
                take = min(needed, lot["remaining"])
                lot["remaining"] -= take
                needed -= take
                if not needed:
                    break
    return [lot for lot in lots if lot["remaining"]], bool(opening or start)


def fifo_for(part_id: int, *, path: Path | str | None = None) -> dict | None:
    with connect(path) as conn:
        # Consistent snapshot for SQLite and serialisation with issues on Postgres.
        _lock_inventory(conn)
        part = conn.execute(_part_lock_query(conn), (part_id,)).fetchone()
        if not part:
            return None
        movements = conn.execute("SELECT * FROM movements WHERE part_id = ? ORDER BY id", (part_id,)).fetchall()
        lots, estimated = _fifo_lots(int(part["quantity"]), movements)
        return {"part": _row_to_part(part), "lots": lots, "has_unknown_history": estimated}


def create_delivery_order(order_number: str, collected_by: str, recorded_by: str, note: str,
                          lines: list[dict], *, path: Path | str | None = None) -> dict:
    """Record a completed handover and allocate all lines atomically in FIFO order."""
    number = order_number.strip()
    if not number or not collected_by.strip() or not recorded_by.strip() or not lines:
        raise ValueError("Delivery order, collector, signed-in user and parts are required")
    if len({line["part_id"] for line in lines}) != len(lines) or any(line["quantity"] <= 0 for line in lines):
        raise ValueError("Each part must appear once with a positive quantity")
    with connect(path) as conn:
        _lock_inventory(conn)
        # Serialize by part id in a consistent order, avoiding PostgreSQL deadlocks.
        parts = {}
        for line in sorted(lines, key=lambda item: item["part_id"]):
            part = conn.execute(_part_lock_query(conn), (line["part_id"],)).fetchone()
            if part is None:
                raise ValueError(f"Part #{line['part_id']} no longer exists")
            if line["quantity"] > int(part["quantity"]):
                raise ValueError(f"Not enough stock for {part['part_name']}")
            parts[line["part_id"]] = part
        stamp = _now()
        inserted = conn.execute("""INSERT INTO delivery_orders
            (order_number, order_key, collected_by, recorded_by, note, handed_over_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(order_key) DO NOTHING""",
            (number, number.upper(), collected_by.strip(), recorded_by.strip(), note.strip(), stamp))
        if inserted.rowcount != 1:
            raise ValueError("This delivery order number is already recorded")
        order_id = conn.execute("SELECT id FROM delivery_orders WHERE order_key = ?", (number.upper(),)).fetchone()["id"]
        output_lines = []
        for line in lines:
            part = parts[line["part_id"]]
            movements = conn.execute("SELECT * FROM movements WHERE part_id = ? ORDER BY id", (line["part_id"],)).fetchall()
            lots, _ = _fifo_lots(int(part["quantity"]), movements)
            needed, allocations = line["quantity"], []
            for lot in lots:
                take = min(needed, lot["remaining"])
                if take:
                    allocations.append({"movement_id": lot["movement_id"], "received_at": lot["received_at"], "quantity": take})
                    needed -= take
                if not needed:
                    break
            if needed:
                raise ValueError("Stock history could not be reconciled")
            details = {"part_id": line["part_id"], "part_name": part["part_name"],
                       "oem_part_number": part["oem_part_number"] or "", "bin_location": part["bin_location"] or "",
                       "quantity": line["quantity"], "allocations": allocations}
            conn.execute("""INSERT INTO delivery_order_lines
                (order_id, part_id, part_name, oem_part_number, bin_location, quantity, allocations)
                VALUES (?,?,?,?,?,?,?)""", (order_id, details["part_id"], details["part_name"],
                     details["oem_part_number"], details["bin_location"], details["quantity"], json.dumps(allocations)))
            conn.execute("UPDATE parts SET quantity = quantity - ?, updated_at = ? WHERE id = ?", (line["quantity"], stamp, line["part_id"]))
            conn.execute("INSERT INTO movements (part_id, delta, reason, note, created_at) VALUES (?,?,?,?,?)",
                         (line["part_id"], -line["quantity"], "delivery_handover", f"Delivery order #{order_id}", stamp))
            output_lines.append(details)
        return {"id": order_id, "order_number": number, "collected_by": collected_by.strip(),
                "recorded_by": recorded_by.strip(), "note": note.strip(), "handed_over_at": stamp, "lines": output_lines}


def delivery_orders(*, query: str = "", limit: int = 100, path: Path | str | None = None) -> list[dict]:
    with connect(path) as conn:
        like = conn.like
        needle = f"%{query}%"
        rows = conn.execute(f"""SELECT * FROM delivery_orders d WHERE
            (d.order_number {like} ? OR d.collected_by {like} ? OR d.recorded_by {like} ? OR
             EXISTS (SELECT 1 FROM delivery_order_lines l WHERE l.order_id = d.id AND
                 (l.part_name {like} ? OR l.oem_part_number {like} ?)))
            ORDER BY d.id DESC LIMIT ?""", (needle, needle, needle, needle, needle, limit)).fetchall()
        out = []
        for row in rows:
            lines = conn.execute("SELECT * FROM delivery_order_lines WHERE order_id = ? ORDER BY id", (row["id"],)).fetchall()
            order = dict(row)
            order.pop("order_key", None)
            order["lines"] = [{**dict(line), "allocations": json.loads(line["allocations"])} for line in lines]
            out.append(order)
        return out


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


def clear_inventory(
    *, include_scans: bool = False, path: Path | str | None = None
) -> dict[str, int]:
    """Delete every part (movements cascade). Optionally clear scan history."""
    before = stats(path=path)
    with connect(path) as conn:
        conn.execute("DELETE FROM movements")
        conn.execute("DELETE FROM parts")
        if include_scans:
            conn.execute("DELETE FROM scans")
    return {
        "parts_removed": int(before["parts"]),
        "units_removed": int(before["units"]),
        "scans_removed": int(before["scans"]) if include_scans else 0,
    }


def stats(*, path: Path | str | None = None) -> dict[str, object]:
    with connect(path) as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS parts, COALESCE(SUM(quantity),0) AS units, "
            "COALESCE(SUM(quantity*unit_price),0) AS value, "
            "COALESCE(SUM(CASE WHEN quantity <= 0 THEN 1 ELSE 0 END),0) AS out_of_stock, "
            "COALESCE(SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END),0) AS verified_parts, "
            "COALESCE(SUM(CASE WHEN verified = 0 THEN 1 ELSE 0 END),0) AS unverified_parts "
            "FROM parts"
        ).fetchone()
        scans = conn.execute("SELECT COUNT(*) AS c FROM scans").fetchone()
    return {
        "parts": int(totals["parts"]),
        "units": int(totals["units"]),
        "inventory_value": round(float(totals["value"]), 2),
        "out_of_stock": int(totals["out_of_stock"]),
        "verified_parts": int(totals["verified_parts"]),
        "unverified_parts": int(totals["unverified_parts"]),
        "scans": int(scans["c"]),
    }

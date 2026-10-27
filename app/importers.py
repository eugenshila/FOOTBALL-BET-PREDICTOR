"""CSV import for an existing stock list.

Shops already have their inventory in a spreadsheet, so the importer is built to
survive real-world mess: any column order, unknown headers, currency symbols,
thousands separators, ``1.200,50`` style decimals, BOMs, semicolon or tab
delimiters, and free-text fitment like "Jeep Grand Cherokee 3.6L 2011-2021".

Everything is dry-runnable first: you get a per-row report (created / updated /
skipped / error) before anything is written.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .models import Condition, PartCreate, VehicleFitment

# Header aliases -> canonical field. Lower-cased, punctuation stripped.
ALIASES: dict[str, str] = {
    "partname": "part_name",
    "name": "part_name",
    "item": "part_name",
    "itemname": "part_name",
    "description": "part_name",
    "component": "part_name",
    "part": "part_name",
    "oem": "oem_part_number",
    "oempartnumber": "oem_part_number",
    "oemno": "oem_part_number",
    "oemnr": "oem_part_number",
    "partnumber": "oem_part_number",
    "partno": "oem_part_number",
    "partnum": "oem_part_number",
    "mpn": "oem_part_number",
    "sku": "oem_part_number",
    "alternate": "alternate_numbers",
    "alternates": "alternate_numbers",
    "alternatenumbers": "alternate_numbers",
    "alternatepartnumbers": "alternate_numbers",
    "crossref": "alternate_numbers",
    "crossrefs": "alternate_numbers",
    "crossreference": "alternate_numbers",
    "crossreferences": "alternate_numbers",
    "othernumbers": "alternate_numbers",
    "superseded": "superseded_numbers",
    "supersedednumbers": "superseded_numbers",
    "replaced": "superseded_numbers",
    "replaces": "superseded_numbers",
    "fitment": "fitment",
    "vehicles": "fitment",
    "vehicle": "fitment",
    "application": "fitment",
    "applications": "fitment",
    "fits": "fitment",
    "compatibility": "fitment",
    "condition": "condition",
    "state": "condition",
    "bin": "bin_location",
    "binlocation": "bin_location",
    "location": "bin_location",
    "shelf": "bin_location",
    "storage": "bin_location",
    "rack": "bin_location",
    "qty": "quantity",
    "quantity": "quantity",
    "stock": "quantity",
    "onhand": "quantity",
    "count": "quantity",
    "quantityinstock": "quantity",
    "price": "unit_price",
    "unitprice": "unit_price",
    "sellingprice": "unit_price",
    "retail": "unit_price",
    "retailprice": "unit_price",
    "cost": "unit_price",
    "amount": "unit_price",
    "currency": "currency",
    "ccy": "currency",
    "category": "category",
    "type": "category",
    "group": "category",
    "notes": "technical_notes",
    "technicalnotes": "technical_notes",
    "servicenotes": "technical_notes",
    "comments": "technical_notes",
    "image": "image_path",
    "imagepath": "image_path",
    "photo": "image_path",
    "picture": "image_path",
    "verified": "verified",
}

REQUIRED = "part_name"

_YEAR_RANGE = re.compile(r"\b(19\d{2}|20\d{2})\s*(?:-|–|—|to)\s*((?:19|20)\d{2}|present|now)\b", re.I)
_ENGINE = re.compile(
    r"\b(\d\.\d\s?L(?:\s?(?:V\d{1,2}|I\d|\d)?)?|\d\.\d\s?(?:litre|liter|turbo|TDI|TSI|CRD|HDi|VVT)?)",
    re.I,
)
_MAKES = {
    "jeep", "dodge", "chrysler", "ram", "ford", "toyota", "nissan", "mazda", "subaru",
    "honda", "hyundai", "kia", "mitsubishi", "suzuki", "isuzu", "volkswagen", "vw",
    "audi", "bmw", "mercedes", "mercedes-benz", "peugeot", "renault", "citroen",
    "land", "rover", "jaguar", "volvo", "chery", "geely", "great", "wall", "ssangyong",
    "chevrolet", "gmc", "cadillac", "buick", "opel", "seat", "skoda", "fiat", "iveco",
    "man", "scania", "hino", "fuso", "tata", "mahindra", "proton", "perodua",
}


class ImportError_(ValueError):
    """Row-level problem that should be reported, not raised."""


@dataclass
class RowResult:
    row: int
    status: str  # created | updated | skipped | error | would_create | would_update
    part_name: str = ""
    oem_part_number: str = ""
    part_id: int | None = None
    message: str = ""


@dataclass
class ImportReport:
    rows_total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = False
    columns_mapped: dict[str, str] = field(default_factory=dict)
    ignored_columns: list[str] = field(default_factory=list)
    results: list[RowResult] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "rows_total": self.rows_total,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "dry_run": self.dry_run,
            "columns_mapped": self.columns_mapped,
            "ignored_columns": self.ignored_columns,
            "problems": [
                {"row": r.row, "message": r.message}
                for r in self.results
                if r.status == "error"
            ][:50],
        }


def _normalise_header(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower().replace("\ufeff", ""))


def map_headers(fieldnames: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return ({csv column -> canonical field}, ignored columns)."""
    mapping: dict[str, str] = {}
    ignored: list[str] = []
    for column in fieldnames:
        canonical = ALIASES.get(_normalise_header(column))
        if canonical and canonical not in mapping.values():
            mapping[column] = canonical
        else:
            ignored.append(column)
    return mapping, ignored


def parse_price(raw: str) -> float:
    """'KES 12,800.50' / '1.200,50' / '$45' -> float. Returns 0.0 if unparseable."""
    text = re.sub(r"[^\d.,\-]", "", str(raw or "")).strip()
    if not text:
        return 0.0
    if text.count(",") and text.count("."):
        if text.rfind(",") > text.rfind("."):  # 1.200,50 (European)
            text = text.replace(".", "").replace(",", ".")
        else:  # 1,200.50 (Anglo)
            text = text.replace(",", "")
    elif text.count(",") and not text.count("."):
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) in {1, 2} else text.replace(",", "")
    try:
        return round(float(text), 2)
    except ValueError:
        return 0.0


def parse_quantity(raw: str) -> int:
    text = re.sub(r"[^\d\-]", "", str(raw or ""))
    if not text or text == "-":
        return 0
    try:
        return max(int(text), 0)
    except ValueError:
        return 0


def parse_condition(raw: str) -> Condition:
    text = (raw or "").strip().lower()
    if text.startswith(("used", "second", "2nd", "s/h")):
        return Condition.used
    if text.startswith(("refurb", "recon", "rebuilt")):
        return Condition.refurbished
    return Condition.new


def parse_fitment(raw: str) -> list[VehicleFitment]:
    """Parse free-text fitment into structured entries.

    Accepts one or more vehicles separated by ';' '|' or newlines, e.g.
    "Jeep Grand Cherokee 3.6L V6 2011-2021; Dodge Durango 5.7L 2011-2020".
    Unknown layouts degrade to a make/model guess rather than failing the row.
    """
    text = str(raw or "").strip()
    if not text:
        return []

    chunks = [c.strip() for c in re.split(r"[;|\n]+", text) if c.strip()]
    out: list[VehicleFitment] = []
    for chunk in chunks[:8]:
        years = ""
        match = _YEAR_RANGE.search(chunk)
        if match:
            years = f"{match.group(1)}-{match.group(2)}"
            chunk_for_model = (chunk[: match.start()] + " " + chunk[match.end() :]).strip()
        else:
            single = re.search(r"\b((?:19|20)\d{2})\b", chunk)
            if single:
                years = single.group(1)
                chunk_for_model = (chunk[: single.start()] + " " + chunk[single.end() :]).strip()
            else:
                chunk_for_model = chunk

        engine = ""
        engine_match = _ENGINE.search(chunk_for_model)
        if engine_match:
            engine = re.sub(r"\s+", " ", engine_match.group(1)).strip()
            chunk_for_model = (
                chunk_for_model[: engine_match.start()] + " " + chunk_for_model[engine_match.end() :]
            ).strip()

        # fuel/drivetrain/gearbox words belong to neither make nor model
        cleaned_model = re.sub(
            r"\b(diesel|petrol|gasoline|turbo|intercooled|4x4|4wd|2wd|awd|auto|automatic|manual|titanium|xlt|xle|glx)\b",
            " ",
            chunk_for_model,
            flags=re.I,
        )
        words = [w for w in re.split(r"[\s,]+", cleaned_model) if w and w.strip("-/")]
        make, model = "", ""
        if words:
            first = words[0].lower().strip(".")
            if first in _MAKES:
                make = words[0]
                # "Land Rover" / "Mercedes-Benz" style two-word makes
                if first in {"land", "mercedes", "great", "alfa"} and len(words) > 1:
                    make = f"{words[0]} {words[1]}"
                    words = words[2:]
                else:
                    words = words[1:]
            model = " ".join(words)
        if not (make or model or engine or years):
            continue
        out.append(VehicleFitment(make=make, model=model, engine=engine, years=years))
    return out


def _split_numbers(raw: str) -> list[str]:
    parts = re.split(r"[,;/|\s]+", str(raw or ""))
    seen: list[str] = []
    for part in parts:
        cleaned = part.strip().strip('"').upper()
        if cleaned and cleaned not in {"-", "N/A", "NA", "NONE"} and cleaned not in seen:
            seen.append(cleaned)
    return seen[:12]


def _split_notes(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    pieces = re.split(r"(?:\r?\n|\s*\|\s*|\s*;\s*)", text)
    return [p.strip() for p in pieces if p.strip()][:12]


def _truthy(raw: str) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "verified", "ok"}


def _row_to_part(row: dict[str, str], mapping: dict[str, str], *, default_currency: str) -> PartCreate:
    values = {canonical: row.get(column, "") for column, canonical in mapping.items()}
    name = str(values.get("part_name", "")).strip()
    if not name:
        raise ImportError_("missing part name")
    return PartCreate(
        part_name=name[:160],
        oem_part_number=_split_numbers(values.get("oem_part_number", ""))[0]
        if _split_numbers(values.get("oem_part_number", ""))
        else "",
        alternate_numbers=_split_numbers(values.get("alternate_numbers", "")),
        superseded_numbers=_split_numbers(values.get("superseded_numbers", "")),
        fitment=parse_fitment(values.get("fitment", "")),
        condition=parse_condition(values.get("condition", "")),
        bin_location=str(values.get("bin_location", "")).strip()[:40],
        quantity=parse_quantity(values.get("quantity", "")),
        unit_price=parse_price(values.get("unit_price", "")),
        currency=str(values.get("currency", "")).strip().upper()[:8] or default_currency,
        category=str(values.get("category", "")).strip()[:80],
        technical_notes=_split_notes(values.get("technical_notes", "")),
        image_path=str(values.get("image_path", "")).strip()[:300],
        verified=_truthy(values.get("verified", "")),
    )


def sniff_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_csv(
    raw: bytes | str,
    *,
    dry_run: bool = False,
    update_existing: bool = False,
    default_currency: str = "KES",
    path: Path | None = None,
) -> ImportReport:
    """Import a CSV stock list.

    ``update_existing`` fills in blanks on records that already match an OEM
    number; it never overwrites prices or quantities of existing stock unless
    that column is present in the file.
    """
    text = sniff_text(raw) if isinstance(raw, bytes) else raw
    if not text.strip():
        report = ImportReport(dry_run=dry_run)
        report.results.append(RowResult(row=0, status="error", message="file is empty"))
        report.errors = 1
        return report

    sample = text[:8000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        report = ImportReport(dry_run=dry_run)
        report.results.append(RowResult(row=0, status="error", message="no header row found"))
        report.errors = 1
        return report

    mapping, ignored = map_headers(list(reader.fieldnames))
    report = ImportReport(dry_run=dry_run, columns_mapped=mapping, ignored_columns=ignored)

    if REQUIRED not in mapping.values():
        report.results.append(
            RowResult(
                row=1,
                status="error",
                message=(
                    "could not find a part-name column; rename one header to "
                    "'Part Name' (or 'Description') - found: "
                    + ", ".join(reader.fieldnames[:8])
                ),
            )
        )
        report.errors = 1
        return report

    for index, row in enumerate(reader, start=2):  # row 1 is the header
        report.rows_total += 1
        try:
            payload = _row_to_part(row, mapping, default_currency=default_currency)
        except ImportError_ as exc:
            report.errors += 1
            report.results.append(RowResult(row=index, status="error", message=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - a bad row must not kill the import
            report.errors += 1
            report.results.append(
                RowResult(row=index, status="error", message=f"{type(exc).__name__}: {exc}")
            )
            continue

        # Match on the OEM number when we have one; otherwise fall back to the
        # name so rows without a part number do not duplicate on every re-import.
        if payload.oem_part_number:
            existing = db.find_by_number(payload.oem_part_number, path)
        else:
            existing = db.find_by_name(payload.part_name, path)

        if existing is not None:
            if not update_existing:
                report.skipped += 1
                report.results.append(
                    RowResult(
                        row=index,
                        status="skipped",
                        part_name=payload.part_name,
                        oem_part_number=payload.oem_part_number,
                        part_id=existing.id,
                        message=f"OEM {payload.oem_part_number} already in stock (#{existing.id})",
                    )
                )
                continue
            if dry_run:
                report.updated += 1
                report.results.append(
                    RowResult(
                        row=index,
                        status="would_update",
                        part_name=payload.part_name,
                        oem_part_number=payload.oem_part_number,
                        part_id=existing.id,
                    )
                )
                continue
            _apply_update(existing.id, payload, path)
            report.updated += 1
            report.results.append(
                RowResult(
                    row=index,
                    status="updated",
                    part_name=payload.part_name,
                    oem_part_number=payload.oem_part_number,
                    part_id=existing.id,
                )
            )
            continue

        if dry_run:
            report.created += 1
            report.results.append(
                RowResult(
                    row=index,
                    status="would_create",
                    part_name=payload.part_name,
                    oem_part_number=payload.oem_part_number,
                )
            )
            continue

        created = db.create_part(payload, path=path)
        report.created += 1
        report.results.append(
            RowResult(
                row=index,
                status="created",
                part_name=payload.part_name,
                oem_part_number=payload.oem_part_number,
                part_id=created.id,
            )
        )

    return report


def _apply_update(part_id: int, payload: PartCreate, path: Path | None) -> None:
    """Fill in missing details on an existing record without clobbering stock."""
    from .models import PartUpdate

    current = db.get_part(part_id, path)
    if current is None:
        return
    patch = PartUpdate(
        alternate_numbers=payload.alternate_numbers or None,
        superseded_numbers=payload.superseded_numbers or None,
        fitment=payload.fitment or None,
        bin_location=payload.bin_location or None,
        category=payload.category or None,
        technical_notes=payload.technical_notes or None,
        image_path=payload.image_path or None,
        unit_price=payload.unit_price or None,
        currency=payload.currency or None,
        verified=True if payload.verified else None,
    )
    db.update_part(part_id, patch, path=path)


def export_csv(path: Path | None = None) -> str:
    """Round-trip friendly export of the current inventory."""
    parts = db.list_parts(limit=100000, path=path)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Part Name", "OEM Part Number", "Alternate Numbers", "Superseded Numbers",
            "Fitment", "Condition", "Bin Location", "Quantity", "Unit Price",
            "Currency", "Category", "Technical Notes", "Image Path", "Verified",
        ]
    )
    for part in parts:
        writer.writerow(
            [
                part.part_name,
                part.oem_part_number,
                "; ".join(part.alternate_numbers),
                "; ".join(part.superseded_numbers),
                "; ".join(f.label() for f in part.fitment),
                part.condition.value,
                part.bin_location,
                part.quantity,
                f"{part.unit_price:.2f}",
                part.currency,
                part.category,
                " | ".join(part.technical_notes),
                part.image_path,
                "yes" if part.verified else "no",
            ]
        )
    return buffer.getvalue()


__all__ = [
    "import_csv",
    "export_csv",
    "parse_fitment",
    "parse_price",
    "parse_quantity",
    "map_headers",
    "ImportReport",
    "RowResult",
]

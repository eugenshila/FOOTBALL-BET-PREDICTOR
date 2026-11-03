"""Read spreadsheet and PDF stock lists into the CSV format used by the importer."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

from . import importers


@dataclass
class StockFile:
    csv_bytes: bytes
    # CSV row number (header is row 1) -> (original file name, image bytes)
    photos: dict[int, tuple[str, bytes]]
    source: str


FIELDS = [
    "Part Name", "OEM Part Number", "Alternate Numbers", "Superseded Numbers",
    "Fitment", "Condition", "Bin Location", "Quantity", "Price", "Currency",
    "Category", "Notes", "Photo", "Verified",
]


def _header_map(values: list[Any]) -> dict[str, str]:
    headers = [str(value or "").strip() for value in values]
    mapped, _ = importers.map_headers(headers)
    return {canonical: source for source, canonical in mapped.items()}


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def parse_xlsx(raw: bytes) -> StockFile:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency error message
        raise ValueError("Excel import needs openpyxl. Install the app requirements first.") from exc

    try:
        workbook = load_workbook(io.BytesIO(raw), data_only=True, keep_links=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not read Excel workbook: {exc}") from exc

    records: list[dict[str, str]] = []
    photos: dict[int, tuple[str, bytes]] = {}
    try:
        for sheet in workbook.worksheets:
            header_row = 0
            headers: list[str] = []
            canonical_sources: dict[str, str] = {}
            for row_index, row in enumerate(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 30), values_only=True), start=1):
                headers = [_value_text(value) for value in row]
                canonical_sources = _header_map(headers)
                if "part_name" in canonical_sources:
                    header_row = row_index
                    break
            if not header_row:
                continue

            header_indices = {name: index for index, name in enumerate(headers) if name}
            source_to_index = {
                canonical: header_indices[source]
                for canonical, source in canonical_sources.items()
                if source in header_indices
            }
            photo_col = source_to_index.get("image_path")
            if photo_col is None:
                # Pictures are deliberately only associated when the sheet has
                # a named Photo/Image/Picture column.
                image_anchors = [
                    image for image in getattr(sheet, "_images", [])
                    if getattr(getattr(image, "anchor", None), "_from", None) is not None
                ]
            else:
                image_anchors = [
                    image for image in getattr(sheet, "_images", [])
                    if getattr(getattr(image, "anchor", None), "_from", None) is not None
                    and image.anchor._from.col == photo_col
                ]
            if image_anchors and photo_col is None:
                raise ValueError(f"Sheet '{sheet.title}' has embedded photos but no Photo column header.")

            by_excel_row: dict[int, list[Any]] = {}
            for image in image_anchors:
                by_excel_row.setdefault(image.anchor._from.row + 1, []).append(image)

            photo_header = canonical_sources.get("image_path")
            for excel_row, row in enumerate(sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
                mapped: dict[str, str] = {}
                for canonical, source in canonical_sources.items():
                    index = header_indices.get(source)
                    if index is not None and index < len(row):
                        mapped[source] = _value_text(row[index])
                if not mapped.get(canonical_sources["part_name"], "").strip():
                    continue

                embedded = by_excel_row.get(excel_row, [])
                if len(embedded) > 1:
                    raise ValueError(f"Sheet '{sheet.title}', row {excel_row} has multiple photos; use one photo per part row.")
                if embedded:
                    image = embedded[0]
                    image_name = f"{sheet.title}-row-{excel_row}.{(image.format or 'jpg').lower()}"
                    photo_bytes = image._data()
                    record_number = len(records) + 2
                    photos[record_number] = (image_name, photo_bytes)
                    mapped[photo_header] = "__EMBEDDED_PHOTO__"

                record = {field: "" for field in FIELDS}
                for source, canonical in importers.map_headers(list(mapped.keys()))[0].items():
                    target = {
                        "part_name": "Part Name", "oem_part_number": "OEM Part Number",
                        "alternate_numbers": "Alternate Numbers", "superseded_numbers": "Superseded Numbers",
                        "fitment": "Fitment", "condition": "Condition", "bin_location": "Bin Location",
                        "quantity": "Quantity", "unit_price": "Price", "currency": "Currency",
                        "category": "Category", "technical_notes": "Notes", "image_path": "Photo",
                        "verified": "Verified",
                    }[canonical]
                    record[target] = mapped[source]
                records.append(record)
    finally:
        workbook.close()

    if not records:
        raise ValueError("No worksheet had a Part Name or Description column with data rows.")
    return StockFile(_records_csv(records), photos, "xlsx")


def parse_pdf(raw: bytes) -> StockFile:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency error message
        raise ValueError("PDF import needs pdfplumber. Install the app requirements first.") from exc

    records: list[dict[str, str]] = []
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    records.extend(_pdf_table_records(table))
                if not records:
                    text = page.extract_text(layout=True) or ""
                    lines = [re.split(r"\s{2,}", line.strip()) for line in text.splitlines() if line.strip()]
                    records.extend(_pdf_table_records(lines))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not read PDF: {exc}") from exc

    if not records:
        raise ValueError(
            "No readable stock table was found in this PDF. It must contain selectable text and a header row; "
            "scanned PDFs need OCR first. For embedded photos, use Excel (.xlsx)."
        )
    return StockFile(_records_csv(records), {}, "pdf")


def _pdf_table_records(table: list[list[Any]]) -> list[dict[str, str]]:
    if not table:
        return []
    header_index = None
    canonical_sources: dict[str, str] = {}
    headers: list[str] = []
    for index, row in enumerate(table[:20]):
        candidate = [_value_text(cell) for cell in row]
        mapped = _header_map(candidate)
        if "part_name" in mapped:
            header_index, canonical_sources, headers = index, mapped, candidate
            break
    if header_index is None:
        return []

    result: list[dict[str, str]] = []
    for row in table[header_index + 1 :]:
        values = [_value_text(cell) for cell in row]
        if _header_map(values).get("part_name"):
            continue  # repeated header on a later printed page
        record = {field: "" for field in FIELDS}
        mapping, _ = importers.map_headers(headers)
        targets = {
            "part_name": "Part Name", "oem_part_number": "OEM Part Number",
            "alternate_numbers": "Alternate Numbers", "superseded_numbers": "Superseded Numbers",
            "fitment": "Fitment", "condition": "Condition", "bin_location": "Bin Location",
            "quantity": "Quantity", "unit_price": "Price", "currency": "Currency",
            "category": "Category", "technical_notes": "Notes", "image_path": "Photo",
            "verified": "Verified",
        }
        for source, canonical in mapping.items():
            position = headers.index(source)
            if position < len(values):
                record[targets[canonical]] = values[position]
        if record["Part Name"].strip():
            result.append(record)
    return result


def _records_csv(records: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode("utf-8-sig")

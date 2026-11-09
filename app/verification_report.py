"""Excel exports for inventory verification status."""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import InventoryPart


HEADERS = (
    "Record ID",
    "Part Name",
    "OEM Part Number",
    "Alternate Numbers",
    "Superseded Numbers",
    "Fitment",
    "Condition",
    "Bin Location",
    "Quantity",
    "Unit Price",
    "Currency",
    "Category",
    "Technical Notes",
    "Image Path",
)

WIDTHS = (12, 34, 22, 32, 32, 42, 16, 20, 12, 15, 12, 22, 48, 40)


def _row(part: InventoryPart) -> tuple[object, ...]:
    return (
        part.id,
        part.part_name,
        part.oem_part_number,
        "; ".join(part.alternate_numbers),
        "; ".join(part.superseded_numbers),
        "; ".join(f.label() for f in part.fitment if f.label()),
        part.condition.value,
        part.bin_location,
        part.quantity,
        part.unit_price,
        part.currency,
        part.category,
        " | ".join(part.technical_notes),
        part.image_path,
    )


def build_verification_workbook(parts: list[InventoryPart]) -> bytes:
    """Build a two-sheet, filterable workbook from the current inventory."""
    workbook = Workbook()
    workbook.remove(workbook.active)

    header_fill = PatternFill("solid", fgColor="25282D")
    header_font = Font(name="Aptos", bold=True, color="FFFFFF")
    bottom_border = Border(bottom=Side(style="medium", color="C9363E"))
    body_font = Font(name="Aptos", size=10, color="24272B")

    for title, verified, tab_color in (
        ("Verified", True, "3B8A62"),
        ("Not Verified", False, "C58B3B"),
    ):
        sheet = workbook.create_sheet(title)
        sheet.sheet_properties.tabColor = tab_color
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A2"
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.page_setup.orientation = "landscape"
        sheet.append(HEADERS)

        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = bottom_border
        sheet.row_dimensions[1].height = 30

        selected = [part for part in parts if bool(part.verified) is verified]
        for part in selected:
            values = _row(part)
            sheet.append(values)
            row_index = sheet.max_row
            for column_index, value in enumerate(values, start=1):
                cell = sheet.cell(row=row_index, column=column_index)
                if isinstance(value, str):
                    # Keep identifiers and user-entered text literal in Excel.
                    cell.data_type = "s"
                    cell.number_format = "@"
                cell.font = body_font
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            sheet.cell(row=row_index, column=9).number_format = "#,##0"
            sheet.cell(row=row_index, column=10).number_format = "#,##0.00"
        for index, width in enumerate(WIDTHS, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(1, sheet.max_row)}"

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()

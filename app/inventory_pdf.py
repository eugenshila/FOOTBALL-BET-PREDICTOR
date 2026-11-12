"""Create a printable inventory catalogue with the part photos stored by VERIFIX."""

from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .config import BASE_DIR, UPLOAD_DIR
from .models import InventoryPart

_NAVY = colors.HexColor("#15243a")
_RED = colors.HexColor("#d82b35")
_MUTED = colors.HexColor("#64748b")
_EDGE = colors.HexColor("#d8dee8")
_PALE = colors.HexColor("#f3f6fa")


def _photo_path(raw_path: str) -> Path | None:
    value = (raw_path or "").strip().replace("\\", "/")
    if not value or "://" in value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        if value.startswith("uploads/"):
            candidate = UPLOAD_DIR / value.removeprefix("uploads/")
        else:
            candidate = BASE_DIR / candidate
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(UPLOAD_DIR.resolve()) or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


class _ContainedPhoto(Flowable):
    def __init__(self, path: Path, max_width: float, max_height: float):
        super().__init__()
        with PILImage.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((900, 720), PILImage.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=82, optimize=True)
        buffer.seek(0)
        self.reader = ImageReader(buffer)
        image_width, image_height = self.reader.getSize()
        scale = min(max_width / image_width, max_height / image_height)
        self.width = image_width * scale
        self.height = image_height * scale
        self.hAlign = "CENTER"

    def draw(self) -> None:
        self.canv.drawImage(
            self.reader,
            0,
            0,
            width=self.width,
            height=self.height,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )


def _photo_flowable(part: InventoryPart, placeholder: Paragraph) -> Flowable | Table:
    path = _photo_path(part.image_path)
    if path is not None:
        try:
            return _ContainedPhoto(path, 38 * mm, 32 * mm)
        except Exception:  # a damaged or unsupported image should not break the export
            pass
    return Table(
        [[placeholder]],
        colWidths=[40 * mm],
        rowHeights=[34 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PALE),
                ("BOX", (0, 0), (-1, -1), 0.6, _EDGE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        ),
    )


def _fitment_text(part: InventoryPart) -> str:
    labels: list[str] = []
    for fit in part.fitment:
        label = " ".join(
            str(value).strip()
            for value in (fit.make, fit.model, fit.engine, fit.years)
            if value and str(value).strip()
        )
        if label:
            labels.append(label)
    return "; ".join(labels) or "-"


def build_inventory_pdf(parts: Iterable[InventoryPart]) -> bytes:
    records = list(parts)
    total_units = sum(part.quantity for part in records)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=26 * mm,
        bottomMargin=18 * mm,
        title="VERIFIX Parts Inventory",
        author="VERIFIX",
    )

    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InventoryTitle", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=18, leading=21, textColor=_NAVY, alignment=TA_LEFT,
        spaceAfter=2 * mm,
    )
    summary_style = ParagraphStyle(
        "InventorySummary", parent=base["Normal"], fontName="Helvetica",
        fontSize=9, leading=12, textColor=_MUTED, spaceAfter=5 * mm,
    )
    part_style = ParagraphStyle(
        "PartTitle", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=11, leading=14, textColor=_NAVY, spaceAfter=1.5 * mm,
    )
    detail_style = ParagraphStyle(
        "PartDetails", parent=base["Normal"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=_NAVY, spaceAfter=1 * mm,
    )
    small_style = ParagraphStyle(
        "PartSmall", parent=detail_style, fontSize=7.5, leading=9,
        textColor=_MUTED,
    )
    placeholder_style = ParagraphStyle(
        "PhotoPlaceholder", parent=small_style, alignment=TA_CENTER,
        fontName="Helvetica-Bold", textColor=_MUTED,
    )

    story: list[Flowable] = [
        Paragraph("VERIFIX - Parts Inventory", title_style),
        Paragraph(
            f"{len(records)} parts &nbsp;|&nbsp; {total_units} units &nbsp;|&nbsp; "
            f"Generated {datetime.now(ZoneInfo('Africa/Nairobi')).strftime('%d %b %Y, %H:%M')} EAT",
            summary_style,
        ),
    ]

    placeholder = Paragraph("NO PHOTO<br/>ATTACHED", placeholder_style)
    for index, part in enumerate(records):
        details: list[Flowable] = [Paragraph(escape(part.part_name), part_style)]
        details.append(Paragraph(f"<b>OEM:</b> {escape(part.oem_part_number or '-')}", detail_style))
        if part.alternate_numbers:
            details.append(Paragraph(f"<b>Alternate:</b> {escape(', '.join(part.alternate_numbers))}", detail_style))
        if part.superseded_numbers:
            details.append(Paragraph(f"<b>Superseded:</b> {escape(', '.join(part.superseded_numbers))}", detail_style))
        details.append(Paragraph(f"<b>Category:</b> {escape(part.category or '-')} &nbsp; <b>Condition:</b> {escape(str(part.condition.value if hasattr(part.condition, 'value') else part.condition))}", detail_style))
        details.append(Paragraph(f"<b>Bin:</b> {escape(part.bin_location or '-')} &nbsp; <b>Qty:</b> {part.quantity} &nbsp; <b>Price:</b> {escape(part.currency)} {part.unit_price:,.2f}", detail_style))
        details.append(Paragraph(f"<b>Fitment:</b> {escape(_fitment_text(part))}", detail_style))
        received = part.last_received_at.astimezone(ZoneInfo('Africa/Nairobi')).strftime('%d %b %Y, %H:%M EAT') if part.last_received_at else '-'
        details.append(Paragraph(f"<b>Date received:</b> {escape(received)} &nbsp; <b>OEM verified:</b> {'Yes' if part.verified else 'No'}", small_style))
        if part.technical_notes:
            notes = "; ".join(str(note).strip() for note in part.technical_notes if str(note).strip())
            if notes:
                details.append(Paragraph(f"<b>Notes:</b> {escape(notes)}", small_style))

        card = Table(
            [[_photo_flowable(part, placeholder), details]],
            colWidths=[43 * mm, doc.width - 43 * mm],
            hAlign="LEFT",
        )
        card.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.7, _EDGE),
                    ("LINEBEFORE", (1, 0), (1, 0), 1.4, _RED),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (0, 0), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (0, 0), 2 * mm),
                    ("LEFTPADDING", (1, 0), (1, 0), 4 * mm),
                    ("RIGHTPADDING", (1, 0), (1, 0), 3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        story.extend([card, Spacer(1, 3 * mm)])

    def draw_page(canvas, document) -> None:
        canvas.saveState()
        page_width, page_height = A4
        canvas.setFillColor(_NAVY)
        canvas.rect(0, page_height - 12 * mm, page_width, 12 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(14 * mm, page_height - 8 * mm, "VERIFIX  |  PARTS CATALOGUE")
        canvas.setStrokeColor(_EDGE)
        canvas.line(14 * mm, 12 * mm, page_width - 14 * mm, 12 * mm)
        canvas.setFillColor(_MUTED)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(14 * mm, 7 * mm, "Inventory with attached part photos")
        canvas.drawRightString(page_width - 14 * mm, 7 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()

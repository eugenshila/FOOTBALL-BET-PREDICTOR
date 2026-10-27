"""Renders the operator-facing report in the exact three-section format the shop
floor expects: identification, warehouse details, then the promo post."""

from __future__ import annotations

from .config import settings
from .formatting import money
from .models import IdentificationReport, InventoryPart

CONFIDENCE_BADGE = {
    "high": "HIGH",
    "medium": "MEDIUM — verify",
    "low": "LOW — manual review required",
}


def _or_blank(value: str, placeholder: str = "— not determined —") -> str:
    return value.strip() if value and value.strip() else placeholder


def _numbers_block(report: IdentificationReport) -> str:
    lines: list[str] = []
    if report.oem_part_number:
        lines.append(f"- **OEM Part Number:** {report.oem_part_number}")
    else:
        lines.append("- **OEM Part Number:** — not legible / not determined —")
    if report.alternate_numbers:
        lines.append(f"- **Alternate Numbers:** {', '.join(report.alternate_numbers)}")
    else:
        lines.append("- **Alternate Numbers:** — none on file —")
    if report.superseded_numbers:
        lines.append(f"- **Superseded Numbers:** {', '.join(report.superseded_numbers)}")
    else:
        lines.append("- **Superseded Numbers:** — none on file —")
    return "\n".join(lines)


def _fitment_block(report: IdentificationReport, part: InventoryPart | None) -> str:
    source = part.fitment if part and part.fitment else report.fitment
    if not source:
        return "- **Vehicle Compatibility:** — no fitment on file — confirm with EPC —"
    lines = [f"- **Vehicle Compatibility:** {source[0].label() or 'see below'}"]
    for extra in source[1:4]:
        lines.append(f"  - Also fits: {extra.label()}")
    return "\n".join(lines)


def render_report(
    report: IdentificationReport,
    part: InventoryPart | None = None,
    *,
    caption: str = "",
) -> str:
    """Markdown matching the required output contract."""
    overall = report.overall_confidence.value
    number_conf = report.number_confidence.value

    notes = report.technical_notes or ["No service notes returned — confirm torque specs in the workshop manual."]
    notes_block = "\n".join(f"- {note}" for note in notes)

    evidence = ""
    if report.visual_evidence:
        trimmed = [item.strip().rstrip(".") for item in report.visual_evidence[:5] if item.strip()]
        evidence = "\n\n_Visual evidence:_ " + "; ".join(trimmed) + "."

    out: list[str] = []
    out.append("### PART IDENTIFICATION REPORT")
    out.append(f"- **Part Name:** {_or_blank(report.part_name, '— unidentified —')}")
    if report.category:
        out.append(f"- **Category:** {report.category}")
    out.append(_numbers_block(report))
    out.append(_fitment_block(report, part))
    out.append(f"- **Identification Confidence:** {CONFIDENCE_BADGE.get(overall, overall.upper())}")
    out.append(f"- **Part-Number Confidence:** {CONFIDENCE_BADGE.get(number_conf, number_conf.upper())}")
    out.append("- **Key Technical Notes:**")
    out.append(notes_block)
    out.append(
        f"- **Engine / Provider:** `{report.provider}`"
        + (f" · `{report.model_name}`" if report.model_name else "")
    )
    if evidence:
        out.append(evidence.strip())

    if report.warnings:
        out.append("\n**Checks required before quoting:**")
        out.extend(f"- ⚠️ {w}" for w in report.warnings)

    out.append("\n---")
    out.append("### WAREHOUSE & INVENTORY DETAILS")
    if part is None:
        out.append("- **Status:** ⚠️ not matched to a catalogue record — add it to inventory before posting.")
    else:
        out.append(f"- **Part Name:** {part.part_name}")
        out.append(f"- **Catalog ID:** #{part.id}")
        out.append(f"- **OEM Part Number:** {_or_blank(part.oem_part_number)}")
        out.append(f"- **Bin Location:** {_or_blank(part.bin_location, '— unassigned —')}")
        out.append(f"- **Condition:** {part.condition.value}")
        out.append(f"- **In-Stock Quantity:** {part.quantity} ({'available' if part.in_stock() else 'out of stock'})")
        out.append(f"- **Price:** {money(part.unit_price, part.currency)}")
        if not part.verified:
            out.append("- **Verification:** ⚠️ record not verified by staff")
    if caption:
        out.append("\n---")
        out.append("### WHATSAPP PROMOTIONAL POST GENERATOR")
        out.append("```")
        out.append(caption)
        out.append("```")
        out.append(
            f"_Contact line used: {settings.whatsapp_number} · "
            f"cards rendered in 9:16, 1:1 and 4:3._"
        )
    return "\n".join(out)

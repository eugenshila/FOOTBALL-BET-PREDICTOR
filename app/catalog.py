"""Catalogue loading, seeding and reconciliation against identification output.

The catalogue is the shop's own source of truth for stock. The vision model may
*suggest* a part; the catalogue decides what is actually on the shelf.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import CATALOG_PATH, DB_PATH
from .models import (
    Condition,
    IdentificationReport,
    InventoryPart,
    PartCreate,
    VehicleFitment,
)

_STOPWORDS = {
    "a", "an", "the", "for", "with", "and", "or", "of", "front", "rear", "left",
    "right", "new", "used", "assembly", "assy", "genuine", "oem", "kit",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def load_catalog(path: Path | None = None) -> dict:
    path = path or CATALOG_PATH
    if not path.exists():
        return {"meta": {}, "parts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def catalogue_parts(path: Path | None = None) -> list[PartCreate]:
    data = load_catalog(path)
    out: list[PartCreate] = []
    for raw in data.get("parts", []):
        out.append(
            PartCreate(
                part_name=raw["part_name"],
                oem_part_number=raw.get("oem_part_number", ""),
                alternate_numbers=raw.get("alternate_numbers", []),
                superseded_numbers=raw.get("superseded_numbers", []),
                fitment=[VehicleFitment(**f) for f in raw.get("fitment", [])],
                condition=Condition(raw.get("condition", "New")),
                bin_location=raw.get("bin_location", ""),
                quantity=int(raw.get("quantity", 0)),
                unit_price=float(raw.get("unit_price", 0)),
                currency=raw.get("currency", "KES"),
                category=raw.get("category", ""),
                technical_notes=raw.get("technical_notes", []),
                image_path=raw.get("image", ""),
                verified=bool(raw.get("verified", False)),
            )
        )
    return out


def seed_if_empty(*, force: bool = False, db_path: Path | None = None) -> int:
    """Populate an empty database from the catalogue file. Returns rows added."""
    existing = db.list_parts(limit=1, path=db_path)
    if existing and not force:
        return 0
    added = 0
    for part in catalogue_parts():
        db.create_part(part, path=db_path)
        added += 1
    return added


@dataclass
class Match:
    part: InventoryPart
    score: float
    reasons: list[str]


def _number_variants(part: InventoryPart) -> set[str]:
    raw = [part.oem_part_number, *part.alternate_numbers, *part.superseded_numbers]
    return {re.sub(r"[^A-Z0-9]", "", n.upper()) for n in raw if n}


def score_against(report: IdentificationReport, part: InventoryPart) -> Match:
    """Score how well an identification matches a catalogue record (0..1)."""
    score = 0.0
    reasons: list[str] = []

    report_numbers = {
        re.sub(r"[^A-Z0-9]", "", n.upper())
        for n in [report.oem_part_number, *report.alternate_numbers, *report.superseded_numbers]
        if n
    }
    overlap = report_numbers & _number_variants(part)
    if overlap:
        score += 0.6
        reasons.append(f"part number match: {sorted(overlap)[0]}")

    name_overlap = _tokens(report.part_name) & _tokens(part.part_name)
    if name_overlap:
        ratio = len(name_overlap) / max(len(_tokens(part.part_name)), 1)
        score += 0.25 * min(ratio, 1.0)
        reasons.append("name tokens: " + ", ".join(sorted(name_overlap)))

    report_vehicles = {
        f"{v.make} {v.model}".lower().strip() for v in report.fitment if v.make or v.model
    }
    catalogue_vehicles = {
        f"{v.make} {v.model}".lower().strip() for v in part.fitment if v.make or v.model
    }
    for candidate in report_vehicles:
        for known in catalogue_vehicles:
            if candidate and known and (candidate in known or known in candidate):
                score += 0.15
                reasons.append(f"fitment match: {known}")
                break
        else:
            continue
        break

    return Match(part=part, score=round(min(score, 1.0), 3), reasons=reasons)


def match_part(
    report: IdentificationReport,
    *,
    threshold: float = 0.45,
    db_path: Path | None = None,
) -> tuple[InventoryPart | None, list[Match]]:
    """Best catalogue match for a report, plus ranked candidates for review."""
    parts = db.list_parts(limit=500, path=db_path)
    ranked = sorted(
        (score_against(report, p) for p in parts),
        key=lambda m: m.score,
        reverse=True,
    )
    best = ranked[0] if ranked else None
    if best and best.score >= threshold:
        return best.part, [m for m in ranked if m.score > 0][:5]
    return None, [m for m in ranked if m.score > 0][:5]


def seed_images() -> list[Path]:
    from .config import SEED_DIR

    if not SEED_DIR.exists():
        return []
    return sorted(p for p in SEED_DIR.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


__all__ = [
    "load_catalog",
    "catalogue_parts",
    "seed_if_empty",
    "match_part",
    "score_against",
    "seed_images",
    "Match",
    "DB_PATH",
]

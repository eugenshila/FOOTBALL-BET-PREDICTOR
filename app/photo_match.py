"""Read-only visual suggestions from inventory photos and local catalogue photos.

This is a coarse fingerprint, not proof of identity. No photo or stock is
changed by this module. Missing, corrupt and remote images are skipped.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from . import db
from .catalog import catalogue_parts
from .image_review import local_image
from .config import BASE_DIR
from .vision.offline import _distance, fingerprint
from .models import Confidence, IdentificationReport


@lru_cache(maxsize=16384)
def _reference(path: str, size: int, mtime_ns: int):
    with Image.open(path) as image:
        image.load()
        return fingerprint(image)


def _numbers(part) -> set[str]:
    import re
    return {re.sub(r"[^A-Z0-9]", "", n.upper()) for n in
            [part.oem_part_number, *part.alternate_numbers, *part.superseded_numbers] if n}


def suggestions(image_bytes: bytes) -> list[dict]:
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        query = fingerprint(image)
    parts = db.list_parts(limit=100000, include_receipt=False)
    by_number: dict[str, list] = {}
    for part in parts:
        for number in _numbers(part):
            by_number.setdefault(number, []).append(part)

    sources = [(p, p.image_path) for p in parts]
    for reference in catalogue_parts():
        matches = {p.id: p for n in _numbers(reference) for p in by_number.get(n, [])}
        sources.extend((p, reference.image_path) for p in matches.values())

    best: dict[int, dict] = {}
    # Several stock records can reference the same photograph; compute its
    # distance only once per query without changing candidate ordering.
    distances: dict[str, float] = {}
    resolved: dict[str, tuple[str, str] | None] = {}
    for part, raw in sources:
        if raw not in resolved:
            resolved[raw] = local_image(raw)
        safe = resolved[raw]
        if not safe:
            continue
        try:
            path = Path(safe[0])
            if not path.is_absolute():
                path = BASE_DIR / path
            key = str(path)
            if key not in distances:
                stat = path.stat()
                distances[key] = _distance(query, _reference(key, stat.st_size, stat.st_mtime_ns))
            distance = distances[key]
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            continue
        # A useful suggestion rather than always presenting a nearest neighbour.
        if distance > 0.30:
            continue
        candidate = {"part": part, "distance": round(distance, 4), "reference_url": safe[1]}
        if part.id not in best or distance < best[part.id]["distance"]:
            best[part.id] = candidate
    ranked = sorted(best.values(), key=lambda item: (item["distance"], item["part"].id))[:5]
    # Only the returned records need the latest receipt date; querying it for
    # every part made large movement histories slow on every photo search.
    dates = db.receipt_dates_for([candidate["part"].id for candidate in ranked])
    for candidate in ranked:
        candidate["part"] = candidate["part"].model_copy(update={"last_received_at": dates.get(candidate["part"].id)})
    return ranked


# The fingerprint is deliberately simple. These are heuristic cut-offs, not
# probabilities or proof that a physical part is the same.
LOCAL_CLEAR_DISTANCE = 0.10
LOCAL_SEPARATION = 0.025


def local_report(candidates: list[dict]) -> tuple[IdentificationReport, object] | None:
    """Show any local suggestions without sending the image elsewhere."""
    if not candidates:
        return None
    uncertain = (candidates[0]["distance"] > LOCAL_CLEAR_DISTANCE or
                 (len(candidates) > 1 and candidates[1]["distance"] - candidates[0]["distance"] < LOCAL_SEPARATION))
    candidate = candidates[0]
    part = candidate["part"]
    report = IdentificationReport(
        part_name=part.part_name, category=part.category,
        oem_part_number=part.oem_part_number, alternate_numbers=part.alternate_numbers,
        superseded_numbers=part.superseded_numbers, fitment=part.fitment,
        technical_notes=part.technical_notes,
        number_confidence=Confidence.low, overall_confidence=Confidence.low if uncertain else Confidence.medium,
        provider="local-inventory", model_name="reference-fingerprint-v1",
        needs_manual_review=True,
        visual_evidence=[f"Closest local inventory photo: record #{part.id} (distance {candidate['distance']:.4f})."],
        warnings=[
            "Local photo similarity is only a suggestion, not proof of identity. "
            "Verify the physical part, OEM number and fitment before updating stock.",
            "Part details are copied from the local inventory record, not read from the uploaded photo. "
            "External identification was not used.",
        ],
    )
    if uncertain:
        report.warnings.append("The local photo match is uncertain or has close alternatives; review all suggestions before choosing.")
    return report, part

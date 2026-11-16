"""Conservative, local photo suggestions. No remote fetches or automatic writes.

Candidates are metadata matches, not visual/OEM verification. Only existing
images inside the seed/upload directories may be offered or approved.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import re
from pathlib import Path

from . import db
from .catalog import catalogue_parts
from .config import BASE_DIR, SEED_DIR, UPLOAD_DIR
from .models import InventoryPart


def _tokens(text: str) -> set[str]:
    stop = {"the", "for", "with", "and", "new", "used", "genuine", "oem", "mopar", "jeep", "assembly", "assy", "part", "parts"}
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in stop}


def _numbers(part) -> set[str]:
    numbers = {re.sub(r"[^A-Z0-9]", "", n.upper()) for n in
               [part.oem_part_number, *part.alternate_numbers, *part.superseded_numbers]}
    return numbers - {""}


def local_image(raw: str) -> tuple[str, str] | None:
    """Return an existing safe local path and URL; never accept a remote URL."""
    if not raw:
        return None
    if raw.startswith("/media/uploads/"):
        path = UPLOAD_DIR / raw.removeprefix("/media/uploads/")
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = BASE_DIR / path
    path = path.resolve()
    for root, prefix in ((SEED_DIR.resolve(), "/assets/seed/"),
                         (UPLOAD_DIR.resolve(), "/media/uploads/")):
        if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            stored = str(path.relative_to(BASE_DIR)) if path.is_relative_to(BASE_DIR) else str(path)
            return stored, prefix + path.relative_to(root).as_posix()
    return None


def candidates_for(part: InventoryPart) -> list[dict]:
    sources = [*catalogue_parts(), *db.list_parts(limit=1000)]
    candidates = {}
    for source in sources:
        if getattr(source, "id", None) == part.id:
            continue
        image = local_image(source.image_path)
        if image is None:
            continue
        exact = bool(_numbers(part) & _numbers(source))
        overlap = _tokens(part.part_name) & _tokens(source.part_name)
        if not exact and not overlap:
            continue
        score = 100 if exact else len(overlap)
        path, url = image
        candidate_id = hashlib.sha256(f"{path}|{source.part_name}|{source.oem_part_number}".encode()).hexdigest()[:24]
        candidates[candidate_id] = {
            "candidate_id": candidate_id,
            "part_name": source.part_name,
            "oem_part_number": source.oem_part_number,
            "image_path": path,
            "image_url": url,
            "reason": "OEM / cross-reference match — check fitment" if exact else "Similar part name only — check OEM and fitment",
            "score": score,
        }
    # Photos supplied with stock ZIPs are candidates for this exact record only.
    manifest = UPLOAD_DIR / ".image-review" / f"{part.id}.json"
    if manifest.exists():
        try:
            uploaded = json.loads(manifest.read_text(encoding="utf-8"))
            image = local_image(uploaded.get("image_path", ""))
            if image and uploaded.get("oem_part_number") == part.oem_part_number:
                path, url = image
                candidate_id = hashlib.sha256(f"zip|{part.id}|{path}".encode()).hexdigest()[:24]
                candidates[candidate_id] = {
                    "candidate_id": candidate_id, "part_name": part.part_name,
                    "oem_part_number": part.oem_part_number, "image_path": path,
                    "image_url": url, "reason": "TEST PLACEHOLDER — not a photo of this part" if uploaded.get("test_only") else "Uploaded filename matches this OEM — inspect the photo before approving",
                    "score": 200,
                }
        except (ValueError, OSError, TypeError):
            pass
    return sorted(candidates.values(), key=lambda c: (-c["score"], c["part_name"], c["candidate_id"]))[:6]


def register_upload(part: InventoryPart, image_path: str, *, test_only: bool = False) -> None:
    """Stage one image without changing the inventory record's current photo."""
    directory = UPLOAD_DIR / ".image-review"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{part.id}.json"
    temporary = directory / f".{part.id}-{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"oem_part_number": part.oem_part_number,
                                     "image_path": image_path, "test_only": test_only}), encoding="utf-8")
    os.replace(temporary, destination)

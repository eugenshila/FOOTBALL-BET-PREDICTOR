"""Offline reference matcher.

No API key, no internet: this compares the uploaded photo against the shop's own
catalogue imagery and returns the closest reference. It is a nearest-neighbour
fingerprint, NOT a trained classifier - every response says so, and the
confidence it reports is a similarity band, never a claim of certainty.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ..catalog import catalogue_parts
from ..config import BASE_DIR, SEED_DIR
from ..models import Confidence, IdentificationReport, Source, VehicleFitment

FEATURE_CACHE = BASE_DIR / "data" / "feature_cache.json"
THUMB = 96


@dataclass
class Fingerprint:
    hist: np.ndarray  # foreground colour histogram (normalised)
    mean_rgb: tuple[float, float, float]
    saturation: float
    brightness: float
    edge_density: float
    foreground_ratio: float
    radial_symmetry: float
    elongation: float


def _background_mask(arr: np.ndarray) -> np.ndarray:
    """Treat pixels close to the border colour as background."""
    border = np.concatenate(
        [arr[0, :, :], arr[-1, :, :], arr[:, 0, :], arr[:, -1, :]], axis=0
    )
    bg = np.median(border, axis=0)
    distance = np.linalg.norm(arr - bg, axis=2)
    return distance > np.percentile(distance, 55)


def fingerprint(image: Image.Image) -> Fingerprint:
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = ImageOps.pad(image, (THUMB, THUMB), color=(245, 245, 245))
    arr = np.asarray(image, dtype=np.float32)

    mask = _background_mask(arr)
    if mask.sum() < 32:  # nearly uniform frame - fall back to the whole image
        mask = np.ones(arr.shape[:2], dtype=bool)

    pixels = arr[mask]
    mean_rgb = tuple(float(x) for x in pixels.mean(axis=0))

    # coarse 3-bit-per-channel histogram over foreground pixels only
    quant = (pixels // 32).astype(int)
    codes = quant[:, 0] * 64 + quant[:, 1] * 8 + quant[:, 2]
    hist, _ = np.histogram(codes, bins=512, range=(0, 512))
    hist = hist.astype(np.float32)
    hist /= max(hist.sum(), 1.0)

    edges = np.asarray(image.convert("L").filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    edge_density = float((edges > 40).mean())

    mx = pixels.max(axis=1)
    mn = pixels.min(axis=1)
    saturation = float(np.mean((mx - mn) / np.maximum(mx, 1e-3)))
    brightness = float(np.mean(mx) / 255.0)

    ys, xs = np.nonzero(mask)
    elongation = 1.0
    if len(xs) > 8:
        h = ys.max() - ys.min() + 1
        w = xs.max() - xs.min() + 1
        elongation = float(max(h, w) / max(min(h, w), 1))

    # crude radial symmetry: mask vs mask rotated 90 degrees
    grid = mask.reshape(THUMB, THUMB)
    radial = float(1.0 - np.mean(np.abs(grid.astype(np.float32) - np.rot90(grid).astype(np.float32))))

    return Fingerprint(
        hist=hist,
        mean_rgb=mean_rgb,
        saturation=saturation,
        brightness=brightness,
        edge_density=edge_density,
        foreground_ratio=float(mask.mean()),
        radial_symmetry=radial,
        elongation=elongation,
    )


def _distance(a: Fingerprint, b: Fingerprint) -> float:
    hist_d = float(np.abs(a.hist - b.hist).sum() / 2.0)  # 0 identical, 1 disjoint
    colour_d = float(np.linalg.norm(np.array(a.mean_rgb) - np.array(b.mean_rgb)) / 441.0)
    scalar_d = (
        abs(a.edge_density - b.edge_density) * 1.6
        + abs(a.foreground_ratio - b.foreground_ratio) * 1.1
        + abs(a.saturation - b.saturation) * 0.9
        + abs(a.brightness - b.brightness) * 0.6
        + abs(a.elongation - b.elongation) * 0.08
        + abs(a.radial_symmetry - b.radial_symmetry) * 0.7
    )
    return 0.42 * hist_d + 0.23 * colour_d + 0.35 * min(scalar_d, 1.4)


def _cache_key(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha1(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode()).hexdigest()


def reference_fingerprints() -> list[tuple[str, Fingerprint]]:
    """Fingerprints for every catalogue image, memoised to disk."""
    cache: dict[str, dict] = {}
    if FEATURE_CACHE.exists():
        try:
            cache = json.loads(FEATURE_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}

    out: list[tuple[str, Fingerprint]] = []
    dirty = False
    for part in catalogue_parts():
        rel = part.image_path
        if not rel:
            continue
        path = BASE_DIR / rel
        if not path.exists():
            continue
        key = _cache_key(path)
        entry = cache.get(str(rel))
        if entry and entry.get("key") == key:
            fp = Fingerprint(
                hist=np.array(entry["hist"], dtype=np.float32),
                mean_rgb=tuple(entry["mean_rgb"]),
                saturation=entry["saturation"],
                brightness=entry["brightness"],
                edge_density=entry["edge_density"],
                foreground_ratio=entry["foreground_ratio"],
                radial_symmetry=entry["radial_symmetry"],
                elongation=entry["elongation"],
            )
        else:
            with Image.open(path) as img:
                fp = fingerprint(img)
            cache[str(rel)] = {
                "key": key,
                "hist": fp.hist.tolist(),
                "mean_rgb": list(fp.mean_rgb),
                "saturation": fp.saturation,
                "brightness": fp.brightness,
                "edge_density": fp.edge_density,
                "foreground_ratio": fp.foreground_ratio,
                "radial_symmetry": fp.radial_symmetry,
                "elongation": fp.elongation,
            }
            dirty = True
        out.append((part.part_name, fp))

    if dirty:
        FEATURE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FEATURE_CACHE.write_text(json.dumps(cache))
    return out


def _describe(fp: Fingerprint) -> list[str]:
    """Factual, feature-derived observations. No invented part claims."""
    notes: list[str] = []
    r, g, b = fp.mean_rgb
    if fp.saturation < 0.12:
        tone = "bare or painted metal (very low colour saturation)"
    elif fp.saturation < 0.3:
        notes_tone = "muted, metal-like tones"
        tone = notes_tone
    else:
        tone = "coloured finish, likely painted or moulded polymer"
    notes.append(f"Dominant surface: {tone}, mean RGB ({r:.0f}, {g:.0f}, {b:.0f}).")
    notes.append(f"Foreground occupies ~{fp.foreground_ratio * 100:.0f}% of the frame.")
    if fp.edge_density > 0.18:
        notes.append("High edge density - machined or cast detail with many internal contours.")
    elif fp.edge_density < 0.06:
        notes.append("Low edge density - smooth, simple, moulded shape.")
    if fp.radial_symmetry > 0.78 and fp.elongation < 1.5:
        notes.append("Near-radially symmetric silhouette, consistent with a hub, pulley or round housing.")
    elif fp.elongation > 1.9:
        notes.append("Elongated silhouette, consistent with an arm, shaft or hose assembly.")
    return notes


def _band(distance: float) -> tuple[Confidence, float]:
    if distance < 0.30:
        return Confidence.high, 0.82
    if distance < 0.52:
        return Confidence.medium, 0.55
    return Confidence.low, 0.25


class OfflineIdentifier:
    """Nearest-reference matcher built from the shop catalogue."""

    name = "offline"
    model_name = "reference-fingerprint-v1"

    def identify(self, image_bytes: bytes, mime: str = "image/jpeg") -> IdentificationReport:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            query = fingerprint(img)

        refs = reference_fingerprints()
        warnings = [
            "Offline reference matcher used: no vision model configured. "
            "Identification is a similarity comparison against catalogue photos, not a trained classifier.",
        ]
        if not refs:
            return IdentificationReport(
                provider=self.name,
                model_name=self.model_name,
                warnings=warnings + ["No catalogue reference images available to compare against."],
                needs_manual_review=True,
            )

        scored = sorted(
            ((_distance(query, fp), name) for name, fp in refs),
            key=lambda t: t[0],
        )
        best_distance, best_name = scored[0]
        confidence, similarity = _band(best_distance)

        catalogue = {p.part_name: p for p in catalogue_parts()}
        part = catalogue[best_name]
        neighbours = [n for _, n in scored[1:3]]
        if len(scored) > 1 and scored[1][0] - best_distance < 0.06:
            warnings.append(
                f"Close alternative: {scored[1][1]} scored within 0.06 of the winner - confirm visually."
            )

        return IdentificationReport(
            part_name=part.part_name,
            category=part.category,
            oem_part_number=part.oem_part_number,
            alternate_numbers=list(part.alternate_numbers),
            superseded_numbers=list(part.superseded_numbers),
            number_confidence=confidence,
            fitment=[VehicleFitment(**f.model_dump()) for f in part.fitment],
            technical_notes=list(part.technical_notes),
            visual_evidence=[
                *_describe(query),
                f"Closest catalogue reference: {best_name} (distance {best_distance:.3f}, "
                f"similarity {similarity:.0%}).",
                *([f"Runners-up: {', '.join(neighbours)}."] if neighbours else []),
            ],
            overall_confidence=confidence,
            provider=self.name,
            model_name=self.model_name,
            needs_manual_review=True,
            warnings=warnings,
        )

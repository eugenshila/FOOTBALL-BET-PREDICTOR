"""Read stock.csv + numbered images without extracting untrusted ZIP paths."""
from __future__ import annotations

import csv
import io
import stat
import zipfile
from itertools import islice
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError

from .importers import map_headers, sniff_text

MAX_UPLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024


@dataclass
class StockBundle:
    csv_text: str
    # CSV data-row number -> validated, normalised JPEG bytes
    images: dict[int, bytes]
    image_names: dict[int, str]
    test_only: bool
    rows: int
    unused_images: list[str]


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name


def _jpeg(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Only JPG, PNG and WebP images are supported")
            if image.width * image.height > 16_000_000:
                raise ValueError("Image exceeds 16 million pixels")
            image.load()
            image = image.convert("RGB")
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("ZIP contains an unreadable or unsafe image") from exc


def read_bundle(raw: bytes) -> StockBundle:
    if len(raw) > MAX_UPLOAD:
        raise ValueError("ZIP exceeds 32 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1200 or sum(item.file_size for item in entries) > MAX_EXPANDED:
                raise ValueError("ZIP has too many files or exceeds 80 MB expanded")
            names = set()
            files = {}
            for item in entries:
                if not _safe_name(item.filename) or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise ValueError("ZIP contains an unsafe path, link or encrypted file")
                key = item.filename.casefold()
                if key in names:
                    raise ValueError("ZIP contains duplicate filenames")
                names.add(key)
                if item.is_dir():
                    continue
                suffix = PurePosixPath(item.filename).suffix.lower()
                if item.filename != "stock.csv" and suffix not in {".jpg", ".jpeg", ".png", ".webp", ".txt"}:
                    raise ValueError("ZIP supports stock.csv, JPG/PNG/WebP images and text instructions only")
                if item.file_size > MAX_IMAGE:
                    raise ValueError("Each CSV/image must be at most 8 MB")
                files[item.filename] = item
            if "stock.csv" not in files:
                raise ValueError("Place exactly one stock.csv at the root of the ZIP")
            text = sniff_text(archive.read(files["stock.csv"]))
            reader = csv.DictReader(io.StringIO(text))
            headers = reader.fieldnames or []
            mapping, _ = map_headers(headers)
            inverse = {value: key for key, value in mapping.items()}
            if "part_name" not in inverse or "oem_part_number" not in inverse:
                raise ValueError("stock.csv needs part_name and oem_part_number columns")
            rows = list(islice(reader, 1001))
            if not rows or len(rows) > 1000:
                raise ValueError("stock.csv must contain 1–1000 rows")
            seen = set()
            images = {}
            image_names = {}
            used = set()
            available = [name for name in files if PurePosixPath(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
            for index, row in enumerate(rows, 2):
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"CSV row {index} has the wrong number of columns")
                number = row[inverse["oem_part_number"]].strip()
                if not number or number.upper() in seen:
                    raise ValueError(f"CSV row {index}: part numbers must be nonempty and unique; merge duplicates first")
                seen.add(number.upper())
                reference = row.get(inverse.get("image_path", ""), "").strip()
                if reference:
                    if not _safe_name(reference) or reference not in available:
                        raise ValueError(f"CSV row {index}: image file is missing or invalid: {reference}")
                else:
                    matches = [name for name in available if PurePosixPath(name).stem.upper() == number.upper()]
                    if len(matches) > 1:
                        raise ValueError(f"CSV row {index}: multiple images match; specify image_path")
                    reference = matches[0] if matches else ""
                if reference:
                    if PurePosixPath(reference).stem.upper() != number.upper():
                        raise ValueError(f"CSV row {index}: image filename must match the part number exactly")
                    images[index] = _jpeg(archive.read(files[reference]))
                    image_names[index] = reference
                    used.add(reference)
                # ZIP uploads cannot grant OEM verification or attach photos silently.
                if "verified" in inverse:
                    row[inverse["verified"]] = "false"
            if "verified" not in inverse:
                headers = [*headers, "verified"]
                for row in rows:
                    row["verified"] = "false"
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
            return StockBundle(output.getvalue(), images, image_names, "TEST-ONLY.txt" in files, len(rows), sorted(set(available) - used))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise ValueError("Not a readable supported ZIP archive") from exc

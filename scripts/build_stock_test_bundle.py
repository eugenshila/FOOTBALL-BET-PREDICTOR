"""Build a clearly labelled placeholder-image ZIP from a stock CSV.

Usage: python scripts/build_stock_test_bundle.py input.csv exports/test-stock.zip
These are NOT product photos, visual matches, or generated depictions of parts.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import textwrap
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets/fonts/Barlow-Medium.ttf"


def placeholder(number: str, name: str) -> bytes:
    image = Image.new("RGB", (900, 540), "#171d20")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((18, 18, 882, 522), 20, outline="#65614b", width=2)
    draw.rectangle((20, 55, 880, 119), fill="#edc66b")
    def font(size):
        return ImageFont.truetype(str(FONT), size)
    draw.text((450, 85), "TEST PLACEHOLDER - NOT A PART PHOTO", fill="#171d20", font=font(30), anchor="mm")
    draw.text((450, 190), "TRAIL VAULT / UPLOAD MATCH TEST", fill="#aeb5af", font=font(20), anchor="mm")
    draw.text((450, 267), number, fill="#fff0c3", font=font(55), anchor="mm")
    for index, line in enumerate(textwrap.wrap(name, 54)[:3]):
        draw.text((450, 330 + index * 29), line, fill="#e0e5df", font=font(24), anchor="mm")
    draw.text((450, 470), "Replace with your actual second-hand part photo before publishing.", fill="#b8beb7", font=font(21), anchor="mm")
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    return output.getvalue()


def build(source: Path, destination: Path) -> tuple[int, int]:
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows or not {"part_name", "oem_part_number", "quantity"}.issubset(fields):
        raise ValueError("Use a nonempty cleaned CSV with part_name, oem_part_number and quantity")
    numbers = [row["oem_part_number"] for row in rows]
    if len(set(n.upper() for n in numbers)) != len(rows) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in numbers):
        raise ValueError("Part numbers must be unique and safe for filenames")
    for field in ("image_path", "verified", "technical_notes"):
        if field not in fields:
            fields.append(field)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("TEST-ONLY.txt", """TRAIL VAULT - UPLOAD TEST ONLY

This ZIP contains stock.csv and one numbered PLACEHOLDER per part number.
There are NO real part photos in this package. Do not publish these as product photos.
The numbers and descriptions come from the supplied CSV and have not been EPC-verified.

1. Drop this ZIP into Trail Vault's CSV/stock ZIP upload area.
2. Review the dry run: rows, quantities, duplicates and matching image filenames.
3. Apply import only if you intend to add the stock to this inventory.
4. Find local candidates for a row. The uploaded placeholder is marked TEST PLACEHOLDER.
5. Approve one image to test saving it, or skip. Approval does not verify the OEM.

To use real photos later:
- Replace images/<PART_NUMBER>.jpg with a photo of that exact physical part.
- Keep the filenames and CSV image_path references in sync.
- Remove this TEST-ONLY.txt marker only after ALL placeholders have been replaced.
- Remove the TEST PLACEHOLDER notes from stock.csv only after real photos are added.
- Keep stock.csv and images/ at the root when re-zipping, without an outer folder.

Existing OEMs are skipped (no quantity increase or replacement) when re-uploading.
Their uploaded images can still be reviewed; existing photos stay until approved.
Prices are blank and the app currently represents blank prices as zero: do not quote
or publish a price until it has been set. No sales-ready verification is granted.
""")
        for row in rows:
            number = row["oem_part_number"]
            filename = f"images/{number}.jpg"
            row["image_path"] = filename
            row["verified"] = "false"
            note = "TEST PLACEHOLDER image only - replace with an actual part photo before publishing"
            row["technical_notes"] = " | ".join(filter(None, [row.get("technical_notes", ""), note]))
            archive.writestr(filename, placeholder(number, row["part_name"]))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        archive.writestr("stock.csv", output.getvalue().encode("utf-8-sig"))
    return len(rows), sum(int(row["quantity"]) for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("zip", type=Path)
    args = parser.parse_args()
    parts, units = build(args.csv, args.zip)
    print(f"Created {args.zip}: {parts} parts, {units} units, {parts} labelled placeholders (not part photos).")

"""Render the documentation sample cards into docs/samples/.

Run after changing the card design:  ./.venv/bin/python scripts/render_samples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cards, db  # noqa: E402
from app.catalog import seed_if_empty  # noqa: E402
from app.config import BASE_DIR  # noqa: E402

OUT = BASE_DIR / "docs" / "samples"


def main() -> None:
    db.init_db()
    seed_if_empty()
    parts = {part.part_name.split()[0].lower(): part for part in db.list_parts()}
    wanted = [
        ("wheel", "story"),
        ("pcv", "square"),
        ("camshaft", "wide"),
        ("high-pressure", "bin_label"),
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    for key, fmt in wanted:
        part = parts.get(key)
        if part is None:
            print(f"skip {key}: not in catalogue")
            continue
        image = cards.render_card(cards.CardContext(part=part), fmt)
        path = cards.save_png(image, OUT / f"{key}-{fmt}.png")
        print(f"· {path.relative_to(BASE_DIR)}  {image.width}x{image.height}")


if __name__ == "__main__":
    main()

"""Guards for the inventory table cleanup.

The table is plain HTML + vanilla JS with no build step, so the layout contract
lives in three static files. These checks keep the one-photo-per-row layout,
fixed column widths, non-wrapping prices and compact row actions from drifting
back into the crowded, duplicated design.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "web/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/styles.css").read_text(encoding="utf-8")
JS = (ROOT / "web/static/app.js").read_text(encoding="utf-8")


class InventoryTableParser(HTMLParser):
    """Read the column definitions and headings of the inventory table only."""

    def __init__(self):
        super().__init__()
        self.found = False
        self.in_table = False
        self.in_th = False
        self.cols = 0
        self.headers: list[str] = []
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table" and "inventory-table" in attributes.get("class", "").split():
            self.found = self.in_table = True
        elif self.in_table and tag == "col":
            self.cols += 1
        elif self.in_table and tag == "th":
            self.in_th = True
            self._text = []

    def handle_data(self, data):
        if self.in_th:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "th" and self.in_th:
            self.headers.append("".join(self._text).strip())
            self.in_th = False
        elif tag == "table" and self.in_table:
            self.in_table = False


def _row_template() -> str:
    """The part of renderParts() that builds one <tr>, excluding listeners."""
    start = JS.index("function renderParts() {")
    end = JS.index("\n  $$('[data-photo]'", start)
    return JS[start:end]


def test_inventory_table_has_eight_even_columns_and_no_photo_column():
    parser = InventoryTableParser()
    parser.feed(HTML)
    assert parser.found
    assert parser.cols == 8
    assert parser.headers == [
        "Part", "OEM number", "Bin", "Qty", "Date received ↕",
        "Price", "Status", "Actions",
    ]
    assert "Photo" not in parser.headers
    # Widths are explicit and add up to the full table width.
    widths = [int(value) for value in re.findall(r"col\.col-\w+ \{ width: (\d+)%; \}", CSS)]
    assert len(widths) == 8 and sum(widths) == 100


def test_row_uses_one_photo_and_one_compact_action_group():
    row = _row_template()
    assert row.count('class="part-photo"') == 1
    assert row.count('class="part-thumb"') == 1
    assert "part-photo-thumb" not in row
    assert "Replace photo" not in row and "Add photo" not in row
    assert row.count('class="part-photo-action"') == 1
    assert row.count('class="row-actions"') == 1
    for action in ("data-edit", "data-card", "data-images"):
        assert row.count(f"{action}=") == 1
    assert row.count('class="btn tiny icon-action"') == 3
    # No text buttons left in the row: labels are titles/aria-labels only.
    assert ">Cards<" not in row and ">Edit<" not in row and ">Photos<" not in row


def test_numeric_and_text_columns_have_stable_classes_and_spacing():
    row = _row_template()
    for cell in ("part-column", "oem-column", "bin-column", "qty-column",
                 "date-column", "price-column", "status-column", "actions-column"):
        assert f'class="{cell}' in row
    assert ".inventory-table { table-layout: fixed;" in CSS
    assert ".inventory-table .price-column, .inventory-table .qty-column," in CSS
    assert "white-space: nowrap; }" in CSS
    assert "font-variant-numeric: tabular-nums;" in CSS
    assert ".row-actions { display: inline-flex; flex-wrap: nowrap;" in CSS
    # Readable spacing for names and OEM numbers.
    assert re.search(r"\.part-name \{[^}]*line-height: 1\.5;[^}]*letter-spacing:", CSS)
    assert re.search(r"\.part-oem \{[^}]*line-height: 1\.55;[^}]*letter-spacing:", CSS)

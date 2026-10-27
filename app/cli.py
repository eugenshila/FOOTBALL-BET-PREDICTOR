"""Command-line interface for counter staff and scripting.

    python -m app.cli identify photo.jpg --cards
    python -m app.cli list --search pentastar
    python -m app.cli lookup 5184894AA
    python -m app.cli cards --part-id 2 --formats story,square
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import cards, db, importers, vision
from .catalog import match_part, seed_if_empty
from .config import EXPORT_DIR, settings
from .report import render_report


def _bootstrap() -> None:
    settings.ensure_dirs()
    db.init_db()
    if seed_if_empty():
        print("· seeded demo catalogue\n")


def cmd_identify(args: argparse.Namespace) -> int:
    path = Path(args.image)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        return 2

    _bootstrap()
    raw = path.read_bytes()
    report = vision.identify(raw, "image/jpeg")
    matched, _candidates = match_part(report)
    caption = cards.caption_for(matched) if matched else ""
    print(render_report(report, matched, caption=caption))

    if args.cards:
        if matched is None:
            print("\n! no stock record matched - add the part before generating cards", file=sys.stderr)
            return 1
        ctx = cards.CardContext(part=matched)
        for fmt_key, image in cards.render_all(ctx, args.formats):
            out = cards.save_png(image, EXPORT_DIR / f"{matched.id}-{fmt_key}.png")
            print(f"· wrote {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _bootstrap()
    parts = db.list_parts(args.search, in_stock_only=args.in_stock)
    if not parts:
        print("no parts found")
        return 0
    header = f"{'ID':>4}  {'PART':<38} {'OEM':<14} {'BIN':<8} {'QTY':>4}  {'PRICE':>14}"
    print(header)
    print("-" * len(header))
    for part in parts:
        price = f"{part.currency} {part.unit_price:,.0f}"
        flag = "" if part.verified else "  (unverified)"
        print(
            f"{part.id:>4}  {part.part_name[:38]:<38} {part.oem_part_number:<14} "
            f"{part.bin_location:<8} {part.quantity:>4}  {price:>14}{flag}"
        )
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    _bootstrap()
    part = db.find_by_number(args.number)
    if part is None:
        print(f"no stock record for {args.number}")
        return 1
    print(render_report(
        report=_report_from_part(part), part=part, caption=cards.caption_for(part)
    ))
    return 0


def _report_from_part(part):
    from .models import IdentificationReport

    return IdentificationReport(
        part_name=part.part_name,
        category=part.category,
        oem_part_number=part.oem_part_number,
        alternate_numbers=part.alternate_numbers,
        superseded_numbers=part.superseded_numbers,
        fitment=part.fitment,
        technical_notes=part.technical_notes,
        provider="catalogue",
        model_name="stored record",
    )


def cmd_cards(args: argparse.Namespace) -> int:
    _bootstrap()
    part = db.get_part(args.part_id)
    if part is None:
        print(f"error: no part with id {args.part_id}", file=sys.stderr)
        return 2
    if settings.require_verification and not part.verified:
        print(
            f"error: part #{part.id} is unverified. Confirm the OEM number first "
            f"(PATCH /api/parts/{part.id} verified=true).",
            file=sys.stderr,
        )
        return 1
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    ctx = cards.CardContext(part=part, headline=args.headline)
    for fmt_key, image in cards.render_all(ctx, formats):
        out = cards.save_png(image, EXPORT_DIR / f"{part.id}-{fmt_key}.png")
        print(f"· wrote {out}")
    print("\n--- caption ---\n")
    print(cards.caption_for(part, ctx))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    _bootstrap()
    raw = Path(args.csv).read_bytes()
    report = importers.import_csv(
        raw,
        dry_run=args.dry_run,
        update_existing=args.update,
        default_currency=settings.currency,
    )
    summary = report.summary()
    mode = "DRY RUN — nothing written" if args.dry_run else "imported"
    print(f"{mode}: {summary['rows_total']} rows")
    print(f"  created: {summary['created']}  updated: {summary['updated']}  "
          f"skipped: {summary['skipped']}  errors: {summary['errors']}")
    if summary["ignored_columns"]:
        print(f"  ignored columns: {', '.join(summary['ignored_columns'])}")
    for problem in summary["problems"]:
        print(f"  ! row {problem['row']}: {problem['message']}")
    if args.dry_run:
        print("\nRe-run with --apply to write these changes.")
    return 0 if summary["errors"] == 0 else 1


def cmd_export(args: argparse.Namespace) -> int:
    _bootstrap()
    target = Path(args.out)
    target.write_text(importers.export_csv(), encoding="utf-8")
    print(f"· wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parts", description="Spare-parts ID engine CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    identify = sub.add_parser("identify", help="identify a part from a photo")
    identify.add_argument("image")
    identify.add_argument("--cards", action="store_true", help="also render promo cards")
    identify.add_argument("--formats", default="story,square,wide")
    identify.set_defaults(func=cmd_identify)

    listing = sub.add_parser("list", help="list inventory")
    listing.add_argument("--search", default="")
    listing.add_argument("--in-stock", action="store_true")
    listing.set_defaults(func=cmd_list)

    lookup = sub.add_parser("lookup", help="look up a part or cross-reference number")
    lookup.add_argument("number")
    lookup.set_defaults(func=cmd_lookup)

    card_cmd = sub.add_parser("cards", help="render promo cards for a stored part")
    card_cmd.add_argument("--part-id", type=int, required=True)
    card_cmd.add_argument("--formats", default="story,square,wide")
    card_cmd.add_argument("--headline", default="")
    card_cmd.set_defaults(func=cmd_cards)

    importer = sub.add_parser("import-csv", help="bulk load a stock list")
    importer.add_argument("csv")
    importer.add_argument("--apply", dest="dry_run", action="store_false",
                          help="write the rows (default is a dry run)")
    importer.add_argument("--update", action="store_true",
                          help="fill blanks on records that already match an OEM number")
    importer.set_defaults(func=cmd_import, dry_run=True)

    exporter = sub.add_parser("export-csv", help="dump inventory to CSV")
    exporter.add_argument("--out", default="inventory.csv")
    exporter.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

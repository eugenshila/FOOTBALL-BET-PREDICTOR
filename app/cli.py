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

from . import cards, db, importers, storage, vision
from .catalog import match_part, seed_if_empty
from .config import EXPORT_DIR, settings
from .report import render_report


def _bootstrap() -> None:
    """Prepare the database. Respects SEED_DEMO_DATA so a production database
    is never polluted with demo parts by a CLI command."""
    settings.ensure_dirs()
    db.init_db()
    if settings.seed_demo_data and seed_if_empty():
        print("· seeded demo catalogue (SEED_DEMO_DATA=true)\n")


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


def cmd_check_db(args: argparse.Namespace) -> int:
    """Diagnose the database connection and show what is stored."""
    info = db.database_info()
    print(f"engine   : {info['dialect']}")
    print(f"dsn      : {info['dsn']}  ({info['source']})")
    if not info.get("connected"):
        error = info.get("error", "cannot connect")
        print(f"status   : FAILED — {error}")
        print("\nHints:")
        lowered = error.lower()
        if "scheme" in lowered:
            print("  · DATABASE_URL must start with postgresql:// (or leave it empty for SQLite)")
            print("    example: postgresql://partsid_user:pw@localhost:5432/partsid")
        elif "psycopg" in lowered:
            print("  · install the driver:  pip install -r requirements-postgres.txt")
        else:
            print("  · is PostgreSQL running?  (Windows: Services -> postgresql-x64; "
                  "macOS: brew services list; Linux: systemctl status postgresql)")
            print("  · did you create the database and user?  see RUNNING.md step 2")
            print("  · is the port right?  PostgreSQL defaults to 5432, not 8000")
        print("  · unset DATABASE_URL to fall back to SQLite")
        return 1

    print(f"server   : {info.get('version', 'unknown')}")
    try:
        db.init_db()
        counts = db.stats()
        print("status   : OK")
        print(
            f"contents : {counts['parts']} parts · {counts['units']} units · "
            f"{counts['scans']} scans"
        )
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        print(f"status   : connected but unusable — {exc}")
        return 1
    return 0


def cmd_check_providers(args: argparse.Namespace) -> int:
    """One live call per configured provider. Never touches the database."""
    sample = Path(args.image) if args.image else _default_sample()
    if sample is None or not sample.exists():
        print("error: no sample image found (assets/seed/*.jpg)", file=sys.stderr)
        return 2

    raw = sample.read_bytes()
    print(f"sample image : {sample}")
    print(f"configured   : VISION_PROVIDER={settings.vision_provider} "
          f"(resolved: {vision.resolve_provider_name()})")
    print(f"grounding    : {'on' if settings.grounding_enabled else 'off'} "
          f"({settings.grounding_model})\n")

    names = vision.available_providers()
    results = [vision.probe(name, raw) for name in names]

    width = max(len(r["label"]) for r in results) + 2
    for result in results:
        mark = "OK  " if result["ok"] else "FAIL"
        header = f"[{mark}] {result['label']:<{width}} {result['ms']:>5} ms"
        if result["ok"]:
            extra = f" → {result['part_name']}"
            if result["oem_part_number"]:
                extra += f" · OEM {result['oem_part_number']} ({result['number_confidence']})"
            if result["grounded"]:
                extra += f" · web {result['sources']} source(s)"
            print(header + extra)
            if result.get("warning"):
                print(f"        note: {result['warning'][:150]}")
        else:
            print(header)
            print(f"        error: {result['error']}")

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n{ok_count}/{len(results)} provider(s) responded.")

    if "gemini-grounded" not in names:
        print(
            "\nTo enable searching identification (free): get a key at "
            "https://aistudio.google.com/apikey\n  then set GEMINI_API_KEY=... and "
            "SEARCH_GROUNDING=true in .env"
        )
    if len(names) == 1:
        print(
            "Only the offline matcher is active, so uploads are compared against your "
            "own catalogue photos.\nNothing is wrong — add a key (or run Ollama) for "
            "identification of parts you have not photographed."
        )
    return 0 if ok_count else 1


def _default_sample() -> Path | None:
    from .config import SEED_DIR

    for name in ("wheel-hub.jpg", "pcv-valve.jpg"):
        candidate = SEED_DIR / name
        if candidate.exists():
            return candidate
    if SEED_DIR.exists():
        images = sorted(SEED_DIR.glob("*.jpg"))
        return images[0] if images else None
    return None


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{question} [type 'yes' to continue]: ").strip().lower()
    return answer == "yes"


def cmd_reset_db(args: argparse.Namespace) -> int:
    """Wipe inventory (and optionally scans). Used before loading real stock."""
    _bootstrap()
    before = db.stats()
    print(f"database : {db.database_info()['dsn']}")
    print(f"contents : {before['parts']} parts · {before['units']} units · {before['scans']} scans")
    if not before["parts"] and not (args.include_scans and before["scans"]):
        print("\nnothing to remove.")
        return 0

    if not _confirm(
        f"\nThis deletes {before['parts']} part(s)"
        + (f" and {before['scans']} scan(s)" if args.include_scans else "")
        + " permanently. Continue?",
        args.yes,
    ):
        print("cancelled — nothing was changed.")
        return 1

    removed = db.clear_inventory(include_scans=args.include_scans)
    print(
        f"\nremoved  : {removed['parts_removed']} part(s), {removed['units_removed']} unit(s)"
        + (f", {removed['scans_removed']} scan(s)" if args.include_scans else "")
    )
    if args.reseed:
        added = seed_if_empty(force=True)
        print(f"re-seeded: {added} demo part(s)")
    else:
        print(
            "\nThe database is empty. Set SEED_DEMO_DATA=false in .env so the demo\n"
            "catalogue is not re-created on the next start, then load your stock:\n"
            "    python -m app.cli load-stock stock.csv --replace --yes"
        )
    return 0


def cmd_load_stock(args: argparse.Namespace) -> int:
    """Replace (or merge) the inventory from a CSV in one verified operation."""
    _bootstrap()
    source = Path(args.csv)
    if not source.exists():
        print(f"error: {source} not found", file=sys.stderr)
        return 2
    raw = source.read_bytes()

    # Always validate first: never delete data for a file that cannot be parsed.
    preview = importers.import_csv(
        raw, dry_run=True, update_existing=False, default_currency=settings.currency
    )
    summary = preview.summary()
    print(f"file     : {source}  ({summary['rows_total']} rows read)")
    if summary["errors"] and summary["created"] == 0:
        print("error: no usable rows found — nothing was changed", file=sys.stderr)
        for problem in summary["problems"][:10]:
            print(f"  ! row {problem['row']}: {problem['message']}", file=sys.stderr)
        return 1
    if summary["rows_total"] and summary["errors"] / summary["rows_total"] > 0.25:
        print("error: more than 25% of rows failed to parse — nothing was changed", file=sys.stderr)
        for problem in summary["problems"][:10]:
            print(f"  ! row {problem['row']}: {problem['message']}", file=sys.stderr)
        print("Fix the file (or use import-csv to load the good rows only).", file=sys.stderr)
        return 1

    before = db.stats()
    if args.replace:
        print(f"replace  : will delete {before['parts']} existing part(s) after the import parses")
        if not _confirm("Continue?", args.yes):
            print("cancelled — nothing was changed.")
            return 1
        removed = db.clear_inventory(include_scans=args.include_scans)
        print(f"removed  : {removed['parts_removed']} part(s)")

    written = importers.import_csv(
        raw,
        dry_run=False,
        update_existing=not args.replace,
        default_currency=settings.currency,
    )
    final = db.stats()
    print(
        f"imported : {written.created} new, {written.updated} updated, "
        f"{written.skipped} already present, {written.errors} problem row(s)"
    )
    for problem in written.summary()["problems"][:10]:
        print(f"  ! row {problem['row']}: {problem['message']}")
    print(f"database : {final['parts']} parts · {final['units']} units · "
          f"{final['inventory_value']:.0f} {settings.currency} value")
    if written.errors:
        print("\nSome rows need attention — see above. The rest were imported.")
    return 0 if written.errors == 0 else 1


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

    reset = sub.add_parser("reset-db", help="delete all inventory (and optionally scans)")
    reset.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    reset.add_argument("--include-scans", action="store_true", help="also delete scan history")
    reset.add_argument("--reseed", action="store_true", help="put the demo catalogue back")
    reset.set_defaults(func=cmd_reset_db)

    load = sub.add_parser(
        "load-stock", help="replace or merge the inventory from a CSV, with validation"
    )
    load.add_argument("csv")
    load.add_argument("--replace", action="store_true", help="delete existing stock first")
    load.add_argument("--include-scans", action="store_true", help="also delete scan history")
    load.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    load.set_defaults(func=cmd_load_stock)

    db_check = sub.add_parser("check-db", help="diagnose the database connection")
    db_check.set_defaults(func=cmd_check_db)

    providers = sub.add_parser(
        "check-providers", help="make one live call per configured vision provider"
    )
    providers.add_argument("--image", default="", help="sample photo to identify")
    providers.set_defaults(func=cmd_check_providers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

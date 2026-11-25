"""FastAPI application: identification, inventory and promo-card generation."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from . import auth, cards, db, image_review, importers, inventory_pdf, links, shop_profile, stock_bundle, storage, stock_files, vision
from .catalog import match_part, seed_if_empty, seed_images
from .config import ASSET_DIR, BASE_DIR, EXPORT_DIR, UPLOAD_DIR, WEB_DIR, settings
from .models import (
    CardAsset,
    CardRequest,
    CardResult,
    DemoImage,
    IdentificationReport,
    IdentifyResponse,
    InventoryPart,
    ImageApproval,
    PartCreate,
    PartUpdate,
    ShopProfileUpdate,
    StockMovement,
)
from .report import render_report
from .verification_report import build_verification_workbook

log = logging.getLogger(__name__)

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_BYTES = settings.max_upload_mb * 1024 * 1024

@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    try:
        dialect = db.init_db()
    except storage.StorageError as exc:
        _explain_database_problem(str(exc))
        raise
    log.info("Database ready (%s)", dialect)
    if settings.seed_demo_data:
        added = seed_if_empty()
        if added:
            log.info("Seeded %s demo catalogue parts (SEED_DEMO_DATA=true)", added)
    else:
        log.info("Demo seeding disabled (SEED_DEMO_DATA=false)")
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Verifix — Parts & Inventory",
    version="1.0.0",
    description=(
        "Identify automotive components from photos, keep warehouse/bin inventory, "
        "and generate WhatsApp-ready promotional cards."
    ),
)

# Preview hosts are proxied under *.e2b.app, so keep CORS permissive but explicit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compress HTML/CSS/JS/JSON on the wire — noticeably faster over Tailscale or
# mobile data. Media (already compressed formats) is largely unaffected.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# Local authentication: no Supabase calls, no public self-registration.
# The login module also works as a drop-in for the auth-enabled Windows version.
app.middleware("http")(auth.guard)
app.include_router(auth.router)


# --- static media ----------------------------------------------------------
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")
app.mount("/media/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/media/exports", StaticFiles(directory=EXPORT_DIR), name="exports")


@app.head("/", include_in_schema=False)
def index_head() -> Response:
    """Health/uptime probes often use HEAD; answer it instead of 405."""
    return Response(status_code=200, media_type="text/html")


@app.head("/api/health", include_in_schema=False)
def health_head() -> Response:
    return Response(status_code=200, media_type="application/json")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    if not getattr(request.state, "auth_user", None):
        return RedirectResponse("/login", status_code=303)
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>UI not installed</h1>", status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    provider = vision.describe()
    return {
        "status": "ok",
        "vision_provider": provider["provider"],
        "vision_label": provider["label"],
        "grounding_active": provider["grounding_active"],
        "grounding_model": provider["grounding_model"],
        "providers_available": provider["available"],
        "shop": settings.shop_name,
        "whatsapp": settings.whatsapp_number,
        "currency": settings.currency,
        "require_verification": settings.require_verification,
        "database": db.database_info(),
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


@app.get("/api/demo-images", response_model=list[DemoImage])
def demo_images() -> list[DemoImage]:
    return [
        DemoImage(
            name=p.stem.replace("-", " ").title(),
            path=str(p.relative_to(BASE_DIR)) if p.is_relative_to(BASE_DIR) else str(p),
            url=f"/api/demo-image/{p.stem}",
        )
        for p in seed_images()
    ]


@app.get("/api/demo-image/{stem}")
def demo_image(stem: str) -> FileResponse:
    for path in seed_images():
        if path.stem == stem:
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="demo image not found")


# --- inventory -------------------------------------------------------------
@app.get("/api/parts", response_model=list[InventoryPart])
def list_parts(
    q: str = Query("", description="Search name, OEM number, bin, fitment"),
    in_stock: bool = False,
    limit: int = Query(200, ge=1, le=1000),
) -> list[InventoryPart]:
    return db.list_parts(q, in_stock_only=in_stock, limit=limit)


def _resolve_fitment(fitment: list, fitment_text: str) -> list:
    """Prefer the structured list; otherwise parse the free-text field."""
    if fitment:
        return fitment
    if fitment_text and fitment_text.strip():
        return importers.parse_fitment(fitment_text)
    return fitment


@app.post("/api/parts", response_model=InventoryPart, status_code=201)
def create_part(payload: PartCreate) -> InventoryPart:
    if not payload.part_name.strip():
        raise HTTPException(status_code=422, detail="part_name is required")
    payload.fitment = _resolve_fitment(payload.fitment, payload.fitment_text)
    if payload.oem_part_number:
        existing = db.find_by_number(payload.oem_part_number)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"OEM {payload.oem_part_number} already exists as part #{existing.id}",
            )
    return db.create_part(payload)


@app.get("/api/parts/{part_id}", response_model=InventoryPart)
def get_part(part_id: int) -> InventoryPart:
    part = db.get_part(part_id)
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.patch("/api/parts/{part_id}", response_model=InventoryPart)
def patch_part(part_id: int, payload: PartUpdate) -> InventoryPart:
    if payload.fitment_text is not None:
        parsed = _resolve_fitment([], payload.fitment_text)
        if parsed:
            payload.fitment = parsed
        payload.fitment_text = None
    try:
        part = db.update_part(part_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.get("/api/parts/{part_id}/image-candidates")
def image_candidates(part_id: int) -> dict:
    part = get_part(part_id)
    return {"part_id": part_id, "candidates": image_review.candidates_for(part),
            "source": "local catalogue", "requires_approval": True}


@app.post("/api/parts/{part_id}/image-approval", response_model=InventoryPart)
def approve_image(part_id: int, payload: ImageApproval) -> InventoryPart:
    part = get_part(part_id)
    candidate = next((c for c in image_review.candidates_for(part)
                      if c["candidate_id"] == payload.candidate_id), None)
    if candidate is None:
        raise HTTPException(status_code=409, detail="Candidate is no longer available. Refresh the image review.")
    # Deliberately leave OEM, fitment, quantity and verification untouched.
    updated = db.update_part(part_id, PartUpdate(image_path=candidate["image_path"]))
    if updated is None:
        raise HTTPException(status_code=404, detail="part not found")
    return updated


@app.post("/api/parts/{part_id}/image", response_model=InventoryPart)
async def upload_part_image(part_id: int, file: UploadFile = File(...)) -> InventoryPart:
    """Attach a normalized photo directly to an existing inventory record."""
    if db.get_part(part_id) is None:
        raise HTTPException(status_code=404, detail="part not found")
    mime = (file.content_type or "").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"unsupported image type: {mime or 'unknown'}")
    raw = await file.read(MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=422, detail="empty upload")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"image larger than {settings.max_upload_mb} MB")
    path, _normalised = _store_upload(raw, file.filename or "part-image.jpg")
    relative = str(path.relative_to(BASE_DIR)) if path.is_relative_to(BASE_DIR) else str(path)
    try:
        part = db.update_part(part_id, PartUpdate(image_path=relative))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.delete("/api/parts/{part_id}", status_code=204)
def delete_part(part_id: int) -> Response:
    if not db.delete_part(part_id):
        raise HTTPException(status_code=404, detail="part not found")
    # 204 must not carry a body: use Response, not JSONResponse(content=None),
    # which would serialise "null" and trip "Response content longer than
    # Content-Length" in the HTTP server.
    return Response(status_code=204)


@app.post("/api/parts/{part_id}/adjust", response_model=InventoryPart)
def adjust_stock(part_id: int, movement: StockMovement) -> InventoryPart:
    try:
        part = db.adjust_stock(
            part_id, movement.delta, reason=movement.reason, note=movement.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.get("/api/parts/{part_id}/movements")
def movements(part_id: int) -> list[dict]:
    if db.get_part(part_id) is None:
        raise HTTPException(status_code=404, detail="part not found")
    return db.movements_for(part_id)


@app.get("/api/lookup")
def lookup(number: str) -> InventoryPart:
    """Counter lookup: does this OEM / cross-reference sit on a shelf?"""
    part = db.find_by_number(number)
    if part is None:
        raise HTTPException(status_code=404, detail=f"no stock record for {number}")
    return part


# --- identification --------------------------------------------------------
THUMB_EDGE = 256
THUMB_QUALITY = 75
_THUMB_RE = re.compile(r"\d{8}-[0-9a-f]{10}\.jpg")

def _thumb_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.thumb.jpg")

def _write_thumbnail(source: Path, thumb: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=THUMB_QUALITY, optimize=True)
    tmp = thumb.with_name(f".{thumb.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(buffer.getvalue())
    os.replace(tmp, thumb)


def _store_upload(raw: bytes, filename: str) -> tuple[Path, bytes]:
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            # Normalise giant phone photos: 1600px long edge is plenty for ID.
            image.thumbnail((1600, 1600), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=92)
            normalised = buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=415, detail="file is not a readable image") from exc

    name = f"{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:10]}.jpg"
    path = UPLOAD_DIR / name
    tmp = path.with_name(f".{name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(normalised)
    os.replace(tmp, path)
    try:
        _write_thumbnail(path, _thumb_path(path))
    except OSError as exc:  # thumbnails are an optimisation; never fail the upload
        log.warning("Thumbnail generation failed for %s: %s", name, exc)
    return path, normalised


@app.get("/api/thumbs/{filename}", include_in_schema=False)
def part_thumbnail(filename: str) -> FileResponse:
    """Small cached thumbnail for the parts table.

    Inventory photos are 1600px JPEGs; the table only shows 46px thumbnails,
    so serving the full file wastes bandwidth on phones. Thumbnails are
    derived from the immutable upload (same content forever) and cached
    aggressively by the browser.
    """
    if not _THUMB_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="thumbnail not found")
    original = UPLOAD_DIR / filename
    thumb = _thumb_path(original)
    if not thumb.is_file():
        if not original.is_file():
            raise HTTPException(status_code=404, detail="thumbnail not found")
        try:
            _write_thumbnail(original, thumb)
        except (UnidentifiedImageError, OSError) as exc:
            raise HTTPException(status_code=404, detail="thumbnail not found") from exc
    response = FileResponse(thumb, media_type="image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@app.post("/api/identify", response_model=IdentifyResponse)
async def identify(request: Request, file: UploadFile = File(...)) -> IdentifyResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="empty upload")
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"image larger than {settings.max_upload_mb} MB"
        )
    mime = (file.content_type or "image/jpeg").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"unsupported image type: {mime}")

    path, normalised = _store_upload(raw, file.filename or "upload.jpg")
    report = vision.identify(normalised, "image/jpeg")

    matched, candidates = match_part(report)
    if matched is not None:
        # Never silently adopt the catalogue's numbers: say when they differ.
        if report.oem_part_number and matched.oem_part_number and (
            report.oem_part_number.upper() != matched.oem_part_number.upper()
        ):
            report.warnings.append(
                f"Model number {report.oem_part_number} differs from stock record "
                f"{matched.oem_part_number} (#{matched.id}) - confirm before quoting."
            )
    else:
        report.warnings.append(
            "No matching stock record found - add the part to inventory before generating a card."
        )

    image_path = str(path.relative_to(BASE_DIR)) if path.is_relative_to(BASE_DIR) else str(path)
    scan_id = db.save_scan(image_path, report.model_dump_json(), matched.id if matched else None)
    caption = cards.caption_for(matched) if matched else ""

    image_url = f"/media/uploads/{path.name}"
    base = links.resolve_base_url(_request_origin(request))
    lens, gimages = links.links_for(image_url, base)

    return IdentifyResponse(
        scan_id=scan_id,
        image_path=image_path,
        image_url=image_url,
        lens_url=lens,
        google_search_url=gimages,
        report=report,
        matched_part=matched,
        candidates=[m.part for m in candidates],
        report_markdown=render_report(report, matched, caption=caption),
    )


@app.get("/api/scans")
def scans(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    return [
        {
            "id": scan.id,
            "created_at": scan.created_at.isoformat() if scan.created_at else None,
            "image_url": _media_url(scan.image_path),
            "matched_part_id": scan.matched_part_id,
            "part_name": scan.report.part_name,
            "confidence": scan.report.overall_confidence.value,
            "provider": scan.report.provider,
        }
        for scan in db.list_scans(limit)
    ]


def _media_url(raw: str) -> str:
    """Map a stored path onto the mounted media URL."""
    if not raw:
        return ""
    if raw.startswith("/media/"):
        return raw
    path = Path(raw)
    try:
        if path.is_absolute():
            if path.is_relative_to(UPLOAD_DIR):
                return f"/media/uploads/{path.name}"
            if path.is_relative_to(EXPORT_DIR):
                return f"/media/exports/{path.name}"
            return f"/api/demo-image/{path.stem}"
        if raw.startswith("assets/seed/"):
            return f"/api/demo-image/{path.stem}"
        if raw.startswith("uploads/"):
            return f"/media/uploads/{path.name}"
        if raw.startswith("exports/"):
            return f"/media/exports/{path.name}"
    except ValueError:
        pass
    return ""


@app.get("/api/parts/{part_id}/report")
def part_report(part_id: int) -> dict:
    part = db.get_part(part_id)
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    report = IdentificationReport(
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
    caption = cards.caption_for(part)
    return {"markdown": render_report(report, part, caption=caption), "caption": caption}


def _request_origin(request: Request) -> str:
    """Reconstruct our public origin, honouring proxy headers."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host", "")
    scheme = forwarded_proto or request.url.scheme
    if not host:
        return ""
    return f"{scheme}://{host}"


# --- bulk import / export --------------------------------------------------
@app.post("/api/inventory/import")
async def import_parts(
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="Parse and report without writing anything"),
    update_existing: bool = Query(False, description="Merge matching OEM/alternate numbers and add imported quantities when explicitly enabled"),
    review_images: bool = Query(False, description="Defer photo paths for individual approval"),
    mark_verified: bool = Query(
        False, description="Treat the file as the shop's own verified records"
    ),
) -> dict:
    filename = (file.filename or "").lower()
    is_bundle = filename.endswith(".zip")
    limit = stock_bundle.MAX_UPLOAD if is_bundle else (64 if filename.endswith((".xlsx", ".pdf")) else 8) * 1024 * 1024
    raw = await file.read(limit + 1)
    if not raw:
        raise HTTPException(status_code=422, detail="empty file")
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"Stock file larger than {limit // (1024 * 1024)} MB")
    if filename and not filename.endswith((".csv", ".zip", ".xlsx", ".pdf")):
        raise HTTPException(status_code=415, detail="Upload CSV, stock ZIP, Excel .xlsx, or text-based PDF")
    bundle = stock_bundle.read_bundle(raw) if is_bundle else None
    upload = None
    try:
        if filename.endswith(".xlsx"):
            upload = stock_files.parse_xlsx(raw)
        elif filename.endswith(".pdf"):
            upload = stock_files.parse_pdf(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    csv_raw = bundle.csv_text if bundle else upload.csv_bytes if upload else raw
    # Embedded workbook/ZIP photos are candidates, never silently attached.
    defer_images = review_images or is_bundle or bool(upload and upload.photos)
    if (bundle or upload) and not dry_run:
        preview = importers.import_csv(csv_raw, dry_run=True, update_existing=update_existing,
                                       default_currency=settings.currency, defer_images=defer_images)
        if preview.errors:
            raise HTTPException(status_code=422, detail="Fix stock file row errors before applying")
    report = importers.import_csv(
        csv_raw, dry_run=dry_run, update_existing=update_existing,
        default_currency=settings.currency,
        default_verified=False if bundle else mark_verified,
        defer_images=defer_images,
    )
    payload = report.summary()
    images_pending = 0
    if not dry_run:
        for result in report.results:
            photo = None
            if bundle and result.row in bundle.images:
                photo = (bundle.image_names[result.row], bundle.images[result.row])
            elif upload and result.row in upload.photos:
                photo = upload.photos[result.row]
            if photo and result.part_id and result.status in {"created", "updated", "skipped"}:
                part = db.get_part(result.part_id)
                if part is not None:
                    path, _ = _store_upload(photo[1], photo[0])
                    image_review.register_upload(part, str(path), test_only=bool(bundle and bundle.test_only))
                    images_pending += 1
    if bundle:
        payload["bundle"] = {
            "images_matched": len(bundle.images), "images_missing": bundle.rows - len(bundle.images),
            "unused_images": bundle.unused_images, "test_only": bundle.test_only,
            "requires_approval": True,
        }
    payload["results"] = [
        {"row": r.row, "status": r.status, "part_name": r.part_name,
         "oem_part_number": r.oem_part_number, "part_id": r.part_id, "message": r.message,
         **({"image_filename": bundle.image_names.get(r.row, "")} if bundle else {})}
        for r in report.results[:500]
    ]
    payload["stats"] = db.stats()
    payload["images_found"] = len(upload.photos) if upload else 0
    payload["images_attached"] = 0
    payload["images_pending"] = images_pending
    return payload


@app.get("/api/inventory.csv", response_class=PlainTextResponse)
def export_parts() -> PlainTextResponse:
    csv_text = importers.export_csv()
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="inventory.csv"'},
    )


@app.get("/api/inventory.pdf")
def export_inventory_pdf() -> Response:
    pdf_bytes = inventory_pdf.build_inventory_pdf(db.list_parts(limit=100000))
    filename = f"parts-inventory-{datetime.now(ZoneInfo('Africa/Nairobi')).strftime('%Y-%m-%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/inventory-verification.xlsx")
def export_verification_report() -> Response:
    workbook = build_verification_workbook(db.list_parts(limit=100000))
    filename = f"parts-verification-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- promotional cards -----------------------------------------------------
def _card_context(request: CardRequest) -> tuple[InventoryPart, cards.CardContext]:
    profile = shop_profile.get_profile()
    branding = {
        "shop": profile["shop_name"],
        "tagline": profile["tagline"],
        "phone": profile["whatsapp_number"],
        "location": profile["location"],
    }
    if request.part_id is not None:
        part = db.get_part(request.part_id)
        if part is None:
            raise HTTPException(status_code=404, detail="part not found")
        if settings.require_verification and not part.verified:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Part #{part.id} is not verified. Confirm the OEM number, then "
                    f"PATCH /api/parts/{part.id} with {{\"verified\": true}} before publishing."
                ),
            )
        return part, cards.CardContext(
            part=part,
            headline=request.headline,
            image_path=request.image_path or None,
            **branding,
        )

    if request.part is None:
        raise HTTPException(status_code=422, detail="provide part_id or an inline part")

    payload = request.part
    preview = InventoryPart(
        id=None,
        part_name=payload.part_name,
        oem_part_number=payload.oem_part_number,
        alternate_numbers=payload.alternate_numbers,
        superseded_numbers=payload.superseded_numbers,
        fitment=payload.fitment,
        condition=payload.condition,
        bin_location=payload.bin_location,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        currency=payload.currency,
        category=payload.category,
        technical_notes=payload.technical_notes,
        image_path=payload.image_path,
        verified=payload.verified,
    )
    return preview, cards.CardContext(
        part=preview, headline=request.headline, image_path=request.image_path or None,
        **branding,
    )


@app.post("/api/cards", response_model=CardResult)
def generate_cards(request: CardRequest) -> CardResult:
    part, ctx = _card_context(request)

    stamp = datetime.now(timezone.utc)
    digest = hashlib.sha1(
        f"{part.id}:{part.part_name}:{part.unit_price}:{part.quantity}:{request.headline}".encode()
    ).hexdigest()[:8]
    assets: list[CardAsset] = []

    try:
        rendered = cards.render_all(ctx, request.formats)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("card render failed")
        raise HTTPException(status_code=500, detail=f"card render failed: {exc}") from exc

    for fmt_key, image in rendered:
        filename = f"{stamp:%Y%m%d}-{part.id or 'new'}-{fmt_key}-{digest}.png"
        out_path = EXPORT_DIR / filename
        cards.save_png(image, out_path)
        assets.append(
            CardAsset(
                format=fmt_key,
                width=image.width,
                height=image.height,
                filename=filename,
                url=f"/media/exports/{filename}",
            )
        )

    return CardResult(
        part=part,
        cards=assets,
        caption=request.caption or cards.caption_for(part, ctx),
        generated_at=stamp,
    )


@app.get("/api/cards/download/{filename}")
def download_card(filename: str) -> FileResponse:
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = EXPORT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="card not found")
    return FileResponse(path, filename=filename, media_type="image/png")


@app.post("/api/seed")
def reseed(force: bool = False) -> dict:
    added = seed_if_empty(force=force)
    return {"added": added, "stats": db.stats()}


@app.get("/api/config")
def public_config() -> dict:
    profile = shop_profile.get_profile()
    return {
        **profile,
        "formats": [
            {"key": f.key, "width": f.width, "height": f.height, "label": f.label}
            for f in cards.FORMATS.values()
        ],
        "max_upload_mb": settings.max_upload_mb,
        "vision": vision.describe(),
        "lens_supported": bool(links.resolve_base_url("")),
    }


@app.put("/api/settings")
def update_shop_settings(payload: ShopProfileUpdate, request: Request) -> dict:
    """Save local shop profile values; this endpoint is intentionally loopback-only."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Shop settings can only be changed from this computer")
    return shop_profile.save_profile(payload)


@app.get("/help/cloud-backups")
def cloud_backup_help() -> FileResponse:
    guide = BASE_DIR / "docs" / "CLOUD_BACKUPS.md"
    if not guide.is_file():
        raise HTTPException(status_code=404, detail="Cloud backup guide not found")
    return FileResponse(guide, media_type="text/markdown", filename="VERIFIX-cloud-backups.md")


@app.exception_handler(ValueError)
def value_error_handler(_request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _explain_database_problem(message: str) -> None:
    """Print a readable hint block instead of a bare traceback."""
    hint = (
        "\n"
        "┌─ Database not usable ──────────────────────────────────────────────\n"
        f"│ {message.splitlines()[0]}\n"
        "│\n"
        "│ Fix it with one of these:\n"
        "│   · start PostgreSQL (Windows: Start-Service postgresql-x64-18)\n"
        "│   · check DATABASE_URL in .env - see RUNNING.md step 3\n"
        '│   · missing driver:  pip install -r requirements-postgres.txt\n'
        "│   · no database server? unset DATABASE_URL to use SQLite instead\n"
        "│\n"
        "│ Diagnose:  python -m app.cli check-db\n"
        "└────────────────────────────────────────────────────────────────────\n"
    )
    if any(line.strip() for line in message.splitlines()[1:]):
        hint += "\n".join("  " + line for line in message.splitlines()[1:]) + "\n"
    print(hint, file=sys.stderr)


def main() -> None:  # pragma: no cover - console entry point
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()

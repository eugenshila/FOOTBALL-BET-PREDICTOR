"""WhatsApp promotional card renderer.

Design language: charcoal automotive theme, metallic-silver typography, restrained
red accents. Sizes are social-native so they never get re-compressed into mush:

    story      1080x1920   9:16   WhatsApp status / reels
    square     1080x1080   1:1    group posts, catalogue grid
    wide       1200x900    4:3    WhatsApp group posts / Facebook
    bin_label   600x300           printable shelf label (white, laser friendly)

Everything is drawn with Pillow - no headless browser, no network calls, so a
card renders in well under a second at the parts counter.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import BASE_DIR, FONT_DIR, settings
from .formatting import money, stock_label, truncate
from .models import InventoryPart

# --- theme -----------------------------------------------------------------
BG_TOP = (24, 26, 30)
BG_BOTTOM = (9, 10, 12)
PANEL = (30, 33, 38)
PANEL_EDGE = (58, 63, 72)
SILVER = (232, 235, 240)
SILVER_DIM = (156, 163, 173)
RED = (217, 43, 43)
RED_DEEP = (150, 26, 26)
GREEN = (46, 196, 126)
INK = (14, 15, 17)

FONT_BLACK = "BarlowCondensed-Black.ttf"
FONT_BOLD = "BarlowCondensed-Bold.ttf"
FONT_SEMI = "BarlowCondensed-SemiBold.ttf"
FONT_MED = "BarlowCondensed-Medium.ttf"
BODY_BOLD = "Barlow-Bold.ttf"
BODY_SEMI = "Barlow-SemiBold.ttf"
BODY_MED = "Barlow-Medium.ttf"
BODY = "Barlow-Regular.ttf"


@dataclass(frozen=True)
class Format:
    key: str
    width: int
    height: int
    label: str


FORMATS: dict[str, Format] = {
    "story": Format("story", 1080, 1920, "9:16 status"),
    "square": Format("square", 1080, 1080, "1:1 post"),
    "wide": Format("wide", 1200, 900, "4:3 post"),
    "bin_label": Format("bin_label", 600, 300, "bin label"),
}

SS = 4  # supersampling for rounded corners and glow


# --- font + drawing primitives --------------------------------------------
@lru_cache(maxsize=256)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / name
    if not path.exists():  # fall back to Pillow's default so nothing hard-fails
        return ImageFont.load_default(size)
    return ImageFont.truetype(str(path), size)


def _text_len(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, tracking: float = 0.0) -> float:
    if tracking:
        return sum(draw.textlength(ch, font=f) for ch in text) + tracking * max(len(text) - 1, 0)
    return draw.textlength(text, font=f)


def draw_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    tracking: float = 0.0,
) -> float:
    """Left-aligned text with optional letter spacing. Returns end x."""
    x, y = xy
    if not tracking:
        draw.text((x, y), text, font=f, fill=fill)
        return x + draw.textlength(text, font=f)
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tracking
    return x


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=f) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_name: str,
    *,
    max_width: float,
    max_lines: int,
    start_size: int,
    min_size: int = 28,
    tracking: float = 0.0,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink until the text fits both width and line budget."""
    size = start_size
    while size > min_size:
        f = font(font_name, size)
        lines = wrap(draw, text, f, max_width - tracking * 40)
        if len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= max_width for ln in lines):
            return f, lines
        size -= 3
    f = font(font_name, min_size)
    return f, wrap(draw, text, f, max_width)[:max_lines]


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    big = Image.new("L", (size[0] * SS, size[1] * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, size[0] * SS - 1, size[1] * SS - 1), radius=radius * SS, fill=255
    )
    return big.resize(size, Image.LANCZOS)


def vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    ramp = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    top_arr = np.array(top, dtype=np.float32)
    bottom_arr = np.array(bottom, dtype=np.float32)
    rows = top_arr + (bottom_arr - top_arr) * ramp
    arr = np.repeat(rows[:, None, :], w, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def radial_glow(
    size: tuple[int, int], colour: tuple[int, int, int], strength: float = 0.55
) -> tuple[Image.Image, Image.Image]:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    dist = np.sqrt(((xx - cx) / (w * 0.58)) ** 2 + ((yy - cy) / (h * 0.58)) ** 2)
    alpha = np.clip(1.0 - dist, 0, 1) ** 2 * strength * 255
    layer = Image.new("RGB", size, colour)
    return layer, Image.fromarray(alpha.astype(np.uint8), "L")


def add_grain(img: Image.Image, amount: float = 7.0) -> Image.Image:
    noise = Image.effect_noise(img.size, 18).convert("L")
    noise = Image.merge("RGB", (noise, noise, noise))
    return Image.blend(img, noise, amount / 100.0)


def sheen(img: Image.Image, band: float = 0.35) -> Image.Image:
    """A soft diagonal metallic highlight, low enough not to hurt text contrast."""
    w, h = img.size
    overlay = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(overlay)
    draw.polygon(
        [(-w * 0.1, h * band), (w * 0.55, -h * 0.1), (w * 0.8, -h * 0.1), (w * 0.05, h * band + h * 0.25)],
        fill=16,
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(w * 0.06))
    white = Image.new("RGB", (w, h), (255, 255, 255))
    return Image.composite(white, img, overlay.point(lambda v: v))


def chip(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    f: ImageFont.FreeTypeFont,
    *,
    fg: tuple[int, int, int],
    bg: tuple[int, int, int] | None = None,
    border: tuple[int, int, int] | None = None,
    pad: tuple[int, int] = (22, 12),
    radius: int = 999,
    tracking: float = 1.6,
) -> tuple[float, float]:
    x, y = xy
    width = _text_len(draw, text, f, tracking) + pad[0] * 2
    height = f.size + pad[1] * 2
    if bg or border:
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=min(radius, height // 2),
            fill=bg,
            outline=border,
            width=2 if border else 0,
        )
    draw_tracked(draw, (x + pad[0], y + pad[1] * 0.62), text, f, fg, tracking)
    return width, height


# --- hero image ------------------------------------------------------------
def _uniform_border(img: Image.Image) -> bool:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]], axis=0)
    return float(border.std(axis=0).mean()) < 12.0


def _grow(seed: np.ndarray, allowed: np.ndarray) -> np.ndarray:
    """4-connected region growing, constrained to ``allowed``. Vectorised."""
    region = seed & allowed
    padded = np.pad(region, 1, mode="constant", constant_values=False)
    for _ in range(allowed.shape[0] * 2):
        grown = (
            padded[1:-1, 1:-1]
            | padded[:-2, 1:-1]
            | padded[2:, 1:-1]
            | padded[1:-1, :-2]
            | padded[1:-1, 2:]
        ) & allowed
        if np.array_equal(grown, region):
            break
        region = grown
        padded = np.pad(region, 1, mode="constant", constant_values=False)
    return region


def _border_seeds(mask: np.ndarray) -> np.ndarray:
    seeds = np.zeros_like(mask, dtype=bool)
    seeds[0, :] = mask[0, :]
    seeds[-1, :] = mask[-1, :]
    seeds[:, 0] = mask[:, 0]
    seeds[:, -1] = mask[:, -1]
    return seeds


def cut_out(img: Image.Image) -> Image.Image:
    """Isolate the part from a flat studio backdrop so it floats on the dark card.

    Four steps, each fixing a failure mode seen on real product photos:
      1. colour-key against the border median        -> backdrop candidate
      2. keep only what is *connected to the border* -> don't erase bright metal
      3. keep only the component holding the centre  -> drop stray blobs
      4. drop low-saturation gray that sits under the
         part (contact shadow)                       -> no "paper cutout" puddle
    A hard, lightly blurred alpha edge avoids the white halo you get from
    ramping a low-resolution mask.
    """
    img = img.convert("RGB")
    full = np.asarray(img, dtype=np.float32)
    h, w = full.shape[:2]

    scale = 384 / max(h, w)
    small = np.asarray(
        img.resize((max(int(w * scale), 32), max(int(h * scale), 32)), Image.BILINEAR),
        dtype=np.float32,
    )
    sh, sw = small.shape[:2]

    border = np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]], axis=0)
    bg = np.median(border, axis=0)
    spread = float(np.percentile(np.linalg.norm(border - bg, axis=1), 90))
    tol = max(34.0, spread * 2.6)

    background_like = np.linalg.norm(small - bg, axis=2) < tol
    background = _grow(_border_seeds(background_like), background_like)

    coverage = float(background.mean())
    if coverage < 0.10 or coverage > 0.985:
        return img.convert("RGBA")  # no clean backdrop - keep the photo

    foreground = ~background

    # 3. keep the blob that owns the centre of mass of all foreground pixels
    ys, xs = np.nonzero(foreground)
    if len(xs) == 0:
        return img.convert("RGBA")
    cy, cx = int(np.median(ys)), int(np.median(xs))
    seed = np.zeros_like(foreground, dtype=bool)
    if not foreground[cy, cx]:
        # snap to the nearest foreground pixel
        d = (ys - cy) ** 2 + (xs - cx) ** 2
        idx = int(np.argmin(d))
        cy, cx = int(ys[idx]), int(xs[idx])
    seed[cy, cx] = True
    subject = _grow(seed, foreground)

    # 4. the faint contact shadow sits *below* the part and is barely darker
    #    than the backdrop; widen the tolerance there instead of colour-keying
    #    the part itself, so silver metal is never punched full of holes.
    lower = np.zeros_like(subject, dtype=bool)
    lower[int(sh * 0.55) :, :] = True
    near_backdrop = np.linalg.norm(small - bg, axis=2) < tol * 2.1
    subject &= ~(lower & near_backdrop)

    # 5. kill the glossy table highlight that hugs the base of the part: it is
    #    barely brighter than the seamless and would otherwise read as a
    #    chalky puddle. Restricted to the bottom band and to desaturated pixels
    #    so real metal on the part itself is left alone.
    bg_lum = float(np.mean(bg))
    lum_small = small.mean(axis=2)
    mx_s = small.max(axis=2)
    mn_s = small.min(axis=2)
    sat_s = (mx_s - mn_s) / np.maximum(mx_s, 1e-3)
    bottom_band = np.zeros_like(subject, dtype=bool)
    bottom_band[int(sh * 0.70) :, :] = True
    chalky = (sat_s < 0.13) & (lum_small > bg_lum * 0.94) & (lum_small < bg_lum * 1.22)
    subject &= ~(bottom_band & chalky)

    mask_small = Image.fromarray((subject * 255).astype(np.uint8), "L")
    mask = mask_small.resize((w, h), Image.BILINEAR)
    alpha = np.asarray(mask, dtype=np.uint8)
    # harden then feather: kills both stray mid-alpha and the bright rim
    alpha = np.where(alpha > 128, 255, 0).astype(np.uint8)
    alpha_img = Image.fromarray(alpha, "L").filter(ImageFilter.MinFilter(3))
    alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(1.0))
    out = img.convert("RGBA")
    out.putalpha(alpha_img)
    return out


def _trim(img: Image.Image) -> Image.Image:
    bbox = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def load_hero(path: Path, box: tuple[int, int], *, float_part: bool) -> Image.Image:
    """Return an RGBA hero image fitted inside `box`, preserving aspect."""
    with Image.open(path) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
    subject = cut_out(img) if (_uniform_border(img) and float_part) else img.convert("RGBA")
    subject = _trim(subject) if subject.getchannel("A").getextrema()[0] < 250 else subject
    subject.thumbnail(box, Image.LANCZOS)
    return subject


def _photo_frame(photo: Image.Image, size: tuple[int, int], radius: int = 26) -> Image.Image:
    """Turn any photo into a deliberate, premium product plate.

    Real counter staff shoot parts on a bench with a phone, so a hard cut-out is
    the wrong default: it fails on busy backgrounds and leaves halos on glossy
    surfaces. A tightly cropped, corner-rounded, vignetted plate always reads as
    intentional design and never lies about the part.
    """
    w, h = size
    framed = ImageOps.fit(photo, (w, h), Image.LANCZOS, centering=(0.5, 0.5))
    # settle the plate into the charcoal theme without hiding the part
    framed = Image.blend(framed, Image.new("RGB", (w, h), BG_TOP), 0.22)

    # corner vignette pulls the eye to the part and blends the plate into the card
    vignette = radial_glow((w, h), (0, 0, 0), strength=0.9)[1]
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    framed = Image.composite(framed, Image.blend(framed, dark, 0.55), vignette)

    # thin cool rim so the plate separates from the charcoal card
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        (1, 1, w - 2, h - 2), radius=radius, outline=(120, 128, 140, 190), width=2
    )
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(framed, (0, 0), rounded_mask((w, h), radius))
    out = Image.alpha_composite(out, edge)
    return out


def place_hero(
    canvas: Image.Image,
    path: Path | None,
    area: tuple[int, int, int, int],
    *,
    float_part: bool = False,
    glow: bool = True,
) -> None:
    """Draw the part photo into ``area``.

    ``float_part=True`` cuts the background out and floats the part on a dark
    plate - lovely for catalogue-style shots on seamless, fragile everywhere
    else, which is why it is opt-in.
    """
    x0, y0, x1, y1 = area
    w, h = x1 - x0, y1 - y0
    resolved = resolve_image_path(str(path)) if path else None
    if resolved is None:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=26, fill=PANEL, outline=PANEL_EDGE, width=2)
        f = font(FONT_MED, max(int(h * 0.06), 20))
        draw.text(((x0 + x1) / 2, (y0 + y1) / 2), "NO PART PHOTO", font=f, fill=SILVER_DIM, anchor="mm")
        return

    with Image.open(resolved) as raw:
        photo = ImageOps.exif_transpose(raw).convert("RGB")

    if float_part and _uniform_border(photo):
        hero = cut_out(photo)
        bbox = hero.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
        if bbox:
            hero = hero.crop(bbox)
        hero.thumbnail((int(w * 0.86), int(h * 0.9)), Image.LANCZOS)
        plate = ImageOps.fit(photo, (w, h), Image.LANCZOS)
        plate = plate.filter(ImageFilter.GaussianBlur(max(w * 0.04, 6)))
        plate = Image.blend(plate, Image.new("RGB", (w, h), BG_BOTTOM), 0.86)
        canvas.paste(plate, (x0, y0), rounded_mask((w, h), 26))
        px = x0 + (w - hero.width) // 2
        py = y0 + (h - hero.height) // 2
        canvas.paste(hero, (px, py), hero)
        ImageDraw.Draw(canvas).rounded_rectangle(
            (x0, y0, x1 - 1, y1 - 1), radius=26, outline=(46, 50, 58), width=2
        )
        return

    canvas.paste(_photo_frame(photo, (w, h)), (x0, y0), _photo_frame(photo, (w, h)))


# --- card composition ------------------------------------------------------
@dataclass
class CardContext:
    part: InventoryPart
    headline: str = ""
    image_path: Path | None = None
    shop: str = settings.shop_name
    tagline: str = settings.shop_tagline
    phone: str = settings.whatsapp_number
    location: str = settings.location

    def __post_init__(self) -> None:
        """Fall back to the inventory record's own photo when none is supplied."""
        if not self.image_path and self.part.image_path:
            self.image_path = resolve_image_path(self.part.image_path)
        elif self.image_path:
            self.image_path = resolve_image_path(str(self.image_path))


def _background(fmt: Format) -> Image.Image:
    canvas = vertical_gradient((fmt.width, fmt.height), BG_TOP, BG_BOTTOM)

    # faint technical grid
    grid = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    step = 90
    for x in range(0, fmt.width, step):
        gd.line([(x, 0), (x, fmt.height)], fill=(255, 255, 255, 9), width=1)
    for y in range(0, fmt.height, step):
        gd.line([(0, y), (fmt.width, y)], fill=(255, 255, 255, 9), width=1)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), grid).convert("RGB")
    canvas = sheen(canvas)
    return add_grain(canvas, 5.0)


def _header(draw: ImageDraw.ImageDraw, ctx: CardContext, fmt: Format, m: int) -> int:
    big = fmt.key == "story"
    name_size = 52 if big else 34
    f_name = font(FONT_BOLD, name_size)
    end = draw_tracked(draw, (m, m * 0.62), ctx.shop.upper(), f_name, SILVER, 3.0 if big else 2.0)

    # red block accent at the right of the wordmark
    draw.rectangle((end + 14, m * 0.62 + name_size * 0.28, end + 14 + 46, m * 0.62 + name_size * 0.28 + 8), fill=RED)

    f_tag = font(BODY_SEMI, 22 if big else 17)
    draw_tracked(draw, (m, m * 0.62 + name_size * 1.12), ctx.tagline.upper(), f_tag, SILVER_DIM, 2.2 if big else 1.6)

    rule_y = int(m * 0.62 + name_size * 1.12 + (44 if big else 34))
    draw.rectangle((m, rule_y, m + (140 if big else 90), rule_y + 5), fill=RED)
    draw.rectangle((m + (150 if big else 100), rule_y + 2, fmt.width - m, rule_y + 3), fill=(70, 74, 82))
    return rule_y + 5


def _cta_band(draw: ImageDraw.ImageDraw, ctx: CardContext, fmt: Format, m: int, height: int) -> None:
    y0 = fmt.height - height
    draw.rectangle((0, y0, fmt.width, fmt.height), fill=RED)
    draw.rectangle((0, y0, fmt.width, y0 + 6), fill=RED_DEEP)

    big = fmt.key == "story"
    label_size = 30 if big else 22
    phone_size = 62 if big else 42
    label = "WHATSAPP TO ORDER"
    f_label = font(FONT_SEMI, label_size)
    f_phone = font(FONT_BLACK, phone_size)

    # simple chat-bubble mark
    cx = m + 26
    cy = y0 + height / 2
    r = 30 if big else 22
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255), width=4)
    draw.polygon([(cx - 10, cy + r - 6), (cx + 2, cy + r - 6), (cx - 8, cy + r + 10)], fill=RED)

    text_x = m + (86 if big else 62)
    draw_tracked(draw, (text_x, y0 + (34 if big else 22)), label, f_label, (255, 240, 240), 2.6)
    draw.text((text_x - 2, y0 + (34 if big else 22) + label_size * 1.05), ctx.phone, font=f_phone, fill=(255, 255, 255))

    f_loc = font(BODY_MED, 20 if big else 16)
    draw.text(
        (fmt.width - m, y0 + height - (26 if big else 18)),
        ctx.location.upper(),
        font=f_loc,
        fill=(255, 226, 226),
        anchor="rs",
    )


def _price_block(
    draw: ImageDraw.ImageDraw,
    part: InventoryPart,
    xy: tuple[float, float],
    *,
    size: int,
    max_width: float,
) -> float:
    x, y = xy
    f_cur = font(BODY_SEMI, max(int(size * 0.3), 20))
    draw_tracked(draw, (x, y), part.currency.upper(), f_cur, SILVER_DIM, 3.0)
    amount = money(part.unit_price, "").strip() if part.unit_price else "ASK"
    f_amt = font(FONT_BLACK, size)
    draw.text((x - 4, y + f_cur.size * 0.95), amount, font=f_amt, fill=SILVER)
    return y + f_cur.size * 0.95 + f_amt.size * 1.06


def _availability(
    draw: ImageDraw.ImageDraw,
    part: InventoryPart,
    xy: tuple[float, float],
    *,
    size: int,
    anchor_right: bool = False,
) -> None:
    x, y = xy
    in_stock = part.in_stock()
    colour = GREEN if in_stock else RED
    label = stock_label(part.quantity)
    f = font(FONT_SEMI, size)
    r = 7
    if anchor_right:
        end_x = draw_tracked(draw, (0, -4000), label, f, colour, 2.4)  # measure off-canvas
        width = end_x
        start = x - width
        dot_x = start - 26
        draw_tracked(draw, (start, y), label, f, colour, 2.4)
        draw.ellipse((dot_x, y + size * 0.42, dot_x + r * 2, y + size * 0.42 + r * 2), fill=colour)
        return
    draw_tracked(draw, (x + 26, y), label, f, colour, 2.4)
    draw.ellipse((x, y + size * 0.42, x + r * 2, y + size * 0.42 + r * 2), fill=colour)


def _fitment_lines(part: InventoryPart, limit: int) -> list[str]:
    lines = []
    for fit in part.fitment[:limit]:
        head = " ".join(x for x in (fit.make, fit.model) if x)
        tail = " · ".join(x for x in (fit.engine, fit.years) if x)
        lines.append(f"{head} — {tail}" if tail else head)
    return lines


def _draw_fitment(
    draw: ImageDraw.ImageDraw,
    part: InventoryPart,
    xy: tuple[float, float],
    *,
    size: int,
    max_width: float,
    limit: int,
    max_lines: int = 4,
) -> float:
    x, y = xy
    f = font(BODY_MED, size)
    lines = _fitment_lines(part, limit)
    if not lines:
        return y
    drawn = 0
    for line in lines:
        for wrapped in wrap(draw, line, f, max_width - 30):
            if drawn >= max_lines:
                break
            draw.rectangle((x, y + size * 0.42, x + 12, y + size * 0.42 + 3), fill=RED)
            draw.text((x + 24, y), wrapped, font=f, fill=SILVER_DIM)
            y += size * 1.28
            drawn += 1
    if len(lines) > 1:
        pass
    return y


def _number_pill(
    draw: ImageDraw.ImageDraw,
    part: InventoryPart,
    xy: tuple[float, float],
    *,
    size: int,
    max_width: float,
) -> float:
    """Red 'OEM' tag + the part number in silver, laid out as one unit."""
    x, y = xy
    f_tag = font(FONT_BOLD, int(size * 0.62))
    tag_w = _text_len(draw, "OEM", f_tag, 2.0) + 28
    tag_h = f_tag.size + 14

    number = part.oem_part_number or "NUMBER NOT CONFIRMED"
    f_num = font(FONT_SEMI, size)
    while draw.textlength(number, font=f_num) > max_width - tag_w - 46 and f_num.size > 18:
        f_num = font(FONT_SEMI, f_num.size - 2)

    box_y0, box_y1 = y - 4, y + max(tag_h, f_num.size + 26) + 2
    draw.rounded_rectangle(
        (x, box_y0, x + max_width, box_y1), radius=(box_y1 - box_y0) / 2,
        fill=(38, 41, 47), outline=(72, 78, 88), width=2,
    )
    draw.rounded_rectangle(
        (x, box_y0, x + tag_w + 6, box_y1), radius=(box_y1 - box_y0) / 2, fill=RED
    )
    draw_tracked(draw, (x + 14, box_y0 + (box_y1 - box_y0 - f_tag.size) / 2 - 2), "OEM", f_tag, (255, 255, 255), 2.0)
    draw.text((x + tag_w + 22, box_y0 + (box_y1 - box_y0 - f_num.size) / 2 - 6), number, font=f_num, fill=SILVER)
    return box_y1


def _info_strip(
    draw: ImageDraw.ImageDraw,
    part: InventoryPart,
    xy: tuple[float, float],
    *,
    width: float,
    size: int = 24,
) -> float:
    """Three-column spec strip: condition, bin, cross-references."""
    x, y = xy
    cross_refs = len(part.alternate_numbers) + len(part.superseded_numbers)
    items = [
        ("CONDITION", part.condition.value),
        ("BIN", part.bin_location or "UNASSIGNED"),
        ("CROSS-REFS", f"{cross_refs} LISTED" if cross_refs else "NONE"),
    ]
    col = width / len(items)
    f_label = font(BODY_SEMI, max(int(size * 0.72), 14))
    f_value = font(FONT_SEMI, int(size * 1.22))
    for index, (label, value) in enumerate(items):
        cx = x + col * index
        if index:
            draw.rectangle((cx - 18, y + 4, cx - 16, y + size * 2.1), fill=(58, 62, 70))
        draw_tracked(draw, (cx, y), label, f_label, SILVER_DIM, 2.2)
        draw.text((cx, y + f_label.size * 1.35), truncate(value, 16), font=f_value, fill=SILVER)
    return y + f_label.size * 1.35 + f_value.size


def render_story(ctx: CardContext) -> Image.Image:
    fmt = FORMATS["story"]
    canvas = _background(fmt)
    draw = ImageDraw.Draw(canvas)
    m = 72
    top = _header(draw, ctx, fmt, m)
    hero_top = top + 34
    hero_h = 720
    place_hero(canvas, ctx.image_path, (m, hero_top, fmt.width - m, hero_top + hero_h))
    draw = ImageDraw.Draw(canvas)

    y = hero_top + hero_h + 52
    headline = ctx.headline or ctx.part.part_name
    f_title, lines = fit_text(
        draw, headline.upper(), FONT_BLACK, max_width=fmt.width - 2 * m,
        max_lines=3, start_size=104, min_size=54, tracking=0.5,
    )
    for line in lines:
        draw.text((m, y), line, font=f_title, fill=SILVER)
        y += f_title.size * 0.98
    y += 34

    y = _number_pill(draw, ctx.part, (m, y), size=40, max_width=fmt.width - 2 * m) + 34
    y = _draw_fitment(draw, ctx.part, (m, y), size=31, max_width=fmt.width - 2 * m, limit=2, max_lines=3)
    y += 18

    price_y = _price_block(draw, ctx.part, (m, y), size=92, max_width=fmt.width - 2 * m)
    _availability(draw, ctx.part, (m + 6, price_y + 4), size=29)
    _info_strip(draw, ctx.part, (m, price_y + 66), width=fmt.width - 2 * m, size=25)

    _cta_band(draw, ctx, fmt, m, 200)
    return canvas


def render_square(ctx: CardContext) -> Image.Image:
    """1:1 - portrait stack so it fills the frame the way a status post does."""
    fmt = FORMATS["square"]
    canvas = _background(fmt)
    draw = ImageDraw.Draw(canvas)
    m = 56
    top = _header(draw, ctx, fmt, m)

    hero_box = (m, top + 26, fmt.width - m, top + 26 + 372)
    place_hero(canvas, ctx.image_path, hero_box)
    draw = ImageDraw.Draw(canvas)

    y = hero_box[3] + 30
    width = fmt.width - 2 * m
    f_title, lines = fit_text(
        draw, (ctx.headline or ctx.part.part_name).upper(), FONT_BLACK,
        max_width=width, max_lines=2, start_size=74, min_size=40,
    )
    for line in lines:
        draw.text((m, y), line, font=f_title, fill=SILVER)
        y += f_title.size * 0.96
    y += 18
    y = _number_pill(draw, ctx.part, (m, y), size=30, max_width=width) + 16
    y = _draw_fitment(draw, ctx.part, (m, y), size=25, max_width=width, limit=1, max_lines=1)

    price_y = _price_block(draw, ctx.part, (m, y + 6), size=62, max_width=width)
    _availability(draw, ctx.part, (fmt.width - m - 6, y + 16), size=24, anchor_right=True)

    _cta_band(draw, ctx, fmt, m, 118)
    return canvas


def render_wide(ctx: CardContext) -> Image.Image:
    fmt = FORMATS["wide"]
    canvas = _background(fmt)
    draw = ImageDraw.Draw(canvas)
    m = 60
    top = _header(draw, ctx, fmt, m)

    hero_size = 512
    hero_box = (m, top + 34, m + hero_size, top + 34 + hero_size)
    place_hero(canvas, ctx.image_path, hero_box)
    draw = ImageDraw.Draw(canvas)

    x = hero_box[2] + 44
    width = fmt.width - m - x
    y = top + 44

    f_title, lines = fit_text(
        draw, (ctx.headline or ctx.part.part_name).upper(), FONT_BLACK,
        max_width=width, max_lines=3, start_size=60, min_size=30,
    )
    for line in lines:
        draw.text((x, y), line, font=f_title, fill=SILVER)
        y += f_title.size * 0.98
    y += 20
    y = _number_pill(draw, ctx.part, (x, y), size=26, max_width=width) + 18
    y = _draw_fitment(draw, ctx.part, (x, y), size=22, max_width=width, limit=2, max_lines=2)
    y += 10
    price_y = _price_block(draw, ctx.part, (x, y), size=60, max_width=width)
    _availability(draw, ctx.part, (x + 4, price_y + 2), size=22)

    _cta_band(draw, ctx, fmt, m, 110)
    return canvas


def render_bin_label(ctx: CardContext) -> Image.Image:
    """Printable shelf tag: high contrast, readable at arm's length."""
    fmt = FORMATS["bin_label"]
    canvas = Image.new("RGB", (fmt.width, fmt.height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    m = 26

    draw.rectangle((0, 0, fmt.width, 74), fill=(20, 22, 26))
    draw_tracked(draw, (m, 22), "BIN LOCATION", font(BODY_SEMI, 20), (170, 176, 186), 4.0)
    draw.text((fmt.width - m, 12), ctx.part.bin_location or "UNASSIGNED", font=font(FONT_BLACK, 52),
              fill=(255, 255, 255), anchor="ra")

    f_name, lines = fit_text(draw, ctx.part.part_name, FONT_BOLD, max_width=fmt.width - 2 * m,
                             max_lines=2, start_size=44, min_size=24)
    y = 88
    for line in lines:
        draw.text((m, y), line, font=f_name, fill=(16, 17, 20))
        y += f_name.size * 1.02

    draw.rectangle((m, y + 16, fmt.width - m, y + 19), fill=(217, 43, 43))
    y += 32
    draw.text((m, y), f"OEM {ctx.part.oem_part_number or '—'}", font=font(BODY_SEMI, 24), fill=(60, 62, 68))
    draw.text((fmt.width - m, y - 4), f"QTY {ctx.part.quantity}", font=font(FONT_BLACK, 40),
              fill=(16, 17, 20), anchor="ra")
    draw.text((m, y + 40), f"{ctx.part.condition.value} · {money(ctx.part.unit_price, ctx.part.currency)}",
              font=font(BODY_MED, 21), fill=(90, 93, 100))
    return canvas


RENDERERS = {
    "story": render_story,
    "square": render_square,
    "wide": render_wide,
    "bin_label": render_bin_label,
}


def render_card(ctx: CardContext, fmt_key: str) -> Image.Image:
    if fmt_key not in RENDERERS:
        raise ValueError(f"unknown card format: {fmt_key}")
    return RENDERERS[fmt_key](ctx)


def render_all(ctx: CardContext, formats: list[str] | None = None) -> list[tuple[str, Image.Image]]:
    keys = formats or ["story", "square", "wide"]
    return [(key, render_card(ctx, key)) for key in keys]


def save_png(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def to_png_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def resolve_image_path(raw: str) -> Path | None:
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate if candidate.exists() else None


def caption_for(part: InventoryPart, ctx: CardContext | None = None) -> str:
    """Ready-to-paste WhatsApp caption."""
    ctx = ctx or CardContext(part=part)
    fits = _fitment_lines(part, 3)
    lines = [
        f"*{part.part_name.upper()}*",
        "",
        f"🔧 OEM: *{part.oem_part_number or 'ask in chat'}*",
    ]
    if part.alternate_numbers:
        lines.append(f"🔁 Cross-refs: {', '.join(part.alternate_numbers[:3])}")
    if fits:
        lines.append("🚗 Fits:")
        lines.extend(f"   • {line}" for line in fits)
    lines.append("")
    lines.append(f"💰 *{money(part.unit_price, part.currency)}*")
    lines.append(f"📦 {stock_label(part.quantity).title()}")
    if part.condition.value != "New":
        lines.append(f"🏷️ Condition: {part.condition.value}")
    if part.bin_location:
        lines.append(f"📍 Shelf: {part.bin_location}")
    lines.extend(
        [
            "",
            f"👉 WhatsApp to order: {ctx.phone}",
            f"🏪 {ctx.shop} · {ctx.location}",
            "",
            "#SpareParts #AutoParts #" + "".join(
                w for w in truncate(part.part_name, 40).title() if w.isalnum()
            ),
        ]
    )
    return "\n".join(lines)


def random_angle() -> float:  # pragma: no cover - tiny helper kept for future use
    return random.uniform(-3, 3)

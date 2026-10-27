# Automotive Spare Parts Identification & Inventory Engine

Identify an automotive component from a photo, keep warehouse/bin stock accurate, and
generate WhatsApp-ready promotional cards — in one flow, on a phone, at the counter.

![Promo card sample](docs/samples/wheel-story.png)

---

## What it does

| Module | Implemented capability |
|---|---|
| **1 · Image capture & identification** | Upload/drag a photo or shoot it with the phone camera — identification starts automatically on upload. Returns part name + category, OEM number, alternate/cross-reference numbers, superseded revisions, vehicle fitment (make · model · engine · years), and the installation/service notes a fitter actually needs. With a Gemini key it also **searches Google** for the part number and lists the pages it used. |
| **2 · Inventory & warehouse** | Structured records: part name, OEM, cross-references, fitment, condition, warehouse bin, quantity, unit price. Full CRUD, search by name/OEM/bin/vehicle/cross-reference, stock adjustments with an audit trail for every quantity change. |
| **3 · WhatsApp promo generator** | Server-side rendered cards in **9:16 (1080×1920)**, **1:1 (1080×1080)**, **4:3 (1200×900)** plus a printable **bin label (600×300)**. Charcoal theme, metallic-silver type, restrained red accents, in-stock badge, price and a WhatsApp CTA. A copy-ready WhatsApp caption is generated alongside. |

The report the engine prints follows the required contract exactly:

```
### PART IDENTIFICATION REPORT
### WAREHOUSE & INVENTORY DETAILS
### WHATSAPP PROMOTIONAL POST GENERATOR
```

---

## Quick start

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. On first start the app creates its SQLite database and
seeds a **4-part demonstration catalogue** so every screen is usable immediately.

No API key is required to boot — see [Vision providers](#vision-providers).

### CLI

```bash
./.venv/bin/python -m app.cli list                       # inventory table
./.venv/bin/python -m app.cli lookup 5184894AA           # counter lookup by OEM/x-ref
./.venv/bin/python -m app.cli identify photo.jpg --cards # identify + render cards
./.venv/bin/python -m app.cli cards --part-id 2 --formats story,square
```

### Tests

```bash
./.venv/bin/python -m pytest tests -q      # 26 tests, ~18s
```

---

## Automatic identification on upload

Drop or shoot a photo and the engine identifies it immediately — no button press
needed (the *Auto-detect on upload* toggle is on by default; uncheck it to review
photos before spending API quota).

Three ways to get a real identification, in order of what I'd recommend:

### 1. Free AI + Google Search (recommended)

[Google AI Studio](https://aistudio.google.com/apikey) issues a **free Gemini API
key with no card**, and the free tier includes search grounding. With a key set,
the default provider becomes `gemini-grounded`:

```bash
echo 'GEMINI_API_KEY=your-key-here' >> .env
```

Each upload then does this in **one request**: the model looks at the photo,
runs up to four Google searches (`"<part> OEM number"`, `"<part> part number
<vehicle>"`), and returns the part number **with the pages it found it on**.
The UI shows those sources as clickable links under the report, so staff can
open the Mopar/RockAuto page and confirm before quoting.

Why this over a plain vision model: a vision model recognises *shapes* — a
searched number is tied to an actual catalogue page. Grounding is also what
keeps the model honest, because the prompt forbids reporting a number that no
result confirms (it must return a blank number instead).

Turn it off with `SEARCH_GROUNDING=false` (grounded requests cost more tokens
and count as searches), or pin a different model with `GROUNDING_MODEL`.

### 2. Google Lens, no key at all

Every scan returns a **Reverse image search** button that opens the uploaded
photo in Google Lens, which is genuinely strong on automotive parts. This costs
nothing and needs no account. It requires the photo to be reachable on the
public internet, so it appears whenever the app is served from a public host
(including this preview) or `PUBLIC_BASE_URL` is set.

This is also the honest fallback when no vision key is configured: the operator
gets a one-tap path to a real identification instead of a guess.

### 3. Local model, no cloud

Run [Ollama](https://ollama.com) with a vision model and it is detected
automatically — nothing leaves your network:

```bash
ollama pull llava          # then: VISION_PROVIDER=auto (or ollama)
```

### What "automatic detection" does *not* do

It identifies **parts**, not inventory: it never silently creates a stock record
or invents a price or bin. The scan produces a report, matches it against your
existing stock, and offers candidates for a human to accept.

## Vision providers

Identification is behind a provider interface. Pick one with `VISION_PROVIDER`:

| Provider | Env | Notes |
|---|---|---|
| `gemini-grounded` | `GEMINI_API_KEY` | **Default when a Gemini key exists.** Gemini vision + the `google_search` tool; returns cited sources and search queries. Free tier. |
| `openai` | `OPENAI_API_KEY` | Any OpenAI-compatible vision endpoint (also `OPENAI_BASE_URL` for proxies/vLLM). Uses strict JSON schema output. |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.0-flash` by default, JSON response mode. |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude vision, JSON extracted from the reply. |
| `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Fully local (`llava`), no cloud, no key. Probed at runtime — not assumed to be running. |
| `offline` | — | Default fallback. Compares the photo against your own catalogue imagery. |

`VISION_PROVIDER=auto` (the default) tries `gemini-grounded → openai → gemini →
anthropic → ollama` and falls back to `offline`. **A provider that errors at runtime also falls back to
offline and says so in the report** — the counter never hits a dead end.

> The offline matcher is a nearest-neighbour fingerprint over your catalogue photos,
> not a trained classifier. It is good at "this is the part I have a photo of" and it
> openly labels itself as low-confidence otherwise.

### Honesty guard rails

A wrong part number costs the customer a second trip, so the system is built to
refuse rather than guess:

- The prompt forbids inventing part numbers; a blank number is acceptable, a
  fabricated one is not.
- Model output is sanitised — sentence-like or over-long "numbers" are dropped.
- Every report carries **separate identification and part-number confidence**
  badges, and flags `needs_manual_review` unless both are high.
- When a model number disagrees with the matching stock record, the report says so
  instead of silently substituting either number.
- Cards for **unverified** records are blocked (`REQUIRE_PART_VERIFICATION=true`)
  until a human confirms the OEM number against the EPC.

---

## WhatsApp cards

Rendered with Pillow — no headless browser, no network call, roughly 1–2 s for all
three formats. The part photo is framed as a deliberately cropped, corner-rounded,
vignetted plate rather than colour-keyed onto the dark background: cut-outs fail on
busy backgrounds and leave halos on glossy metal, and misrepresenting the part on a
sales card is worse than a visible photo edge.

| Format | Size | Use |
|---|---|---|
| `story` | 1080×1920 | WhatsApp status, Reels |
| `square` | 1080×1080 | Group posts, catalogue grid |
| `wide` | 1200×900 | Group posts, Facebook |
| `bin_label` | 600×300 | Printable shelf tag |

Sample renders live in [`docs/samples/`](docs/samples).

---

## Architecture

```
web/                     Vanilla JS + CSS operator UI (no build step)
app/
  main.py                FastAPI routes: identify · inventory · cards · scans
  models.py              Pydantic schemas (report, part, fitment, card, stock movement)
  db.py                  SQLite persistence: parts, movements, scans
  catalog.py             Catalogue loading, seeding, report→stock matching
  report.py              Three-section markdown contract
  cards.py               Pillow card renderer (story/square/wide/bin_label)
  formatting.py          Currency, stock labels, truncation
  cli.py                 Counter CLI
  vision/
    __init__.py          Provider selection + graceful degradation
    prompt.py            Identification prompt + JSON schema + honesty rules
    llm.py               OpenAI / Gemini / Anthropic / Ollama providers
    offline.py           Reference-fingerprint matcher
data/catalog.json        Demo catalogue (see disclaimer below)
assets/seed/             Demo part photography
assets/fonts/            Barlow / Barlow Condensed (SIL OFL)
tests/                   API + pipeline tests
```

**Storage.** SQLite (WAL) with connection-per-operation — no ORM, no server. Parts,
every stock movement, and every scan are persisted; the DB, uploads and generated
cards are git-ignored so shop data never lands in the repository.

**Bulk data.** `importers.py` handles CSV in and out with fuzzy header mapping,
locale-tolerant number parsing and OEM-based dedupe.

**Matching.** `report → stock record` is scored on part-number overlap (dominant),
name tokens and fitment, with a 0.45 threshold. Candidates below the threshold are
returned for human selection instead of being auto-adopted.

---

## Loading your existing stock list

Your inventory almost certainly lives in a spreadsheet. Import it — no template
required, the importer maps common headers itself:

```bash
./.venv/bin/python -m app.cli import-csv stock.csv          # dry run (default)
./.venv/bin/python -m app.cli import-csv stock.csv --apply  # write
```

In the UI: **Inventory → ⇪ Import CSV** runs the dry run first, shows what would
happen, and only writes when you press *Apply import*. **⇩ Export** downloads the
current inventory in the same shape, so it round-trips.

What it handles from real-world files:

- any column order; header aliases (`Description`, `OEM No`, `Cross Refs`,
  `Shelf`, `On Hand`, `Retail`, `Notes`, …); unknown columns are listed, not fatal
- `,` `;` `\t` `|` delimiters, UTF-8 with BOM, and cp1252/latin-1 files
- prices as `KES 12,800.50`, `1.200,50`, `$45`, `46 500`
- free-text fitment: `Jeep Grand Cherokee 3.6L V6 2011-2021; Dodge Durango 5.7L 2011-2020`
  becomes structured make/model/engine/years
- dedupe by OEM number, falling back to the part name when there is no number, so
  re-importing the same file is safe
- bad rows are reported with row numbers and never abort the import

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/identify` | Upload a photo → report + matched stock + candidates |
| `GET` | `/api/parts?q=&in_stock=` | Search inventory |
| `POST` | `/api/parts` | Create a part |
| `GET`/`PATCH`/`DELETE` | `/api/parts/{id}` | Read / update / remove |
| `POST` | `/api/parts/{id}/adjust` | Stock movement (`delta`, `reason`) |
| `GET` | `/api/parts/{id}/movements` | Stock audit trail |
| `GET` | `/api/lookup?number=` | Counter lookup by OEM or cross-reference |
| `GET` | `/api/parts/{id}/report` | Re-print the stored report + caption |
| `POST` | `/api/cards` | Render promo cards (`part_id` or inline `part`) |
| `GET` | `/api/cards/download/{file}` | Download a rendered PNG |
| `POST` | `/api/inventory/import?dry_run=&update_existing=` | Import a CSV stock list |
| `GET` | `/api/inventory.csv` | Export inventory as CSV |
| `GET` | `/api/scans` | Scan history |
| `GET` | `/api/health` · `/api/config` · `/api/stats` | Runtime info |

Interactive docs: `/docs`.

---

## Configuration

Copy `.env.example` to `.env` and load it (or export the vars). Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `VISION_PROVIDER` | `auto` | `auto`/`openai`/`gemini`/`anthropic`/`ollama`/`offline` |
| `SHOP_NAME` | `SHILA AUTO PARTS` | Wordmark on every card |
| `WHATSAPP_NUMBER` | `+254 700 000 000` | Order CTA on cards and captions |
| `CURRENCY` | `KES` | Price formatting |
| `REQUIRE_PART_VERIFICATION` | `true` | Block cards for unverified records |
| `MAX_UPLOAD_MB` | `12` | Upload limit (images are downscaled to 1600 px) |

---

## Known limits (read before selling from this)

1. **The demo catalogue is illustrative.** `data/catalog.json` contains part numbers,
   prices and bin locations invented for demonstration. Replace it with your supplier/
   EPC data — the engine treats that file as *your* stock, not as an authority.
2. **No EPC integration.** Real OEM verification needs a parts database (Snap-on EPC,
   PartsTech, TecDoc, dealer portal). The engine never claims a number is correct; it
   says how confident it is and asks a human to confirm.
3. **OCR is incidental.** Printed numbers are read by the vision model when legible;
   there is no dedicated OCR pass. Etched or worn numbers will often come back blank —
   by design rather than a guess.
4. **Search grounding needs internet.** With no key and no network the app falls back
   to the offline matcher, which only recognises parts you already have photos of.
   Google Lens links also require a publicly reachable photo.
5. **Offline matching only knows your catalogue.** It cannot identify a part you have
   never photographed.
6. **Single-user by default.** There is no authentication. Put it behind a login/
   reverse proxy before exposing it beyond a trusted network.

---

## Roadmap

- Authentication + per-user audit trail
- Vehicle → compatible parts search ("what fits a 2014 Grand Cherokee 3.6")
- Purchase orders and supplier records, reorder points
- Dedicated OCR pass with per-character confidence for etched numbers
- Groq / OpenRouter free vision tiers as additional providers
- Automatic supplier-price lookup from grounded search results
- Native WhatsApp Business API push (today: rendered cards + copyable caption)

---

## Licence

Code: MIT. Fonts: Barlow / Barlow Condensed under the SIL Open Font License
(`assets/fonts/OFL.txt`).

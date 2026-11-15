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

PostgreSQL is the default engine. Create the database and user once, then start:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env                                # Windows: copy .env.example .env
psql -U postgres -f scripts/setup_postgres.sql      # creates partsid + partsid_user
# .env: set POSTGRES_PASSWORD (and POSTGRES_USER/POSTGRES_DB if you changed them)
./.venv/bin/python -m app.cli check-db              # verify the connection first
./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Use `DATABASE_URL=postgresql://user:pass@host:5432/dbname` instead of the
`POSTGRES_*` variables if you prefer one line; it wins over everything else.
The masked DSN and which variable it came from are printed by `check-db` and on
`/api/health`, so there is never any doubt which database is live.

Open <http://localhost:8000>. On first start the app creates its schema and seeds a
**4-part demonstration catalogue** so every screen is usable immediately.

The app is behind Supabase Auth, so the first request redirects to `/login`:

1. `.env` must contain `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` — copying
   `.env.example` already gives working values (see [Supabase Auth setup](#supabase-auth-setup)).
   Without them, `/login` loads but every sign-in returns
   `503 Supabase Auth is not configured`.
2. On the login page click **Create account**, then sign in with that email/password.
   If the Supabase project requires email confirmation, confirm the email first and
   then sign in.

No vision API key is required to boot — with none configured the app falls back to the
offline reference matcher. Set `GEMINI_API_KEY` in `.env` for real identification with
Google Search citations; see [Vision providers](#vision-providers).

**No database server? Opt into the file-backed engine** — nothing else changes:

```bash
DB_ENGINE=sqlite ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# or set DB_ENGINE=sqlite in .env / run `make run-sqlite`
```

That keeps everything in `data/inventory.db`. It is the right choice for a trial or a
single-user laptop, not for a counter with several tills. The two engines are never
mixed automatically: the app always uses the one you configured and fails loudly if it
cannot reach it.

**Windows users: follow [RUNNING.md](RUNNING.md)** — it covers creating the database,
the `.env` lines, and the usual connection errors.

> **Ports:** the app listens on **8000**; PostgreSQL stays on its default **5432**.
> The app connects to Postgres through `DATABASE_URL`/`POSTGRES_*`, so nothing needs
> to move ports and 5432 is not a web page.

### CLI

```bash
./.venv/bin/python -m app.cli list                       # inventory table
./.venv/bin/python -m app.cli lookup 5184894AA           # counter lookup by OEM/x-ref
./.venv/bin/python -m app.cli identify photo.jpg --cards # identify + render cards
./.venv/bin/python -m app.cli cards --part-id 2 --formats story,square
./.venv/bin/python -m app.cli import-csv stock.csv       # CSV dry run, then --apply
./.venv/bin/python -m app.cli export-csv --out stock.csv # round-trip export
./.venv/bin/python -m app.cli check-db                   # diagnose the database
./.venv/bin/python -m app.cli check-providers            # one live call per provider
```

`check-db` reports the engine, the masked DSN, the server version and row counts, and
explains the usual failures (service down, missing database, missing driver).
`check-providers` makes exactly one live call per configured provider — with latency,
the part it identified, and for grounded providers the number of web sources — so you
can prove a key works before the shop opens. Neither writes to the database.

### Tests

There is also a **browser-level smoke test** that loads the real page and drives
every UI flow in jsdom (identify → report → citations → inventory search → card
generation → CSV import → new-part drawer):

```bash
npm install jsdom && node scripts/ui_smoke.cjs   # server on :8000
```

API tests:

```bash
./.venv/bin/python -m pytest tests -q                       # SQLite (no server needed)
TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/partsid_test \
  ./.venv/bin/python -m pytest tests -q                     # the same suite on Postgres
```

The suite runs on either engine; on PostgreSQL a few engine-specific tests also run.
The API tests assume an authenticated user, so they replace the Supabase session
check with a stub (see `tests/conftest.py`) — nothing else in the auth layer is
touched.

### Importing stock lists in the web app

The **Stock → Import stock file** button accepts CSV, Excel `.xlsx`, and PDFs with
selectable text and a recognizable table header. In Excel, use a `Part Name` (or
`Description`) column and put at most one inserted picture in the `Photo` column
on each part's row. The dry run reports the row counts and embedded photo count;
photos are saved and attached when you apply the import. Insert pictures in the
`Photo` cells using the ordinary **Insert → Pictures** workflow and align each
picture to its part's row. If parts already exist,
enable **Update matching OEM records** before selecting the file to attach their
photos to matching OEM records. That option can replace existing part details.

PDF import reads selectable table text. A scanned PDF needs OCR first, and photos
should be embedded in an Excel `.xlsx` file rather than a PDF.

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
  db.py                  Portable persistence: parts, movements, scans
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

**Storage.** Runs on **PostgreSQL** (default) or **SQLite** (explicit
`DB_ENGINE=sqlite`, zero setup, WAL) — the same code runs on either. `app/storage.py` is the only place
that knows the difference: it translates `?` → `%s`, maps `AUTOINCREMENT` to identity
columns, turns `lastrowid` into `INSERT … RETURNING id`, picks `ILIKE` over `LIKE`,
and keeps a small connection pool for Postgres. The repository layer (`app/db.py`)
writes portable SQL and never branches on engine. Parts, every stock movement and
every scan are persisted; the database files, uploads and generated cards are
git-ignored so shop data never lands in the repository.

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
happen, and only writes when you press *Apply import*. **⇩ CSV Export** downloads
the current inventory in the same shape, so it round-trips. The **Reports** tab
shows verified/unverified totals and downloads a workbook with separate
**Verified** and **Not Verified** sheets.

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
| `GET` | `/api/inventory-verification.xlsx` | Download verified and unverified inventory on separate Excel sheets |
| `POST` | `/api/inventory/import?...&mark_verified=true` | Import and mark rows verified |
| `GET` | `/api/scans` | Scan history |
| `GET` | `/api/health` · `/api/config` · `/api/stats` | Runtime info (health includes database dialect/version) |
| `HEAD` | `/` · `/api/health` | Liveness probes (no 405s for uptime monitors) |

Interactive docs: `/docs`.

---

## Configuration

Copy `.env.example` to `.env` and load it (or export the vars). Highlights:

| Variable | Default | Meaning |
|---|---|---|
| `DB_ENGINE` | `postgres` | `postgres` or `sqlite` — which engine to use |
| `DATABASE_URL` | *(empty)* | Full DSN, overrides `DB_ENGINE`/`POSTGRES_*` |
| `POSTGRES_HOST` / `_PORT` | `localhost` / `5432` | Where the server listens |
| `POSTGRES_DB` / `_USER` | `partsid` / `partsid_user` | Database and role |
| `POSTGRES_PASSWORD` | *(empty)* | Leave empty to use `PGPASSWORD` or `~/.pgpass` |
| `VISION_PROVIDER` | `auto` | `auto`/`openai`/`gemini`/`anthropic`/`ollama`/`offline` |
| `SHOP_NAME` | `SHILA AUTO PARTS` | Wordmark on every card |
| `WHATSAPP_NUMBER` | `+254 700 000 000` | Order CTA on cards and captions |
| `CURRENCY` | `KES` | Price formatting |
| `REQUIRE_PART_VERIFICATION` | `true` | Block cards for unverified records |
| `SEED_DEMO_DATA` | `true` | Seed the demo catalogue into an empty database |
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
6. **Authentication uses Supabase Auth.** Configure `SUPABASE_URL` and
   `SUPABASE_PUBLISHABLE_KEY` as described below.

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


## Supabase Auth setup

The connected Supabase project is configured through `SUPABASE_URL` and the publishable key in `.env.example`. Copy those values into `.env` (or set them as deployment environment variables). Publishable keys are safe for this client integration; never use a Supabase secret or `service_role` key here.

In Supabase Dashboard → Authentication → URL Configuration, set the Site URL to your app origin and add that origin to Redirect URLs. Enable email/password sign-in and decide whether email confirmation should be required. When confirmation is required, a new user must confirm their email before signing in. Use HTTPS in production. The app stores sessions in HTTP-only cookies, refreshes them, and protects the app, API, and private media routes.

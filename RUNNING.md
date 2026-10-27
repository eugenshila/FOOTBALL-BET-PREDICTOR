# Running the app

Two ports, two different things — this trips people up, so first:

| What | Port | Address in your browser |
|---|---|---|
| **The app** (this project) | **8000** | <http://localhost:8000> |
| **PostgreSQL** (database server) | **5432** | *not a web page* — the app talks to it |

You asked for "local on server 8000": that is the app. PostgreSQL keeps its own
default port 5432 and the app connects to it using the `DATABASE_URL` setting.
Neither needs changing, and nothing has to be "opened in a browser" on 5432.

---

## 0. Prerequisites

- **Python 3.10 or newer** — check with `python --version` (Windows) or `python3 --version`.
- **PostgreSQL 13+** — you already have it. Confirm with `psql --version`.
- No Node.js, no Docker.

All commands below are for **Windows PowerShell** (your PC). macOS/Linux notes are
inline where they differ.

---

## 1. Install the project (once)

```powershell
cd C:\path\to\FOOTBALL-BET-PREDICTOR

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-postgres.txt    # only if you will use PostgreSQL
```

> If PowerShell blocks the activate script, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
>
> macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate`

---

## 2. Create the database (once)

Open a terminal and run `psql` as the Postgres superuser. On Windows this is
usually:

```powershell
psql -U postgres
```

If it asks for a password, use the one you chose when installing PostgreSQL.
If `psql` is not found, add PostgreSQL's `bin` folder to your PATH — typically
`C:\Program Files\PostgreSQL\17\bin`.

Once you are at the `postgres=#` prompt, paste these one at a time:

```sql
CREATE DATABASE partsid;
CREATE USER partsid_user WITH PASSWORD 'change-this-password';
GRANT ALL PRIVILEGES ON DATABASE partsid TO partsid_user;
\c partsid
GRANT ALL ON SCHEMA public TO partsid_user;
\q
```

The last two lines matter on PostgreSQL 15+ (they grant table-creation rights on
the default schema). Without them you get `permission denied for schema public`.

**You do not need to create any tables.** The app creates them itself on first start.

---

## 3. Point the app at your database (once)

Create a file named `.env` in the project root (copy `.env.example` and edit it).
The two lines that matter:

```ini
DATABASE_URL=postgresql://partsid_user:change-this-password@localhost:5432/partsid
GEMINI_API_KEY=
```

- If your password contains `@ : / ?` characters, URL-encode them
  (`@` → `%40`), or the URL will be parsed wrongly.
- Leave `GEMINI_API_KEY` empty for now — the app still works, using the offline
  matcher. See step 6 to turn on real identification.

The app reads `.env` automatically if you run it through the helper below, or you
can set the variable yourself:

```powershell
$env:DATABASE_URL = "postgresql://partsid_user:change-this-password@localhost:5432/partsid"
```

---

## 4. Verify the connection

```powershell
python -m app.cli check-db
```

Expected output:

```
engine   : postgres
dsn      : postgresql://partsid_user:***@localhost:5432/partsid  (DATABASE_URL)
server   : PostgreSQL 16.2
status   : OK
contents : 4 parts · 47 units · 0 scans
```

The `4 parts` are the demo catalogue the app seeds on first start. Delete them
when you import your real stock (they are ordinary rows — Inventory → Delete).

If this fails, jump to **Troubleshooting**.

---

## 5. Start the app

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Leave that window open. Then open <http://localhost:8000> in your browser.

- `--host 0.0.0.0` makes it reachable from your phone on the same Wi-Fi
  (`http://<your-pc-ip>:8000`) — useful for testing the camera capture.
- Add `--reload` while developing so edits apply automatically.
- Stop with **Ctrl+C**.

First page load should show a green-ish provider badge and `4 parts · 47 units`
in the top-right. If the badge says *Offline reference matcher*, that is normal
until you add a key.

---

## 6. Turn on real identification (optional, free)

1. Get a free Gemini API key at <https://aistudio.google.com/apikey> (no card).
2. Put it in `.env`: `GEMINI_API_KEY=AIza...`
3. Restart the app.

Now uploads are identified by Gemini **with Google Search**: it searches for the
OEM number and the report lists the pages it used, so you can click through and
confirm.

Check it worked:

```powershell
python -m app.cli check-providers
```

```
[OK  ] Gemini + Google Search        1840 ms → PCV Valve · OEM 5184894AA (high) · web 4 source(s)
[OK  ] Offline reference matcher       41 ms → PCV Valve · OEM 5184894AA (high)
```

If the first line says `FAIL`, the message tells you why (bad key, quota,
region). The app still works — it falls back automatically.

---

## 7. Everyday use

| Task | How |
|---|---|
| Identify a part | **Identify** tab → drop a photo (it starts on its own) |
| Load your stock list | **Inventory** → `⇪ Import CSV` → review → *Apply import* |
| Publish a WhatsApp card | **Promo studio** → pick part → *Generate cards* → download |
| Look up a number at the counter | `python -m app.cli lookup 5184894AA` |
| Add stock received | `＋1 stock` on a matched part, or `POST /api/parts/{id}/adjust` |
| Back up everything | `pg_dump -U partsid_user partsid > backup.sql` |

Seed the database from your spreadsheet **before** going live:

```powershell
python -m app.cli import-csv stock.csv            # dry run, shows what would happen
python -m app.cli import-csv stock.csv --apply    # write it
```

---

## 8. Replacing the demo data with your real stock

The app seeds 4 demonstration parts on first start. When you are ready to go
live, replace them in one command:

```powershell
python -m app.cli load-stock stock.csv --replace
```

What it does:

1. **parses the whole file first** — if the file cannot be read, if no row has a
   part name, or if more than 25% of rows fail, it stops and changes *nothing*;
2. shows how many existing parts will be deleted and asks you to confirm
   (add `--yes` to skip the prompt in a script);
3. deletes the old stock, imports the new file, and prints the result.

If the file is your own authoritative record and you do not want to tick each
part off by hand, add `--verified`:

```powershell
python -m app.cli load-stock stock.csv --replace --verified
```

That is the only thing standing between a new part and a published card, so only
use it once you have checked the numbers — see *known issues* in the handover.

Then make sure the demo catalogue cannot come back:

```ini
# .env
SEED_DEMO_DATA=false
```

Without that line, an emptied database is re-seeded with demo parts the next time
the app starts.

Other options:

| Command | Effect |
|---|---|
| `load-stock stock.csv` | merge into existing stock (no deletion) |
| `load-stock stock.csv --verified` | also mark the rows verified (your own confirmed records) |
| `load-stock stock.csv --replace --include-scans` | also clear scan history |
| `reset-db --yes` | delete all inventory, keep nothing |
| `reset-db --yes --reseed` | delete everything, put the demo parts back |
| `export-csv --out backup.csv` | back up first — do this before `--replace` |

**Always back up before a replace:** `pg_dump -U partsid_user partsid > backup.sql`
(or `python -m app.cli export-csv --out backup.csv` for a spreadsheet copy).

## 9. Where things live

| Item | Location |
|---|---|
| Inventory database | your PostgreSQL server, database `partsid` |
| Uploaded photos | `uploads/` |
| Generated cards | `exports/` |
| Settings | `.env` |
| Demo catalogue | `data/catalog.json` |

`uploads/`, `exports/` and the SQLite file are git-ignored — your shop data never
ends up in the repository.

---

## Troubleshooting

| Message | Fix |
|---|---|
| `Cannot reach PostgreSQL ... connection refused` | The service isn't running. Windows: `Get-Service *postgres*` then `Start-Service postgresql-x64-17`. macOS: `brew services start postgresql`. Linux: `sudo systemctl start postgresql` |
| `password authentication failed` | Wrong password in `DATABASE_URL`, or you used a different user. Reset: `psql -U postgres -c "ALTER USER partsid_user WITH PASSWORD 'new';"` |
| `database "partsid" does not exist` | Step 2 was skipped — run `CREATE DATABASE partsid;` |
| `permission denied for schema public` | Run the two `GRANT` lines (including `\c partsid` first) from step 2 |
| `PostgreSQL support needs psycopg` | `pip install -r requirements-postgres.txt` |
| `Unsupported DATABASE_URL scheme` | Typo in the URL — it must start `postgresql://` |
| `[Errno 10048] / address already in use` when starting | Port 8000 is taken. Either stop the other process, or run on another port: `--port 8001` |
| Badge says *Offline reference matcher* | No vision key. Expected until step 6 |
| `check-providers` shows `FAIL ... 400` | The key is valid but the model/region rejects search grounding. Try `GROUNDING_MODEL=gemini-2.0-flash`, or set `SEARCH_GROUNDING=false` to use plain vision |
| "Reverse image search" button missing | Google Lens needs a *publicly reachable* photo, so it is hidden on `localhost`. Set `PUBLIC_BASE_URL=https://your-domain` (or use a tunnel) to enable it |
| Everything is slow on first upload | First call loads the model / warms caches; later uploads are ~1–3 s |

---

## Prefer no database server at all?

Remove `DATABASE_URL` (or leave it empty) and the app uses SQLite at
`data/inventory.db`. Zero setup, same features, single user. Good for trying the
app; PostgreSQL is the better choice for a real shop (concurrent tills,
backups, remote access).

---

## Testing what is actually on screen

The API tests do not prove the page works. There is a browser-level smoke test
that loads the real page and `app.js` in jsdom, then drives every flow — upload
and identify, the report, citations, inventory search, card generation and
download, CSV dry-run + apply, the new-part drawer, and the unverified-part gate:

```powershell
npm install jsdom                 # once (needs Node.js)
node scripts/ui_smoke.cjs         # server must be running on :8000
```

It prints `PASS`/`FAIL` per check, exits non-zero on failure, and cleans up the
test parts it creates. Set `BASE=http://localhost:8001` to point it elsewhere.

## Running the tests

```powershell
python -m pytest tests -q                      # SQLite
$env:TEST_DATABASE_URL = "postgresql://partsid_user:pw@localhost:5432/partsid_test"
python -m pytest tests -q                      # the same suite against PostgreSQL
```

Create `partsid_test` first (`CREATE DATABASE partsid_test;`) — the suite drops
and recreates its own tables inside it.

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
pip install -r requirements.txt             # includes psycopg, the Postgres driver
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
If `psql` is not found, run it by its full path or add PostgreSQL's `bin` folder
to your PATH — for PostgreSQL 18, typically
`C:\Program Files\PostgreSQL\18\bin`.

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
PostgreSQL is the default engine, so the lines that matter are:

```ini
DB_ENGINE=postgres
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=partsid
POSTGRES_USER=partsid_user
POSTGRES_PASSWORD=change-this-password
GEMINI_API_KEY=
```

- One line does the same job if you prefer a DSN; it overrides the `POSTGRES_*`
  values above:
  `DATABASE_URL=postgresql://partsid_user:change-this-password@localhost:5432/partsid`
- If your password contains `@ : / ?` characters, URL-encode them
  (`@` → `%40`), or the URL will be parsed wrongly. With `POSTGRES_PASSWORD`
  the encoding is done for you.
- Leave `POSTGRES_PASSWORD` empty to source the password from `PGPASSWORD` or
  `%APPDATA%\postgresql\pgpass.conf` instead of storing it in `.env`.
- Leave `GEMINI_API_KEY` empty for now — the app still works, using the offline
  matcher. See step 6 to turn on real identification.
- Keep `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` from `.env.example`. They are
  what the login page uses; with them blank, `/login` loads but signing in returns
  `503 Supabase Auth is not configured`.

The app reads `.env` automatically on import, or you can set the variables yourself:

```powershell
$env:POSTGRES_PASSWORD = "change-this-password"
# or, as one line:
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

The first page you see is `/login`. Click **Create account** to register an
email/password against the Supabase project from `.env`, then sign in. If that
project requires email confirmation, confirm the email first — otherwise sign-in
reports that the account is not confirmed yet. Sessions are stored in HTTP-only
cookies, so you stay signed in across restarts.

- `--host 0.0.0.0` makes it reachable from your phone on the same Wi-Fi
  (`http://<your-pc-ip>:8000`) — useful for testing the camera capture.
- Add `--reload` while developing so edits apply automatically.
- Stop with **Ctrl+C**.

### Start automatically after Windows sign-in

PostgreSQL is configured to start automatically on this PC. To start the app
when your Windows account signs in, run this once from PowerShell in the project
folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_app_startup_task.ps1
```

The task waits up to two minutes for PostgreSQL, then starts the app in the
background on port 8000. It skips launch if that port is already in use. Check
`data\app-startup.log` for startup status and `data\app-uvicorn-error.log` for
app errors. To remove automatic startup, run:

```powershell
Unregister-ScheduledTask -TaskName "VERIFIX App Startup" -Confirm:$false
```

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

---

## 8b. Restoring a backup (getting your old stock back)

Two kinds of file can be restored:

| File | Made by | Restore with |
|---|---|---|
| `*.dump` (custom format, starts with `PGDMP`) | `pg_dump -Fc` — includes `backups/partsid_backup.dump` in this repo | `pg_restore` |
| `*.sql` (plain text) | `pg_dump partsid > backup.sql` | `psql -f backup.sql` |

The app never deletes or rewrites rows on startup — it only creates missing tables
(`CREATE TABLE IF NOT EXISTS`). So a database that still holds your old stock is
picked up as-is; restoring is only needed when the dump file is the only copy left.

**Inspect the file first** (lists tables and data entries):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -l .\backups\partsid_backup.dump
```

**Restore into a scratch database before touching `partsid`:**

```powershell
$env:PGPASSWORD = "change-this-password"
$pg = "C:\Program Files\PostgreSQL\18\bin"

& "$pg\psql.exe" -U postgres -d postgres -c "CREATE DATABASE partsid_restore"
& "$pg\pg_restore.exe" --no-owner --no-privileges -d "postgresql://partsid_user:change-this-password@localhost:5432/partsid_restore" .\backups\partsid_backup.dump

# prove the app reads it, without touching the live database
$env:DATABASE_URL = "postgresql://partsid_user:change-this-password@localhost:5432/partsid_restore"
python -m app.cli check-db
python -m app.cli list
```

Happy with the numbers? Restore over the live database. `--clean --if-exists` drops
the existing tables first, which is what avoids "relation parts already exists":

```powershell
Remove-Item Env:\DATABASE_URL
& "$pg\pg_restore.exe" --no-owner --no-privileges --clean --if-exists -d "postgresql://partsid_user:change-this-password@localhost:5432/partsid" .\backups\partsid_backup.dump
python -m app.cli check-db
```

Then start the app as usual. If the restore brought real stock with it, also set
`SEED_DEMO_DATA=false` in `.env` so a later wipe does not re-seed demo parts.

`sqlite:///` DSNs are not affected by any of this; `pg_restore` only ever talks to
PostgreSQL.

> **"unsupported version (1.16) in file header"** means the `pg_restore` you ran is
> older than the `pg_dump` that wrote the file. Use the executable from your
> PostgreSQL installation (`C:\Program Files\PostgreSQL\18\bin`), not one from an
> older client or a bundled tool.

## 9. Where things live

| Item | Location |
|---|---|
| Inventory database | your PostgreSQL server, database `partsid` |
| Uploaded photos | `uploads/` |
| Generated cards | `exports/` |
| Settings | `.env` |
| Demo catalogue | `data/catalog.json` |
| Seed backup shipped with the repo | `backups/partsid_backup.dump` (restore: step 8b) |

`uploads/`, `exports/` and the SQLite file are git-ignored — your shop data never
ends up in the repository.

---

## Troubleshooting

| Message | Fix |
|---|---|
| `Cannot reach PostgreSQL ... connection refused` | The service isn't running. Windows: check `Get-Service *postgres*`, then start the matching service (this laptop uses `Start-Service postgresql-x64-18`). macOS: `brew services start postgresql`. Linux: `sudo systemctl start postgresql` |
| `password authentication failed` | Wrong password in `DATABASE_URL`, or you used a different user. Reset: `psql -U postgres -c "ALTER USER partsid_user WITH PASSWORD 'new';"` |
| `database "partsid" does not exist` | Step 2 was skipped — run `CREATE DATABASE partsid;` |
| `permission denied for schema public` | Run the two `GRANT` lines (including `\c partsid` first) from step 2 |
| `PostgreSQL support needs psycopg` | `pip install -r requirements-postgres.txt` |
| `Unsupported database URL ...` | `DB_ENGINE` must be `postgres` or `sqlite`; a DSN must start `postgresql://` or `sqlite://` |
| `DB_ENGINE=mysql`-style typo | Any other engine is refused instead of silently ignored |
| `[Errno 10048] / address already in use` when starting | Port 8000 is taken. Either stop the other process, or run on another port: `--port 8001` |
| Badge says *Offline reference matcher* | No vision key. Expected until step 6 |
| `check-providers` shows `FAIL ... 400` | The key is valid but the model/region rejects search grounding. Try `GROUNDING_MODEL=gemini-2.0-flash`, or set `SEARCH_GROUNDING=false` to use plain vision |
| "Reverse image search" button missing | Google Lens needs a *publicly reachable* photo, so it is hidden on `localhost`. Set `PUBLIC_BASE_URL=https://your-domain` (or use a tunnel) to enable it |
| Everything is slow on first upload | First call loads the model / warms caches; later uploads are ~1–3 s |
| Signed out on every click or after about an hour | Sessions renew from the 30-day refresh cookie. If it still happens: clear the site's cookies once, then check the clock on the PC matches real time, and that `.env` has the `SUPABASE_*` values (a 503 page now says so instead of pretending you were signed out). Embedded preview panes need `AUTH_COOKIE_SAMESITE=none` |

---

## Prefer no database server at all?

Set one variable and the app uses SQLite at `data/inventory.db` instead — same
features, same code, single user:

```ini
# .env
DB_ENGINE=sqlite
```

or run a single command without touching `.env`:

```powershell
$env:DB_ENGINE = "sqlite"; python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

(`make run-sqlite` does the same on macOS/Linux.) Good for trying the app;
PostgreSQL is the better choice for a real shop (concurrent tills, backups,
remote access).

**Nothing falls back on its own.** If PostgreSQL is configured and not reachable,
the app refuses to start and says so, rather than quietly writing to a different
database than the one you think you are using. `check-db` and `/api/health` both
print the masked DSN plus which variable chose it.

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

# Start here — commands for this machine

Everything below is **Windows PowerShell** in the project folder, unless a step says
otherwise. macOS/Linux differences are in the notes.

```powershell
cd C:\path\to\FOOTBALL-BET-PREDICTOR
```

There are three jobs in this file:

1. [Install once](#1-install-once-10-minutes) — 5 commands, do it one time.
2. [Get your old data back](#2-get-your-old-data-back) — only if the data is not already in `partsid`.
3. [Start the system](#3-start-the-system-every-day) — 1 command, every day.

Jump to [Troubleshooting](#5-troubleshooting) if something errors.

---

## 1. Install once (10 minutes)

### 1.1 Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use `.\.venv\Scripts\python.exe -m ...` for every command in this file. It cannot
accidentally use the wrong Python, which is what caused
`No module named uvicorn`.

> You can `Activate.ps1` instead and then type plain `python`. If PowerShell blocks
> it, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### 1.2 Create the database (once)

```powershell
psql -U postgres -f scripts\setup_postgres.sql
```

This creates database `partsid` and user `partsid_user` with password
`change-this-password`. If `psql` is not on PATH:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f scripts\setup_postgres.sql
```

Already done in your case — skip to 1.3.

### 1.3 Create `.env`

```powershell
copy .env.example .env
notepad .env
```

Set these five lines and leave the rest alone:

```ini
AUTH_MODE=off
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=partsid
POSTGRES_USER=partsid_user
POSTGRES_PASSWORD=change-this-password
```

- `AUTH_MODE=off` — no sign-in, no internet needed, pages load in milliseconds.
  Only for a machine you control; see [step 6](#6-optional-turning-sign-in-back-on).
- `POSTGRES_PASSWORD` must be the password from `setup_postgres.sql` (or your own,
  if you created the role yourself).
- Save and close Notepad.

### 1.4 Prove both halves work

```powershell
.\.venv\Scripts\python.exe -m app.cli check-db
```

Expected:

```
engine   : postgres
dsn      : postgresql://partsid_user:***@localhost:5432/partsid  (DB_ENGINE=postgres)
server   : PostgreSQL 18.x
status   : OK
contents : 4 parts · 47 units · 0 scans
```

`status : OK` means the database half is done. `4 parts` are demo parts.

---

## 2. Get your old data back

**First check whether you even need this.** If the `contents` line above shows more
than 4 parts, your stock is already in the database and you should skip to
[step 3](#3-start-the-system-every-day).

Three cases:

| Your situation | Do this |
|---|---|
| Data still in the `partsid` database | Nothing. It is already loaded. |
| You have `backups\partsid_backup.dump` only | [2.1](#21-restore-the-bundled-backup) |
| Old data is in the SQLite file `data\inventory.db` | [2.3](#23-move-data-from-sqlite-instead) |

### 2.1 Restore the bundled backup

Step A — look inside the file first (it only lists, it does not change anything):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" -l .\backups\partsid_backup.dump
```

You should see `parts`, `movements`, `scans` and a list of `TABLE DATA` entries.

Step B — restore into a **scratch** database and check it there:

```powershell
$env:PGPASSWORD = "change-this-password"
$pg = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$restore = "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"
$dsn = "postgresql://partsid_user:change-this-password@localhost:5432/partsid_restore"

& $pg -U postgres -d postgres -c "CREATE DATABASE partsid_restore"
& $restore --no-owner --no-privileges -d $dsn .\backups\partsid_backup.dump

# point the app at the scratch copy just to look at it
$env:DATABASE_URL = $dsn
.\.venv\Scripts\python.exe -m app.cli check-db
.\.venv\Scripts\python.exe -m app.cli list
```

If the part names and quantities look right, continue.

Step C — put it into the live database. `--clean --if-exists` replaces the tables
that are already there (this is what avoids `relation "parts" already exists`):

```powershell
Remove-Item Env:\DATABASE_URL
& $restore --no-owner --no-privileges --clean --if-exists `
  -d "postgresql://partsid_user:change-this-password@localhost:5432/partsid" `
  .\backups\partsid_backup.dump

.\.venv\Scripts\python.exe -m app.cli check-db
```

Step D — keep the demo catalogue from coming back, and tidy up:

```powershell
# add this line to .env
#   SEED_DEMO_DATA=false

& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d postgres -c "DROP DATABASE partsid_restore"
Remove-Item Env:\PGPASSWORD
```

> `unsupported version (1.16) in file header` means you ran an old `pg_restore`.
> Use the one from `C:\Program Files\PostgreSQL\18\bin`, never one from an older
> client or a bundled tool.
>
> `--clean` **deletes whatever is in `partsid` right now**, including anything added
> since the backup. That is why step B checks the scratch copy first.

### 2.2 Restore a plain `.sql` backup instead

If your backup ends in `.sql` rather than `.dump`:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U partsid_user -d partsid -f .\backup.sql
```

### 2.3 Move data from SQLite instead

If the old data is in `data\inventory.db`, bridge it through CSV:

```powershell
$env:DB_ENGINE = "sqlite"
.\.venv\Scripts\python.exe -m app.cli export-csv --out stock.csv
Remove-Item Env:\DB_ENGINE

.\.venv\Scripts\python.exe -m app.cli import-csv stock.csv            # dry run, writes nothing
.\.venv\Scripts\python.exe -m app.cli import-csv stock.csv --apply    # writes it
```

---

## 3. Start the system (every day)

PostgreSQL must be running before the app. It normally starts with Windows, but if
you are unsure:

```powershell
Get-Service postgresql*        # Status should be: Running
Start-Service postgresql-x64-18
```

Then start the app:

```powershell
cd C:\path\to\FOOTBALL-BET-PREDICTOR
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Successful start looks like:

```
INFO:     Started server process [1234]
AUTH_MODE=off - sign-in is disabled. Anyone who can reach this port has full access...
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

- Open <http://localhost:8000> — with `AUTH_MODE=off` it goes straight to the dashboard.
- **Leave the window open.** Closing it or pressing **Ctrl+C** stops the app.
- From a phone on the same Wi-Fi: `http://<this-pc-ip>:8000` (find it with `ipconfig`).
  Use `--host 127.0.0.1` instead if you do not want the network to see it.
- Add `--reload` only while editing code.

### Quick health check from a second window

```powershell
curl.exe -s http://localhost:8000/api/health
```

Look for `"status":"ok"`, `"auth":"off"` and `"database":{"dialect":"postgres", ..., "connected":true}`.

### Stop it

**Ctrl+C** in the app window. Nothing is lost; everything is committed to PostgreSQL.

---

## 4. Day-to-day commands

Run these with the app **stopped** (or with `--host 127.0.0.1` running; both work,
but stopping avoids write conflicts during bulk loads).

```powershell
# what is in the database
.\.venv\Scripts\python.exe -m app.cli list

# look up a number at the counter
.\.venv\Scripts\python.exe -m app.cli lookup 5184894AA

# load a stock list (dry run first, always)
.\.venv\Scripts\python.exe -m app.cli import-csv stock.csv
.\.venv\Scripts\python.exe -m app.cli import-csv stock.csv --apply

# replace the whole inventory from a spreadsheet
.\.venv\Scripts\python.exe -m app.cli load-stock stock.csv --replace

# spreadsheets out (fine as a backup too)
.\.venv\Scripts\python.exe -m app.cli export-csv --out stock.csv

# wipe inventory, or wipe and put the demo parts back
.\.venv\Scripts\python.exe -m app.cli reset-db --yes
.\.venv\Scripts\python.exe -m app.cli reset-db --yes --reseed
```

### Make your own backup (do this before any `--replace`)

```powershell
$env:PGPASSWORD = "change-this-password"
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U partsid_user -Fc -f "C:\backups\partsid-$(Get-Date -f yyyy-MM-dd).dump" partsid
Remove-Item Env:\PGPASSWORD
```

Restore that file later with the exact steps in [2.1](#21-restore-the-bundled-backup),
replacing the filename.

Photos are **not** in the dump — they live in `uploads\`. Copy that folder alongside
your backups.

---

## 5. Troubleshooting

| Message / symptom | Cause and fix |
|---|---|
| `No module named uvicorn` | Wrong Python. Use `.\.venv\Scripts\python.exe -m uvicorn ...`, or activate the venv first and check `python -c "import sys; print(sys.executable)"` ends with `.venv\Scripts\python.exe` |
| `Cannot reach PostgreSQL ... Connection refused` | PostgreSQL is not running: `Get-Service postgresql*`, `Start-Service postgresql-x64-18` |
| `password authentication failed` | `POSTGRES_PASSWORD` in `.env` does not match the role. Reset it: `& "...\psql.exe" -U postgres -c "ALTER USER partsid_user WITH PASSWORD 'new-password';"` |
| `database "partsid" does not exist` | Run 1.2 again (`setup_postgres.sql`) |
| `Unsupported database URL` / `DB_ENGINE` error | `AUTH_MODE`/`DB_ENGINE` typo in `.env`. Valid: `DB_ENGINE=postgres` or `sqlite` |
| `unsupported version (1.16) in file header` | Too-old `pg_restore`. Use PostgreSQL 18's binary |
| `relation "parts" already exists` | Add `--clean --if-exists` to `pg_restore` |
| `[Errno 10048] address already in use` | Port 8000 is taken. Stop the other copy, or run `--port 8001` |
| Pages take ~8 seconds, then sign-in page | `AUTH_MODE=off` is missing from `.env`; restart the app after adding it |
| App opens but everything is empty | You are on a different database. `check-db` shows which one; correct `POSTGRES_DB`/`DATABASE_URL` |
| Badge says *Offline reference matcher* | No vision key. See step 6 |

Always start with:

```powershell
.\.venv\Scripts\python.exe -m app.cli check-db
```

It prints the engine, the masked connection string, **which `.env` variable chose
it**, the server version and the row counts.

---

## 6. Optional extras

**Real photo identification** (free tier, no card) — <https://aistudio.google.com/apikey>,
then in `.env`:

```ini
GEMINI_API_KEY=your-key-here
```

Check it (one live call per provider, writes nothing):

```powershell
.\.venv\Scripts\python.exe -m app.cli check-providers
```

**Turning sign-in back on** (needed if anyone else can reach this machine):

```ini
AUTH_MODE=supabase
```

Supabase's keys are already in `.env.example`, so copying it is enough. Then create
an account on the `/login` page. With sign-in on, every request is checked against
Supabase, so it needs working internet.

**Run without PostgreSQL** (laptop demo):

```powershell
$env:DB_ENGINE = "sqlite"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Data then lives in `data\inventory.db` instead of PostgreSQL.

---

## The whole thing, in one block

Only the everyday path, assuming step 1 is already done:

```powershell
cd C:\path\to\FOOTBALL-BET-PREDICTOR
Get-Service postgresql*                                  # expect Running
.\.venv\Scripts\python.exe -m app.cli check-db           # expect status: OK
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000     Ctrl+C to stop
```

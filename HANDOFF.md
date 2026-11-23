# Where we left off — backup setup

**Last updated:** 23 November 2026
**Branch:** `arena/ecc4154e-football-bet-predictor`
**Status:** backup tooling is finished, tested and pushed. **Nothing has been
switched on in your Google Drive yet** — that part needs you at your own
computer, following the steps below.

---

## What is already done (in this repository)

| Commits | What they added |
|---|---|
| `25f0fe7` | Backups that include the database **and** every part photo, plus logins, shop profile and catalogue |
| `82e8deb` | Google Drive support (via rclone), plus retention and restore |
| `f535832` | Plain-English setup guide, `verify` command, working nightly scheduler |

**Verified by testing, not assumed:** backed up → deleted the database and a
part photo → restored → parts and photos came back intact. 210 automated
tests pass.

**Your existing files are untouched.** `data/local-auth.db` and
`backups/partsid_backup.dump` have the same checksums as before this work
began. No `inventory.db` was ever created in this checkout, and the
temporary demo database used for testing was deleted.

---

## What is left to do — at your computer

Nothing below has happened yet. Full click-by-click instructions are in
**[docs/BACKUP_SETUP_GUIDE.md](docs/BACKUP_SETUP_GUIDE.md)**.

1. **Install rclone** — download the Windows zip from <https://rclone.org/downloads/>,
   copy `rclone.exe` into `C:\Tools\rclone`.
2. **Connect it to Google Drive** — run `C:\Tools\rclone\rclone.exe config`,
   answer with the table in Step 3 of the guide (name it `gdrive`).
3. **Add three lines to `.env`:**
   ```dotenv
   CLOUD_BACKUP_RCLONE_REMOTE=gdrive:VerifixBackups
   CLOUD_BACKUP_KEEP=30
   RCLONE_PATH=C:\Tools\rclone\rclone.exe
   ```
4. **Double-click `backup-now.bat`** and look for `Photos: N/N` and a
   `Cloud (Google Drive): …` line.
5. **Schedule it** (one PowerShell command, Step 7 of the guide), then test
   the task with right-click → **Run**.
6. **Prove it:** `python -m app.cli verify` — checks the backup is intact
   without changing anything.
7. **Optional but recommended:** copy the `backups` folder to a USB stick so
   one Google account is never your only copy.

---

## Quick command reference

```powershell
# in the VERIFIX folder
python -m app.cli backup            # back up now (local + cloud)
python -m app.cli verify            # is the newest backup complete and undamaged?
python -m app.cli backups           # what can I restore from?
python -m app.cli restore --force   # restore the newest backup (stops nothing; close the app first)
```

Windows shortcut: double-click `backup-now.bat`.

---

## Files worth knowing

| Path | Why |
|---|---|
| `docs/BACKUP_SETUP_GUIDE.md` | **Start here** — plain-English setup steps |
| `docs/CLOUD_BACKUPS.md` | Technical reference: what is backed up, provider comparison, restore details |
| `app/backup.py` | The backup engine (database, photos, checksums, Drive/S3 upload, restore) |
| `backup-now.bat` | Double-click backup for Windows |
| `scripts/backup_to_cloud.py` | What the nightly task runs |
| `scripts/register_cloud_backup_task.ps1` | Registers the daily Windows task |
| `tests/test_cloud_backup.py` | 23 tests covering photo inclusion and restore |

---

## If you have lost data and need it back

Follow **Part 5** of the setup guide. In short: install rclone, reconnect
Google Drive, restore `.env`, close the app, then
`python -m app.cli restore --force`, and start the app.

Note: `.env` is deliberately **not** in the backups (it holds your secret
keys), so keep a copy of it somewhere safe of your own.

---

## To resume this work in a new chat

Open this repository and say:

> Continue from `HANDOFF.md` — help me with step N.

The branch, commits and this file carry everything needed to pick up.

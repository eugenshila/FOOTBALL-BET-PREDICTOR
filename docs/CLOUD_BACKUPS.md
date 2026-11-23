# VERIFIX cloud backups — database + part photos

Your stock list and every photo attached to a part live **outside Git**
(`data/`, `uploads/` are git-ignored), so a lost laptop used to mean lost
inventory *and* lost photos. This guide sets up automatic backups that
package the database **together with the photos it points to**, and uploads
them to private cloud storage.

## What gets backed up

Every backup is one timestamped folder with four files:

| File | What it contains |
|---|---|
| `inventory.db` (SQLite) or `inventory.dump` (PostgreSQL) | The inventory database — parts, stock quantities, and each part's `image_path` link to its photo |
| `uploads.zip` | **Every photo attached to a part** (the whole `uploads/` folder, including photo-review manifests) |
| `data-files.zip` | Login accounts (`local-auth.db`), shop profile (`shop_profile.json`), reference catalogue (`catalog.json`) |
| `manifest.json` | File sizes, SHA-256 checksums, and a **photo check**: how many parts have a photo, how many photos were found, and which parts are missing theirs |

Before packaging, the backup **verifies every part's photo link** against
the files actually in `uploads/`. If a part points to a photo that isn't
there, the manifest lists it (`missing photo — part 7: …`) so you can fix
it in Inventory → Edit → photo before the next backup. A restore never
silently brings back a database full of broken image links.

> `.env` (API keys, database password) is **never** backed up. Keep it
> somewhere safe separately — a restore needs it, but it must not sit in a
> cloud bucket.

## Cloud options compared

All options below speak the S3 API, so the same backup tool works with
every one of them — you only change the endpoint and keys in `.env`.

| Provider | Storage | Download (egress) | Free tier | Best for |
|---|---|---|---|---|
| **Backblaze B2** | $0.006/GB/mo — cheapest | $0.01/GB (free up to ~3× stored data) | 10 GB permanent | **Backups & archives** — purpose-built for this [1](https://devopsboys.com/blog/cloudflare-r2-vs-aws-s3-vs-backblaze-b2-2026) [3](https://trybuildpilot.com/423-cloudflare-r2-vs-aws-s3-vs-backblaze-b2-2026) |
| **Cloudflare R2** | $0.015/GB/mo | **$0 — zero egress fees** | 10 GB + 1M uploads/mo, permanent | Zero-egress default; generous free tier [1](https://devopsboys.com/blog/cloudflare-r2-vs-aws-s3-vs-backblaze-b2-2026) [4](https://softfolioz.com/blog/cloudflare-r2-vs-aws-s3-vs-backblaze-b2) |
| **AWS S3** | $0.023/GB/mo | $0.09/GB | 5 GB for 12 months only | AWS ecosystem, compliance features [1](https://devopsboys.com/blog/cloudflare-r2-vs-aws-s3-vs-backblaze-b2-2026) |
| **Wasabi** | ~$0.005/GB/mo flat | $0 (fair use) | 30-day trial | Flat-rate backup archives; 90-day minimum retention per object [2](https://github.com/mixpeek/awesome-object-storage) |

| **Google Drive** (already paid for) | $0 — uses space you have | $0 | Whatever your plan gives | Reusing space you already pay for |

**Recommendation for a small parts shop:** **Backblaze B2** (cheapest per
GB, built for backups) or **Cloudflare R2** (zero egress, 10 GB free
forever). A shop's database + part photos are usually well under 10 GB, so
either free tier may cover you entirely — and even paid, 10 GB costs under
$0.20/month on B2.

**Already paying for Google Drive?** Use it — it's configured below and
needs no new subscription. It has no S3 API, so it goes through `rclone`
instead of the S3 path. That works well, with three caveats worth knowing:
Drive is not built for automated backups, its quota is **shared with your
Gmail and Google Photos** (so a runaway backup could fill your mailbox),
and its login token can expire, silently stopping scheduled runs — which
is why every run reports whether the upload actually succeeded.

## One-time setup — pick A, B, or both

You can configure either destination, or both at once: every backup goes
local first, then to whichever cloud destinations are configured. If one
upload fails, the local copy is still kept and the run reports the failure.

### Option A — S3-compatible bucket (B2 / R2 / S3 / Wasabi)

1. Create a **private** bucket with your chosen provider. Create a
   restricted access key that can only list the bucket and upload objects.
2. Install the Python cloud library (once):
   ```bash
   pip install boto3
   ```
   (Windows PowerShell users can keep using the AWS CLI + the `.ps1`
   script instead — see below.)
3. Open the project `.env` file and add these values. Do not send keys in
   chat or put them in the workbook:
   ```dotenv
   CLOUD_BACKUP_BUCKET=your-private-bucket
   CLOUD_BACKUP_PREFIX=verifix/backups
   CLOUD_BACKUP_REGION=us-east-1
   CLOUD_BACKUP_ENDPOINT=       # blank for AWS S3; set for R2/B2/Wasabi (see below)
   CLOUD_BACKUP_KEEP=30         # cloud backups to keep before pruning old ones
   AWS_ACCESS_KEY_ID=your-access-key
   AWS_SECRET_ACCESS_KEY=your-secret-key
   ```
   Endpoint examples:
   - **Backblaze B2:** `https://s3.us-west-004.backblazeb2.com` (use the
     region shown in your B2 console)
   - **Cloudflare R2:** `https://<account-id>.r2.cloudflarestorage.com`
   - **AWS S3:** leave blank
   Keep `.env` private; it is already excluded from Git.
### Option B — Google Drive you already have (via rclone)

Google Drive has no S3 API, so `rclone` does the copying for us. It handles
the Google login, upload retries and resumable transfers; the same setup
also works for OneDrive, Dropbox, MEGA or an external USB drive.

1. Download and install rclone: <https://rclone.org/downloads/> (Windows:
   unzip `rclone.exe` somewhere permanent, e.g. `C:\Tools\rclone\`).
2. Configure the Drive connection **once**, in a terminal:
   ```bash
   rclone config
   ```
   - `n` → new remote
   - name: `gdrive`
   - storage type: `drive` (Google Drive)
   - client_id / client_secret: leave blank (press Enter) — this uses
     rclone's shared app, which is fine for one shop
   - scope: `1` (full access) or `3` (read-only is **not** enough)
   - `Use web browser to automatically authenticate?` → `y`, sign in with
     the Google account that owns the Drive space, and allow access
   - when asked `Configure this as a Shared Drive (Team Drive)?` → `n`
   - confirm with `y`, then `q` to quit
   Verify it works: `rclone lsd gdrive:` should list your Drive folders.
3. In the project `.env`, set the remote and a folder:
   ```dotenv
   CLOUD_BACKUP_RCLONE_REMOTE=gdrive:VerifixBackups
   CLOUD_BACKUP_KEEP=30
   # RCLONE_PATH=C:\Tools\rclone\rclone.exe   # only if rclone is not on PATH
   ```
   That folder is created in your Drive on the first backup.
4. Check you have space to spare, since Drive quota is shared with Gmail
   and Photos: `rclone about gdrive:` shows Used / Free / Total.

### Test one backup (either option)

Run from the project directory:

```bash
python -m app.cli backup
# or: python scripts/backup_to_cloud.py
```

This writes a local copy to `./backups/verifix-backup-<timestamp>/`
**and** uploads it to every configured destination. Example output:

```
Backup: /path/backups/verifix-backup-20261123-230000
Engine: sqlite · parts: 4 · upload files: 4
Photos: 4/4 part photos present in uploads/
Cloud (Google Drive): gdrive:VerifixBackups/verifix-backup-20261123-230000
```

To test without any cloud: `python -m app.cli backup --local-only`.
To see all backups you could restore from: `python -m app.cli backups`.
Windows PowerShell alternative (PostgreSQL + AWS CLI):
`.\scripts\backup_to_cloud.ps1`.

**Google Drive specifics**

- Backups are private to your Google account; keep 2-factor on it, since
  Drive access is now access to your stock and photos.
- Old backups beyond `CLOUD_BACKUP_KEEP` are pruned with a genuine delete
  (not the Drive bin) so they actually free space. That is permanent.
- If you change your Google password or revoke access, scheduled runs stop
  uploading. `rclone config reconnect gdrive:` fixes it. Watch the log for
  the first week after setting it up.
- Never keep Drive backups as your *only* copy: if you lose access to the
  Google account, they go with it. Keep the local `./backups/` folder (or a
  USB drive) too — that is the "3-2-1" idea below.

## Schedule it daily

**Windows (Task Scheduler):**
```powershell
.\scripts\register_cloud_backup_task.ps1 -At "23:00"
```
This registers a daily 11:00 PM East Africa Time job. It runs while you
are signed in; if the computer is asleep it runs when available. It
cannot back up while the laptop is powered off or offline.

**Linux / macOS (cron):** add this line with `crontab -e` (adjust the
path and time):
```cron
0 23 * * * cd /path/to/FOOTBALL-BET-PREDICTOR && ./.venv/bin/python scripts/backup_to_cloud.py >> exports/cloud-backup.log 2>&1
```

Check the log after the first scheduled run — scheduled jobs fail
silently, so glance at it weekly. The project must stay at the same path
after the task is created.

## Restore

**Stop the app first**, then:

```bash
python -m app.cli backups                          # what can I restore from?
python -m app.cli restore --force                  # newest cloud backup
python -m app.cli restore --from gdrive --force    # newest Google Drive backup
python -m app.cli restore --timestamp 20261123-230000 --force
python -m app.cli restore --local-dir ./backups/verifix-backup-20261123-230000 --force
```

Restore verifies each file's checksum against the manifest, then puts
back the database, `uploads/` (all part photos), logins, profile and
catalogue. Before overwriting, it copies your current state to
`./backups/pre-restore-<timestamp>/` as a safety net. Start the app again
afterwards. (PostgreSQL restores need `pg_restore` on PATH.)

## Retention & the 3-2-1 rule

- `CLOUD_BACKUP_KEEP` (default 30) prunes older cloud backups
  automatically, on Drive and in buckets alike. Set a bucket lifecycle
  rule as a second safety net.
- Keep **one local copy** too (the `./backups/` folder, or copy it to a
  USB drive weekly). Cloud + local + one off-site copy means no single
  failure — laptop, account, or provider — can take your stock and photos
  with it.
- Test a restore at least once: restore to a spare folder/machine and
  confirm a part's photo loads. Restores download and re-verify every
  file's checksum, so a corrupt backup is refused rather than half-applied.

## Hosted-server option

If VERIFIX is later deployed to a cloud host, run the backup job from
that host or its scheduler instead of this laptop. For uninterrupted
availability, move the live database to managed PostgreSQL and put photos
in object storage; keep automated database snapshots plus this separate
media backup. A local scheduled job only backs up the database reachable
from this computer.

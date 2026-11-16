# VERIFIX scheduled cloud backups

The current app uses a PostgreSQL server on this computer. The scheduled task
will connect to that server each day, create a PostgreSQL custom-format dump,
package `uploads/` and the saved shop profile, then send those files to a
private S3-compatible object-storage bucket. It does not move the live database
to the cloud or make the app public.

## One-time setup

1. Create a **private** bucket with your chosen object-storage provider. Create
   a restricted access key that can only list the bucket and upload objects.
2. Install AWS CLI v2 and confirm `aws --version` works in PowerShell. Ensure
   PostgreSQL's `pg_dump.exe` is on PATH (`pg_dump --version`).
3. Open the project `.env` file and add these values. Do not send keys in chat
   or put them in the workbook:

   ```dotenv
   CLOUD_BACKUP_BUCKET=your-private-bucket
   CLOUD_BACKUP_PREFIX=verifix/backups
   CLOUD_BACKUP_REGION=us-east-1
   CLOUD_BACKUP_ENDPOINT=
   AWS_ACCESS_KEY_ID=your-access-key
   AWS_SECRET_ACCESS_KEY=your-secret-key
   ```

   For AWS S3, leave `CLOUD_BACKUP_ENDPOINT` empty. For an S3-compatible service,
   use the endpoint URL supplied by that provider. Keep `.env` private; it is
   already excluded from Git.

4. Run one backup manually from the project directory:

   ```powershell
   .\scripts\backup_to_cloud.ps1
   ```

   It uploads a database dump and a separate archive for uploaded photos and
   shop profile. A run log is written to `exports/cloud-backup.log`.

5. Register the daily Windows Task Scheduler job. This example runs at 11:00 PM
   East Africa Time; change the time if desired:

   ```powershell
   .\scripts\register_cloud_backup_task.ps1 -At "23:00"
   ```

   The task runs under your Windows account while you are signed in. If the
   computer is asleep at the scheduled time, Windows is asked to run it when
   the task becomes available. It cannot back up while the laptop is powered
   off or has no network connection. Check Task Scheduler and the log after the
   first run. The project must stay at the same path after the task is created.

## Restore overview

Download the matching `inventory-*.dump` and `uploads-and-profile-*.zip` objects.
Use PostgreSQL's `pg_restore` to restore the custom-format database into a
prepared database, then extract the archive's `uploads/` folder back into the
project's configured `APP_UPLOAD_DIR` (normally the project `uploads/` folder).
Restore the `shop_profile.json` into `data/` as well. Keep a second copy of
important backups in a separate account or location and set a bucket lifecycle
policy for retention so old backups do not accumulate indefinitely.

## Hosted-server option

If VERIFIX is later deployed to a cloud host, run the backup job from that host
or its scheduler instead of this laptop. For uninterrupted availability, move
the live database to managed PostgreSQL and put photos in object storage; keep
automated database snapshots plus this separate media backup. A local
Task Scheduler job is only a backup of the PostgreSQL server that is reachable
from this computer.

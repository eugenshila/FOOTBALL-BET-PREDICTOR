# Set up VERIFIX backups — plain-English guide

This turns on automatic backups of your **inventory database and every part
photo**, saved to **your Google Drive**.

- **Time needed:** about 10 minutes, once.
- **What you need:** your Google account password, and the VERIFIX folder on
  this computer.
- **You are not changing anything in your inventory** — this only copies data
  out to Drive. It never alters or deletes parts, stock or photos.

Work through Part 1 in order. Do not skip Part 2 — that is the part that
proves the backup actually works.

---

## Part 1 — Turn it on

### Step 1. Find your VERIFIX folder

Open File Explorer and find the folder that contains `app`, `scripts` and a
file called `.env`. This is the folder you use to start VERIFIX.

Write its location down — for example `C:\VERIFIX` or
`C:\Users\YourName\Desktop\FOOTBALL-BET-PREDICTOR`. Every step below needs
it, and so will future you.

> Wherever this guide shows a path like `C:\VERIFIX`, use **your** folder
> instead.

### Step 2. Install rclone (the tool that talks to Google Drive)

1. Go to <https://rclone.org/downloads/>
2. Under **Windows**, click **Download rclone zip** (the 64-bit one).
3. Open the downloaded zip file.
4. Inside it, find `rclone.exe` and copy it into a new folder:
   `C:\Tools\rclone`
   (Create the `Tools` folder if it is not there. There is nothing to
   install — copying the file is the whole installation.)

**Check it worked:** press the Windows key, type `PowerShell`, press Enter.
In the blue window, type this and press Enter:

```powershell
C:\Tools\rclone\rclone.exe version
```

You should see a version number. If you see an error, `rclone.exe` is not in
`C:\Tools\rclone` — redo step 4.

### Step 3. Connect rclone to your Google Drive

In that same PowerShell window, type:

```powershell
C:\Tools\rclone\rclone.exe config
```

You are asked a series of questions. Answer exactly as below (press Enter
after each):

| It asks | You type |
|---|---|
| `n/s/q>` (new/edit/quit) | `n` |
| `name>` | `gdrive` |
| Storage type list | `drive` then Enter |
| `client_id>` | press Enter (leave blank) |
| `client_secret>` | press Enter (leave blank) |
| `scope>` | `1` (full access) |
| `root_folder_id>` | press Enter (blank) |
| `service_account_file>` | press Enter (blank) |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate?` | `y` |
| Browser opens Google sign-in | sign in with **the Google account that has your Drive space**, then click **Allow** |
| `Configure this as a Shared Drive (Team Drive)?` | `n` |
| `y/e/d>` or `Keep this "gdrive" remote?` | `y` |
| `e) Edit this remote` / `n` / `q` | `q` |

**Check it worked:** type this and press Enter:

```powershell
C:\Tools\rclone\rclone.exe about gdrive:
```

You should see `Used`, `Free` and `Total` for your Drive. If you see
`Failed to configure` or `token expired`, run
`C:\Tools\rclone\rclone.exe config reconnect gdrive:` and sign in again.

> **Look at the `Free` line.** Your VERIFIX backups will need roughly the
> size of your `uploads` folder plus a little more. Google Drive space is
> shared with your Gmail and Google Photos, so make sure there is room to
> spare.

### Step 4. Tell VERIFIX to use Google Drive

1. In File Explorer, go to your VERIFIX folder and find the file `.env`.
2. Right-click it → **Open with** → **Notepad**.
   (If Windows only offers other apps: click **Choose another app** →
   **More apps** → **Notepad**.)
3. Scroll to the bottom of the file and add these three lines exactly:

```dotenv
CLOUD_BACKUP_RCLONE_REMOTE=gdrive:VerifixBackups
CLOUD_BACKUP_KEEP=30
RCLONE_PATH=C:\Tools\rclone\rclone.exe
```

4. Save with **Ctrl+S**, then close Notepad.

What these mean: the backups go into a folder called `VerifixBackups` in your
Drive, the last 30 backups are kept, and that is where `rclone.exe` lives.

> `.env` may contain your API keys. Never email it, paste it in chat, or
> upload it anywhere. A printed copy in a drawer is fine.

### Step 5. Run your first backup

In your VERIFIX folder, find **`backup-now.bat`** and **double-click it**.

A black window opens. Wait a few seconds. You are looking for these lines:

```
Photos: 42/42 part photos present in uploads/
Cloud (Google Drive): gdrive:VerifixBackups/verifix-backup-20261123-230000
```

- **`Photos: 42/42`** — every part's photo is inside the backup. This is the
  line that matters most to you.
- **`Cloud (Google Drive): …`** — it was copied to your Drive.

If it says an error instead, jump to **Part 4 (Problems)** below. Nothing on
your computer is damaged by a failed backup.

### Step 6. Look at it in Google Drive

1. Go to <https://drive.google.com> and sign in.
2. Open the folder **VerifixBackups**, then the folder with today's date/time.
3. You should see four files: `inventory.db`, `uploads.zip`,
   `data-files.zip`, `manifest.json`.

`uploads.zip` is where **all your part photos** are. It is one zip file
rather than loose pictures, because that way a photo can never be lost or
separated from the part record that points to it.

### Step 7. Make it happen every night automatically

1. Press the Windows key, type `PowerShell`, press Enter.
2. Type this (with **your** folder path, keeping the quotes):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\VERIFIX\scripts\register_cloud_backup_task.ps1" -At 23:00
```

3. Press Enter. You should see:

```
Registered 'VERIFIX Cloud Backup' to run daily at 23:00 (Python runner).
```

**Check it worked:** press the Windows key, type `Task Scheduler`, press
Enter. Click **Task Scheduler Library** on the left. You should see
**VERIFIX Cloud Backup** in the list with a **Next Run Time** of tonight.

**Test the scheduled job immediately** (do this now, do not wait for
tonight): right-click **VERIFIX Cloud Backup** → **Run**. Wait a minute,
then open `exports\cloud-backup.log` in your VERIFIX folder — it should show
the same successful lines as Step 5.

> The task runs only while you are signed in, and catches up if the PC was
> asleep. If the laptop is switched off at 23:00, the backup runs when you
> next switch it on and sign in.

---

## Part 2 — Prove the backup works (do this once)

A backup you have never tested is only a hope. This check downloads your
newest backup, verifies every file, and **changes nothing** on your computer.

1. Open the VERIFIX folder in File Explorer.
2. Click in the address bar at the top, type `powershell`, press Enter.
   A PowerShell window opens already in the right folder.
3. Type this and press Enter:

```powershell
.\.venv\Scripts\python.exe -m app.cli verify
```

(If that says the file cannot be found, use `python -m app.cli verify`
instead.)

You should see:

```
Checked gdrive backup 20261123-230000
OK: all 3 files are present and undamaged (checksums match)
Photos: 42/42 part photos were in uploads/ at backup time
Nothing on this computer was changed.
```

That message means: if this laptop died right now, your stock list and all
part photos could be recovered from Drive.

**Do this check once a month.** It takes 30 seconds.

---

## Part 3 — Your monthly 2-minute routine

On the first of each month:

1. Double-click `backup-now.bat` and confirm `Photos: N/N` has no `!` lines.
2. If you see names like `! missing photo — part 7: …`, that part's picture
   is missing *on your computer*. Fix it in **Inventory → Edit → photo**, and
   the next backup will include it.
3. Open `exports\cloud-backup.log` and skim the last few entries for errors.
4. Run the `verify` command from Part 2.

---

## Part 4 — Problems and what they mean

| What you see | What to do |
|---|---|
| `rclone was not found` | The `RCLONE_PATH=` line in `.env` is missing or has the wrong path. Check that `C:\Tools\rclone\rclone.exe` exists; the line must have no quotes. |
| `Set CLOUD_BACKUP_RCLONE_REMOTE in .env first` | The line `CLOUD_BACKUP_RCLONE_REMOTE=gdrive:VerifixBackups` is missing from `.env`, or Notepad saved it as `.env.txt`. In Notepad use **File → Save as**, choose *All files*, and name it `.env`. |
| `Google Drive upload failed: … token expired` | Google signed rclone out (password change, or long inactivity). Run `C:\Tools\rclone\rclone.exe config reconnect gdrive:`, sign in, then double-click `backup-now.bat` again. |
| `Google Drive upload failed: … quota` | Your Drive is full. Delete old folders in `VerifixBackups` you no longer need, lower `CLOUD_BACKUP_KEEP` in `.env` (e.g. to 10), or free space in Google Photos. **Do not delete the newest folder.** |
| `Photos: 38/42` with `! missing photo` lines | The backup worked; those 4 parts simply have no photo file on your computer. Re-attach their photos in Inventory, then back up again. The photos already in the backup are safe. |
| `Inventory database not found` | VERIFIX has not been started on this computer yet, or you are in the wrong folder. Start the app once, then back up. |
| The nightly task does nothing | In Task Scheduler, click **VERIFIX Cloud Backup** and look at **Last Run Result** (anything other than `0x0` is a failure), then read `exports\cloud-backup.log`. The usual cause is the laptop being off or signed out. |
| `Nothing happens when I double-click backup-now.bat` | Python is not installed or `.venv` is missing. Run `python --version` in PowerShell; if that fails, Python needs installing. |

---

## Part 5 — If the worst happens: getting your data back

You need: this VERIFIX folder (or a fresh copy of the app), your Google
account, and the `.env` values. The backup itself **does not contain
`.env`**, because it holds your secret keys.

1. Get VERIFIX back on the new computer and start it once so the folders
   exist (it is fine if it starts empty).
2. Do **Steps 2 and 3** from Part 1 again (install rclone, connect Google
   Drive).
3. Put the `.env` values back (Step 4).
4. **Stop VERIFIX** — close the app window. Do not run a restore while it is
   running.
5. In the VERIFIX folder: click the address bar, type `powershell`, press
   Enter, then run:

```powershell
.\.venv\Scripts\python.exe -m app.cli backups
```

This lists your backups, newest last. Now restore the newest one:

```powershell
.\.venv\Scripts\python.exe -m app.cli restore --force
```

6. You should see:

```
restored from … (gdrive)
safety copy of previous state: …\backups\pre-restore-…
start the app again to use the restored data
```

7. Start VERIFIX. Open a part you know had a photo and check the picture
   loads. Your stock, prices and photos are back.

Notes:
- `--force` is required because it overwrites what is on the computer. Before
  it overwrites anything, it saves your current state into
  `backups\pre-restore-<date>` — so even the restore has an undo.
- To restore a specific older backup instead: add
  `--timestamp 20261123-230000` (the number from the `backups` list).

---

## What is backed up — and what is not

| Included | Not included |
|---|---|
| Inventory database: every part, stock, prices, fitment, and the link to its photo | Your `.env` file (holds secret keys — keep it separately) |
| **Every part photo** (`uploads.zip`) | The app's own code (that lives on GitHub) |
| Login accounts (`local-auth.db`) | Windows temp files and caches |
| Shop profile and reference catalogue | |

## The one rule that protects you

Google Drive is **one** copy. Keep a second one on something else — a USB
stick, or an external drive — by copying the `backups` folder from your
VERIFIX folder now and then. If you ever lose access to your Google account,
Drive will not help you; the USB stick will.

Cloud + this computer + a USB stick means no single accident can take your
stock list and photos again.

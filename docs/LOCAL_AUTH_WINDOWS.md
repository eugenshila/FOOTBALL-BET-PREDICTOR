# Local login for the Windows shop app (Command Prompt / CMD)

This replaces Supabase authentication with local login, without turning password
protection off. The replacement retains `router`, `guard`, and the existing
`/api/auth/signin`, `/signout`, `/session`, `/signup` paths. It can replace the
`app/auth.py` supplied by the Windows deployment without changing its main.py
when that file already installs the router and middleware.

The repository's main.py now installs this local router/middleware as well.

## What stays untouched

- Your PostgreSQL `partsid` database and its stock, prices and photo references.
- Your `.env` and `DATABASE_URL`. Do not switch back to demo SQLite.
- Uploaded photos, exports, and existing Supabase accounts (they are not deleted).

## What changes

- Login and session checks make **no network requests** and require no email service.
- Create local accounts on the server; public signup is disabled.
- Existing Supabase passwords/sessions are not transferred. You may use the same
  email as a local account, but must create its password on this machine.
- Salted PBKDF2-SHA256 hashes (600,000 iterations); no plaintext password storage.
- Random session tokens, stored only as SHA256 hashes; HttpOnly / SameSite=Lax
  cookie, Secure on HTTPS. Default expiry is 12 hours, surviving app restarts.
- Signout revokes the session; password reset revokes all that user's sessions.
- Login rate limits (10 attempts/account and 50/IP per 10 minutes), browser origin
  checks, and fail-closed behavior if local session storage is unavailable.

Authentication lives in **data/local-auth.db**, separate from stock storage.
Keep it in backups if you want to retain local accounts. Protect its Windows
folder permissions; anyone able to modify the auth file can change credentials.
Do not commit, share or upload it. `LOCAL_AUTH_DB` optionally overrides its path;
never point it at `inventory.db`. `LOCAL_AUTH_HOURS` accepts 1–168 (default 12).

## Install without replacing the rest of your Windows project

1. Download `install_local_auth.py` to your Windows **Downloads** folder. This
   installer is a self-contained, readable Python file; no remote downloads are
   executed. If the attachment viewer isn't visible, use the live preview named
   **Local authentication download** and click its installer download button.
2. Stop the existing app with **Ctrl+C**.
3. Open **Command Prompt**, then run each command separately:

```cmd
cd /d "C:\Users\Administrator\Documents\GitHub\FOOTBALL-BET-PREDICTOR"
```

```cmd
.\.venv\Scripts\python.exe "%USERPROFILE%\Downloads\install_local_auth.py"
```

The installer verifies its embedded source, syntax-checks it, creates a uniquely
named `app/auth.before-local-....bak`, and atomically replaces only `app/auth.py`.
It does not edit your main.py, .env or databases. If your Windows main.py has
additional Supabase-specific startup requirements, send that file for a targeted
follow-up; don't overwrite it with an unrelated checkout's main.py.

4. Create your first account. Enter an email-style local account name, then a
   new unique password of 12–128 characters twice. Password input is hidden:

```cmd
.\.venv\Scripts\python.exe -m app.auth create-user
```

5. Confirm your existing stock connection still works (expected from your
   earlier check: PostgreSQL, 92 parts / 102 units; later shop edits may differ):

```cmd
.\.venv\Scripts\python.exe -m app.cli check-db
```

6. For now, disable cloud identification calls in this same CMD window:

```cmd
set "VISION_PROVIDER=offline"
```

```cmd
set "SEARCH_GROUNDING=false"
```

To persist these, edit the existing entries in .env to those values. Offline
identification only compares local references; it is NOT reliable OEM verification.
Skip the identifier for this week's stock work. This change does not disable other
network traffic in unrelated code, nor move remotely hosted photo files locally.

7. Start the app (keep this CMD window open):

```cmd
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

8. Visit http://localhost:8000, then sign in with the newly created LOCAL account.
   On the phone use http://YOUR_PC_WIFI_IP:8000 and sign in separately. Both use the
   same local inventory; the computer must remain running. Use only trusted Wi-Fi.
   Local HTTP does not encrypt passwords in transit; use HTTPS on untrusted LANs.
   Do not expose port 8000 through your router to the public internet.

The Windows version may retain its old login page text because main.py can serve
its own page. The auth API response includes `provider: local`; the backend no
longer uses Supabase. If that page runs the Supabase JS SDK directly rather than
posting to `/api/auth/signin`, its frontend needs a small follow-up update. The
repository's included login page is fully local and has no external dependencies.

## Password reset / additional account

Run on the server PC; never send credentials in chat:

```cmd
.\.venv\Scripts\python.exe -m app.auth reset-password
```

Use `create-user` again for another local account. The local-auth file must stay
on this one server, not in a shared network folder or OneDrive-synchronised folder.

## Roll back the authentication change

Stop the app. Find the uniquely named auth backup:

```cmd
dir app\auth.before-local-*.bak
```

Copy the actual backup filename back over app/auth.py, then restart. Do not touch
partsid or photos. Existing Supabase configuration was deliberately preserved.

## Tests

`tests/test_local_auth.py` tests authentication with network access forbidden,
including restart persistence, expiry, revocation, cookie flags, CSRF, rate limits,
password reset and the separation from the inventory database. No real account or
shop password is bundled. No default administrator password exists.

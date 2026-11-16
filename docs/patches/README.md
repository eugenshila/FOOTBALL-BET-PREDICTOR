> **Superseded for the shop deployment:** local authentication is now available
> in `app/auth.py`. Prefer [the local-auth installer guide](../LOCAL_AUTH_WINDOWS.md)
> when removing Supabase. The patch below is retained only for reference/testing
> of the earlier remote session-refresh repair. Do not install it over local auth.

# Local Supabase authentication repair

`auth_fixed.py` is a replacement for the separately supplied Windows app's
`app/auth.py`. It preserves that file's router, cookie names, configuration
attributes and `guard(request, call_next)` middleware interface. It is NOT wired
into this checkout: this checkout does not contain the user's login page,
Supabase configuration or auth middleware integration. Do not add this patch to
an unauthenticated deployment without its existing authentication setup.

## Fixes

- Refresh a session when the access cookie is absent but the refresh cookie remains.
  The original only attempted refresh inside `if token`, so browser cookie expiry
  prevented renewal and caused the refresh cookie to be cleared.
- Also refresh after explicit rejection (401) of an existing access token.
- Retain cookies on network errors, provider errors and rate limits. Return 503
  with Retry-After rather than falsely treating a temporary outage as a logout.
- Continue to deny anonymous/invalid sessions; clear rejected refresh credentials.
- Preserve HttpOnly, SameSite and request-appropriate Secure cookie flags.
- Disable caching of authenticated responses. No passwords or tokens are logged.

This does not lengthen token lifetimes, bypass auth, or change database settings.
The access-cookie lifetime still comes from the provider's `expires_in`.
The original file does not itself specify a fixed 60-second timeout.

## Install on Windows (Command Prompt)

Download `auth_fixed.py` into Downloads. Stop the server with Ctrl+C. Then run
these individually, replacing the project path if necessary:

```cmd
cd /d "C:\Users\Administrator\Documents\GitHub\FOOTBALL-BET-PREDICTOR"
```

Back up the original; decline overwriting the backup if it already exists:

```cmd
copy /-Y app\auth.py app\auth.before-session-fix.bak
```

Install ONLY after verifying the downloaded filename and backup:

```cmd
copy /Y "%USERPROFILE%\Downloads\auth_fixed.py" "app\auth.py"
```

Check syntax:

```cmd
.\.venv\Scripts\python.exe -m py_compile app\auth.py
```

Restart in the CMD window with the correct database configuration:

```cmd
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level debug
```

Log in again at the same origin (e.g. http://localhost:8000). Test beyond the
previous logout interval, then refresh a protected page. Do not shorten provider
expiry settings as part of installing this patch. Do not close the server window.

If the frontend redirects on *all* non-200 responses, its 503 handling may also
need repair; inspect the local frontend and main.py if that occurs. Session
refresh changes do not explain the previously reported server shutdown.

Rollback, with the server stopped:

```cmd
copy /Y app\auth.before-session-fix.bak app\auth.py
```

Tests are in `tests/test_auth_repair.py`. Provider HTTP requests are mocked;
passing tests are not a live validation of the user's Supabase deployment.

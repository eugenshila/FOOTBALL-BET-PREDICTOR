"""Package the local-login replacement without shop data or credentials.

Output is a readable standalone installer plus ZIP and a small download page.
Only the existing auth.py is replaced on the Windows PC, after a unique backup.
"""
from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "exports/local-auth-download"


def build() -> Path:
    source = (ROOT / "app/auth.py").read_text(encoding="utf-8")
    template = '''"""Install local login into the existing auth-enabled Verifix project.

Run from your project directory with its Python environment. No internet calls,
no changes to .env, inventory, photos, PostgreSQL or installed dependencies.
The replacement source is embedded as readable Python text below.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import os
import secrets

AUTH_SOURCE = __AUTH_SOURCE__
EXPECTED_SHA256 = __HASH__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.project.resolve()
    target = root / "app/auth.py"
    if not (root / "app/main.py").is_file() or not (root / "app/config.py").is_file() or not target.is_file():
        raise SystemExit("Stop: run this installer from the existing auth-enabled project folder. Nothing was changed.")
    old = target.read_bytes()
    if b"guard" not in old or b"router" not in old:
        raise SystemExit("Stop: auth.py does not match the expected router/guard interface. Nothing was changed.")
    payload = AUTH_SOURCE.encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != EXPECTED_SHA256:
        raise SystemExit("Installer source checksum failed. Nothing was changed.")
    compile(AUTH_SOURCE, str(target), "exec")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = root / f"app/auth.before-local-{stamp}-{secrets.token_hex(3)}.bak"
    with backup.open("xb") as handle:
        handle.write(old)
    temporary = root / f"app/.auth-{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Local authentication installed.")
    print("Original authentication backup:", backup)
    print("Inventory, PostgreSQL, .env and photos were NOT modified.")
    print("NEXT: .\\\\.venv\\\\Scripts\\\\python.exe -m app.auth create-user")
    print("Then restart the app and sign in with that new LOCAL account.")


if __name__ == "__main__":
    main()
'''
    installer = template.replace("__AUTH_SOURCE__", repr(source)).replace("__HASH__", repr(hashlib.sha256(source.encode()).hexdigest()))
    compile(installer, "install_local_auth.py", "exec")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "install_local_auth.py").write_text(installer, encoding="utf-8")
    (OUTPUT / "auth_local.py").write_text(source, encoding="utf-8")
    instructions = ROOT / "docs/LOCAL_AUTH_WINDOWS.md"
    with zipfile.ZipFile(OUTPUT / "trail-vault-local-auth.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("install_local_auth.py", installer)
        archive.writestr("auth_local.py", source)
        if instructions.exists():
            archive.write(instructions, "INSTALL.md")
    (OUTPUT / "index.html").write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Verifix · Local login download</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#161c20;color:#f4f2e9;font:16px/1.6 system-ui}main{max-width:620px;margin:24px;padding:32px;border:1px solid #ffffff25;border-radius:18px;background:linear-gradient(145deg,#353d40,#1d2427)}a{display:block;border-radius:8px;padding:14px 18px;margin:16px 0;text-decoration:none;background:linear-gradient(#fff0be,#e5b75d);color:#282419;font-weight:600}p{color:#b7c0ba}small{color:#edc66b}</style><main><small>VERIFIX · LOCAL AUTHENTICATION</small><h1>Download to your Windows PC</h1><p>No file-viewer button needed. Click below and save to Downloads. The installer backs up and replaces only app/auth.py, leaving your inventory, .env and photos untouched.</p><a href="install_local_auth.py" download="install_local_auth.py">Download installer — install_local_auth.py</a><a href="trail-vault-local-auth.zip" download>Download ZIP — source + instructions</a><p>After downloading: stop the app, run the installer from your project folder, then create a local account. Your old Supabase password is not automatically transferred.</p></main></html>''', encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(build())

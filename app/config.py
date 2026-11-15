"""Runtime configuration for the parts identification engine.

Everything is environment-driven so the same code runs as a demo (no API key)
or wired to a real multimodal model in production.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from .env without overriding real environment vars.

    Keeps deployment to "edit one file" instead of exporting variables in every
    new terminal. Anything malformed is skipped rather than crashing startup.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


BASE_DIR = Path(__file__).resolve().parent.parent
_load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
ASSET_DIR = BASE_DIR / "assets"
SEED_DIR = ASSET_DIR / "seed"
FONT_DIR = ASSET_DIR / "fonts"
UPLOAD_DIR = Path(os.getenv("APP_UPLOAD_DIR", BASE_DIR / "uploads"))
EXPORT_DIR = Path(os.getenv("APP_EXPORT_DIR", BASE_DIR / "exports"))
WEB_DIR = BASE_DIR / "web"
DB_PATH = Path(os.getenv("APP_DB_PATH", DATA_DIR / "inventory.db"))


# ── Database engine selection ──────────────────────────────────────────────
# PostgreSQL is the default, so a shop deployment never quietly ends up on a
# local file. SQLite remains a first-class fallback you opt into explicitly.
#
#   DATABASE_URL   full DSN; wins over everything when it is set
#                  postgresql://user:pass@localhost:5432/partsid
#                  sqlite:///./data/inventory.db
#   DB_ENGINE      postgres (default) | sqlite
#   POSTGRES_HOST  localhost      POSTGRES_PORT      5432
#   POSTGRES_DB    partsid        POSTGRES_USER      partsid_user
#   POSTGRES_PASSWORD             leave empty to use PGPASSWORD / ~/.pgpass
DEFAULT_DB_ENGINE = "postgres"
_POSTGRES_DEFAULTS = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "partsid",
    "POSTGRES_USER": "partsid_user",
}
_POSTGRES_ALIASES = {"postgres", "postgresql", "psql", "pg"}
_SQLITE_ALIASES = {"sqlite", "sqlite3"}


def sqlite_dsn(path: Path | str | None = None) -> str:
    """DSN for the file-backed database used by ``DB_ENGINE=sqlite`` and tests."""
    return f"sqlite:///{path or DB_PATH}"


def postgres_dsn(environ: Mapping[str, str] | None = None) -> str:
    """Build a DSN from POSTGRES_* variables.

    The password is omitted when it is not set, so libpq can fall back to
    ``PGPASSWORD`` or ``~/.pgpass`` instead of forcing it into a file.
    """
    env = os.environ if environ is None else environ

    def value(key: str) -> str:
        return (env.get(key) or _POSTGRES_DEFAULTS[key]).strip()

    host = value("POSTGRES_HOST")
    port = value("POSTGRES_PORT")
    if not port.isdigit():
        port = _POSTGRES_DEFAULTS["POSTGRES_PORT"]
    database = value("POSTGRES_DB")
    user = quote(value("POSTGRES_USER"), safe="")
    password = (env.get("POSTGRES_PASSWORD") or "").strip()
    credentials = f"{user}:{quote(password, safe='')}" if password else user
    return f"postgresql://{credentials}@{host}:{port}/{database}"


def resolve_database(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return ``(dsn, source)`` for the configured engine.

    ``source`` is what diagnostics print next to the masked DSN, so an operator
    can see *why* the app picked that database.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("DATABASE_URL") or "").strip()
    if explicit:
        return explicit, "DATABASE_URL"

    engine = (env.get("DB_ENGINE") or DEFAULT_DB_ENGINE).strip().lower()
    if engine in _SQLITE_ALIASES:
        return sqlite_dsn(), "DB_ENGINE=sqlite"
    if engine in _POSTGRES_ALIASES:
        return postgres_dsn(env), "DB_ENGINE=postgres"
    # Unknown engine: keep it in the DSN so storage raises an explanatory error
    # naming the offending value instead of silently picking something.
    return f"{engine}://", f"DB_ENGINE={engine}"


DATABASE_URL, DATABASE_SOURCE = resolve_database(os.environ)
DATABASE_ENGINE = (os.getenv("DB_ENGINE") or DEFAULT_DB_ENGINE).strip().lower()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Deployment settings.

    vision_provider: ``auto`` | ``openai`` | ``gemini`` | ``anthropic`` |
    ``ollama`` | ``offline``.

    ``auto`` picks the first provider that has credentials available and falls
    back to the offline fingerprint matcher, so the product always works even
    with no internet and no API key.
    """

    database_url: str = field(default_factory=lambda: DATABASE_URL)
    database_source: str = field(default_factory=lambda: DATABASE_SOURCE)
    database_engine: str = field(default_factory=lambda: DATABASE_ENGINE)
    supabase_url: str = field(default_factory=lambda: SUPABASE_URL)
    supabase_publishable_key: str = field(default_factory=lambda: SUPABASE_PUBLISHABLE_KEY)
    vision_provider: str = field(default_factory=lambda: os.getenv("VISION_PROVIDER", "auto").lower())
    vision_model: str = field(default_factory=lambda: os.getenv("VISION_MODEL", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "llava"))

    # Google Search grounding: lets the vision model *search* to identify the
    # part and find part numbers, and returns the pages it used.
    grounding_enabled: bool = field(default_factory=lambda: _env_bool("SEARCH_GROUNDING", True))
    grounding_model: str = field(
        default_factory=lambda: os.getenv("GROUNDING_MODEL", "gemini-2.5-flash")
    )
    # Public URL of this deployment, needed to build one-tap Google Lens links.
    # Auto-detected from the request when empty.
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))

    # Shop identity used on generated cards.
    shop_name: str = field(default_factory=lambda: os.getenv("SHOP_NAME", "VERIFIX"))
    shop_tagline: str = field(default_factory=lambda: os.getenv("SHOP_TAGLINE", "OEM & GENUINE SPARE PARTS"))
    whatsapp_number: str = field(default_factory=lambda: os.getenv("WHATSAPP_NUMBER", "+254 700 000 000"))
    currency: str = field(default_factory=lambda: os.getenv("CURRENCY", "KES"))
    location: str = field(default_factory=lambda: os.getenv("SHOP_LOCATION", "Nairobi, Kenya"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "12")))
    # Demo catalogue only fills an empty database once. Set false in production
    # so a cleared database stays cleared instead of being re-seeded on boot.
    seed_demo_data: bool = field(default_factory=lambda: _env_bool("SEED_DEMO_DATA", True))
    require_verification: bool = field(default_factory=lambda: _env_bool("REQUIRE_PART_VERIFICATION", True))

    def ensure_dirs(self) -> None:
        for path in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Catalogue is re-read on change; keep the path here for the loader.
CATALOG_PATH = DATA_DIR / "catalog.json"

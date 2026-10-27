"""Runtime configuration for the parts identification engine.

Everything is environment-driven so the same code runs as a demo (no API key)
or wired to a real multimodal model in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
ASSET_DIR = BASE_DIR / "assets"
SEED_DIR = ASSET_DIR / "seed"
FONT_DIR = ASSET_DIR / "fonts"
UPLOAD_DIR = Path(os.getenv("APP_UPLOAD_DIR", BASE_DIR / "uploads"))
EXPORT_DIR = Path(os.getenv("APP_EXPORT_DIR", BASE_DIR / "exports"))
WEB_DIR = BASE_DIR / "web"
DB_PATH = Path(os.getenv("APP_DB_PATH", DATA_DIR / "inventory.db"))


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
    shop_name: str = field(default_factory=lambda: os.getenv("SHOP_NAME", "SHILA AUTO PARTS"))
    shop_tagline: str = field(default_factory=lambda: os.getenv("SHOP_TAGLINE", "OEM & GENUINE SPARE PARTS"))
    whatsapp_number: str = field(default_factory=lambda: os.getenv("WHATSAPP_NUMBER", "+254 700 000 000"))
    currency: str = field(default_factory=lambda: os.getenv("CURRENCY", "KES"))
    location: str = field(default_factory=lambda: os.getenv("SHOP_LOCATION", "Nairobi, Kenya"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "12")))
    require_verification: bool = field(default_factory=lambda: _env_bool("REQUIRE_PART_VERIFICATION", True))

    def ensure_dirs(self) -> None:
        for path in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Catalogue is re-read on change; keep the path here for the loader.
CATALOG_PATH = DATA_DIR / "catalog.json"

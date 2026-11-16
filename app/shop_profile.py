"""Persist non-secret shop identity and external profile links."""

from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR, settings
from .models import ShopProfileUpdate

PROFILE_PATH = DATA_DIR / "shop_profile.json"


def get_profile() -> dict:
    profile = {
        "shop_name": settings.shop_name,
        "tagline": settings.shop_tagline,
        "whatsapp_number": settings.whatsapp_number,
        "currency": settings.currency,
        "location": settings.location,
        "social_links": {"facebook": "", "instagram": "", "tiktok": "", "x": ""},
    }
    if not PROFILE_PATH.exists():
        return profile
    try:
        saved = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            return profile
        profile.update({key: saved[key] for key in profile if key in saved and key != "social_links"})
        if isinstance(saved.get("social_links"), dict):
            profile["social_links"].update({
                key: str(saved["social_links"].get(key) or "")
                for key in profile["social_links"]
            })
    except (OSError, ValueError, TypeError):
        return profile
    return profile


def save_profile(update: ShopProfileUpdate) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    profile = update.model_dump()
    temporary = PROFILE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    temporary.replace(PROFILE_PATH)
    return profile

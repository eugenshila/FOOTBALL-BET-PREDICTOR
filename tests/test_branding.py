"""The Verifix refresh must not reset custom branding or existing local sessions."""
import json
from pathlib import Path

import pytest

from app import auth, shop_profile
from app.config import Settings


@pytest.mark.parametrize("name", [
    None, "", "TRAIL VAULT", "Trail Vault", "VERIFIX", "Verifix",
    "SHILA AUTO PARTS", "Shila Auto Parts", "  shila   auto  parts  ",
])
def test_default_and_previous_brand_become_verifix(monkeypatch, name):
    if name is None:
        monkeypatch.delenv("SHOP_NAME", raising=False)
    else:
        monkeypatch.setenv("SHOP_NAME", name)
    assert Settings().shop_name == "Verifix"


def test_custom_shop_name_is_preserved(monkeypatch):
    monkeypatch.setenv("SHOP_NAME", "Nairobi Parts")
    assert Settings().shop_name == "Nairobi Parts"


def test_saved_legacy_brand_is_upgraded_without_touching_other_fields(monkeypatch, tmp_path):
    path = tmp_path / "shop_profile.json"
    path.write_text(json.dumps({
        "shop_name": "SHILA AUTO PARTS",
        "tagline": "Jeep spares",
        "currency": "KES",
        "social_links": {"instagram": "https://instagram.com/verifix"},
    }), encoding="utf-8")
    monkeypatch.setattr(shop_profile, "PROFILE_PATH", path)
    profile = shop_profile.get_profile()
    assert profile["shop_name"] == "Verifix"
    assert profile["tagline"] == "Jeep spares"
    assert profile["social_links"]["instagram"] == "https://instagram.com/verifix"


def test_saved_custom_brand_is_kept(monkeypatch, tmp_path):
    path = tmp_path / "shop_profile.json"
    path.write_text(json.dumps({"shop_name": "Verifix Counter"}), encoding="utf-8")
    monkeypatch.setattr(shop_profile, "PROFILE_PATH", path)
    assert shop_profile.get_profile()["shop_name"] == "Verifix Counter"


def test_login_brand_and_cookie_compatibility(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Verifix" in response.text
    assert "Trail Vault" not in response.text
    assert 'id="email"' in response.text and 'id="password"' in response.text
    assert auth.ACCESS_COOKIE == "trailvault_session"


def test_workspace_brand_and_local_icon(client):
    response = client.get("/")
    assert response.status_code == 200
    assert '<h1 id="shop-name">Verifix</h1>' in response.text
    assert "Trail Vault" not in response.text
    icon = client.get("/static/verifix.svg")
    assert icon.status_code == 200 and "<svg" in icon.text
    # Do not introduce a network dependency for typography.
    css = Path("web/static/styles.css").read_text()
    assert "https://fonts." not in css
    assert "prefers-reduced-motion" in css

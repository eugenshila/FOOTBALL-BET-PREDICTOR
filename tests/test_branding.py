"""The Verifix refresh must not reset custom branding or existing local sessions."""
from pathlib import Path

import pytest

from app import auth
from app.config import Settings


@pytest.mark.parametrize("name", [None, "", "TRAIL VAULT", "Trail Vault", "VERIFIX", "Verifix"])
def test_default_and_previous_brand_become_verifix(monkeypatch, name):
    if name is None:
        monkeypatch.delenv("SHOP_NAME", raising=False)
    else:
        monkeypatch.setenv("SHOP_NAME", name)
    assert Settings().shop_name == "Verifix"


def test_custom_shop_name_is_preserved(monkeypatch):
    monkeypatch.setenv("SHOP_NAME", "Nairobi Parts")
    assert Settings().shop_name == "Nairobi Parts"


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

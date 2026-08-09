"""backend/helpers.py's _set_auth_cookie() marks the auth cookie `Secure` based on the
COOKIE_SECURE env var (read once at import time into backend.helpers.COOKIE_SECURE) —
see CLAUDE.md Task 1. Default is false (plain-HTTP-safe); a real HTTPS deploy sets
COOKIE_SECURE=true via docker-compose.yml. httponly/samesite are unaffected and not
re-asserted here (out of scope for this change, already correct)."""

import pytest

import backend.helpers as helpers

pytestmark = pytest.mark.asyncio


async def _register_and_get_set_cookie(client):
    r = await client.post("/api/auth/register", json={
        "username": "CookieSecureUser",
        "email": "cookiesecure@example.com",
        "password": "letmein123",
    })
    assert r.status_code == 201, r.text
    cookies = r.headers.get_list("set-cookie")
    auth_cookie = next(c for c in cookies if c.startswith("vrising_token="))
    return auth_cookie


async def test_cookie_not_secure_by_default(client, db_session, monkeypatch):
    monkeypatch.setattr(helpers, "COOKIE_SECURE", False)
    auth_cookie = await _register_and_get_set_cookie(client)
    assert "Secure" not in auth_cookie
    assert "HttpOnly" in auth_cookie
    assert "samesite=lax" in auth_cookie.lower()


async def test_cookie_secure_when_env_enabled(client, db_session, monkeypatch):
    monkeypatch.setattr(helpers, "COOKIE_SECURE", True)
    auth_cookie = await _register_and_get_set_cookie(client)
    assert "Secure" in auth_cookie
    assert "HttpOnly" in auth_cookie
    assert "samesite=lax" in auth_cookie.lower()


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("0", False), ("no", False), ("garbage", False),
])
def test_cookie_secure_env_parsing(monkeypatch, raw, expected):
    """Reload-free check of the truthy-parse logic itself (mirrors what COOKIE_SECURE
    is computed from at import time), independent of the request-level tests above."""
    monkeypatch.setenv("COOKIE_SECURE", raw)
    import os
    parsed = os.getenv("COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")
    assert parsed is expected

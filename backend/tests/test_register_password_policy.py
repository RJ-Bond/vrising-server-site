"""POST /api/auth/register (backend/routers/auth.py) now enforces a real password
complexity bar via UserRegister.password_complexity (backend/schemas.py) — length >= 8
plus at least one letter and one digit — instead of the old bare 6-character minimum.
See CLAUDE.md Task 3. Mirrors PluginRegister.password_complexity's existing bar."""

import pytest

pytestmark = pytest.mark.asyncio


def _payload(password, username="NewPolicyUser", email="newpolicyuser@example.com"):
    return {"username": username, "email": email, "password": password}


async def test_short_password_rejected(client, db_session):
    r = await client.post("/api/auth/register", json=_payload("ab1", username="ShortPwUser", email="shortpw@example.com"))
    assert r.status_code == 422
    body = r.json()
    assert any("8" in (d.get("msg") or "") for d in body["detail"])


async def test_password_without_digit_rejected(client, db_session):
    r = await client.post("/api/auth/register", json=_payload("onlyletters", username="NoDigitUser", email="nodigit@example.com"))
    assert r.status_code == 422
    body = r.json()
    assert any("цифр" in (d.get("msg") or "").lower() or "digit" in (d.get("msg") or "").lower() for d in body["detail"])


async def test_password_without_letter_rejected(client, db_session):
    r = await client.post("/api/auth/register", json=_payload("12345678", username="NoLetterUser", email="noletter@example.com"))
    assert r.status_code == 422


async def test_valid_password_accepted(client, db_session):
    r = await client.post("/api/auth/register", json=_payload("letmein123", username="ValidPwUser", email="validpw@example.com"))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["user"]["username"] == "ValidPwUser"

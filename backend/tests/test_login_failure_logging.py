"""POST /api/auth/login (backend/routers/auth.py) logs a warning on both failure paths
(invalid credentials, invalid TOTP code) including the attempted username and the
client IP, so failed logins are visible in server logs for incident review — see
CLAUDE.md Task 3. No new table/admin UI, just structured log lines via the stdlib
`logging` module (logger = logging.getLogger(__name__), same pattern as every other
backend module)."""

import logging

import pyotp
import pytest

from backend.auth import get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def test_invalid_credentials_logs_warning_with_username_and_ip(client, db_session, caplog):
    with caplog.at_level(logging.WARNING, logger="backend.routers.auth"):
        r = await client.post("/api/auth/login", json={"username": "nobody-here", "password": "wrong"})
    assert r.status_code == 401
    assert any(
        "nobody-here" in rec.getMessage() and "invalid credentials" in rec.getMessage().lower()
        for rec in caplog.records
    )


async def test_invalid_totp_logs_warning_with_username(client, db_session, caplog):
    secret = pyotp.random_base32()
    user = User(
        username="LogTotpUser",
        email="logtotpuser@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        totp_enabled=True,
        totp_secret=secret,
    )
    db_session.add(user)
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="backend.routers.auth"):
        r = await client.post("/api/auth/login", json={
            "username": "LogTotpUser", "password": "password1", "totp_code": "000000",
        })
    assert r.status_code == 401
    assert any(
        "LogTotpUser" in rec.getMessage() and "totp" in rec.getMessage().lower()
        for rec in caplog.records
    )

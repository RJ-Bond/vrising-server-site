"""POST /api/admin/rcon (backend/routers/admin_system.py) — superadmin-tier per
CLAUDE.md's "Admin roles" section (RCON is full game-server console access, same
tier as backups/deploy/SSL). Covers the role-tier boundary (admin, one level below
superadmin, must be rejected — same pattern as test_admin_backups.py), the
"RCON password not configured" 400 short-circuit, and the happy/error paths of
admin_rcon() itself with _rcon_exec (the actual RCON socket client) monkeypatched
out — this suite never opens a real socket, matching how test_event_reminder_push.py
and friends stub out other external-I/O calls rather than hitting anything real.

SSL install (POST /api/admin/ssl/install, same file) is deliberately NOT covered
here — it drives the Docker socket (spins up a certbot container) and would need a
running Docker daemon to exercise for real; out of scope for a unit test."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import Setting, User
import backend.routers.admin_system as admin_system

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username, email=f"{username}@example.com",
        hashed_password=get_password_hash("x"), role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


async def _set_setting(db_session, key, value):
    db_session.add(Setting(key=key, value=value))
    await db_session.commit()


async def test_admin_tier_forbidden(client, db_session):
    """One level below superadmin — must be rejected regardless of RCON config."""
    admin = await _make_user(db_session, "rcon_admin", role="admin")
    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "listplayers"}, headers=_bearer(admin))
    assert r.status_code == 403


async def test_superadmin_can_reach_rcon_config_check(client, db_session):
    """Superadmin clears the role gate; with no rcon_password Setting row at all this
    still 400s on the "not configured" branch below it — this only proves the role
    check itself passes for superadmin (the actual RCON call path is covered by the
    mocked-_rcon_exec tests below)."""
    superadmin = await _make_user(db_session, "rcon_super", role="superadmin")
    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "listplayers"}, headers=_bearer(superadmin))
    assert r.status_code == 400
    assert "пароль" in r.json()["detail"].lower()


async def test_missing_rcon_password_400s(client, db_session):
    """rcon_password Setting present but empty string — same "not configured" 400,
    distinct from the row being entirely absent in the test above."""
    superadmin = await _make_user(db_session, "rcon_super2", role="superadmin")
    await _set_setting(db_session, "server_ip", "10.0.0.5")
    await _set_setting(db_session, "rcon_port", "25575")
    await _set_setting(db_session, "rcon_password", "")
    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "listplayers"}, headers=_bearer(superadmin))
    assert r.status_code == 400
    assert "пароль" in r.json()["detail"].lower()


async def test_empty_command_400s(client, db_session):
    superadmin = await _make_user(db_session, "rcon_super3", role="superadmin")
    await _set_setting(db_session, "rcon_password", "secret")
    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "   "}, headers=_bearer(superadmin))
    assert r.status_code == 400


async def test_successful_command_returns_output(client, db_session, monkeypatch):
    """Happy path — _rcon_exec is the actual game-server socket client, mocked out so
    this test never opens a real connection. Also verifies server=2 selects the
    server2_*-prefixed Settings, not server 1's."""
    superadmin = await _make_user(db_session, "rcon_super4", role="superadmin")
    await _set_setting(db_session, "server2_ip", "10.0.0.9")
    await _set_setting(db_session, "rcon2_port", "25576")
    await _set_setting(db_session, "rcon2_password", "hunter2")

    calls = []

    async def _fake_rcon_exec(ip, port, password, command, timeout=5.0):
        calls.append((ip, port, password, command))
        return "Players online: 3"

    monkeypatch.setattr(admin_system, "_rcon_exec", _fake_rcon_exec)

    r = await client.post("/api/admin/rcon", json={"server": 2, "command": "listplayers"}, headers=_bearer(superadmin))
    assert r.status_code == 200
    assert r.json() == {"output": "Players online: 3"}
    assert calls == [("10.0.0.9", 25576, "hunter2", "listplayers")]


async def test_wrong_rcon_password_returns_401(client, db_session, monkeypatch):
    """_rcon_exec raises ValueError for a rejected RCON auth handshake — admin_rcon
    maps that specifically to 401, distinct from any other failure (503 below)."""
    superadmin = await _make_user(db_session, "rcon_super5", role="superadmin")
    await _set_setting(db_session, "rcon_password", "wrong-guess")

    async def _fake_rcon_exec(ip, port, password, command, timeout=5.0):
        raise ValueError("RCON: неверный пароль")

    monkeypatch.setattr(admin_system, "_rcon_exec", _fake_rcon_exec)

    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "listplayers"}, headers=_bearer(superadmin))
    assert r.status_code == 401


async def test_connection_failure_returns_503(client, db_session, monkeypatch):
    """Any non-ValueError exception (connection refused/timeout/etc) maps to 503, not
    a raw 500 — the server being unreachable is an expected operational state, not a
    bug in this endpoint."""
    superadmin = await _make_user(db_session, "rcon_super6", role="superadmin")
    await _set_setting(db_session, "rcon_password", "secret")

    async def _fake_rcon_exec(ip, port, password, command, timeout=5.0):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(admin_system, "_rcon_exec", _fake_rcon_exec)

    r = await client.post("/api/admin/rcon", json={"server": 1, "command": "listplayers"}, headers=_bearer(superadmin))
    assert r.status_code == 503

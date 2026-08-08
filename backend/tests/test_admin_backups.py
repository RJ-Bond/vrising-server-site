"""DELETE /api/admin/backups/{filename} (backend/routers/admin_system.py) — superadmin-
tier per CLAUDE.md's "Admin roles" section, since backups are full-DB access. Covers the
happy path (file actually removed from disk), the role-tier boundary (admin, one level
below superadmin, must be rejected), and path-traversal filenames."""

import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(username=username, email=f"{username}@example.com", hashed_password=get_password_hash("x"), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    """Point admin_system's BACKUP_DIR (a module-level name copied out of
    backend.helpers at import time, not a live reference into that module) at a
    throwaway directory for this test only."""
    import backend.routers.admin_system as admin_system
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr(admin_system, "BACKUP_DIR", d)
    return d


async def test_superadmin_can_delete_backup(client, db_session, backup_dir):
    f = backup_dir / "vrising_20260101_000000.db"
    f.write_bytes(b"fake db content")

    superadmin = await _make_user(db_session, "backup_super", role="superadmin")
    r = await client.delete(f"/api/admin/backups/{f.name}", headers=_bearer(superadmin))
    assert r.status_code == 204
    assert not f.exists()


async def test_admin_tier_forbidden(client, db_session, backup_dir):
    f = backup_dir / "vrising_20260102_000000.db"
    f.write_bytes(b"fake db content")

    admin = await _make_user(db_session, "backup_admin", role="admin")
    r = await client.delete(f"/api/admin/backups/{f.name}", headers=_bearer(admin))
    assert r.status_code == 403
    assert f.exists()  # untouched


@pytest.mark.parametrize("evil_name", [
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "..\\..\\windows\\system32",
    "....//....//etc/passwd",
])
async def test_path_traversal_rejected(client, db_session, backup_dir, evil_name):
    superadmin = await _make_user(db_session, f"backup_super_pt_{hash(evil_name) & 0xffff}", role="superadmin")
    r = await client.delete(f"/api/admin/backups/{evil_name}", headers=_bearer(superadmin))
    # httpx path-encodes "../" segments before they ever reach FastAPI's routing, so a
    # traversal attempt either 400s (caught by the explicit ".." check) or 404s (path
    # doesn't resolve to a real file) — either way it must never reach path.unlink().
    assert r.status_code in (400, 404)


async def test_delete_nonexistent_backup_404s(client, db_session, backup_dir):
    superadmin = await _make_user(db_session, "backup_super_missing", role="superadmin")
    r = await client.delete("/api/admin/backups/vrising_does_not_exist.db", headers=_bearer(superadmin))
    assert r.status_code == 404

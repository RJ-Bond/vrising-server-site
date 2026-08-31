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


# ─── GET /api/admin/backups/disk-usage ───────────────────────────────────────
# Registered ahead of GET /api/admin/backups/{filename} (an untyped str path param, so
# it would otherwise greedily match the literal segment "disk-usage" as a filename —
# same class of route-ordering issue as GET /api/clans/leaderboard vs.
# GET /api/clans/{clan_id} in clans.py). These tests exercise the real filesystem
# (shutil.disk_usage has no meaningful fake), so they only assert shape/sanity, not
# exact byte counts.

async def test_disk_usage_requires_superadmin(client, db_session, backup_dir):
    admin = await _make_user(db_session, "disk_admin", role="admin")
    r = await client.get("/api/admin/backups/disk-usage", headers=_bearer(admin))
    assert r.status_code == 403


async def test_disk_usage_reports_totals_and_backup_size(client, db_session, backup_dir):
    (backup_dir / "vrising_20260101_000000.db").write_bytes(b"x" * 1000)
    (backup_dir / "vrising_20260102_000000.db").write_bytes(b"y" * 2000)
    (backup_dir / "not_a_backup.txt").write_bytes(b"ignored")

    superadmin = await _make_user(db_session, "disk_super", role="superadmin")
    r = await client.get("/api/admin/backups/disk-usage", headers=_bearer(superadmin))
    assert r.status_code == 200
    body = r.json()
    assert body["backups_count"] == 2
    assert body["backups_total_bytes"] == 3000
    assert body["disk_total"] >= body["disk_used"] >= 0
    assert body["disk_free"] >= 0


# ─── POST /api/admin/backups/create — now uses sqlite3's online backup API
# (helpers.sqlite_backup_copy) instead of a raw shutil.copy2 of the .db file, which
# isn't safe against a WAL-mode database (recently-committed data can be sitting in a
# separate -wal file a plain file copy would miss). Verifies the produced backup is
# actually a valid, readable SQLite database with the source's data in it. ───────────

@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """A real (tiny) SQLite database standing in for the live app DB, wired into
    admin_system.find_live_db_path() the same way `backup_dir` above wires in
    BACKUP_DIR — module-level name, patched for this test only."""
    import sqlite3
    import backend.routers.admin_system as admin_system
    db_path = tmp_path / "live_vrising.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sanity_check (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO sanity_check (value) VALUES ('hello')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(admin_system, "find_live_db_path", lambda: db_path)
    return db_path


async def test_create_backup_now_produces_a_valid_readable_sqlite_file(client, db_session, backup_dir, live_db):
    import sqlite3
    superadmin = await _make_user(db_session, "backup_creator", role="superadmin")
    r = await client.post("/api/admin/backups/create", headers=_bearer(superadmin))
    assert r.status_code == 201, r.text
    filename = r.json()["filename"]

    dst = backup_dir / filename
    assert dst.exists()
    conn = sqlite3.connect(str(dst))
    try:
        rows = conn.execute("SELECT value FROM sanity_check").fetchall()
    finally:
        conn.close()
    assert rows == [("hello",)]

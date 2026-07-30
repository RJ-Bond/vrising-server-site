"""Regression tests for GET /api/admin/update/check — compares the locally deployed
VERSION file against GitHub's latest Release for this repo (created by the CI release
workflow, .github/workflows/ci.yml) so the admin panel can show "update available" +
its changelog before the admin runs the actual update."""
import pytest

import backend.routers.admin_system as admin_system
from backend.auth import create_access_token, get_password_hash
from backend.models import User

pytestmark = pytest.mark.asyncio


async def _make_superadmin(db_session):
    user = User(username="owner", email="owner@example.com", hashed_password=get_password_hash("x"), role="superadmin")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def _fake_client(status_code=200, json_data=None):
    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _FakeResponse(status_code, json_data or {})

    return _FakeAsyncClient


async def test_reports_update_available_when_versions_differ(client, db_session, monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v2026.07.20")
    monkeypatch.setattr(admin_system, "_REPO_VERSION_FILE", version_file)
    monkeypatch.setattr(admin_system.httpx, "AsyncClient", _fake_client(200, {
        "tag_name": "v2026.07.23",
        "body": "- ✨ Add thing\n- \U0001f41b Fix thing",
        "html_url": "https://github.com/RJ-Bond/vrising-server-site/releases/tag/v2026.07.23",
        "published_at": "2026-07-23T00:00:00Z",
    }))

    owner = await _make_superadmin(db_session)
    r = await client.get("/api/admin/update/check", headers=_bearer(owner))
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["current_version"] == "v2026.07.20"
    assert body["latest_version"] == "v2026.07.23"
    assert body["up_to_date"] is False
    assert "Add thing" in body["changelog"]


async def test_reports_up_to_date_when_versions_match(client, db_session, monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v2026.07.23")
    monkeypatch.setattr(admin_system, "_REPO_VERSION_FILE", version_file)
    monkeypatch.setattr(admin_system.httpx, "AsyncClient", _fake_client(200, {
        "tag_name": "v2026.07.23", "body": "- ✨ Add thing",
        "html_url": "https://example.com", "published_at": "2026-07-23T00:00:00Z",
    }))

    owner = await _make_superadmin(db_session)
    r = await client.get("/api/admin/update/check", headers=_bearer(owner))
    body = r.json()
    assert body["up_to_date"] is True


async def test_degrades_gracefully_when_github_unreachable(client, db_session, monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("v2026.07.20")
    monkeypatch.setattr(admin_system, "_REPO_VERSION_FILE", version_file)

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            raise ConnectionError("no network in test")

    monkeypatch.setattr(admin_system.httpx, "AsyncClient", _BoomClient)

    owner = await _make_superadmin(db_session)
    r = await client.get("/api/admin/update/check", headers=_bearer(owner))
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["current_version"] == "v2026.07.20"


async def test_missing_version_file_falls_back_to_unknown(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(admin_system, "_REPO_VERSION_FILE", tmp_path / "does-not-exist")
    monkeypatch.setattr(admin_system.httpx, "AsyncClient", _fake_client(200, {"tag_name": "v2026.07.23", "body": ""}))

    owner = await _make_superadmin(db_session)
    r = await client.get("/api/admin/update/check", headers=_bearer(owner))
    assert r.json()["current_version"] == "unknown"


async def test_requires_superadmin(client, db_session):
    admin = User(username="regular_admin", email="ra@example.com", hashed_password=get_password_hash("x"), role="admin")
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    r = await client.get("/api/admin/update/check", headers=_bearer(admin))
    assert r.status_code == 403

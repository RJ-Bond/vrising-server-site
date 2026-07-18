"""Regression tests for GET /api/plugin/wipe-info — resolves wipe_date/wipe_type (server 1)
or wipe_date2/wipe_type2 (server 2) Settings, converts the stored local-wall-clock value
(entered via the admin panel's <input type="datetime-local">, e.g. "2030-06-15T18:30") to
UTC using the site's configured timezone (Setting "timezone", default Europe/Moscow), and
serializes with the same Z-suffixed format used by the scheduled-restart endpoints. See the
"Wipe countdown (plugin)" section comments in main.py."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.models import Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-wipe"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_wipe_info_requires_plugin_key(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/wipe-info")
    assert r.status_code == 401


async def test_wipe_info_null_when_unset(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/wipe-info", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"wipe_date": None, "wipe_type": None}


async def test_wipe_info_server1(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="wipe_date", value="2030-06-15T18:30"))
    db_session.add(Setting(key="wipe_type", value="partial"))
    await db_session.commit()

    r = await client.get("/api/plugin/wipe-info", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["wipe_type"] == "partial"
    assert body["wipe_date"].endswith("Z")

    local = datetime(2030, 6, 15, 18, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    expected_utc = local.astimezone(timezone.utc).replace(tzinfo=None)
    assert body["wipe_date"] == expected_utc.isoformat() + "Z"


async def test_wipe_info_server2_uses_separate_settings(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="wipe_date", value="2030-01-01T00:00"))
    db_session.add(Setting(key="wipe_type", value="full"))
    db_session.add(Setting(key="wipe_date2", value="2030-07-04T12:00"))
    db_session.add(Setting(key="wipe_type2", value="partial"))
    await db_session.commit()

    r = await client.get("/api/plugin/wipe-info", params={"server_num": 2}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["wipe_type"] == "partial"
    local = datetime(2030, 7, 4, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    expected_utc = local.astimezone(timezone.utc).replace(tzinfo=None)
    assert body["wipe_date"] == expected_utc.isoformat() + "Z"


async def test_wipe_info_unknown_server_num_is_null(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="wipe_date", value="2030-06-15T18:30"))
    db_session.add(Setting(key="wipe_type", value="full"))
    await db_session.commit()

    r = await client.get("/api/plugin/wipe-info", params={"server_num": 99}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"wipe_date": None, "wipe_type": None}


async def test_wipe_info_respects_custom_timezone_setting(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="timezone", value="UTC"))
    db_session.add(Setting(key="wipe_date", value="2030-06-15T18:30"))
    db_session.add(Setting(key="wipe_type", value="full"))
    await db_session.commit()

    r = await client.get("/api/plugin/wipe-info", params={"server_num": 1}, headers=_hdr())
    assert r.status_code == 200
    assert r.json()["wipe_date"] == "2030-06-15T18:30:00Z"

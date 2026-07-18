"""Regression tests for the plugin-facing connect/disconnect message template endpoint
GET /api/plugin/message-templates — lets the site drive the BepInEx plugin's connect/
disconnect chat message text (Settings "connect_message_template" /
"disconnect_message_template") without editing the plugin's local .cfg."""
import pytest

from backend.models import Setting

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-123"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def test_message_templates_without_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates")
    assert r.status_code == 401


async def test_message_templates_with_wrong_plugin_key_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates", headers=_hdr("not-the-real-key"))
    assert r.status_code == 401


async def test_message_templates_returns_empty_strings_when_unset(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.get("/api/plugin/message-templates", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"connect": "", "disconnect": ""}


async def test_message_templates_returns_saved_setting_values(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="connect_message_template", value="<color=#00FF00>{name} присоединился</color>"))
    db_session.add(Setting(key="disconnect_message_template", value="<color=#FF3355>{name} покинул сервер</color>"))
    await db_session.commit()

    r = await client.get("/api/plugin/message-templates", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {
        "connect": "<color=#00FF00>{name} присоединился</color>",
        "disconnect": "<color=#FF3355>{name} покинул сервер</color>",
    }


async def test_message_templates_partial_set_returns_empty_for_unset_side(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="connect_message_template", value="<color=#00FF00>{name} в сети</color>"))
    await db_session.commit()

    r = await client.get("/api/plugin/message-templates", headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"connect": "<color=#00FF00>{name} в сети</color>", "disconnect": ""}

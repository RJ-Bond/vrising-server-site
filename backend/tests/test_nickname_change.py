"""Regression tests for POST /api/plugin/nickname-change — backs the plugin's ".nick <name>"
chat command. The endpoint is the sole authority on validation/cost/cooldown/points; it
always returns 200 with {"ok": bool, ...} (never an HTTP error status for a business-rule
rejection) so the plugin can relay "message" straight to chat. See the "Nickname change
(plugin, spends points)" section in backend/routers/plugin_integration.py."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.auth import get_password_hash
from backend.models import PointsTransaction, Setting, User

pytestmark = pytest.mark.asyncio

PLUGIN_KEY = "test-plugin-key-nick"


async def _set_plugin_key(db_session, value=PLUGIN_KEY):
    db_session.add(Setting(key="plugin_api_key", value=value))
    await db_session.commit()


def _hdr(key=PLUGIN_KEY):
    return {"X-Plugin-Key": key}


async def _make_user(db_session, username, steam_id, points_balance=0):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
        steam_id=steam_id,
        points_balance=points_balance,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_requires_plugin_key(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post("/api/plugin/nickname-change", json={"steam_id": "1", "new_nickname": "Foo"})
    assert r.status_code == 401


async def test_unlinked_steam_id_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": "76561198000000001", "new_nickname": "Newname"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "not_linked"


async def test_successful_change_deducts_default_cost_and_logs_ledger_row(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Renamer1", "76561198000000002", points_balance=500)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "CoolName"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "new_nickname": "CoolName", "cost": 100, "balance": 400}

    await db_session.refresh(user)
    assert user.points_balance == 400

    rows = (await db_session.execute(
        select(PointsTransaction).where(PointsTransaction.user_id == user.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == "nickname_change"
    assert rows[0].delta == -100
    assert rows[0].balance_after == 400


async def test_insufficient_points_is_rejected_and_does_not_charge(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "PoorRenamer", "76561198000000003", points_balance=10)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "CoolName"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "insufficient_points"

    await db_session.refresh(user)
    assert user.points_balance == 10


@pytest.mark.parametrize("bad_name", ["", "a", "x" * 25, "Name<color=#fff>", "Name[tag]", "   "])
async def test_invalid_names_are_rejected(client, db_session, bad_name):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Validator1", "76561198000000004", points_balance=1000)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": bad_name},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_name"

    await db_session.refresh(user)
    assert user.points_balance == 1000  # never charged


async def test_reserved_name_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Validator2", "76561198000000005", points_balance=1000)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "Admin"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "reserved_name"


async def test_cyrillic_name_within_length_is_accepted(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Validator3", "76561198000000006", points_balance=1000)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "Тёмный Рыцарь"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["new_nickname"] == "Тёмный Рыцарь"


async def test_second_change_within_cooldown_is_rejected(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Renamer2", "76561198000000007", points_balance=1000)

    r1 = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "FirstName"},
        headers=_hdr(),
    )
    assert r1.json()["ok"] is True

    r2 = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "SecondName"},
        headers=_hdr(),
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["ok"] is False
    assert body2["error"] == "cooldown"

    await db_session.refresh(user)
    # Only the first change's cost was ever deducted.
    assert user.points_balance == 900


async def test_change_allowed_again_after_cooldown_window_passes(client, db_session):
    await _set_plugin_key(db_session)
    user = await _make_user(db_session, "Renamer3", "76561198000000008", points_balance=1000)

    db_session.add(PointsTransaction(
        user_id=user.id, delta=-100, balance_after=900, reason="nickname_change",
        detail="Old -> Older", created_at=datetime.utcnow() - timedelta(days=8),
    ))
    user.points_balance = 900
    await db_session.commit()

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "FreshName"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["balance"] == 800


async def test_custom_cost_and_cooldown_settings_are_respected(client, db_session):
    await _set_plugin_key(db_session)
    db_session.add(Setting(key="nickname_change_cost", value="250"))
    db_session.add(Setting(key="nickname_change_cooldown_days", value="1"))
    await db_session.commit()
    user = await _make_user(db_session, "Renamer4", "76561198000000009", points_balance=1000)

    r = await client.post(
        "/api/plugin/nickname-change",
        json={"steam_id": user.steam_id, "new_nickname": "ConfiguredName"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cost"] == 250
    assert body["balance"] == 750

"""Regression tests for the admin-facing "sustained server downtime" Web Push alert
in backend/main.py: _monitor_poll_cycle() tracks how long a server has been
continuously offline via the module-level _offline_since dict and, once that exceeds
ADMIN_DOWNTIME_ALERT_THRESHOLD, fans a push out to every admin-tier-or-above user
(role_level >= "admin") with a PushSubscription via _notify_admins_server_down() —
exactly once per outage episode (tracked via _admin_alerted_offline), re-arming only
once the server is observed online again. This is the inverse of the player-facing
"server is back online" trigger covered by test_server_online_push.py."""
import asyncio
import time

import pytest

import backend.main as main
from backend.auth import get_password_hash
from backend.models import PushSubscription, Setting, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _seed_server1_settings(db_session, name="Test Server"):
    db_session.add_all([
        Setting(key="server_ip", value="127.0.0.1"),
        Setting(key="server_port", value="9876"),
        Setting(key="server_name", value=name),
    ])
    await db_session.commit()


def _fake_status(online: bool):
    async def _fake(ip, port):
        return {
            "online": online,
            "name": "Raw Server Name",
            "players": 3,
            "max_players": 20,
            "version": "1.0",
            "map": "TestMap",
            "vac": False,
            "players_list": [],
            "latency_ms": 10,
        }
    return _fake


class _TaskTracker:
    """Same shape as test_server_online_push.py's tracker: waits for the whole
    fire-and-forget chain (_monitor_poll_cycle -> _notify_admins_server_down ->
    send_push per admin) to finish before a test asserts on it."""

    def __init__(self, monkeypatch):
        self.tasks = []
        self._orig = asyncio.create_task

        def _tracking_create_task(coro, *a, **kw):
            t = self._orig(coro, *a, **kw)
            self.tasks.append(t)
            return t

        monkeypatch.setattr(asyncio, "create_task", _tracking_create_task)

    async def drain(self):
        for _ in range(50):
            await asyncio.sleep(0.01)
            if all(t.done() for t in self.tasks):
                break
        await asyncio.gather(*self.tasks)


@pytest.fixture
def push_calls(monkeypatch):
    calls = []

    async def _fake_send_push(user_id, title, body, url="/"):
        calls.append({"user_id": user_id, "title": title, "body": body, "url": url})

    monkeypatch.setattr(main, "send_push", _fake_send_push)
    return calls


@pytest.fixture(autouse=True)
def _clean_monitor_state(monkeypatch):
    """Module-level dicts in main.py that would otherwise leak state between tests."""
    monkeypatch.setattr(main, "_prev_server_online", {})
    monkeypatch.setattr(main, "_status_cache", {})
    monkeypatch.setattr(main, "_status_cache_ts", {})
    monkeypatch.setattr(main, "_last_snapshot", {})
    monkeypatch.setattr(main, "_offline_since", {})
    monkeypatch.setattr(main, "_admin_alerted_offline", set())


async def _seed_admin_with_push(db_session, username="AlertAdmin", role="admin"):
    admin = await _make_user(db_session, username, role=role)
    db_session.add(PushSubscription(user_id=admin.id, endpoint=f"https://push.example/{username}", p256dh="p", auth="a"))
    await db_session.commit()
    return admin


async def test_no_alert_when_offline_under_threshold(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    await _seed_admin_with_push(db_session)

    monkeypatch.setattr(main, "get_server_status", _fake_status(False))
    # Offline for well under ADMIN_DOWNTIME_ALERT_THRESHOLD (15 min).
    main._offline_since[1] = time.time() - 60

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert push_calls == []
    assert 1 not in main._admin_alerted_offline


async def test_alert_fires_once_past_threshold_across_multiple_polls(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    admin = await _seed_admin_with_push(db_session)
    # A regular (non-admin) subscriber must NOT get this operational alert.
    regular = await _make_user(db_session, "RegularUser", role="user")
    db_session.add(PushSubscription(user_id=regular.id, endpoint="https://push.example/regular", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(main, "get_server_status", _fake_status(False))
    # Already offline for longer than the threshold as of the first poll.
    main._offline_since[1] = time.time() - (main.ADMIN_DOWNTIME_ALERT_THRESHOLD + 60)

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert len(push_calls) == 1
    assert push_calls[0]["user_id"] == admin.id
    assert 1 in main._admin_alerted_offline

    # A second poll cycle while still offline must NOT re-fire.
    tracker2 = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker2.drain()

    assert len(push_calls) == 1


async def test_alert_rearms_after_recovery_and_new_outage(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    admin = await _seed_admin_with_push(db_session)

    def downtime_alerts():
        # The "recovers" step below also fires the pre-existing, unrelated
        # _notify_server_back_online() player-facing push (the admin's subscription
        # counts for that broadcast too, since it targets every subscriber) — filter
        # to just this feature's own message so that expected, separate push doesn't
        # get confused for a second/duplicate downtime alert.
        return [c for c in push_calls if c["title"] == "Сервер недоступен"]

    # First outage: already past threshold, alert fires.
    monkeypatch.setattr(main, "get_server_status", _fake_status(False))
    main._offline_since[1] = time.time() - (main.ADMIN_DOWNTIME_ALERT_THRESHOLD + 60)
    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()
    assert len(downtime_alerts()) == 1

    # Server recovers — state must reset (this also fires the unrelated "back
    # online" push to the admin, filtered out by downtime_alerts() above).
    monkeypatch.setattr(main, "get_server_status", _fake_status(True))
    tracker2 = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker2.drain()
    assert 1 not in main._offline_since
    assert 1 not in main._admin_alerted_offline

    # New outage, also already past threshold — must alert again (re-armed).
    monkeypatch.setattr(main, "get_server_status", _fake_status(False))
    main._offline_since[1] = time.time() - (main.ADMIN_DOWNTIME_ALERT_THRESHOLD + 60)
    tracker3 = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker3.drain()

    alerts = downtime_alerts()
    assert len(alerts) == 2
    assert alerts[1]["user_id"] == admin.id

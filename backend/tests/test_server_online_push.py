"""Regression tests for the "favorite server back online" Web Push trigger in
backend/main.py: _monitor_poll_cycle() detects an offline->online transition per
server_num (tracked via the module-level _prev_server_online dict) and fans out a
push notification to every user with at least one PushSubscription row via
_notify_server_back_online(). Must fire only on a real transition — never on every
poll while a server stays online, and never on the very first poll after a process
restart (unknown previous state)."""
import asyncio

import pytest

import backend.main as main
from backend.auth import get_password_hash
from backend.models import PushSubscription, Setting, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username):
    user = User(
        username=username,
        email=f"{username.lower()}@example.com",
        hashed_password=get_password_hash("password1"),
        role="user",
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
    """Tracks every asyncio.create_task() call made anywhere (including nested calls
    made by tasks this tracker already scheduled) so a test can wait for the whole
    fire-and-forget chain (_monitor_poll_cycle -> _notify_server_back_online ->
    send_push per user) to actually finish before asserting on it."""

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
    """_prev_server_online/_status_cache/_last_snapshot are module-level dicts in
    main.py that would otherwise leak transition state between tests."""
    monkeypatch.setattr(main, "_prev_server_online", {})
    monkeypatch.setattr(main, "_status_cache", {})
    monkeypatch.setattr(main, "_status_cache_ts", {})
    monkeypatch.setattr(main, "_last_snapshot", {})


async def test_push_fires_on_offline_to_online_transition(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session, name="Crimson Keep")
    user1 = await _make_user(db_session, "PushSrvUser1")
    user2 = await _make_user(db_session, "PushSrvUser2")
    db_session.add_all([
        PushSubscription(user_id=user1.id, endpoint="https://push.example/srv1", p256dh="p", auth="a"),
        PushSubscription(user_id=user2.id, endpoint="https://push.example/srv2", p256dh="p", auth="a"),
    ])
    await db_session.commit()

    monkeypatch.setattr(main, "get_server_status", _fake_status(True))
    main._prev_server_online[1] = False  # was offline

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert len(push_calls) == 2
    notified_user_ids = {c["user_id"] for c in push_calls}
    assert notified_user_ids == {user1.id, user2.id}
    for c in push_calls:
        assert "Crimson Keep" in c["body"]
        assert c["url"] == "/servers.html"

    # state updated so a later poll while still online won't re-fire
    assert main._prev_server_online[1] is True


async def test_no_push_when_online_stays_online(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    user = await _make_user(db_session, "PushSrvUser3")
    db_session.add(PushSubscription(user_id=user.id, endpoint="https://push.example/srv3", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(main, "get_server_status", _fake_status(True))
    main._prev_server_online[1] = True  # already online

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert push_calls == []
    assert main._prev_server_online[1] is True


async def test_no_push_on_first_poll_after_restart(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    user = await _make_user(db_session, "PushSrvUser4")
    db_session.add(PushSubscription(user_id=user.id, endpoint="https://push.example/srv4", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(main, "get_server_status", _fake_status(True))
    assert 1 not in main._prev_server_online  # unknown previous state

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert push_calls == []
    # now known, so a genuine future transition can be detected
    assert main._prev_server_online[1] is True


async def test_no_push_when_offline_stays_offline(db_session, monkeypatch, push_calls):
    await _seed_server1_settings(db_session)
    user = await _make_user(db_session, "PushSrvUser5")
    db_session.add(PushSubscription(user_id=user.id, endpoint="https://push.example/srv5", p256dh="p", auth="a"))
    await db_session.commit()

    monkeypatch.setattr(main, "get_server_status", _fake_status(False))
    main._prev_server_online[1] = False

    tracker = _TaskTracker(monkeypatch)
    await main._monitor_poll_cycle()
    await tracker.drain()

    assert push_calls == []
    assert main._prev_server_online[1] is False

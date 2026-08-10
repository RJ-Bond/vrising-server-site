"""Regression tests for the event "starting in ~1 hour" Web Push reminder in
backend/main.py: _event_reminder_cycle() looks for Event rows with status
upcoming/active whose start_date falls inside a narrow window (55-65 minutes from
now) and pushes every EventParticipant of that event exactly once (tracked via the
module-level _reminded_event_ids set)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import backend.main as main
from backend.auth import get_password_hash
from backend.models import Event, EventParticipant, PushSubscription, User

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


async def _make_event(db_session, creator, *, title, minutes_from_now, status="upcoming"):
    start = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes_from_now)
    ev = Event(
        title=title,
        description="",
        event_type="pvp",
        start_date=start,
        status=status,
        created_by=creator.id,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    return ev


async def _join(db_session, event, user):
    db_session.add(EventParticipant(event_id=event.id, user_id=user.id))
    await db_session.commit()


class _TaskTracker:
    """Same shape as test_server_online_push.py's tracker: records every
    asyncio.create_task() call (including nested ones) so a test can wait for the
    whole fire-and-forget chain (_event_reminder_cycle -> send_push per participant)
    to actually finish before asserting on it."""

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
def _clean_reminder_state(monkeypatch):
    """_reminded_event_ids is a module-level set in main.py that would otherwise leak
    between tests."""
    monkeypatch.setattr(main, "_reminded_event_ids", set())


async def test_event_60_min_out_gets_reminded(db_session, monkeypatch, push_calls):
    creator = await _make_user(db_session, "ReminderCreator1")
    participant = await _make_user(db_session, "ReminderUser1")
    db_session.add(PushSubscription(user_id=participant.id, endpoint="https://push.example/rem1", p256dh="p", auth="a"))
    await db_session.commit()

    ev = await _make_event(db_session, creator, title="Осада замка", minutes_from_now=60)
    await _join(db_session, ev, participant)

    tracker = _TaskTracker(monkeypatch)
    await main._event_reminder_cycle()
    await tracker.drain()

    assert len(push_calls) == 1
    assert push_calls[0]["user_id"] == participant.id
    assert "Осада замка" in push_calls[0]["body"]
    assert push_calls[0]["url"] == "/events.html"
    assert ev.id in main._reminded_event_ids


async def test_event_90_min_out_not_yet_reminded(db_session, monkeypatch, push_calls):
    creator = await _make_user(db_session, "ReminderCreator2")
    participant = await _make_user(db_session, "ReminderUser2")
    db_session.add(PushSubscription(user_id=participant.id, endpoint="https://push.example/rem2", p256dh="p", auth="a"))
    await db_session.commit()

    ev = await _make_event(db_session, creator, title="Слишком рано", minutes_from_now=90)
    await _join(db_session, ev, participant)

    tracker = _TaskTracker(monkeypatch)
    await main._event_reminder_cycle()
    await tracker.drain()

    assert push_calls == []
    assert ev.id not in main._reminded_event_ids


async def test_already_reminded_event_not_reminded_again(db_session, monkeypatch, push_calls):
    creator = await _make_user(db_session, "ReminderCreator3")
    participant = await _make_user(db_session, "ReminderUser3")
    db_session.add(PushSubscription(user_id=participant.id, endpoint="https://push.example/rem3", p256dh="p", auth="a"))
    await db_session.commit()

    ev = await _make_event(db_session, creator, title="Уже напомнили", minutes_from_now=60)
    await _join(db_session, ev, participant)
    main._reminded_event_ids.add(ev.id)  # simulate an earlier poll cycle already reminded it

    tracker = _TaskTracker(monkeypatch)
    await main._event_reminder_cycle()
    await tracker.drain()

    assert push_calls == []


async def test_cancelled_event_never_reminded(db_session, monkeypatch, push_calls):
    creator = await _make_user(db_session, "ReminderCreator4")
    participant = await _make_user(db_session, "ReminderUser4")
    db_session.add(PushSubscription(user_id=participant.id, endpoint="https://push.example/rem4", p256dh="p", auth="a"))
    await db_session.commit()

    ev = await _make_event(db_session, creator, title="Отменено", minutes_from_now=60, status="cancelled")
    await _join(db_session, ev, participant)

    tracker = _TaskTracker(monkeypatch)
    await main._event_reminder_cycle()
    await tracker.drain()

    assert push_calls == []

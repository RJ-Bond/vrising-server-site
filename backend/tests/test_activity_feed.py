"""Regression tests for GET /api/activity-feed — the read-only homepage "what's
happening" widget merging recent news, recently-created events, and playtime-hours /
connect-streak milestone crossings into one reverse-chronological, capped list."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import get_password_hash
from backend.models import Event, News, PlayerDailyActivity, PlayerRankSnapshot, PlayerRecord, ShopRedemption, User

pytestmark = pytest.mark.asyncio


def _day(offset):
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


async def _make_user(db_session, username, **kwargs):
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=get_password_hash("x"),
        **kwargs,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_news(db_session, author_id, title, slug, **kwargs):
    news = News(
        title=title,
        slug=slug,
        summary=kwargs.pop("summary", "Краткое описание"),
        content=kwargs.pop("content", "<p>Текст новости</p>"),
        author_id=author_id,
        published=kwargs.pop("published", True),
        **kwargs,
    )
    db_session.add(news)
    await db_session.commit()
    return news


async def _make_event(db_session, creator_id, title, **kwargs):
    ev = Event(
        title=title,
        start_date=kwargs.pop("start_date", datetime.now(timezone.utc) + timedelta(days=1)),
        created_by=creator_id,
        **kwargs,
    )
    db_session.add(ev)
    await db_session.commit()
    return ev


async def test_empty_feed_returns_empty_list(client, db_session):
    r = await client.get("/api/activity-feed")
    assert r.status_code == 200
    assert r.json() == []


async def test_includes_published_news_excludes_unpublished(client, db_session):
    author = await _make_user(db_session, "author1")
    await _make_news(db_session, author.id, "Опубликованная новость", "published-news")
    await _make_news(db_session, author.id, "Черновик", "draft-news", published=False)

    r = await client.get("/api/activity-feed")
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "news"
    assert body[0]["title"] == "Опубликованная новость"
    assert body[0]["url"] == "/?news=published-news"
    assert body[0]["subtitle"] == "Краткое описание"


async def test_includes_recently_created_event(client, db_session):
    author = await _make_user(db_session, "eventauthor")
    await _make_event(db_session, author.id, "Турнир выходного дня")

    r = await client.get("/api/activity-feed")
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "event"
    assert body[0]["title"] == "Турнир выходного дня"
    assert body[0]["url"] == "/events.html"


async def test_news_and_events_sorted_reverse_chronologically(client, db_session):
    author = await _make_user(db_session, "mixauthor")
    now = datetime.now(timezone.utc)
    await _make_news(db_session, author.id, "Старая новость", "old-news", created_at=now - timedelta(hours=5))
    await _make_event(db_session, author.id, "Свежее событие", created_at=now - timedelta(hours=1))
    await _make_news(db_session, author.id, "Свежая новость", "fresh-news", created_at=now - timedelta(minutes=10))

    r = await client.get("/api/activity-feed")
    titles = [item["title"] for item in r.json()]
    assert titles == ["Свежая новость", "Свежее событие", "Старая новость"]


async def test_news_category_capped(client, db_session):
    author = await _make_user(db_session, "capauthor")
    for i in range(9):
        await _make_news(db_session, author.id, f"Новость {i}", f"news-{i}")

    r = await client.get("/api/activity-feed")
    body = r.json()
    assert len(body) == 6  # _NEWS_LIMIT
    assert all(item["type"] == "news" for item in body)


async def test_hour_milestone_included_when_crossed_since_last_snapshot(client, db_session):
    await _make_user(db_session, "MilestonePlayer", steam_id="100")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(
        server_num=1, player_name="MilestonePlayer", steam_id="100",
        total_seconds=40000, last_seen=now,
    ))
    # Snapshot from before crossing 36000s (10h) — proves this is a *crossing*, not
    # just "currently above".
    db_session.add(PlayerRankSnapshot(
        server_num=1, player_name="MilestonePlayer", total_seconds=30000, recorded_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    body = r.json()
    milestones = [item for item in body if item["type"] == "milestone"]
    assert len(milestones) == 1
    assert milestones[0]["title"] == "MilestonePlayer"
    assert "10+" in milestones[0]["subtitle"]
    assert milestones[0]["url"] == "/user.html?u=MilestonePlayer"


async def test_hour_milestone_not_reported_when_already_above_threshold(client, db_session):
    await _make_user(db_session, "AlreadyThere", steam_id="101")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(
        server_num=1, player_name="AlreadyThere", steam_id="101",
        total_seconds=40000, last_seen=now,
    ))
    # Snapshot already above the 10h tier — no crossing happened recently.
    db_session.add(PlayerRankSnapshot(
        server_num=1, player_name="AlreadyThere", total_seconds=37000, recorded_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    milestones = [item for item in r.json() if item["type"] == "milestone"]
    assert milestones == []


async def test_hour_milestone_skipped_without_baseline_snapshot(client, db_session):
    # A brand-new player with no PlayerRankSnapshot history yet must not be credited
    # with "just crossing" a tier their very first session happens to exceed.
    await _make_user(db_session, "BrandNew", steam_id="102")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(
        server_num=1, player_name="BrandNew", steam_id="102",
        total_seconds=40000, last_seen=now,
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    milestones = [item for item in r.json() if item["type"] == "milestone"]
    assert milestones == []


async def test_milestone_skipped_when_no_linked_user_account(client, db_session):
    # steam_id present on PlayerRecord but no User row has that steam_id (never ran
    # the in-game .register command) — no profile to link to, so skip.
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(
        server_num=1, player_name="Unlinked", steam_id="103",
        total_seconds=40000, last_seen=now,
    ))
    db_session.add(PlayerRankSnapshot(
        server_num=1, player_name="Unlinked", total_seconds=30000, recorded_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    milestones = [item for item in r.json() if item["type"] == "milestone"]
    assert milestones == []


async def test_streak_milestone_included_on_exact_tier_day(client, db_session):
    await _make_user(db_session, "StreakHero", steam_id="200")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(server_num=1, player_name="StreakHero", steam_id="200", total_seconds=500, last_seen=now))
    db_session.add_all([
        PlayerDailyActivity(server_num=1, steam_id="200", activity_date=_day(i)) for i in range(3)
    ])
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    milestones = [item for item in r.json() if item["type"] == "milestone"]
    assert len(milestones) == 1
    assert milestones[0]["title"] == "StreakHero"
    assert "3 дня подряд" in milestones[0]["subtitle"]


async def test_streak_milestone_not_reported_off_tier(client, db_session):
    await _make_user(db_session, "MidStreak", steam_id="201")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(server_num=1, player_name="MidStreak", steam_id="201", total_seconds=500, last_seen=now))
    db_session.add_all([
        PlayerDailyActivity(server_num=1, steam_id="201", activity_date=_day(i)) for i in range(4)
    ])
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    milestones = [item for item in r.json() if item["type"] == "milestone"]
    assert milestones == []


async def test_stale_player_not_considered_for_milestones(client, db_session):
    # Seen outside the recency lookback window — must not surface a milestone no
    # matter how it would score, since this feed is about "recently".
    await _make_user(db_session, "StaleGhost", steam_id="300")
    now = datetime.now(timezone.utc)
    db_session.add(PlayerRecord(
        server_num=1, player_name="StaleGhost", steam_id="300",
        total_seconds=40000, last_seen=now - timedelta(days=30),
    ))
    db_session.add(PlayerRankSnapshot(
        server_num=1, player_name="StaleGhost", total_seconds=30000, recorded_at=now - timedelta(days=31),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    assert r.json() == []


async def test_mixed_feed_shape_and_overall_cap(client, db_session):
    author = await _make_user(db_session, "shapeauthor")
    now = datetime.now(timezone.utc)
    await _make_news(db_session, author.id, "Shape News", "shape-news", created_at=now)
    await _make_event(db_session, author.id, "Shape Event", created_at=now - timedelta(minutes=1))
    await _make_user(db_session, "ShapePlayer", steam_id="400")
    db_session.add(PlayerRecord(
        server_num=1, player_name="ShapePlayer", steam_id="400",
        total_seconds=40000, last_seen=now - timedelta(minutes=2),
    ))
    db_session.add(PlayerRankSnapshot(
        server_num=1, player_name="ShapePlayer", total_seconds=30000, recorded_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed", params={"server": 1})
    body = r.json()
    types = {item["type"] for item in body}
    assert types == {"news", "event", "milestone"}
    for item in body:
        assert set(item.keys()) == {"type", "title", "subtitle", "url", "icon", "timestamp"}
        assert item["timestamp"]  # non-empty ISO string


# ─── Shop redemptions ────────────────────────────────────────────────────────

async def test_fulfilled_shop_redemption_included(client, db_session):
    buyer = await _make_user(db_session, "Buyer1")
    db_session.add(ShopRedemption(
        user_id=buyer.id, item_name_snapshot="Waypoint Shard", cost_snapshot=50,
        status="fulfilled", resolved_at=datetime.now(timezone.utc), resolved_by="AdminOne",
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed")
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "shop_redemption"
    assert body[0]["title"] == "Waypoint Shard"
    assert body[0]["subtitle"] == "Получил: Buyer1"
    assert body[0]["url"] == "/shop.html"


async def test_pending_and_cancelled_redemptions_excluded(client, db_session):
    buyer = await _make_user(db_session, "Buyer2")
    db_session.add(ShopRedemption(
        user_id=buyer.id, item_name_snapshot="Pending Item", cost_snapshot=10, status="pending",
    ))
    db_session.add(ShopRedemption(
        user_id=buyer.id, item_name_snapshot="Cancelled Item", cost_snapshot=10, status="cancelled",
        resolved_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    r = await client.get("/api/activity-feed")
    assert [item for item in r.json() if item["type"] == "shop_redemption"] == []

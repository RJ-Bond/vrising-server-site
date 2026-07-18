from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, Float
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(128), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False, default="user")
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    avatar_url = Column(String(512), nullable=True)
    clan_id = Column(Integer, ForeignKey("clans.id", ondelete="SET NULL"), nullable=True)
    rules_accepted_at = Column(DateTime, nullable=True)
    game_nickname = Column(String(64), nullable=True)
    admin_title = Column(String(128), nullable=True)
    last_active_at = Column(DateTime, nullable=True)
    badge_icon_url = Column(String(512), nullable=True)
    badge_style = Column(String(32), nullable=True, default='default')
    cover_url = Column(String(512), nullable=True)
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False, server_default="0")
    bio = Column(String(160), nullable=True)
    # Set by the BepInEx plugin's .register/.login in-game commands — the authoritative
    # link between a site account and a game account (unlike game_nickname, which is just
    # a free-text field with no verification). Nullable: most existing/web-registered users
    # have no linked game account.
    steam_id = Column(String(32), unique=True, nullable=True, index=True)
    # Added via ALTER TABLE in main.py's lifespan (not a fresh-install column), but
    # declared here too so Base.metadata.create_all() (used by backend/tests/) creates
    # it on a from-scratch test DB. auth.py/main.py still read/write it via raw text()
    # SQL rather than this attribute — left that way to avoid touching working code.
    revoke_before = Column(DateTime, nullable=True)


class Clan(Base):
    __tablename__ = "clans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(32), unique=True, nullable=False, index=True)
    tag = Column(String(6), unique=True, nullable=False, index=True)
    description = Column(String(256), nullable=True, default="")
    leader_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class GameClan(Base):
    """In-game clan roster, synced wholesale from the BepInEx plugin via
    POST /api/plugin/clans/sync. Read-only from the website's perspective — the game
    is the source of truth, not the old manually-managed `Clan` model above (which is
    kept in place as unused dead schema rather than risking a SQLite DROP COLUMN
    migration on User.clan_id)."""

    __tablename__ = "game_clans"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    clan_guid = Column(String(36), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    motto = Column(String(64), nullable=True, default="")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    members = relationship("GameClanMember", back_populates="clan", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (UniqueConstraint("server_num", "clan_guid", name="uq_game_clan_server_guid"),)


class GameClanMember(Base):
    __tablename__ = "game_clan_members"

    id = Column(Integer, primary_key=True, index=True)
    clan_id = Column(Integer, ForeignKey("game_clans.id", ondelete="CASCADE"), nullable=False)
    steam_id = Column(String(32), nullable=False)
    character_name = Column(String(64), nullable=False)
    role = Column(String(16), nullable=False, default="member")  # "member" | "officer" | "leader"

    clan = relationship("GameClan", back_populates="members")

    __table_args__ = (Index("ix_game_clan_members_clan", "clan_id"),)


class News(Base):
    __tablename__ = "news"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(256), nullable=False)
    slug = Column(String(300), unique=True, nullable=False, index=True)
    summary = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    thumbnail_url = Column(String(512), nullable=True)
    tags = Column(String(256), nullable=True, default="")
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    published = Column(Boolean, default=True, nullable=False)
    pinned = Column(Boolean, default=False, nullable=False)
    views = Column(Integer, default=0, nullable=False)
    publish_at = Column(DateTime, nullable=True)
    is_template = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    author = relationship("User", backref="news_posts", lazy="selectin")
    comments = relationship("Comment", back_populates="news", cascade="all, delete-orphan", lazy="noload")


class ServerSnapshot(Base):
    __tablename__ = "server_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    recorded_at = Column(DateTime, nullable=False)
    online = Column(Boolean, nullable=False, default=False)
    players = Column(Integer, nullable=False, default=0)
    max_players = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=True)
    map_name = Column(String(128), nullable=True)

    __table_args__ = (Index("ix_snapshots_server_time", "server_num", "recorded_at"),)


class PlayerRecord(Base):
    __tablename__ = "player_records"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    player_name = Column(String(128), nullable=False)
    total_seconds = Column(Integer, nullable=False, default=0)
    last_seen = Column(DateTime, nullable=True)
    last_duration = Column(Integer, nullable=False, default=0)
    session_count = Column(Integer, nullable=False, default=0)
    steam_id = Column(String(32), nullable=True, index=True)  # set once the plugin reports a session for this row; NULL for A2S-only rows never claimed by a real session report

    __table_args__ = (UniqueConstraint("server_num", "player_name", name="uq_player_server"),)


class PlayerRankSnapshot(Base):
    """Nightly copy of each player's total_seconds, used to compute leaderboard rank deltas."""
    __tablename__ = "player_rank_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    player_name = Column(String(128), nullable=False)
    total_seconds = Column(Integer, nullable=False, default=0)
    recorded_at = Column(DateTime, nullable=False)

    __table_args__ = (Index("ix_rank_snap_server_player_time", "server_num", "player_name", "recorded_at"),)


class Wipe(Base):
    __tablename__ = "wipes"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    wipe_type = Column(String(32), nullable=False, default="full")
    wipe_date = Column(DateTime, nullable=False)
    note = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    news_id = Column(Integer, ForeignKey("news.id", ondelete="CASCADE"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)
    parent_id = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    author = relationship("User", lazy="selectin")
    news = relationship("News", back_populates="comments")
    replies = relationship("Comment", foreign_keys="[Comment.parent_id]", lazy="noload")

    __table_args__ = (Index("ix_comments_news_id", "news_id"),)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class Reaction(Base):
    __tablename__ = "reactions"
    id = Column(Integer, primary_key=True, index=True)
    news_id = Column(Integer, ForeignKey("news.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    emoji = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("news_id", "user_id", "emoji", name="uq_reaction"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    admin_username = Column(String(64), nullable=False)
    action = Column(String(128), nullable=False)
    target_type = Column(String(50), nullable=True)
    target_id = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False, nullable=False)


class CommentReaction(Base):
    __tablename__ = "comment_reactions"
    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, ForeignKey("comments.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    emoji = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("comment_id", "user_id", "emoji", name="uq_comment_reaction"),)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(32), nullable=False)  # "reply", "mention"
    data = Column(Text, nullable=False, default="{}")  # JSON: {comment_id, news_slug, news_title, from_username, preview}
    read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (Index("ix_notifications_user", "user_id", "read"),)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    sender = relationship("User", foreign_keys=[sender_id], lazy="selectin")
    recipient = relationship("User", foreign_keys=[recipient_id], lazy="selectin")


class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_type = Column(String(32), nullable=False)  # "comment", "user", "news"
    target_id = Column(Integer, nullable=False)
    reason = Column(String(512), nullable=False)
    status = Column(String(16), nullable=False, default="pending")  # pending, reviewed, dismissed
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    reviewed_at = Column(DateTime, nullable=True)


class Poll(Base):
    __tablename__ = "polls"
    id = Column(Integer, primary_key=True, index=True)
    news_id = Column(Integer, ForeignKey("news.id", ondelete="CASCADE"), nullable=False)
    question = Column(String(256), nullable=False)
    multiple = Column(Boolean, default=False, nullable=False)
    ends_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    options = relationship("PollOption", back_populates="poll", cascade="all, delete-orphan", lazy="selectin")


class PollOption(Base):
    __tablename__ = "poll_options"
    id = Column(Integer, primary_key=True, index=True)
    poll_id = Column(Integer, ForeignKey("polls.id", ondelete="CASCADE"), nullable=False)
    text = Column(String(256), nullable=False)
    poll = relationship("Poll", back_populates="options")


class PollVote(Base):
    __tablename__ = "poll_votes"
    id = Column(Integer, primary_key=True, index=True)
    poll_id = Column(Integer, ForeignKey("polls.id", ondelete="CASCADE"), nullable=False)
    option_id = Column(Integer, ForeignKey("poll_options.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint("poll_id", "user_id", name="uq_poll_vote"),)


class PageView(Base):
    __tablename__ = "page_views"
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String(256), nullable=False)
    ip_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (Index("ix_page_views_date", "created_at"),)


class ErrorLog(Base):
    __tablename__ = "error_logs"
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String(256), nullable=False)
    method = Column(String(8), nullable=False, default="GET")
    status_code = Column(Integer, nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (Index("ix_error_logs_date", "created_at"),)


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(512), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    event_type = Column(String(32), nullable=False, default="pvp")  # pvp | pve | social | other
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)
    max_participants = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False, default="upcoming")  # upcoming | active | ended | cancelled
    cover_url = Column(String(512), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    participants = relationship("EventParticipant", back_populates="event", cascade="all, delete-orphan", lazy="noload")
    creator = relationship("User", foreign_keys=[created_by], lazy="selectin")


class EventParticipant(Base):
    __tablename__ = "event_participants"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    registered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    event = relationship("Event", back_populates="participants")
    user = relationship("User", lazy="selectin")


class PluginHeartbeat(Base):
    __tablename__ = "plugin_heartbeats"

    server_num = Column(Integer, primary_key=True)
    server_name = Column(String(128), nullable=True)
    plugin_version = Column(String(32), nullable=True)
    player_count = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(DateTime, nullable=False)


class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(Text, nullable=False)
    interval_minutes = Column(Integer, nullable=True)  # NULL = send once, never repeat
    enabled = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # optional; NULL = never expires
    last_sent_at = Column(DateTime, nullable=True)
    target_steam_id = Column(String(32), nullable=True)  # NULL = broadcast to everyone (normal case); set = a one-off test send to a single player's SteamID
    server_num = Column(Integer, nullable=False, default=1)  # which game server this announcement broadcasts to
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class ServerMessageTemplate(Base):
    """Per-server connect/disconnect in-game chat message templates, replacing the old
    global "connect_message_template"/"disconnect_message_template" Settings now that the
    plugin runs on more than one server — one row per server_num, created on first save."""
    __tablename__ = "server_message_templates"

    server_num = Column(Integer, primary_key=True)
    connect_template = Column(Text, nullable=True)
    disconnect_template = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class ServerApiKey(Base):
    """Optional per-server override of the global "plugin_api_key" Setting — lets one
    game server have its own secret (better isolation: a leaked config only compromises
    that server) while servers without a row here keep using the global key as a
    fallback. See _require_plugin_key in main.py for the lookup/precedence logic."""
    __tablename__ = "server_api_keys"

    server_num = Column(Integer, primary_key=True)
    api_key = Column(String(128), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class ScheduledRestart(Base):
    """Per-server pending scheduled restart — one row per server_num, upserted via
    POST /api/plugin/schedule-restart (in-game admin chat command) or the admin panel's
    POST /api/admin/servers/{server_num}/restart. The plugin polls
    GET /api/plugin/restart-status?server_num=N (same cadence as its heartbeat) to know
    when to start broadcasting a countdown and when to execute the actual restart;
    restart_at is naive UTC (this repo's usual DateTime convention) and is cleared back
    to None (row kept, not deleted) rather than deleted, both when an admin cancels a
    pending restart and by the plugin itself right after it executes one.

    daily_restart_time is an independent, optional recurring schedule layered on top of
    the one-off restart_at above — e.g. "06:00", interpreted in the site's configured
    timezone (Setting "timezone"). Set/cleared via GET/POST/DELETE
    /api/admin/servers/{server_num}/daily-restart. GET /api/plugin/restart-status
    self-arms it: whenever restart_at is None but daily_restart_time is set, that
    endpoint computes the next occurrence, persists it into restart_at (as if an admin
    had just scheduled a one-off restart), and returns it — no separate cron/scheduler
    needed. Cancelling a restart (admin or plugin cleanup) only ever clears restart_at,
    never daily_restart_time, so the next poll re-arms the following day automatically."""
    __tablename__ = "scheduled_restarts"

    server_num = Column(Integer, primary_key=True)
    restart_at = Column(DateTime, nullable=True)
    daily_restart_time = Column(String(8), nullable=True)


class Warning(Base):
    """A moderation warning issued to a player via the in-game .warn admin chat command
    (POST /api/plugin/warn), listed back via .warnings (GET /api/plugin/warnings).
    steam_id is the authoritative identity (players may rename their in-game character);
    admin_name is just the issuing admin's in-game character name for an audit trail —
    there's no admin user_id FK since the "admin" here is whoever had IsAdmin in-game at
    the time, not necessarily a linked site account. created_at is naive UTC (this repo's
    usual DateTime convention), set explicitly by the endpoint rather than a column
    default."""
    __tablename__ = "warnings"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    steam_id = Column(String(32), nullable=False, index=True)
    character_name = Column(String(64), nullable=False)
    reason = Column(String(512), nullable=False)
    admin_name = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)

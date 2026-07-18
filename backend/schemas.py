from datetime import datetime
from html.parser import HTMLParser
from typing import Literal, Optional
from pydantic import BaseModel, EmailStr, field_validator
import re


class _TagStripper(HTMLParser):
    """Collects only the text data of an HTML fragment, dropping every tag."""
    def __init__(self):
        super().__init__()
        self._fed = []

    def handle_data(self, d):
        self._fed.append(d)

    def get_data(self):
        return ''.join(self._fed)


def strip_html_tags(value: str) -> str:
    """Defense-in-depth for plain-text fields (comments, DMs): today the frontend
    always escapes/sanitizes before rendering these, so this isn't exploitable via
    the current UI — but nothing on the backend enforced that contract, so any future
    rendering path (a new client, an admin-panel tweak) that trusted this field raw
    would have stored XSS. Strip tags at the source instead of trusting every future
    consumer to remember to escape."""
    stripper = _TagStripper()
    stripper.feed(value)
    return stripper.get_data()


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str
    game_nickname: Optional[str] = None

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[a-zA-Z0-9_а-яёА-ЯЁ ]{3,32}$", v):
            raise ValueError("Имя пользователя: 3–32 символа, буквы, цифры, _ и пробелы")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class UserLogin(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


class PluginRegister(BaseModel):
    """Body for POST /api/plugin/register — sent by the BepInEx plugin's .register
    in-game command. steam_id is the authoritative identity; character_name becomes
    the site username."""
    steam_id: str
    character_name: str
    password: str
    server_num: int = 1

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class PluginLogin(BaseModel):
    """Body for POST /api/plugin/login — links steam_id to an existing site account
    (e.g. one created via the website) after verifying username+password."""
    steam_id: str
    character_name: str
    password: str
    server_num: int = 1


class PluginHeartbeatIn(BaseModel):
    """Body for POST /api/plugin/heartbeat — sent periodically (~every 60s) by the
    BepInEx plugin so the admin panel can show whether it's actually talking to the site."""
    server_num: int = 1
    server_name: Optional[str] = None
    plugin_version: Optional[str] = None
    player_count: int = 0


class PluginHeartbeatOut(BaseModel):
    server_num: int
    server_name: Optional[str] = None
    plugin_version: Optional[str] = None
    player_count: int = 0
    last_seen_at: datetime

    model_config = {"from_attributes": True}


class AnnouncementCreate(BaseModel):
    """Body for POST /api/admin/announcements. interval_minutes=None means the
    announcement is sent once (on the plugin's next poll) and never repeats."""
    text: str
    interval_minutes: Optional[int] = None
    expires_at: Optional[datetime] = None
    enabled: bool = True
    server_num: int = 1

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Announcement text cannot be empty")
        return v[:200]

    @field_validator("interval_minutes")
    @classmethod
    def interval_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("interval_minutes must be greater than 0")
        return v


class AnnouncementUpdate(BaseModel):
    """Body for PUT /api/admin/announcements/{id} — every field optional (partial update),
    same exclude_unset convention as the rest of this schema: server_num defaults to None
    (not 1) so an update that omits it never silently reassigns the announcement to a
    different server."""
    text: Optional[str] = None
    interval_minutes: Optional[int] = None
    expires_at: Optional[datetime] = None
    enabled: Optional[bool] = None
    server_num: Optional[int] = None

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Announcement text cannot be empty")
        return v[:200]

    @field_validator("interval_minutes")
    @classmethod
    def interval_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("interval_minutes must be greater than 0")
        return v


class AnnouncementOut(BaseModel):
    id: int
    text: str
    interval_minutes: Optional[int] = None
    enabled: bool
    expires_at: Optional[datetime] = None
    last_sent_at: Optional[datetime] = None
    target_steam_id: Optional[str] = None
    server_num: int = 1
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnnouncementTestSend(BaseModel):
    """Body for POST /api/admin/announcements/test-send — a one-off announcement sent
    only to the requesting admin's own linked SteamID (see current_user.steam_id), not
    broadcast to everyone. server_num picks which server's plugin instance should deliver
    it, since the admin's character might only be online on one of several servers."""
    text: str
    server_num: int = 1

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Announcement text cannot be empty")
        return v[:200]


class ServerMessageTemplateOut(BaseModel):
    """Response for GET /api/admin/message-templates and GET /api/plugin/message-templates
    — empty strings mean "not set" (the plugin falls back to its own local default)."""
    connect: str = ""
    disconnect: str = ""


class ServerMessageTemplateUpdate(BaseModel):
    """Body for PUT /api/admin/message-templates?server_num=N — partial update, same
    exclude_unset convention as AnnouncementUpdate: a field omitted from the body leaves
    that side of the row untouched."""
    connect: Optional[str] = None
    disconnect: Optional[str] = None


class ServerApiKeyOut(BaseModel):
    """Response for GET /api/admin/server-api-key?server_num=N — empty string means this
    server has no key of its own and is using the global plugin_api_key Setting as a
    fallback (see _require_plugin_key in main.py)."""
    api_key: str = ""


class ServerApiKeyUpdate(BaseModel):
    """Body for PUT /api/admin/server-api-key?server_num=N. An empty api_key clears the
    per-server override (deletes the row) so the server reverts to the global fallback
    key, rather than being stored as a literal empty-string secret."""
    api_key: str = ""


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    avatar_url: Optional[str] = None
    cover_url: Optional[str] = None
    rules_accepted_at: Optional[datetime] = None
    game_nickname: Optional[str] = None
    admin_title: Optional[str] = None
    last_active_at: Optional[datetime] = None
    badge_icon_url: Optional[str] = None
    badge_style: Optional[str] = 'default'
    totp_enabled: bool = False
    bio: Optional[str] = None

    model_config = {"from_attributes": True}


class LinkedAccountOut(BaseModel):
    """A site account linked to a SteamID via the in-game .register/.login flow
    (see User.steam_id). Deliberately narrower than UserOut — this view is only ever
    shown next to the SteamID/unlink action, so email/role/etc. would just be noise."""
    id: int
    username: str
    steam_id: str
    avatar_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class NewsCreate(BaseModel):
    title: str
    summary: str = ''
    content: str
    thumbnail_url: Optional[str] = None
    tags: Optional[str] = None
    published: bool = True
    publish_at: Optional[datetime] = None
    is_template: bool = False

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Title cannot be empty")
        return v.strip()


class NewsUpdate(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    thumbnail_url: Optional[str] = None
    tags: Optional[str] = None
    published: Optional[bool] = None
    pinned: Optional[bool] = None
    publish_at: Optional[datetime] = None
    is_template: Optional[bool] = None


class NewsOut(BaseModel):
    id: int
    title: str
    slug: str
    summary: str
    content: str
    thumbnail_url: Optional[str]
    tags: Optional[str] = None
    published: bool
    pinned: bool = False
    views: int = 0
    publish_at: Optional[datetime] = None
    is_template: bool = False
    created_at: datetime
    updated_at: datetime
    author: UserOut

    model_config = {"from_attributes": True}


class NewsListOut(BaseModel):
    id: int
    title: str
    slug: str
    summary: str
    thumbnail_url: Optional[str]
    tags: Optional[str] = None
    published: bool
    pinned: bool = False
    views: int = 0
    publish_at: Optional[datetime] = None
    is_template: bool = False
    created_at: datetime
    author: UserOut
    comment_count: int = 0

    model_config = {"from_attributes": True}


class PlayerRecordOut(BaseModel):
    id: int
    server_num: int
    player_name: str
    total_seconds: int
    last_seen: Optional[datetime] = None
    last_duration: int = 0
    session_count: int = 0
    avatar_url: Optional[str] = None  # populated at runtime from User table, not stored in PlayerRecord
    rank_delta: Optional[int] = None  # populated at runtime: rank change vs. ~7 days ago (positive = climbed)
    verified: bool = False  # populated at runtime from PlayerRecord.steam_id is not None — True once at least
    # one session for this row was reported by the game plugin, vs. purely from A2S polling.
    # Exposed as a boolean (not the raw steam_id) since this is a public-facing response.

    model_config = {"from_attributes": True}


class WipeCreate(BaseModel):
    server_num: int = 1
    wipe_type: str = "full"
    wipe_date: datetime
    note: Optional[str] = None

    @field_validator("wipe_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("full", "map", "progress"):
            raise ValueError("wipe_type must be full, map, or progress")
        return v

    @field_validator("server_num")
    @classmethod
    def validate_server(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError("server_num must be 1 or 2")
        return v


class WipeOut(BaseModel):
    id: int
    server_num: int
    wipe_type: str
    wipe_date: datetime
    note: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CommentCreate(BaseModel):
    content: str
    parent_id: Optional[int] = None

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = strip_html_tags(v).strip()
        if not v:
            raise ValueError("Comment cannot be empty")
        if len(v) > 2000:
            raise ValueError("Comment too long (max 2000 chars)")
        return v


class CommentUpdate(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = strip_html_tags(v).strip()
        if not v:
            raise ValueError("Comment cannot be empty")
        if len(v) > 2000:
            raise ValueError("Comment too long (max 2000 chars)")
        return v


class CommentOut(BaseModel):
    id: int
    content: str
    parent_id: Optional[int] = None
    created_at: datetime
    author: Optional[UserOut] = None
    replies: list["CommentOut"] = []
    reactions: dict = {}
    user_reaction: Optional[str] = None

    model_config = {"from_attributes": True}


class NotificationOut(BaseModel):
    id: int
    type: str
    data: str  # JSON string
    read: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class SettingUpdate(BaseModel):
    value: str


class SettingOut(BaseModel):
    key: str
    value: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class PaginatedNews(BaseModel):
    items: list[NewsListOut]
    total: int
    page: int
    pages: int


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def pw_min_length(cls, v: str) -> str:
        if len(v.strip()) < 6:
            raise ValueError("Минимум 6 символов")
        return v.strip()


class ChangeEmailBody(BaseModel):
    new_email: EmailStr
    password: str


class ReactBody(BaseModel):
    emoji: str


class PaginatedComments(BaseModel):
    items: list[CommentOut]
    total: int
    page: int
    pages: int


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatHistoryItem] = []


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def pw_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Минимум 6 символов")
        return v


class SetupComplete(BaseModel):
    username: str
    email: EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[a-zA-Z0-9_а-яёА-ЯЁ ]{3,32}$", v):
            raise ValueError("Имя пользователя: 3–32 символа, буквы, цифры, _ и пробелы")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class PluginSessionReport(BaseModel):
    """Body for POST /api/plugin/sessions — sent by the BepInEx plugin's SessionTracker
    when a player disconnects, reporting the session that just ended. steam_id is the
    authoritative identity used to upsert/claim the matching PlayerRecord row (see the
    endpoint docstring for the claim logic)."""
    server_num: int = 1
    steam_id: str
    character_name: str
    session_seconds: int
    ended_at: Optional[datetime] = None


class PluginClanMemberIn(BaseModel):
    steam_id: str
    character_name: str
    role: str = "member"  # member | officer | leader


class PluginClanIn(BaseModel):
    clan_guid: str
    name: str
    motto: Optional[str] = ""
    members: list[PluginClanMemberIn] = []


class PluginClansSyncIn(BaseModel):
    """Body for POST /api/plugin/clans/sync — the plugin's FULL current clan roster for
    one server. This replaces (not merges/diffs) all game_clans rows for that server_num,
    since the plugin always sends its complete current state, not incremental deltas."""
    server_num: int = 1
    clans: list[PluginClanIn] = []


class GameClanMemberOut(BaseModel):
    steam_id: str
    character_name: str
    role: str
    # populated at read time by joining User.steam_id — None if this player never linked
    # a site account via .register/.login
    username: Optional[str] = None
    avatar_url: Optional[str] = None

    model_config = {"from_attributes": True}


class GameClanOut(BaseModel):
    id: int
    server_num: int
    server_name: str
    clan_guid: str
    name: str
    motto: Optional[str] = ""
    updated_at: datetime
    member_count: int = 0

    model_config = {"from_attributes": True}


class GameClanDetailOut(GameClanOut):
    members: list[GameClanMemberOut] = []


class ReportCreate(BaseModel):
    target_type: str
    target_id: int
    reason: str

    @field_validator("target_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("comment", "user", "news"):
            raise ValueError("target_type must be comment, user, or news")
        return v

    @field_validator("reason")
    @classmethod
    def reason_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reason cannot be empty")
        return v[:512]


class ReportReview(BaseModel):
    status: str
    admin_note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("reviewed", "dismissed"):
            raise ValueError("status must be reviewed or dismissed")
        return v


class ReportOut(BaseModel):
    id: int
    reporter_id: Optional[int] = None
    target_type: str
    target_id: int
    reason: str
    status: str
    admin_note: Optional[str] = None
    created_at: datetime
    reviewed_at: Optional[datetime] = None
    model_config = {"from_attributes": True}


class PollOptionCreate(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Option text cannot be empty")
        return v[:256]


class PollCreate(BaseModel):
    question: str
    multiple: bool = False
    ends_at: Optional[datetime] = None
    options: list[PollOptionCreate]

    @field_validator("question")
    @classmethod
    def question_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty")
        return v[:256]

    @field_validator("options")
    @classmethod
    def min_options(cls, v: list) -> list:
        if len(v) < 2:
            raise ValueError("Poll must have at least 2 options")
        return v


class PollOptionOut(BaseModel):
    id: int
    text: str
    votes: int = 0
    model_config = {"from_attributes": True}


class PollOut(BaseModel):
    id: int
    news_id: int
    question: str
    multiple: bool
    ends_at: Optional[datetime] = None
    created_at: datetime
    options: list[PollOptionOut] = []
    total_votes: int = 0
    user_voted: list[int] = []
    model_config = {"from_attributes": True}



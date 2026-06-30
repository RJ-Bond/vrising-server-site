from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, EmailStr, field_validator
import re


class UserRegister(BaseModel):
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


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    avatar_url: Optional[str] = None

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
    avatar_url: Optional[str] = None

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

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        v = v.strip()
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
        v = v.strip()
        if not v:
            raise ValueError("Comment cannot be empty")
        if len(v) > 2000:
            raise ValueError("Comment too long (max 2000 chars)")
        return v


class CommentOut(BaseModel):
    id: int
    content: str
    created_at: datetime
    author: Optional[UserOut] = None

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


class ClanCreate(BaseModel):
    name: str
    tag: str
    description: Optional[str] = ""

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[a-zA-Z0-9_а-яёА-ЯЁ \-]{3,32}$", v):
            raise ValueError("Название клана: 3–32 символа, буквы, цифры, пробел, _ и -")
        return v

    @field_validator("tag")
    @classmethod
    def tag_valid(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.match(r"^[A-ZА-ЯЁ0-9]{2,6}$", v):
            raise ValueError("Тег клана: 2–6 латинских/кириллических букв или цифр")
        return v

    @field_validator("description")
    @classmethod
    def desc_len(cls, v: Optional[str]) -> str:
        v = (v or "").strip()
        if len(v) > 256:
            raise ValueError("Описание клана: максимум 256 символов")
        return v


class ClanUpdate(BaseModel):
    description: Optional[str] = ""

    @field_validator("description")
    @classmethod
    def desc_len(cls, v: Optional[str]) -> str:
        v = (v or "").strip()
        if len(v) > 256:
            raise ValueError("Описание клана: максимум 256 символов")
        return v


class ClanMemberOut(BaseModel):
    id: int
    username: str
    avatar_url: Optional[str] = None

    model_config = {"from_attributes": True}


class ClanOut(BaseModel):
    id: int
    name: str
    tag: str
    description: Optional[str] = ""
    leader_id: int
    leader_username: str
    member_count: int
    created_at: datetime


class ClanDetailOut(ClanOut):
    members: list[ClanMemberOut] = []

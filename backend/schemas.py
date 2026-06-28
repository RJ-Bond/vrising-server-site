from datetime import datetime
from typing import Optional
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


class NewsOut(BaseModel):
    id: int
    title: str
    slug: str
    summary: str
    content: str
    thumbnail_url: Optional[str]
    tags: Optional[str] = None
    published: bool
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
    created_at: datetime
    author: UserOut

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
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatHistoryItem] = []


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

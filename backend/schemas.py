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
        if not re.match(r"^[a-zA-Z0-9_]{3,32}$", v):
            raise ValueError("Username must be 3-32 chars, letters/digits/underscore only")
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

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class NewsCreate(BaseModel):
    title: str
    summary: str
    content: str
    thumbnail_url: Optional[str] = None
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
    published: Optional[bool] = None


class NewsOut(BaseModel):
    id: int
    title: str
    slug: str
    summary: str
    content: str
    thumbnail_url: Optional[str]
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
    published: bool
    created_at: datetime
    author: UserOut

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

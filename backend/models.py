from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, UniqueConstraint
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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    avatar_url = Column(String(512), nullable=True)


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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    author = relationship("User", backref="news_posts", lazy="selectin")
    comments = relationship("Comment", back_populates="news", cascade="all, delete-orphan", lazy="noload")


class PlayerRecord(Base):
    __tablename__ = "player_records"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    player_name = Column(String(128), nullable=False)
    total_seconds = Column(Integer, nullable=False, default=0)
    last_seen = Column(DateTime, nullable=True)
    last_duration = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("server_num", "player_name", name="uq_player_server"),)


class Wipe(Base):
    __tablename__ = "wipes"

    id = Column(Integer, primary_key=True, index=True)
    server_num = Column(Integer, nullable=False, default=1)
    wipe_type = Column(String(32), nullable=False, default="full")
    wipe_date = Column(DateTime, nullable=False)
    note = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    news_id = Column(Integer, ForeignKey("news.id", ondelete="CASCADE"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    author = relationship("User", lazy="selectin")
    news = relationship("News", back_populates="comments")

    __table_args__ = (Index("ix_comments_news_id", "news_id"),)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(String(512), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

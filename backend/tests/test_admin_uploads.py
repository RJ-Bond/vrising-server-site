"""Regression tests for the "used_by" cross-reference GET /api/admin/uploads and
GET /api/admin/media (backend/routers/admin_system.py) share via
_uploads_used_by_lookup() — checks Settings (site logo/hero logo/favicon/background),
News (thumbnail + inline content), ShopItem.image_url, and User.avatar_url against the
upload directory listing, powering the admin file manager's "используется"/"не
используется" badges and unused-file filter. Deliberately not exhaustive (see that
function's docstring) — these tests only cover the columns it actually checks."""
import pytest

from backend.auth import create_access_token, get_password_hash
from backend.models import News, Setting, ShopItem, User

pytestmark = pytest.mark.asyncio


async def _make_user(db_session, username, role="user"):
    user = User(username=username, email=f"{username}@example.com", hashed_password=get_password_hash("x"), role=role)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bearer(user):
    token = create_access_token({"sub": str(user.id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """Points admin_system's UPLOAD_DIR (a module-level name copied out of
    backend.helpers at import time — same pattern as test_admin_backups.py's
    backup_dir fixture for BACKUP_DIR) at a throwaway directory for this test only."""
    import backend.routers.admin_system as admin_system
    d = tmp_path / "uploads"
    d.mkdir()
    monkeypatch.setattr(admin_system, "UPLOAD_DIR", d)
    return d


async def test_uploads_requires_admin(client, db_session, upload_dir):
    user = await _make_user(db_session, "UploadsPlain", role="user")
    r = await client.get("/api/admin/uploads", headers=_bearer(user))
    assert r.status_code == 403


async def test_uploads_flags_file_referenced_by_setting(client, db_session, upload_dir):
    (upload_dir / "logo123.png").write_bytes(b"x")
    (upload_dir / "orphan.png").write_bytes(b"y")
    db_session.add(Setting(key="site_logo_url", value="/api/uploads/logo123.png"))
    await db_session.commit()

    admin = await _make_user(db_session, "UploadsAdmin1", role="admin")
    r = await client.get("/api/admin/uploads", headers=_bearer(admin))
    assert r.status_code == 200
    by_name = {f["filename"]: f for f in r.json()}
    assert by_name["logo123.png"]["used_by"][0]["type"] == "logo"
    assert by_name["orphan.png"]["used_by"] == []


async def test_uploads_flags_file_referenced_by_shop_item_image(client, db_session, upload_dir):
    (upload_dir / "item.png").write_bytes(b"x")
    db_session.add(ShopItem(name="Cool Sword", cost=10, image_url="/api/uploads/item.png"))
    await db_session.commit()

    admin = await _make_user(db_session, "UploadsAdmin2", role="admin")
    r = await client.get("/api/admin/uploads", headers=_bearer(admin))
    assert r.status_code == 200
    by_name = {f["filename"]: f for f in r.json()}
    used = by_name["item.png"]["used_by"]
    assert len(used) == 1
    assert used[0]["type"] == "shop_item"
    assert "Cool Sword" in used[0]["label"]


async def test_uploads_flags_file_referenced_by_news_thumbnail_and_content(client, db_session, upload_dir):
    (upload_dir / "thumb.png").write_bytes(b"x")
    (upload_dir / "inline.png").write_bytes(b"y")
    author = await _make_user(db_session, "NewsAuthor1", role="admin")
    db_session.add(News(
        title="Post", slug="post-1", summary="s", author_id=author.id,
        thumbnail_url="/api/uploads/thumb.png",
        content="<p>see <img src=\"/api/uploads/inline.png\"></p>",
    ))
    await db_session.commit()

    r = await client.get("/api/admin/uploads", headers=_bearer(author))
    by_name = {f["filename"]: f for f in r.json()}
    assert by_name["thumb.png"]["used_by"][0]["type"] == "news_thumb"
    assert by_name["inline.png"]["used_by"][0]["type"] == "news_content"


async def test_uploads_flags_file_referenced_by_avatar(client, db_session, upload_dir):
    (upload_dir / "av.png").write_bytes(b"x")
    user = await _make_user(db_session, "AvatarUser1", role="user")
    user.avatar_url = "/api/uploads/av.png"
    await db_session.commit()

    admin = await _make_user(db_session, "UploadsAdmin3", role="admin")
    r = await client.get("/api/admin/uploads", headers=_bearer(admin))
    by_name = {f["filename"]: f for f in r.json()}
    used = by_name["av.png"]["used_by"]
    assert used[0]["type"] == "avatar"
    assert "AvatarUser1" in used[0]["label"]


async def test_media_requires_admin(client, db_session, upload_dir):
    user = await _make_user(db_session, "MediaPlain", role="user")
    r = await client.get("/api/admin/media", headers=_bearer(user))
    assert r.status_code == 403


async def test_media_includes_used_by_like_uploads(client, db_session, upload_dir):
    (upload_dir / "bg.png").write_bytes(b"x")
    (upload_dir / "unused.png").write_bytes(b"y")
    db_session.add(Setting(key="bg_image_url", value="/api/uploads/bg.png"))
    await db_session.commit()

    admin = await _make_user(db_session, "MediaAdmin1", role="admin")
    r = await client.get("/api/admin/media", headers=_bearer(admin))
    assert r.status_code == 200
    by_name = {f["filename"]: f for f in r.json()["items"]}
    assert by_name["bg.png"]["used_by"][0]["type"] == "background"
    assert by_name["unused.png"]["used_by"] == []

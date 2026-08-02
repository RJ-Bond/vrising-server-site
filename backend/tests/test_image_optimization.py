"""optimize_image_bytes() (backend/helpers.py) — downscale/re-compress an uploaded
raster image before it hits disk, used by every upload endpoint (admin generic upload,
avatar, profile cover, badge icon)."""
from io import BytesIO

from PIL import Image

from backend.helpers import optimize_image_bytes, _IMAGE_MAX_DIMENSION


def _make_png(width, height, mode="RGB"):
    img = Image.new(mode, (width, height), color=(120, 30, 60) if mode == "RGB" else (120, 30, 60, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_downscaled_to_max_dimension():
    content = _make_png(3000, 1500)
    out = optimize_image_bytes(content, ".png")
    img = Image.open(BytesIO(out))
    assert max(img.size) == _IMAGE_MAX_DIMENSION
    assert img.size == (_IMAGE_MAX_DIMENSION, _IMAGE_MAX_DIMENSION // 2)


def test_small_image_is_not_upscaled():
    content = _make_png(100, 80)
    out = optimize_image_bytes(content, ".png")
    img = Image.open(BytesIO(out))
    assert img.size == (100, 80)


def test_gif_is_left_untouched():
    # Re-saving via Pillow would collapse an animated GIF to its first frame —
    # deliberately a no-op regardless of size.
    content = _make_png(3000, 3000)  # not a real gif, but suffix alone must short-circuit
    out = optimize_image_bytes(content, ".gif")
    assert out == content


def test_ico_is_left_untouched():
    content = _make_png(256, 256)
    out = optimize_image_bytes(content, ".ico")
    assert out == content


def test_garbage_bytes_fall_back_to_original():
    content = b"this is not an image"
    out = optimize_image_bytes(content, ".png")
    assert out == content


def test_source_format_is_preserved_not_normalized_to_jpeg():
    # PNG stays PNG even when uploaded to a .jpg-suffixed field — several uploads here
    # (site logo, hero logo, favicon) rely on transparency a forced JPEG re-encode
    # would flatten onto an opaque background.
    content = _make_png(3200, 1200, mode="RGBA")
    out = optimize_image_bytes(content, ".jpg")
    img = Image.open(BytesIO(out))
    assert img.format == "PNG"
    assert max(img.size) == _IMAGE_MAX_DIMENSION


def test_real_jpeg_is_downscaled_and_stays_jpeg():
    src = Image.new("RGB", (3200, 1200), color=(120, 30, 60))
    buf = BytesIO()
    src.save(buf, format="JPEG")
    out = optimize_image_bytes(buf.getvalue(), ".jpg")
    img = Image.open(BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) == _IMAGE_MAX_DIMENSION

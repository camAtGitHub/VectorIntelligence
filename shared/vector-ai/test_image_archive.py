"""Unit tests for rolling camera-frame archive."""
import base64
from pathlib import Path

from image_archive import (
    MAX_IMAGES,
    _decode_image_bytes,
    save_submitted_image,
)


# Minimal valid-ish JPEG header + padding (decoder only needs non-trivial bytes)
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def test_decode_raw_base64():
    b64 = base64.b64encode(_FAKE_JPEG).decode("ascii")
    assert _decode_image_bytes(b64) == _FAKE_JPEG


def test_decode_data_url():
    b64 = base64.b64encode(_FAKE_JPEG).decode("ascii")
    url = f"data:image/jpeg;base64,{b64}"
    assert _decode_image_bytes(url) == _FAKE_JPEG


def test_decode_empty_and_junk():
    assert _decode_image_bytes("") is None
    assert _decode_image_bytes(None) is None
    assert _decode_image_bytes("   ") is None


def test_save_and_prune_keeps_max(tmp_path: Path):
    b64 = base64.b64encode(_FAKE_JPEG).decode("ascii")
    # Write more than max_keep; prune should leave max_keep files
    max_keep = 5
    paths = []
    for i in range(8):
        p = save_submitted_image(
            b64,
            f"ambient-{i}",
            images_dir=tmp_path,
            max_keep=max_keep,
        )
        assert p is not None
        assert p.exists()
        paths.append(p)

    remaining = list(tmp_path.glob("*.jpg"))
    assert len(remaining) == max_keep
    # Newest reasons should still be present (later loop indices)
    names = " ".join(r.name for r in remaining)
    assert "ambient-7" in names or "ambient-6" in names


def test_filename_shape(tmp_path: Path):
    b64 = base64.b64encode(_FAKE_JPEG).decode("ascii")
    p = save_submitted_image(b64, "ambient", images_dir=tmp_path, max_keep=50)
    assert p is not None
    # YYYYMMDD-HHMMSS-reason.jpg
    assert p.suffix == ".jpg"
    parts = p.stem.split("-")
    assert len(parts) >= 3
    assert len(parts[0]) == 8 and parts[0].isdigit()
    assert parts[-1] == "ambient"


def test_reason_sanitized(tmp_path: Path):
    b64 = base64.b64encode(_FAKE_JPEG).decode("ascii")
    p = save_submitted_image(b64, "Chat Vision!!", images_dir=tmp_path)
    assert p is not None
    assert "chat-vision" in p.name.lower() or "chat" in p.name.lower()


def test_skip_tiny_payload(tmp_path: Path):
    tiny = base64.b64encode(b"x").decode("ascii")
    assert save_submitted_image(tiny, "ambient", images_dir=tmp_path) is None
    assert list(tmp_path.glob("*")) == []


def test_default_max_images_constant():
    assert MAX_IMAGES == 50

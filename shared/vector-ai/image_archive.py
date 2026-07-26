"""Rolling archive of camera frames submitted to the LLM.

Saves under ROOT/images with names like 20260727-164301-ambient.jpg.
Keeps at most MAX_IMAGES files (oldest deleted). Best-effort: never raises
to callers — a disk failure must not break ambient or chat vision.
"""
from __future__ import annotations

import base64
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from logging_util import print  # noqa: A001
from paths import ROOT

MAX_IMAGES = 50
IMAGES_DIR = ROOT / "images"

# reason slug: ambient, chat, vision, …
_REASON_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_DATA_URL_RE = re.compile(
    r"^data:image/(?:jpeg|jpg|png|webp|gif);base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)

_lock = threading.Lock()


def _sanitize_reason(reason: str) -> str:
    slug = _REASON_RE.sub("-", (reason or "frame").strip()).strip("-_.")
    return (slug or "frame")[:48].lower()


def _decode_image_bytes(data: Union[str, bytes, None]) -> Optional[bytes]:
    if data is None:
        return None
    if isinstance(data, bytes):
        return data if data else None
    text = data.strip()
    if not text:
        return None
    m = _DATA_URL_RE.match(text)
    if m:
        text = m.group(1)
    # ambient posts raw base64 JPEG without a data: prefix
    try:
        return base64.b64decode(text, validate=False)
    except Exception:
        return None


def _prune_locked(directory: Path, max_keep: int = MAX_IMAGES) -> None:
    """Delete oldest image files until at most max_keep remain."""
    if max_keep < 1:
        return
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    ]
    if len(files) <= max_keep:
        return
    # Sort by mtime then name; oldest first for deletion
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    for old in files[: len(files) - max_keep]:
        try:
            old.unlink()
        except OSError as e:
            print(f"[images] prune failed {old.name}: {e}")


def save_submitted_image(
    data: Union[str, bytes, None],
    reason: str,
    *,
    images_dir: Optional[Path] = None,
    max_keep: int = MAX_IMAGES,
) -> Optional[Path]:
    """Write one frame to the rolling archive. Returns path or None on skip/fail."""
    raw = _decode_image_bytes(data)
    if not raw or len(raw) < 32:
        return None

    reason_slug = _sanitize_reason(reason)
    directory = images_dir if images_dir is not None else IMAGES_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Default extension: JPEG (chipper ambient + typical vision path)
    ext = ".jpg"

    with _lock:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[images] mkdir failed: {e}")
            return None

        base = f"{stamp}-{reason_slug}{ext}"
        path = directory / base
        n = 1
        while path.exists():
            path = directory / f"{stamp}-{reason_slug}-{n}{ext}"
            n += 1
            if n > 99:
                break

        try:
            path.write_bytes(raw)
        except OSError as e:
            print(f"[images] write failed: {e}")
            return None

        try:
            _prune_locked(directory, max_keep=max_keep)
        except OSError as e:
            print(f"[images] prune error: {e}")

    print(f"[images] saved {path.name} ({len(raw)} bytes, reason={reason_slug})")
    return path

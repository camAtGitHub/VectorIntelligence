"""Load/save Soul durable state to ROOT/soul.json (atomic write).

Internal file uses unix timestamps; LLM-facing views live in soul/view.py.
Never store image_b64 or frame payloads.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from paths import ROOT

from .types import FaceSighting, VisionDigest

SOUL_PATH: Path = ROOT / "soul.json"
SOUL_FORMAT = 2

# Caps mirror RAM rings (face 32, eye 16, notify 20, log 64).
_FACE_CAP = 32
_EYE_CAP = 16
_NOTIFY_CAP = 20
_LOG_CAP = 64
# Keep spoken lines intact in soul.json (same cap as Soul._NOTIFY_TEXT_MAX).
_NOTIFY_TEXT_MAX = 2000


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _safe_str(v: Any, max_len: int = 2000) -> str:
    s = str(v or "")
    if max_len > 0 and len(s) > max_len:
        return s[:max_len]
    return s


def serialize_soul(soul: Any) -> dict[str, Any]:
    """Build soul_format 2 payload from a Soul instance (unix ok; no images)."""
    goals = list(getattr(soul, "goals", None) or ["", "", ""])
    while len(goals) < 3:
        goals.append("")
    goals = [_safe_str(g, 200) for g in goals[:3]]

    face_buffer: list = []
    try:
        raw = soul.face.buffer_list() if soul.face is not None else []
        for item in list(raw)[-_FACE_CAP:]:
            if not isinstance(item, dict):
                continue
            face_buffer.append(
                {
                    "t": _safe_float(item.get("t")),
                    "face_id": item.get("face_id"),
                    "name": _safe_str(item.get("name"), 64),
                    "is_stranger": bool(item.get("is_stranger")),
                    "source": _safe_str(item.get("source"), 64),
                }
            )
    except Exception:
        face_buffer = []

    eye_digests: list = []
    try:
        raw_e = soul.eye.buffer_list() if soul.eye is not None else []
        for item in list(raw_e)[-_EYE_CAP:]:
            if not isinstance(item, dict):
                continue
            # Explicit allow-list — never image_b64 / full frame.
            eye_digests.append(
                {
                    "t": _safe_float(item.get("t")),
                    "reason": _safe_str(item.get("reason"), 64),
                    "presence": _safe_str(item.get("presence"), 32),
                    "name_hint": _safe_str(item.get("name_hint"), 64),
                    "note": _safe_str(item.get("note"), 300),
                    "spoken": _safe_str(item.get("spoken"), 200),
                    "model": _safe_str(item.get("model"), 64),
                    "frame_path": _safe_str(item.get("frame_path"), 500),
                    "error": _safe_str(item.get("error"), 128),
                    "occupied": bool(item.get("occupied")),
                }
            )
    except Exception:
        eye_digests = []

    notify_recent: list = []
    try:
        for item in list(getattr(soul, "_notify_recent", []) or [])[-_NOTIFY_CAP:]:
            if not isinstance(item, dict):
                continue
            notify_recent.append(
                {
                    "t": _safe_float(item.get("t")),
                    "source": _safe_str(item.get("source"), 64),
                    "text": _safe_str(item.get("text"), _NOTIFY_TEXT_MAX),
                }
            )
    except Exception:
        notify_recent = []

    log_items: list = []
    try:
        for item in list(getattr(soul, "_log", []) or [])[-_LOG_CAP:]:
            if not isinstance(item, dict):
                continue
            log_items.append(
                {
                    "t": _safe_float(item.get("t")),
                    "txt": _safe_str(item.get("txt"), 500),
                    "amb": bool(item.get("amb")),
                }
            )
    except Exception:
        log_items = []

    payload: dict[str, Any] = {
        "soul_format": SOUL_FORMAT,
        "last_seen_at": _safe_float(getattr(soul, "last_seen_at", 0.0)),
        "last_spoke_at": _safe_float(getattr(soul, "last_spoke_at", 0.0)),
        "last_user_voice_at": _safe_float(getattr(soul, "last_user_voice_at", 0.0)),
        "mood_text": _safe_str(getattr(soul, "_mood_text", "") or "", 500),
        "mood_updated": _safe_float(getattr(soul, "_mood_updated", 0.0)),
        "goals": goals,
        "last_night_i_dreamed_about": _safe_str(
            getattr(soul, "last_night_i_dreamed_about", "") or "", 500
        ),
        "face_buffer": face_buffer,
        "eye_digests": eye_digests,
        "notify_recent": notify_recent,
        "log": log_items,
    }
    last_dream = _safe_float(getattr(soul, "last_dream_job_at", 0.0), 0.0)
    if last_dream > 0:
        payload["last_dream_job_at"] = last_dream
    return payload


def save_from(soul: Any, path: Optional[Path] = None) -> None:
    """Atomic write of soul state to path (default ROOT/soul.json)."""
    dest = Path(path) if path is not None else SOUL_PATH
    payload = serialize_soul(soul)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent),
        prefix="soul.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _restore_face_buffer(soul: Any, items: list) -> None:
    if soul.face is None or not items:
        return
    buf = getattr(soul.face, "_buffer", None)
    if buf is None:
        return
    buf.clear()
    for item in items[-_FACE_CAP:]:
        if not isinstance(item, dict):
            continue
        try:
            fid = item.get("face_id")
            if fid is not None:
                try:
                    fid = int(fid)
                except (TypeError, ValueError):
                    fid = None
            sighting = FaceSighting(
                t=_safe_float(item.get("t")),
                face_id=fid,
                name=_safe_str(item.get("name"), 64),
                is_stranger=bool(item.get("is_stranger")),
                source=_safe_str(item.get("source"), 64),
            )
            buf.append(sighting)
            if sighting.t > getattr(soul.face, "last_seen_at", 0.0):
                soul.face.last_seen_at = sighting.t
            # Best-effort enrolled restore: last non-stranger wins.
            if not sighting.is_stranger and sighting.face_id is not None:
                soul.face._enrolled_id = sighting.face_id
                soul.face._enrolled_name = sighting.name or ""
                soul.face._enrolled_seen = sighting.t
            elif sighting.is_stranger and sighting.t:
                soul.face._stranger_seen = max(
                    float(getattr(soul.face, "_stranger_seen", 0.0) or 0.0),
                    sighting.t,
                )
        except Exception:
            continue


def _restore_eye_digests(soul: Any, items: list) -> None:
    if soul.eye is None or not items:
        return
    buf = getattr(soul.eye, "_buffer", None)
    if buf is None:
        return
    buf.clear()
    last: Optional[VisionDigest] = None
    for item in items[-_EYE_CAP:]:
        if not isinstance(item, dict):
            continue
        try:
            digest = VisionDigest(
                t=_safe_float(item.get("t")),
                reason=_safe_str(item.get("reason"), 64),
                presence=_safe_str(item.get("presence"), 32),
                name_hint=_safe_str(item.get("name_hint"), 64),
                note=_safe_str(item.get("note"), 300),
                spoken=_safe_str(item.get("spoken"), 200),
                model=_safe_str(item.get("model"), 64),
                frame_path=_safe_str(item.get("frame_path"), 500),
                error=_safe_str(item.get("error"), 128),
                occupied=bool(item.get("occupied")),
            )
            buf.append(digest)
            last = digest
            if digest.frame_path:
                soul.eye._last_frame_path = digest.frame_path
            if digest.t:
                soul.eye._last_frame_ts = float(digest.t)
        except Exception:
            continue
    if last is not None:
        soul.eye._last_vision = last


def load_into(soul: Any, path: Optional[Path] = None) -> bool:
    """Load soul.json into an existing Soul. Returns True if file applied.

    Corrupt or missing files return False without raising (starts clean).
    """
    src = Path(path) if path is not None else SOUL_PATH
    if not src.is_file():
        return False
    try:
        raw = src.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        print(f"[soul] load failed ({src}): {e}")
        return False
    if not isinstance(data, dict):
        print(f"[soul] load ignored: root is not an object ({src})")
        return False

    try:
        soul._last_seen_at = _safe_float(data.get("last_seen_at"))
        soul._last_spoke_at = _safe_float(data.get("last_spoke_at"))
        soul._last_user_voice_at = _safe_float(data.get("last_user_voice_at"))
        soul._mood_text = _safe_str(data.get("mood_text"), 500)
        soul._mood_updated = _safe_float(data.get("mood_updated"))

        goals_in = data.get("goals")
        if isinstance(goals_in, list):
            goals = [_safe_str(g, 200) for g in goals_in[:3]]
            while len(goals) < 3:
                goals.append("")
            soul._goals = goals
        dream = data.get("last_night_i_dreamed_about")
        if dream is not None:
            soul._last_night_i_dreamed_about = _safe_str(dream, 500)

        soul._last_dream_job_at = _safe_float(data.get("last_dream_job_at"), 0.0)

        face_buf = data.get("face_buffer")
        if isinstance(face_buf, list):
            _restore_face_buffer(soul, face_buf)

        eye_buf = data.get("eye_digests")
        if isinstance(eye_buf, list):
            _restore_eye_digests(soul, eye_buf)

        notify = data.get("notify_recent")
        if isinstance(notify, list):
            ring = getattr(soul, "_notify_recent", None)
            if ring is not None:
                ring.clear()
                for item in notify[-_NOTIFY_CAP:]:
                    if not isinstance(item, dict):
                        continue
                    ring.append(
                        {
                            "t": _safe_float(item.get("t")),
                            "source": _safe_str(item.get("source"), 64),
                            "text": _safe_str(item.get("text"), _NOTIFY_TEXT_MAX),
                        }
                    )

        log_in = data.get("log")
        if isinstance(log_in, list):
            log = getattr(soul, "_log", None)
            if log is not None:
                log.clear()
                for item in log_in[-_LOG_CAP:]:
                    if not isinstance(item, dict):
                        continue
                    log.append(
                        {
                            "t": _safe_float(item.get("t")),
                            "txt": _safe_str(item.get("txt"), 500),
                            "amb": bool(item.get("amb")),
                        }
                    )

        # Mirror mood into process_state when present (continuity with mood route).
        if soul._mood_text:
            try:
                import process_state

                process_state._mood_state["text"] = soul._mood_text
                process_state._mood_state["updated"] = soul._mood_updated
            except Exception:
                pass
        if soul._last_user_voice_at > 0:
            try:
                import process_state

                process_state._LAST_USER_VOICE_TS = soul._last_user_voice_at
            except Exception:
                pass
    except Exception as e:
        print(f"[soul] load apply failed: {e}")
        return False
    return True

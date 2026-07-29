"""Soul hot-state types: face sightings, vision digests, snapshot helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class FaceSighting:
    """One raw face observation (ring-buffer entry)."""

    t: float
    face_id: Optional[int]
    name: str
    is_stranger: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "face_id": self.face_id,
            "name": self.name,
            "is_stranger": self.is_stranger,
            "source": self.source,
        }


@dataclass
class VisionDigest:
    """Compact vision result stored in scratch (not full JPEG)."""

    t: float
    reason: str = ""
    presence: str = ""  # person | empty | unknown | ...
    name_hint: str = ""
    note: str = ""
    spoken: str = ""
    model: str = ""
    frame_path: str = ""
    error: str = ""
    occupied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def face_current_dict(
    face_id: Optional[int],
    name: str,
    is_stranger: bool,
) -> dict[str, Any]:
    """Shape matching process_state.current_face() / routes/face."""
    return {
        "face_id": face_id,
        "name": name,
        "is_stranger": is_stranger,
    }


def build_soul_snapshot(
    now: float,
    *,
    occupied: bool,
    session_started_at: float,
    last_seen_at: float,
    last_spoke_at: float,
    last_user_voice_at: float,
    face_current: Optional[dict],
    face_buffer: list,
    presence_debug: Optional[dict] = None,
    mood_text: str = "",
    mood_updated: float = 0.0,
    last_vision: Optional[dict] = None,
    notify_recent: Optional[list] = None,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    """Ops-friendly snapshot dict for GET/debug."""
    snap: dict[str, Any] = {
        "now": now,
        "occupied": occupied,
        "session_started_at": session_started_at,
        "last_seen_at": last_seen_at,
        "last_spoke_at": last_spoke_at,
        "last_user_voice_at": last_user_voice_at,
        "face": {
            "current": face_current,
            "buffer": list(face_buffer),
        },
        "mood": {
            "text": mood_text,
            "updated": mood_updated,
        },
        "eye": {
            "last_vision": last_vision,
        },
        "notify": {
            "recent": list(notify_recent or []),
        },
        "presence": dict(presence_debug or {}),
    }
    if extra:
        snap.update(extra)
    return snap

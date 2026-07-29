"""Face sighting ring buffer + enrolled-wins effective face.

Merges process_state.current_face / _face_state rules with PresenceCache
_apply_face ideas (firmware-wins for ambient soft; stranger never wipes
fresh enrolled).
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from process_state import FACE_RECENT_WINDOW

from .types import FaceSighting, face_current_dict

_BUFFER_MAX = 32


class FaceService:
    """Hot face state: buffer of sightings + enrolled-wins current()."""

    def __init__(
        self,
        recent_window_s: Optional[float] = None,
        max_buffer: int = _BUFFER_MAX,
    ):
        self.recent_window_s = float(
            FACE_RECENT_WINDOW if recent_window_s is None else recent_window_s
        )
        self.max_buffer = max(1, int(max_buffer))
        self._buffer: Deque[FaceSighting] = deque(maxlen=self.max_buffer)
        # Mirrors process_state._face_state
        self._enrolled_id: Optional[int] = None
        self._enrolled_name: Optional[str] = None
        self._enrolled_seen: float = 0.0
        self._stranger_seen: float = 0.0
        # Any note() updates this (person sighted at desk).
        self.last_seen_at: float = 0.0

    def note(
        self,
        now: float,
        face_id,
        name: str,
        is_stranger: bool,
        source: str,
    ) -> FaceSighting:
        """Record a sighting; update effective face with enrolled-wins rules.

        Always refreshes ``last_seen_at``. Ambient soft faces never replace a
        still-fresh enrolled primary (PresenceCache._apply_face idea). Soft
        strangers never replace enrolled; process_state still tracks stranger_seen
        for when enrolled ages out.
        """
        now = float(now)
        name = (name or "").strip()
        is_stranger = bool(is_stranger)
        source = str(source or "")
        try:
            fid = int(face_id) if face_id is not None else None
        except (TypeError, ValueError):
            fid = None
        if fid is not None and fid <= 0 and not is_stranger:
            # Negative / zero id without explicit stranger → treat as stranger.
            is_stranger = True

        self.last_seen_at = now

        enrolled_fresh = self._enrolled_fresh(now)

        if not is_stranger:
            # Enrolled match.
            if enrolled_fresh and source == "ambient":
                # Ambient soft: only refresh age if same face_id; never handoff.
                if (
                    fid is not None
                    and self._enrolled_id is not None
                    and fid == self._enrolled_id
                ):
                    self._enrolled_seen = now
                    if name:
                        self._enrolled_name = name
                # else keep firmware primary
            else:
                # face_seen / greeting / tick hard sources may replace (handoff).
                self._enrolled_id = fid
                self._enrolled_name = name or self._enrolled_name or ""
                self._enrolled_seen = now
        else:
            # Stranger blip: never wipe enrolled state (current() still prefers
            # enrolled while fresh). Still advance stranger_seen so a genuine
            # stranger can surface after enrolled ages out — same as process_state.
            self._stranger_seen = now

        sighting = FaceSighting(
            t=now,
            face_id=fid,
            name=name,
            is_stranger=is_stranger,
            source=source,
        )
        self._buffer.append(sighting)
        return sighting

    def _enrolled_fresh(self, now: float) -> bool:
        return bool(
            self._enrolled_seen
            and (now - self._enrolled_seen) <= self.recent_window_s
        )

    def _stranger_fresh(self, now: float) -> bool:
        return bool(
            self._stranger_seen
            and (now - self._stranger_seen) <= self.recent_window_s
        )

    def current(self, now: float) -> Optional[dict]:
        """Who Vector is effectively looking at (enrolled wins over stranger).

        Copied from process_state.current_face(): enrolled within window always
        wins over stranger noise; only when enrolled is stale do stranger
        sightings count.
        """
        now = float(now)
        if self._enrolled_fresh(now):
            return face_current_dict(
                self._enrolled_id,
                self._enrolled_name or "",
                False,
            )
        if self._stranger_fresh(now):
            return face_current_dict(None, "", True)
        return None

    def buffer_list(self) -> list:
        """Newest-last list of sighting dicts (for snapshot)."""
        return [s.to_dict() for s in self._buffer]

    def raw_state(self) -> dict:
        """process_state._face_state-shaped dict for debug/compat."""
        return {
            "enrolled_id": self._enrolled_id,
            "enrolled_name": self._enrolled_name,
            "enrolled_seen": self._enrolled_seen,
            "stranger_seen": self._stranger_seen,
        }

    def apply_sleep_clear(self) -> None:
        """Clear face identity after long sleep (presence sleep clear companion)."""
        self._enrolled_id = None
        self._enrolled_name = None
        self._enrolled_seen = 0.0
        self._stranger_seen = 0.0
        # Keep last_seen_at and buffer for diary/debug; only drop effective face.

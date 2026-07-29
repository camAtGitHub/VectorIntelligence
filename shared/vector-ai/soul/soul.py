"""Soul facade: one hot scratch for presence, face, speech clocks, mood."""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Optional

from behaviors.presence import PresenceCache
from behaviors.types import FaceIdentity

from .face import FaceService
from .types import VisionDigest, build_soul_snapshot

_NOTIFY_RING_MAX = 16
_LOG_MAX = 64


class Soul:
    """Desk-day continuity brain (scratch).

    Owns one PresenceCache (shared with BehaviorRuntime) and one FaceService.
    Policies read via occupied / current_face / snapshot; writers call note_*.
    """

    def __init__(
        self,
        presence: Optional[PresenceCache] = None,
        face: Optional[FaceService] = None,
        memory: Any = None,
        continuity: Any = None,
        *,
        face_max_age_s: int = 1800,
        image_max_age_s: int = 45,
        sticky_s: int = 1800,
        empty_streak_clear: int = 2,
        face_recent_window_s: Optional[float] = None,
    ):
        if presence is not None:
            self.presence = presence
        else:
            self.presence = PresenceCache(
                face_max_age_s=face_max_age_s,
                image_max_age_s=image_max_age_s,
                sticky_s=sticky_s,
                empty_streak_clear=empty_streak_clear,
            )
        self.face = face or FaceService(recent_window_s=face_recent_window_s)
        self.memory = memory
        self.continuity = continuity

        # Speech / presence clocks (floats; 0 = never).
        self._last_seen_at: float = 0.0
        self._last_spoke_at: float = 0.0
        self._last_user_voice_at: float = 0.0

        # Mood (process-local; may mirror process_state._mood_state).
        self._mood_text: str = ""
        self._mood_updated: float = 0.0

        # Eye / notify stubs (filled in later phases).
        self._last_vision: Optional[VisionDigest] = None
        self._notify_recent: Deque[dict] = deque(maxlen=_NOTIFY_RING_MAX)
        self._log: Deque[dict] = deque(maxlen=_LOG_MAX)

    # -- clocks (public attrs for simple access; also methods for design API) --

    @property
    def last_seen_at(self) -> float:
        return self._last_seen_at

    @last_seen_at.setter
    def last_seen_at(self, value: float) -> None:
        self._last_seen_at = float(value or 0.0)

    @property
    def last_spoke_at(self) -> float:
        return self._last_spoke_at

    @last_spoke_at.setter
    def last_spoke_at(self, value: float) -> None:
        self._last_spoke_at = float(value or 0.0)

    @property
    def last_user_voice_at(self) -> float:
        return self._last_user_voice_at

    @last_user_voice_at.setter
    def last_user_voice_at(self, value: float) -> None:
        self._last_user_voice_at = float(value or 0.0)

    # -- writers ----------------------------------------------------------------

    def note_face(
        self,
        now: float,
        *,
        face_id,
        name: str,
        is_stranger: bool,
        source: str,
    ) -> None:
        """Record face sighting into face service; refresh last_seen_at."""
        now = float(now)
        self.face.note(
            now,
            face_id=face_id,
            name=name,
            is_stranger=is_stranger,
            source=source,
        )
        self._last_seen_at = now

    def note_person(
        self,
        now: float,
        *,
        source: str,
        name_hint: Optional[str] = None,
        face: Optional[FaceIdentity] = None,
        image_ref: Optional[str] = None,
        on_charger: Optional[bool] = None,
        voice_recent: Optional[bool] = None,
        image_b64: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Delegate sticky occupancy + soft face to shared PresenceCache."""
        del kwargs  # forward-compat; ignore unknown for now
        now = float(now)
        # image_ref is path/string; PresenceCache wants image_b64 if provided.
        self.presence.note_person_evidence(
            now,
            source=source,
            name_hint=name_hint,
            face=face,
            on_charger=on_charger,
            voice_recent=voice_recent,
            image_b64=image_b64,
        )
        self._last_seen_at = now
        # Soft path: if caller only has name_hint/face for face service, mirror
        # into face buffer when a FaceIdentity is supplied.
        if face is not None:
            self.face.note(
                now,
                face_id=face.face_id,
                name=face.name or (name_hint or ""),
                is_stranger=bool(face.is_stranger),
                source=source,
            )

    def note_empty(self, now: float, *, source: str) -> None:
        self.presence.note_empty_evidence(float(now), source=source)

    def note_spoke(self, now: float, *, source: str, text: str = "") -> None:
        """Robot or user conversational speech edge."""
        del source, text  # ring may use later
        self._last_spoke_at = float(now)

    def note_user_voice(self, now: float) -> None:
        self._last_user_voice_at = float(now)
        # Keep process_state clock in sync for SpeechArbiter / existing readers.
        try:
            import process_state

            process_state._LAST_USER_VOICE_TS = float(now)
        except Exception:
            pass

    def apply_sleep_clear(self, now: float) -> None:
        self.presence.apply_sleep_clear(float(now))
        self.face.apply_sleep_clear()

    # -- readers ----------------------------------------------------------------

    def occupied(self, now: float) -> bool:
        return bool(self.presence.occupied_effective(float(now)))

    def session_started_at(self) -> float:
        snap = self.presence.snapshot
        return float(getattr(snap, "session_started_at", 0.0) or 0.0)

    def current_face(self, now: Optional[float] = None) -> Optional[dict]:
        """Effective enrolled-wins face. ``now`` optional (defaults to time.time)."""
        if now is None:
            import time

            now = time.time()
        return self.face.current(float(now))

    def mood_text(self) -> str:
        # Prefer live process_state if set (shared with mood route today).
        try:
            import process_state

            t = (process_state._mood_state.get("text") or "").strip()
            if t:
                return t
        except Exception:
            pass
        return self._mood_text or ""

    def set_mood(self, text: str, now: float) -> None:
        text = (text or "").strip()
        now = float(now)
        self._mood_text = text
        self._mood_updated = now
        try:
            import process_state

            process_state._mood_state["text"] = text
            process_state._mood_state["updated"] = now
        except Exception:
            pass
        if self.memory is not None:
            try:
                self.memory.set_state("mood", text)
            except Exception:
                pass

    def day_strip(self, date_s: str) -> str:
        if self.continuity is None:
            return ""
        try:
            return str(self.continuity.day_strip(date_s) or "")
        except Exception:
            return ""

    def append_log(self, now: float, txt: str, *, amb: bool = False) -> None:
        self._log.append(
            {"t": float(now), "txt": str(txt or "")[:500], "amb": bool(amb)}
        )

    # -- stubs (later phases fill real eye / notify) ----------------------------

    def see(
        self,
        now: float,
        *,
        frame_b64_or_url: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> Optional[VisionDigest]:
        """Stub: eye.see implemented in a later phase. Returns None."""
        del now, frame_b64_or_url, reason, kwargs
        return None

    def last_vision(self) -> Optional[VisionDigest]:
        return self._last_vision

    def notify_speech(self, text: str, *, source: str = "chat") -> None:
        """Record outbound speech preview + fire-and-forget Pushover."""
        message = (text or "").strip()
        if not message:
            return
        import time

        self._notify_recent.append(
            {
                "t": time.time(),
                "source": source,
                "text": message[:200],
            }
        )
        try:
            from speech_notify import schedule_speech_notify

            schedule_speech_notify(message, source=source)
        except Exception:
            pass

    def snapshot(self, now: float) -> dict:
        now = float(now)
        face_cur = self.face.current(now)
        last_v = self._last_vision.to_dict() if self._last_vision else None
        mood_text = self.mood_text()
        mood_updated = self._mood_updated
        try:
            import process_state

            if process_state._mood_state.get("updated"):
                mood_updated = float(process_state._mood_state["updated"] or 0.0)
        except Exception:
            pass
        return build_soul_snapshot(
            now,
            occupied=self.occupied(now),
            session_started_at=self.session_started_at(),
            last_seen_at=self._last_seen_at,
            last_spoke_at=self._last_spoke_at,
            last_user_voice_at=self._last_user_voice_at,
            face_current=face_cur,
            face_buffer=self.face.buffer_list(),
            presence_debug=self.presence.debug_dict(now),
            mood_text=mood_text,
            mood_updated=mood_updated,
            last_vision=last_v,
            notify_recent=list(self._notify_recent),
        )

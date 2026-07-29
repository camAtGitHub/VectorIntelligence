"""Soul facade: one hot scratch for presence, face, eye, speech clocks, mood."""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, List, Optional

from behaviors.presence import PresenceCache
from behaviors.types import FaceIdentity

from .eye import EyeService
from .face import FaceService
from .types import VisionDigest
from .view import llm_view as build_llm_view

_NOTIFY_RING_MAX = 20
_LOG_MAX = 64
# Full spoken lines in the ring/soul.json (not a 120-char teaser). Cap only for
# pathological length; matches practical TTS/Pushover scale with headroom.
_NOTIFY_TEXT_MAX = 2000
_GOALS_N = 3
_GOAL_MAX_LEN = 200
_DREAM_MAX_LEN = 500
_SAVE_DEBOUNCE_S = 2.0


def _normalize_goals(goals: Optional[list]) -> List[str]:
    raw = list(goals or [])
    out: List[str] = []
    for g in raw[:_GOALS_N]:
        s = str(g or "").strip()
        if len(s) > _GOAL_MAX_LEN:
            s = s[:_GOAL_MAX_LEN]
        out.append(s)
    while len(out) < _GOALS_N:
        out.append("")
    return out


class Soul:
    """Desk-day continuity brain (scratch + optional soul.json persistence).

    Owns one PresenceCache (shared with BehaviorRuntime), one FaceService,
    and one EyeService. Policies read via occupied / current_face / snapshot;
    writers call note_* / see.
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
        eye: Optional[EyeService] = None,
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
        self.eye = eye or EyeService(image_max_age_s=image_max_age_s)

        # Speech / presence clocks (floats; 0 = never).
        self._last_seen_at: float = 0.0
        self._last_spoke_at: float = 0.0
        self._last_user_voice_at: float = 0.0

        # Mood (process-local; may mirror process_state._mood_state).
        self._mood_text: str = ""
        self._mood_updated: float = 0.0

        # Goals[3] + dream field (persist across restarts).
        self._goals: List[str] = ["", "", ""]
        self._last_night_i_dreamed_about: str = ""
        self._last_dream_job_at: float = 0.0

        self._notify_recent: Deque[dict] = deque(maxlen=_NOTIFY_RING_MAX)
        self._log: Deque[dict] = deque(maxlen=_LOG_MAX)

        self._save_lock = threading.Lock()
        self._save_timer: Optional[threading.Timer] = None
        self._save_debounce_s = _SAVE_DEBOUNCE_S
        # None → paths.ROOT / soul.json; tests may set a tmp path.
        self._persist_path: Any = None
        # Off until service enables (avoids unit-test writes to install dir).
        self._auto_persist: bool = False

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

    # -- goals / dream ----------------------------------------------------------

    @property
    def goals(self) -> List[str]:
        return list(self._goals)

    @property
    def last_night_i_dreamed_about(self) -> str:
        return self._last_night_i_dreamed_about

    @property
    def last_dream_job_at(self) -> float:
        return float(self._last_dream_job_at or 0.0)

    def set_goals(self, goals: Optional[list], *, force_save: bool = True) -> List[str]:
        """Set exactly 3 goal strings (pad/truncate + max length)."""
        self._goals = _normalize_goals(goals)
        if force_save:
            self.save_now()
        else:
            self._schedule_save()
        return list(self._goals)

    def set_dreamed_about(self, text: str, *, force_save: bool = True) -> str:
        """Set last_night_i_dreamed_about (capped)."""
        s = str(text or "").strip()
        if len(s) > _DREAM_MAX_LEN:
            s = s[:_DREAM_MAX_LEN]
        self._last_night_i_dreamed_about = s
        if force_save:
            self.save_now()
        else:
            self._schedule_save()
        return self._last_night_i_dreamed_about

    # -- persistence ------------------------------------------------------------

    def load_from_disk(self, path: Any = None) -> bool:
        """Load ROOT/soul.json (or path). Returns True if applied."""
        from .persist import load_into

        if path is not None:
            self._persist_path = path
        ok = load_into(self, path=self._persist_path)
        return bool(ok)

    def save_now(self) -> None:
        """Force immediate atomic save; cancel pending debounce.

        No-op unless ``_auto_persist`` is True or ``_persist_path`` is set
        (service enables auto-persist; tests set a tmp path).
        """
        with self._save_lock:
            if self._save_timer is not None:
                try:
                    self._save_timer.cancel()
                except Exception:
                    pass
                self._save_timer = None
        if not self._auto_persist and self._persist_path is None:
            return
        try:
            from .persist import save_from

            save_from(self, path=self._persist_path)
        except Exception as e:
            print(f"[soul] save_now failed: {e}")

    def _schedule_save(self) -> None:
        """Debounced coalesce write (~2s)."""
        if not self._auto_persist and self._persist_path is None:
            return

        def _fire() -> None:
            try:
                self.save_now()
            except Exception as e:
                print(f"[soul] debounced save failed: {e}")

        with self._save_lock:
            if self._save_timer is not None:
                try:
                    self._save_timer.cancel()
                except Exception:
                    pass
            t = threading.Timer(self._save_debounce_s, _fire)
            t.daemon = True
            self._save_timer = t
            t.start()

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
        self._schedule_save()

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
        self._schedule_save()

    def note_empty(self, now: float, *, source: str) -> None:
        self.presence.note_empty_evidence(float(now), source=source)
        self._schedule_save()

    def note_spoke(self, now: float, *, source: str, text: str = "") -> None:
        """Robot conversational speech edge (updates last_spoke_at)."""
        del source, text  # source/text reserved for future ring detail
        self._last_spoke_at = float(now)
        self._schedule_save()

    def note_user_voice(self, now: float) -> None:
        self._last_user_voice_at = float(now)
        # Keep process_state clock in sync for SpeechArbiter / existing readers.
        try:
            import process_state

            process_state._LAST_USER_VOICE_TS = float(now)
        except Exception:
            pass
        self._schedule_save()

    def apply_sleep_clear(self, now: float, sleep_gap_s: float = 0.0) -> None:
        self.presence.apply_sleep_clear(float(now), sleep_gap_s)
        self.face.apply_sleep_clear()
        self._schedule_save()

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
        # Force save on mood (D7).
        self.save_now()

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
        self._schedule_save()

    # -- eye --------------------------------------------------------------------

    async def see(
        self,
        now: float,
        *,
        image: str = "",
        reason: str = "ambient",
        frame_b64_or_url: str = "",
        **kwargs: Any,
    ) -> VisionDigest:
        """Vision path: archive + (ambient) VISION_MODEL PRESENCE + presence write.

        ``image`` is preferred; ``frame_b64_or_url`` is an alias for design API.
        """
        del kwargs
        frame = image or frame_b64_or_url or ""
        digest = await self.eye.see(
            self, float(now), image=frame, reason=reason or "ambient"
        )
        self._schedule_save()
        return digest

    def note_eye_frame(
        self,
        now: float,
        *,
        reason: str = "chat",
        frame_path: str = "",
        note: str = "",
        presence: str = "",
        name_hint: str = "",
    ) -> VisionDigest:
        """Light eye digest without a second LLM (chat already archived the frame)."""
        digest = self.eye.note_frame(
            float(now),
            reason=reason,
            frame_path=frame_path,
            note=note,
            presence=presence,
            name_hint=name_hint,
            occupied=self.occupied(float(now)),
        )
        self._schedule_save()
        return digest

    def last_vision(self) -> Optional[VisionDigest]:
        return self.eye.last_vision

    def image_fresh(self, now: float) -> bool:
        """Whether eye has a recent archived frame (IMAGE_CACHE_MAX_AGE_S)."""
        return self.eye.image_fresh(float(now))

    def notify_speech(self, text: str, *, source: str = "chat") -> None:
        """Record outbound speech clock + ring, then fire-and-forget Pushover.

        Single path for robot speech: updates ``last_spoke_at`` via
        ``note_spoke``, appends notify ring (cap 20, full text up to
        ``_NOTIFY_TEXT_MAX``), then ``schedule_speech_notify``. Never raises
        into caller's critical path.
        """
        message = (text or "").strip()
        if not message:
            return
        import time

        now = time.time()
        try:
            self.note_spoke(now, source=source, text=message)
        except Exception:
            pass
        try:
            stored = message
            if len(stored) > _NOTIFY_TEXT_MAX:
                stored = stored[: _NOTIFY_TEXT_MAX - 1] + "…"
            self._notify_recent.append(
                {
                    "t": now,
                    "source": str(source or "chat"),
                    "text": stored,
                }
            )
        except Exception:
            pass
        try:
            from speech_notify import schedule_speech_notify

            schedule_speech_notify(message, source=source)
        except Exception:
            pass
        # note_spoke already scheduled save; ensure ring is included.
        self._schedule_save()

    def llm_view(self, now: float, *, detail: str = "full") -> dict:
        """Long-key relative-time export for GET / LLM / growth jobs."""
        return build_llm_view(self, float(now), detail=detail)  # type: ignore[arg-type]

    def snapshot(self, now: float) -> dict:
        """Ops snapshot: alias of llm_view(detail='ops') — long keys, no raw unix."""
        return self.llm_view(float(now), detail="ops")

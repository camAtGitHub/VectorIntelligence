from __future__ import annotations

from typing import Optional

from .types import FaceIdentity, PresenceSnapshot


class PresenceCache:
    """Desk occupancy + soft/hard identity cache.

    Occupancy is sticky: once a person is noted, the desk stays occupied until
    consecutive empty evidence reaches ``empty_streak_clear``, the sticky TTL
    expires, or a sleep gap clears the session. Tick/chipper empty alone does
    not wipe a warm sticky hold.
    """

    def __init__(
        self,
        face_max_age_s: int = 1800,
        image_max_age_s: int = 45,
        sticky_s: int = 1800,
        empty_streak_clear: int = 2,
    ):
        self.face_max_age_s = face_max_age_s
        self.image_max_age_s = image_max_age_s
        self.sticky_s = sticky_s
        self.empty_streak_clear = empty_streak_clear
        self._snap = PresenceSnapshot()
        self._last_person_at: float = 0.0
        self._empty_streak: int = 0
        self._last_source: str = ""
        self._soft_name: str = ""

    @property
    def snapshot(self) -> PresenceSnapshot:
        return self._snap

    @property
    def last_person_at(self) -> float:
        return self._last_person_at

    @property
    def empty_streak(self) -> int:
        return self._empty_streak

    @property
    def last_source(self) -> str:
        return self._last_source

    @property
    def soft_name(self) -> str:
        return self._soft_name

    def occupied_effective(self, now: float) -> bool:
        """Sticky evaluation: person until empty streak, sticky TTL, or clear."""
        if self._last_person_at > 0:
            age = now - self._last_person_at
            if age <= self.sticky_s and self._empty_streak < self.empty_streak_clear:
                return True
            if age > self.sticky_s:
                return False
            if self._empty_streak >= self.empty_streak_clear:
                return False
        return bool(self._snap.occupied)

    def _sync_occupied(self, now: float) -> None:
        self._snap.occupied = self.occupied_effective(now)

    def note_person_evidence(
        self,
        now: float,
        *,
        name_hint: Optional[str] = None,
        source: str = "ambient",
        face: Optional[FaceIdentity] = None,
        on_charger: Optional[bool] = None,
        voice_recent: Optional[bool] = None,
        image_b64: Optional[str] = None,
    ) -> PresenceSnapshot:
        """Record positive person evidence (ambient, face_seen, or tick)."""
        s = self._snap
        self._last_person_at = now
        self._empty_streak = 0
        self._last_source = source
        s.updated_at = now
        if on_charger is not None:
            s.on_charger = bool(on_charger)
        if voice_recent is not None:
            s.voice_recent = bool(voice_recent)
        if image_b64 is not None:
            s.image_b64 = image_b64
            s.image_ts = now

        if face is not None:
            # Firmware enrolled face is authoritative; soft stranger must not
            # replace a still-fresh enrolled identity.
            enrolled_fresh = (
                self.identity_fresh(now)
                and s.face is not None
                and not s.face.is_stranger
            )
            if face.is_stranger and enrolled_fresh:
                pass
            else:
                s.face = face
                s.face_ts = now
                if face.name:
                    self._soft_name = face.name
        elif name_hint:
            hint = str(name_hint).strip()[:64]
            if hint:
                self._soft_name = hint
                enrolled_fresh = (
                    self.identity_fresh(now)
                    and s.face is not None
                    and not s.face.is_stranger
                )
                if not enrolled_fresh:
                    s.face = FaceIdentity(
                        face_id=0, name=hint, is_stranger=True
                    )
                    s.face_ts = now

        self._sync_occupied(now)
        return s

    def note_empty_evidence(
        self,
        now: float,
        *,
        source: str = "ambient",
    ) -> PresenceSnapshot:
        """Strong empty evidence (ambient). Increments streak; may clear sticky."""
        self._empty_streak += 1
        self._last_source = source
        s = self._snap
        s.updated_at = now
        sticky_expired = (
            self._last_person_at <= 0
            or (now - self._last_person_at) > self.sticky_s
        )
        if self._empty_streak >= self.empty_streak_clear or sticky_expired:
            s.occupied = False
            self._last_person_at = 0.0
        else:
            self._sync_occupied(now)
        return s

    def apply_sleep_clear(
        self,
        now: float,
        sleep_gap_s: float = 0.0,
    ) -> PresenceSnapshot:
        """Clear desk occupancy after a long ambient sleep gap (new session)."""
        del sleep_gap_s  # documented for callers; clear is unconditional once invoked
        s = self._snap
        s.occupied = False
        s.updated_at = now
        self._last_person_at = 0.0
        self._empty_streak = 0
        self._last_source = "sleep"
        return s

    def update(
        self,
        now: float,
        occupied: bool,
        face: Optional[FaceIdentity] = None,
        image_b64: Optional[str] = None,
        on_charger: bool = False,
        voice_recent: bool = False,
    ) -> PresenceSnapshot:
        """Legacy direct write. Prefer note_person_evidence / note_empty_evidence.

        Positive occupancy feeds sticky state. Negative occupancy alone does
        **not** clear a warm sticky hold (tick-empty is weak).
        """
        s = self._snap
        s.on_charger = bool(on_charger)
        s.voice_recent = bool(voice_recent)
        s.updated_at = now
        if face is not None:
            s.face = face
            s.face_ts = now
            if face.name:
                self._soft_name = face.name
        if image_b64 is not None:
            s.image_b64 = image_b64
            s.image_ts = now
        if occupied:
            self._last_person_at = now
            self._empty_streak = 0
            self._last_source = self._last_source or "update"
            s.occupied = True
        else:
            # Weak empty: keep sticky if still warm.
            self._sync_occupied(now)
        return s

    def identity_fresh(self, now: float) -> bool:
        if self._snap.face is None or self._snap.face_ts <= 0:
            return False
        return (now - self._snap.face_ts) <= self.face_max_age_s

    def image_fresh(self, now: float) -> bool:
        if not self._snap.image_b64 or self._snap.image_ts <= 0:
            return False
        return (now - self._snap.image_ts) <= self.image_max_age_s

    def effective_face(self, now: float) -> Optional[FaceIdentity]:
        return self._snap.face if self.identity_fresh(now) else None

    def debug_dict(self, now: float) -> dict:
        return {
            "occupied": self.occupied_effective(now),
            "last_person_at": self._last_person_at,
            "empty_streak": self._empty_streak,
            "empty_streak_clear": self.empty_streak_clear,
            "presence_source": self._last_source,
            "soft_name": self._soft_name,
            "sticky_s": self.sticky_s,
            "face_max_age_s": self.face_max_age_s,
        }

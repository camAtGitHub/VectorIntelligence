from __future__ import annotations

from typing import Optional

from .types import FaceIdentity, PresenceSnapshot


class PresenceCache:
    def __init__(self, face_max_age_s: int = 120, image_max_age_s: int = 45):
        self.face_max_age_s = face_max_age_s
        self.image_max_age_s = image_max_age_s
        self._snap = PresenceSnapshot()

    @property
    def snapshot(self) -> PresenceSnapshot:
        return self._snap

    def update(
        self,
        now: float,
        occupied: bool,
        face: Optional[FaceIdentity] = None,
        image_b64: Optional[str] = None,
        on_charger: bool = False,
        voice_recent: bool = False,
    ) -> PresenceSnapshot:
        s = self._snap
        s.occupied = bool(occupied)
        s.on_charger = bool(on_charger)
        s.voice_recent = bool(voice_recent)
        s.updated_at = now
        if face is not None:
            s.face = face
            s.face_ts = now
        if image_b64 is not None:
            s.image_b64 = image_b64
            s.image_ts = now
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

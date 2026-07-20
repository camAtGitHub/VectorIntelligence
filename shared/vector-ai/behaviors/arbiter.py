from __future__ import annotations

from typing import Tuple

from .logutil import blog
from .types import SpeechRequest

_TAG = "arbiter"


class SpeechArbiter:
    def __init__(self, min_gap_s: int = 90, suppress_after_voice_s: int = 120):
        self.min_gap_s = min_gap_s
        self.suppress_after_voice_s = suppress_after_voice_s
        self._last_speech_at: float = 0.0

    def allow(
        self,
        req: SpeechRequest,
        now: float,
        quiet: bool,
        voice_recent_ts: float,
    ) -> Tuple[bool, str]:
        if not (req.text or "").strip():
            return False, "empty"
        if quiet:
            blog(
                _TAG,
                f"deny {req.behavior_id}: quiet mode on",
                verbose=True,
            )
            return False, "quiet"
        if voice_recent_ts > 0 and (now - voice_recent_ts) < self.suppress_after_voice_s:
            left = self.suppress_after_voice_s - (now - voice_recent_ts)
            blog(
                _TAG,
                f"deny {req.behavior_id}: recent_voice ({left:.0f}s left of "
                f"{self.suppress_after_voice_s}s suppress)",
            )
            return False, "recent_voice"
        if self._last_speech_at > 0 and (now - self._last_speech_at) < self.min_gap_s:
            left = self.min_gap_s - (now - self._last_speech_at)
            blog(
                _TAG,
                f"deny {req.behavior_id}: min_gap ({left:.0f}s left of "
                f"{self.min_gap_s}s global gap)",
            )
            return False, "min_gap"
        return True, "ok"

    def record_speech(self, now: float) -> None:
        self._last_speech_at = now
        blog(_TAG, f"recorded speech at now={now:.0f}", verbose=True)

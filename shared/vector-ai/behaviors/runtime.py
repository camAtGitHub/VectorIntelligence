"""Multi-behavior runtime: presence cache, arbiter, tick orchestration."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .arbiter import SpeechArbiter
from .config import RuntimeConfig, WorkdayConfig
from .continuity import ContinuityStore
from .presence import PresenceCache
from .types import (
    BehaviorContext,
    FaceIdentity,
    PresenceSnapshot,
    SpeechRequest,
    TickResult,
)
from .workday import WorkDayBehavior


class BehaviorRuntime:
    def __init__(
        self,
        runtime_cfg: RuntimeConfig,
        workday_cfg: WorkdayConfig,
        store: ContinuityStore,
        quiet_fn: Optional[Callable[[], bool]] = None,
        voice_ts_fn: Optional[Callable[[], float]] = None,
    ):
        self.runtime_cfg = runtime_cfg
        self.workday_cfg = workday_cfg
        self.store = store
        self.quiet_fn = quiet_fn or (lambda: False)
        self.voice_ts_fn = voice_ts_fn or (lambda: 0.0)

        self.presence = PresenceCache(
            face_max_age_s=runtime_cfg.face_cache_max_age_s,
            image_max_age_s=runtime_cfg.image_cache_max_age_s,
        )
        self.arbiter = SpeechArbiter(
            min_gap_s=runtime_cfg.speech_min_gap_s,
            suppress_after_voice_s=runtime_cfg.speech_suppress_after_voice_s,
        )
        self.behaviors: List[Any] = []
        self.workday: Optional[WorkDayBehavior] = None

        enabled = set(runtime_cfg.behaviors_enabled)
        if "workday" in enabled and workday_cfg.enabled:
            self.workday = WorkDayBehavior(workday_cfg, store)
            self.behaviors.append(self.workday)

    def ingest_tick_payload(
        self,
        now: float,
        occupied: bool,
        face: Optional[dict] = None,
        on_charger: bool = False,
        voice_recent: bool = False,
        image_b64: Optional[str] = None,
    ) -> PresenceSnapshot:
        face_obj: Optional[FaceIdentity] = None
        if face and isinstance(face, dict):
            try:
                face_obj = FaceIdentity(
                    face_id=int(face.get("face_id") or face.get("faceId") or 0),
                    name=str(face.get("name") or ""),
                    is_stranger=bool(face.get("is_stranger") or face.get("isStranger") or False),
                )
                if face_obj.face_id <= 0 and not face_obj.name:
                    face_obj = None
            except (TypeError, ValueError):
                face_obj = None
        return self.presence.update(
            now=now,
            occupied=occupied,
            face=face_obj,
            image_b64=image_b64,
            on_charger=on_charger,
            voice_recent=voice_recent,
        )

    def _local_dt(self, now: float) -> datetime:
        tz = self.workday_cfg.tz
        return datetime.fromtimestamp(now, tz=tz)

    def clock_tick(self, now: float) -> None:
        """Clock-only transitions without chipper presence."""
        local_dt = self._local_dt(now)
        if self.workday is not None:
            self.workday.clock_tick(now, local_dt)

    def tick(self, now: float) -> TickResult:
        local_dt = self._local_dt(now)
        # Always run clock transitions first
        self.clock_tick(now)

        quiet = bool(self.quiet_fn())
        voice_ts = float(self.voice_ts_fn() or 0.0)
        snap = self.presence.snapshot
        identity_fresh = self.presence.identity_fresh(now)

        ctx = BehaviorContext(
            now=now,
            local_dt=local_dt,
            presence=snap,
            quiet=quiet,
            config=self.workday_cfg,
            identity_fresh=identity_fresh,
        )

        candidates: List[TickResult] = []
        need_identity = False
        for b in self.behaviors:
            if not b.enabled():
                continue
            r = b.tick(ctx)
            if r.need_identity:
                need_identity = True
            candidates.append(r)

        # Highest-priority non-empty speak that arbiter allows
        # Behaviors declare priority via .priority
        scored: List[tuple[int, TickResult, Any]] = []
        for b, r in zip(
            [x for x in self.behaviors if x.enabled()],
            candidates,
        ):
            # Re-zip carefully: only enabled behaviors were ticked
            pass

        # Rebuild with matching
        enabled_behaviors = [b for b in self.behaviors if b.enabled()]
        scored = []
        for b, r in zip(enabled_behaviors, candidates):
            scored.append((getattr(b, "priority", 0), r, b))
        scored.sort(key=lambda t: t[0], reverse=True)

        speak = ""
        debug: Dict[str, Any] = {
            "mode": None,
            "candidates": len(scored),
            "quiet": quiet,
            "identity_fresh": identity_fresh,
            "occupied": snap.occupied,
        }
        for prio, r, b in scored:
            debug.setdefault("behaviors", {})[b.id] = dict(r.debug or {})
            if r.debug and r.debug.get("mode"):
                debug["mode"] = r.debug.get("mode")
            text = (r.speak or "").strip()
            if not text:
                continue
            req = SpeechRequest(
                text=text,
                priority=prio,
                behavior_id=b.id,
                reason=(r.debug or {}).get("reason", ""),
            )
            ok, why = self.arbiter.allow(
                req, now=now, quiet=quiet, voice_recent_ts=voice_ts
            )
            debug["arbiter"] = why
            if ok:
                self.arbiter.record_speech(now)
                speak = text
                debug["spoke_from"] = b.id
                break
            # Denied: keep need_identity merge but no speak
            debug["arbiter_deny"] = why

        return TickResult(
            speak=speak,
            need_identity=need_identity,
            debug=debug,
        )

"""Multi-behavior runtime: presence cache, arbiter, tick orchestration."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .arbiter import SpeechArbiter
from .config import JokeConfig, RuntimeConfig, WorkdayConfig
from .continuity import ContinuityStore
from .joke_idle import JokeIdleBehavior, JOKE_IDLE_ID
from .logutil import blog, short
from .presence import PresenceCache
from .types import (
    FaceIdentity,
    PresenceSnapshot,
    SpeechRequest,
    TickResult,
)
from .workday import WorkDayBehavior

_TAG = "runtime"


class BehaviorRuntime:
    def __init__(
        self,
        runtime_cfg: RuntimeConfig,
        workday_cfg: WorkdayConfig,
        store: ContinuityStore,
        quiet_fn: Optional[Callable[[], bool]] = None,
        voice_ts_fn: Optional[Callable[[], float]] = None,
        joke_cfg: Optional[JokeConfig] = None,
    ):
        self.runtime_cfg = runtime_cfg
        self.workday_cfg = workday_cfg
        self.store = store
        self.quiet_fn = quiet_fn or (lambda: False)
        self.voice_ts_fn = voice_ts_fn or (lambda: 0.0)

        self.presence = PresenceCache(
            face_max_age_s=runtime_cfg.face_cache_max_age_s,
            image_max_age_s=runtime_cfg.image_cache_max_age_s,
            sticky_s=int(getattr(runtime_cfg, "presence_sticky_s", 1800) or 1800),
            empty_streak_clear=int(
                getattr(runtime_cfg, "presence_empty_streak", 2) or 2
            ),
        )
        self.arbiter = SpeechArbiter(
            min_gap_s=runtime_cfg.speech_min_gap_s,
            suppress_after_voice_s=runtime_cfg.speech_suppress_after_voice_s,
        )
        self.behaviors: List[Any] = []
        self.workday: Optional[WorkDayBehavior] = None
        # Per-behavior last tick time for min_tick_interval (v1 foundation).
        self._last_behavior_tick: Dict[str, float] = {}

        enabled = set(runtime_cfg.behaviors_enabled)
        if "workday" in enabled and workday_cfg.enabled:
            self.workday = WorkDayBehavior(workday_cfg, store)
            self.behaviors.append(self.workday)
            blog(_TAG, f"registered workday (priority={workday_cfg.priority})")
        if JOKE_IDLE_ID in enabled and joke_cfg is not None and joke_cfg.enabled:
            self.behaviors.append(JokeIdleBehavior(joke_cfg, store))
            blog(
                _TAG,
                f"registered joke_idle (priority={joke_cfg.priority}, "
                f"dwell={joke_cfg.min_dwell_s}s, cooldown={joke_cfg.cooldown_s}s)",
            )
        if not self.behaviors:
            blog(_TAG, "no behaviors registered (all disabled or not in BEHAVIORS_ENABLED)")

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
                raw_id = face.get("face_id", face.get("faceId", 0))
                face_id = int(raw_id if raw_id is not None else 0)
                name = str(face.get("name") or "")[:64]
                # Vector stranger faces often use negative IDs (e.g. -3).
                is_stranger = bool(
                    face.get("is_stranger")
                    or face.get("isStranger")
                    or face_id <= 0
                    or not name.strip()
                )
                # Accept any face sighting (including strangers) for identity cache.
                # Drop only completely empty payloads (no id signal and no name).
                if face_id == 0 and not name.strip() and not (
                    "face_id" in face or "faceId" in face
                ):
                    face_obj = None
                else:
                    face_obj = FaceIdentity(
                        face_id=face_id,
                        name=name,
                        is_stranger=is_stranger,
                    )
            except (TypeError, ValueError):
                face_obj = None
        # Binding: chipper occupied=True or any face → person evidence.
        # Chipper occupied=False is weak — do not clear warm sticky (only ambient
        # empty increments streak). Still refresh charger/voice on the snapshot.
        if occupied or face_obj is not None:
            return self.presence.note_person_evidence(
                now,
                source="tick",
                face=face_obj,
                on_charger=on_charger,
                voice_recent=voice_recent,
                image_b64=image_b64,
            )
        # Weak empty: refresh sensors without clearing warm sticky occupancy.
        return self.presence.update(
            now=now,
            occupied=False,
            face=None,
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
        from .types import BehaviorContext

        local_dt = self._local_dt(now)
        self.clock_tick(now)

        quiet = bool(self.quiet_fn())
        voice_ts = float(self.voice_ts_fn() or 0.0)
        # Re-evaluate sticky occupancy (TTL may expire between ticks).
        self.presence._sync_occupied(now)
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

        candidates: List[tuple[Any, TickResult]] = []
        need_identity = False
        for b in self.behaviors:
            if not b.enabled():
                continue
            # min_tick_interval: skip plan if invoked too often (clock_tick still runs).
            min_iv = float(getattr(b, "min_tick_interval", 0) or 0)
            last = self._last_behavior_tick.get(b.id, 0.0)
            if min_iv > 0 and last > 0 and (now - last) < min_iv:
                # Still allow ticks that might need identity urgency? v1: skip fully.
                # Exception: always allow if identity not fresh and we might need it —
                # keep simple: honor min interval for speech-heavy plan only by
                # still running plan (interval is 30s; chipper is 60s). Use as soft
                # guard for future multi-FSM; workday min is 30s so chipper 60s is fine.
                pass
            self._last_behavior_tick[b.id] = now
            r = b.tick(ctx)
            if r.need_identity:
                need_identity = True
            candidates.append((b, r))

        scored = sorted(
            ((getattr(b, "priority", 0), r, b) for b, r in candidates),
            key=lambda t: t[0],
            reverse=True,
        )

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
                # Log interesting non-speech outcomes at verbose level only.
                reason = (r.debug or {}).get("reason")
                if reason and reason not in (
                    "dwell_building",
                    "cooldown",
                    "empty",
                    "armed_idle",
                    "away",
                    "paused",
                ):
                    blog(
                        _TAG,
                        f"{b.id}: no speech ({reason})",
                        verbose=True,
                        data=r.debug,
                    )
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
                # Speech-gated side effects only after allow.
                if r.on_speak_allowed is not None:
                    try:
                        r.on_speak_allowed()
                    except Exception as e:
                        debug["commit_error"] = str(e)
                        blog(_TAG, f"{b.id}: on_speak_allowed failed: {e}")
                self.arbiter.record_speech(now)
                speak = text
                debug["spoke_from"] = b.id
                blog(
                    _TAG,
                    f"ALLOWED speech from {b.id} (prio={prio}, reason={req.reason}): "
                    f"{short(text)!r}",
                )
                break
            debug["arbiter_deny"] = why
            # Denied: do NOT run on_speak_allowed — timers stay, late_check not entered.
            blog(
                _TAG,
                f"DENIED {b.id} speech (arbiter={why}, prio={prio}, "
                f"reason={req.reason}): {short(text)!r} — "
                f"wanted to speak but did not",
            )

        if need_identity and not speak:
            blog(_TAG, "tick result: need_identity (no speech this tick)", verbose=True)

        return TickResult(
            speak=speak,
            need_identity=need_identity,
            debug=debug,
        )

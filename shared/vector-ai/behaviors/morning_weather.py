"""Morning weather FSM: once per live workday morning, fire built-in weather intent.

Pure and synchronous. No network/LLM in tick(). Daily commit only via
on_speak_allowed (speech-gated). Chipper fires intent_weather_extend.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .config import MorningWeatherConfig, minutes_since_midnight
from .continuity import ContinuityStore, WorkdayRecord
from .logutil import blog
from .types import BehaviorContext, FaceIdentity, TickResult, WorkdayMode
from .workday import _identified_primary

MORNING_WEATHER_ID = "morning_weather"
WEATHER_INTENT = "intent_weather_extend"
_TAG = "morning_weather"

REASON_NOT_WORKING = "workday not started"
REASON_WINDOW = "outside morning-lunch window"
REASON_FIRED = "already told weather today"
REASON_EMPTY = "desk empty"
REASON_DWELL = "waiting for quiet dwell"
REASON_VOICE = "recent voice activity"
REASON_NEED_FACE = "need enrolled face"
REASON_STRANGER = "stranger or non-primary"
REASON_FACE_OLD = "enrolled face too old"
REASON_READY = "ready to tell weather"
REASON_PROPOSE = "proposing weather"


def _in_window(local_dt: datetime, begin: tuple[int, int], end: tuple[int, int]) -> bool:
    now_m = minutes_since_midnight(local_dt.hour, local_dt.minute)
    b = minutes_since_midnight(*begin)
    e = minutes_since_midnight(*end)
    return b <= now_m < e


class MorningWeatherBehavior:
    id = MORNING_WEATHER_ID
    min_tick_interval: float = 30.0

    def __init__(self, cfg: MorningWeatherConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _face_gate(
        self,
        now: float,
        rec: WorkdayRecord,
        face: Optional[FaceIdentity],
        face_ts: float,
        identity_fresh: bool,
    ) -> Optional[str]:
        """Skip reason for enrolled/primary/freshness, or None if the face is ok."""
        if not identity_fresh or face is None:
            return REASON_NEED_FACE
        if not _identified_primary(face, rec):
            return REASON_STRANGER
        ts = float(face_ts or 0.0)
        if ts <= 0 or (now - ts) > float(self.cfg.face_max_age_s):
            return REASON_FACE_OLD
        return None

    def _ops_snapshot(
        self,
        now: float,
        *,
        session_started_at: float = 0.0,
        occupied: bool = False,
        last_user_voice_at: float = 0.0,
        face: Optional[FaceIdentity] = None,
        face_ts: float = 0.0,
        identity_fresh: bool = False,
    ) -> dict:
        local_dt = datetime.fromtimestamp(now, tz=self.cfg.tz)
        date = local_dt.date().isoformat()
        rec = self.store.load_workday(date)
        daily = self.store.weather_load_daily(date)
        last_fired = float(daily["last_fired_at"] or 0.0)
        in_window = _in_window(local_dt, self.cfg.window_begin, self.cfg.lunch_end)
        quiet_base = max(
            float(session_started_at or 0.0),
            float(last_user_voice_at or 0.0),
            last_fired,
        )
        quiet_dwell = (now - quiet_base) if quiet_base > 0 else 0.0
        mode = rec.mode.value if isinstance(rec.mode, WorkdayMode) else str(rec.mode)
        ts = float(face_ts or 0.0)
        face_age_s = (now - ts) if ts > 0 else None
        enrolled_primary = bool(face is not None and _identified_primary(face, rec))
        face_reason = self._face_gate(now, rec, face, ts, identity_fresh)

        if rec.mode != WorkdayMode.WORKING:
            reason = REASON_NOT_WORKING
        elif not in_window:
            reason = REASON_WINDOW
        elif last_fired > 0:
            reason = REASON_FIRED
        elif not occupied:
            reason = REASON_EMPTY
        elif face_reason:
            reason = face_reason
        elif quiet_dwell < float(self.cfg.min_dwell_s):
            reason = REASON_DWELL
        else:
            reason = REASON_READY

        return {
            "date": date,
            "workday_mode": mode,
            "arm_source": rec.arm_source or "",
            "window_begin": f"{self.cfg.window_begin[0]:02d}:{self.cfg.window_begin[1]:02d}",
            "lunch_end": f"{self.cfg.lunch_end[0]:02d}:{self.cfg.lunch_end[1]:02d}",
            "in_window": in_window,
            "min_dwell_s": self.cfg.min_dwell_s,
            "quiet_dwell_s": quiet_dwell,
            "dwell_remaining_s": max(0.0, float(self.cfg.min_dwell_s) - quiet_dwell),
            "last_fired_at": last_fired if last_fired > 0 else None,
            "occupied": bool(occupied),
            "identity_fresh": bool(identity_fresh),
            "face_age_s": face_age_s,
            "face_max_age_s": int(self.cfg.face_max_age_s),
            "enrolled_primary": enrolled_primary,
            "intent": WEATHER_INTENT,
            "reason": reason,
        }

    def status_summary(self, now: float, **kwargs) -> str:
        snap = self._ops_snapshot(
            now,
            session_started_at=float(kwargs.get("session_started_at") or 0.0),
            occupied=bool(kwargs.get("occupied", False)),
            last_user_voice_at=float(kwargs.get("last_user_voice_at") or 0.0),
            face=kwargs.get("face"),
            face_ts=float(kwargs.get("face_ts") or 0.0),
            identity_fresh=bool(kwargs.get("identity_fresh", False)),
        )
        return str(snap["reason"])

    def status(
        self,
        now: float,
        *,
        session_started_at: float = 0.0,
        occupied: bool = False,
        last_user_voice_at: float = 0.0,
        face: Optional[FaceIdentity] = None,
        face_ts: float = 0.0,
        identity_fresh: bool = False,
        **_kwargs,
    ) -> dict:
        snap = self._ops_snapshot(
            now,
            session_started_at=session_started_at,
            occupied=occupied,
            last_user_voice_at=last_user_voice_at,
            face=face,
            face_ts=face_ts,
            identity_fresh=identity_fresh,
        )
        return {
            "id": self.id,
            "schema_version": 1,
            "enabled": bool(self.cfg.enabled),
            **snap,
        }

    def tick(self, ctx: BehaviorContext, *, skip_dwell: bool = False) -> TickResult:
        r = TickResult()
        cfg = self.cfg
        local_dt = ctx.local_dt
        if getattr(local_dt, "tzinfo", None) is None:
            local_dt = datetime.fromtimestamp(ctx.now, tz=cfg.tz)
        date = local_dt.date().isoformat()

        def _skip(reason: str, **extra) -> TickResult:
            r.debug = {"mode": "idle", "reason": reason, **extra}
            blog(_TAG, f"skip: {reason}", verbose=True, data=extra or None)
            return r

        rec = self.store.load_workday(date)
        if rec.mode != WorkdayMode.WORKING:
            return _skip(REASON_NOT_WORKING, workday_mode=rec.mode.value)

        if not _in_window(local_dt, cfg.window_begin, cfg.lunch_end):
            return _skip(REASON_WINDOW)

        daily = self.store.weather_load_daily(date)
        last_fired = float(daily["last_fired_at"] or 0.0)
        if last_fired > 0:
            return _skip(REASON_FIRED)

        if not ctx.presence.occupied:
            return _skip(REASON_EMPTY)

        if ctx.presence.voice_recent:
            return _skip(REASON_VOICE)

        face = ctx.presence.face
        face_ts = float(getattr(ctx.presence, "face_ts", 0.0) or 0.0)
        face_reason = self._face_gate(
            ctx.now, rec, face, face_ts, bool(ctx.identity_fresh)
        )
        if face_reason:
            return _skip(face_reason)

        if not skip_dwell:
            quiet_base = max(
                float(getattr(ctx.presence, "session_started_at", 0.0) or 0.0),
                float(getattr(ctx, "last_user_voice_at", 0.0) or 0.0),
            )
            quiet_dwell = (ctx.now - quiet_base) if quiet_base > 0 else 0.0
            if quiet_dwell < float(cfg.min_dwell_s):
                return _skip(REASON_DWELL)

        spoke_date, spoke_at = date, ctx.now

        def _on_allowed() -> None:
            self.store.weather_commit_fired(spoke_date, spoke_at)
            blog(_TAG, f"fired {WEATHER_INTENT} [day committed]")

        r.intent = WEATHER_INTENT
        r.on_speak_allowed = _on_allowed
        r.debug = {
            "mode": "idle",
            "reason": REASON_PROPOSE,
            "intent": WEATHER_INTENT,
        }
        blog(
            _TAG,
            f"want {WEATHER_INTENT} — waiting on speech arbiter",
        )
        return r

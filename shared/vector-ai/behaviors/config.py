from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

_log = logging.getLogger("behaviors.config")


def parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time {s!r}")
    return h, m


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        _log.warning("invalid int for %s=%r; using default %s", key, raw, default)
        return default


def _hhmm(env: Mapping[str, str], key: str, default: str) -> tuple[int, int]:
    raw = env.get(key) or default
    try:
        return parse_hhmm(str(raw))
    except ValueError as e:
        _log.warning("invalid HH:MM for %s=%r (%s); using %s", key, raw, e, default)
        return parse_hhmm(default)


@dataclass(frozen=True)
class RuntimeConfig:
    face_cache_max_age_s: int = 120
    image_cache_max_age_s: int = 45
    speech_min_gap_s: int = 90
    speech_suppress_after_voice_s: int = 120
    behaviors_enabled: tuple[str, ...] = ("workday",)


@dataclass(frozen=True)
class WorkdayConfig:
    enabled: bool = False
    tz: ZoneInfo = ZoneInfo("UTC")
    start_begin: tuple[int, int] = (9, 0)
    start_end: tuple[int, int] = (10, 30)
    away_window_begin: tuple[int, int] = (9, 30)
    end: tuple[int, int] = (18, 0)
    poke_interval_s: int = 5400
    away_s: int = 1800
    late_check_timeout_s: int = 900
    reid_after_away_s: int = 3600  # 0 = never re-ID
    priority: int = 80
    # After a non-primary / stranger ID at a juncture, wait this long before
    # requesting need_identity again (avoids re-probe every FACE_CACHE_MAX_AGE).
    identity_reject_cooldown_s: int = 600


def load_runtime_config(env: Optional[Mapping[str, str]] = None) -> RuntimeConfig:
    env = env if env is not None else os.environ
    try:
        raw = (env.get("BEHAVIORS_ENABLED") or "workday").strip()
        behaviors = tuple(b.strip() for b in raw.split(",") if b.strip())
        return RuntimeConfig(
            face_cache_max_age_s=_int(env, "FACE_CACHE_MAX_AGE_S", 120),
            image_cache_max_age_s=_int(env, "IMAGE_CACHE_MAX_AGE_S", 45),
            speech_min_gap_s=_int(env, "SPEECH_MIN_GAP_S", 90),
            speech_suppress_after_voice_s=_int(env, "SPEECH_SUPPRESS_AFTER_VOICE_S", 120),
            behaviors_enabled=behaviors or ("workday",),
        )
    except Exception as e:
        _log.warning("load_runtime_config failed (%s); using defaults", e)
        return RuntimeConfig()


def load_workday_config(env: Optional[Mapping[str, str]] = None) -> WorkdayConfig:
    """Load workday config. Bad HH:MM / ints fall back; hard failure → disabled defaults."""
    env = env if env is not None else os.environ
    try:
        tz_name = (env.get("WORKDAY_TZ") or env.get("TZ") or "UTC").strip()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            _log.warning("invalid WORKDAY_TZ/TZ %r; using UTC", tz_name)
            tz = ZoneInfo("UTC")
        return WorkdayConfig(
            enabled=_truthy(env.get("WORKDAY_ENABLED")),
            tz=tz,
            start_begin=_hhmm(env, "WORKDAY_START_BEGIN", "09:00"),
            start_end=_hhmm(env, "WORKDAY_START_END", "10:30"),
            away_window_begin=_hhmm(env, "WORKDAY_AWAY_WINDOW_BEGIN", "09:30"),
            end=_hhmm(env, "WORKDAY_END", "18:00"),
            poke_interval_s=_int(env, "WORKDAY_POKE_INTERVAL_S", 5400),
            away_s=_int(env, "WORKDAY_AWAY_S", 1800),
            late_check_timeout_s=_int(env, "WORKDAY_LATE_CHECK_TIMEOUT_S", 900),
            reid_after_away_s=_int(env, "WORKDAY_REID_AFTER_AWAY_S", 3600),
            priority=_int(env, "WORKDAY_PRIORITY", 80),
            identity_reject_cooldown_s=_int(env, "WORKDAY_IDENTITY_REJECT_COOLDOWN_S", 600),
        )
    except Exception as e:
        # Never crash vector-ai import on bad env — chat must still work.
        _log.error("load_workday_config failed (%s); Work Day disabled with defaults", e)
        print(f"[behaviors] WORKDAY config error ({e}); disabled with defaults")
        return WorkdayConfig(enabled=False)


def minutes_since_midnight(h: int, m: int) -> int:
    return h * 60 + m

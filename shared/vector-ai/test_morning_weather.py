"""Unit tests for morning_weather FSM.

Run from VectorIntelligence repo root:
  python3 -m pytest shared/vector-ai/test_morning_weather.py -q
"""
from __future__ import annotations

import ast
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from behaviors.config import (
    MorningWeatherConfig,
    WorkdayConfig,
    load_morning_weather_config,
    load_runtime_config,
)
from behaviors.continuity import ContinuityStore, WorkdayRecord
from behaviors.morning_weather import (
    MORNING_WEATHER_ID,
    REASON_DWELL,
    REASON_EMPTY,
    REASON_FACE_OLD,
    REASON_FIRED,
    REASON_NEED_FACE,
    REASON_NOT_WORKING,
    REASON_PROPOSE,
    REASON_READY,
    REASON_STRANGER,
    REASON_WINDOW,
    WEATHER_INTENT,
    MorningWeatherBehavior,
)
from behaviors.runtime import BehaviorRuntime
from behaviors.types import (
    BehaviorContext,
    FaceIdentity,
    PresenceSnapshot,
    WorkdayMode,
)


def check(name: str, cond: bool) -> None:
    assert cond, name


_CAM_FACE = FaceIdentity(1, "Cam", False)
_CAM_FACE_PAYLOAD = {"face_id": 1, "name": "Cam", "is_stranger": False}
_FACE_UNSET = object()


def _wx_cfg(**kwargs) -> MorningWeatherConfig:
    base = dict(
        enabled=True,
        priority=40,
        window_begin=(9, 0),
        lunch_end=(12, 30),
        min_dwell_s=60,
        face_max_age_s=90,
        tz=ZoneInfo("UTC"),
    )
    base.update(kwargs)
    return MorningWeatherConfig(**base)


def _wd_cfg() -> WorkdayConfig:
    return WorkdayConfig(
        enabled=True,
        tz=ZoneInfo("UTC"),
        start_begin=(9, 0),
        start_end=(10, 30),
        away_window_begin=(9, 30),
        end=(18, 0),
        poke_interval_s=5400,
        away_s=1800,
        late_check_timeout_s=900,
        reid_after_away_s=3600,
        priority=80,
        identity_reject_cooldown_s=600,
    )


def _arm_working(store: ContinuityStore, date: str, now: float) -> None:
    store.save_workday(
        WorkdayRecord(
            date=date,
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            primary_face_name="Cam",
            started_at=now - 600,
            arm_source="morning",
        )
    )


def _ctx(
    behavior: MorningWeatherBehavior,
    now: float,
    *,
    hour: int,
    minute: int,
    occupied: bool,
    session_started_at: float | None = None,
    voice_recent: bool = False,
    last_user_voice_at: float = 0.0,
    face: FaceIdentity | None | object = _FACE_UNSET,
    identity_fresh: bool | None = None,
    face_ts: float | None = None,
) -> BehaviorContext:
    local = datetime(2026, 8, 29, hour, minute, tzinfo=behavior.cfg.tz)
    sess = now - 300.0 if session_started_at is None else session_started_at
    if face is _FACE_UNSET:
        face_obj: FaceIdentity | None = _CAM_FACE if occupied else None
    else:
        face_obj = face  # type: ignore[assignment]
    id_fresh = occupied if identity_fresh is None else identity_fresh
    if face_ts is None:
        ts = now if face_obj is not None else 0.0
    else:
        ts = face_ts
    snap = PresenceSnapshot(
        occupied=occupied,
        face=face_obj,
        face_ts=ts,
        updated_at=now,
        session_started_at=sess if occupied else 0.0,
        voice_recent=voice_recent,
    )
    return BehaviorContext(
        now=now,
        local_dt=local,
        presence=snap,
        quiet=False,
        config=behavior.cfg,
        identity_fresh=id_fresh,
        last_user_voice_at=last_user_voice_at,
    )


def _ingest_cam(rt: BehaviorRuntime, now: float, *, occupied_at: float | None = None) -> None:
    """Occupancy session first, then a fresh enrolled Cam probe (face age ~0)."""
    t0 = now - 120.0 if occupied_at is None else occupied_at
    rt.ingest_tick_payload(now=t0, occupied=True, voice_recent=False)
    rt.ingest_tick_payload(
        now=now,
        occupied=True,
        voice_recent=False,
        face=_CAM_FACE_PAYLOAD,
        hard_face=True,
    )


def _apply(r) -> None:
    if r.on_speak_allowed is not None:
        r.on_speak_allowed()


def test_load_disabled_by_default() -> None:
    cfg = load_morning_weather_config({})
    check("default off", cfg.enabled is False)
    check("default priority 40", cfg.priority == 40)
    check("default lunch 12:30", cfg.lunch_end == (12, 30))
    check("default dwell 60", cfg.min_dwell_s == 60)
    check("default face max age 90", cfg.face_max_age_s == 90)


def test_load_window_begin_falls_back_to_workday_start() -> None:
    cfg = load_morning_weather_config(
        {
            "MORNING_WEATHER_ENABLED": "1",
            "WORKDAY_START_BEGIN": "08:15",
            "MORNING_WEATHER_LUNCH_END": "13:00",
        }
    )
    check("enabled", cfg.enabled is True)
    check("window from workday start", cfg.window_begin == (8, 15))
    check("custom lunch", cfg.lunch_end == (13, 0))


def test_load_explicit_window_begin_wins() -> None:
    cfg = load_morning_weather_config(
        {
            "MORNING_WEATHER_ENABLED": "yes",
            "WORKDAY_START_BEGIN": "08:15",
            "MORNING_WEATHER_WINDOW_BEGIN": "09:30",
        }
    )
    check("explicit window", cfg.window_begin == (9, 30))


def test_load_face_max_age_override() -> None:
    cfg = load_morning_weather_config({"MORNING_WEATHER_FACE_MAX_AGE_S": "45"})
    check("custom face max age", cfg.face_max_age_s == 45)
    bad = load_morning_weather_config({"MORNING_WEATHER_FACE_MAX_AGE_S": "nope"})
    check("bad int falls back to 90", bad.face_max_age_s == 90)


def test_skip_when_workday_not_working() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = MorningWeatherBehavior(_wx_cfg(), store)
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        r = b.tick(_ctx(b, now, hour=10, minute=0, occupied=True))
        check("no intent", r.intent == "")
        check("reason", r.debug.get("reason") == REASON_NOT_WORKING)


def test_skip_late_working() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        store.save_workday(
            WorkdayRecord(
                date="2026-08-29",
                mode=WorkdayMode.LATE_WORKING,
                arm_source="late_yes",
                started_at=now,
            )
        )
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(_ctx(b, now, hour=10, minute=0, occupied=True))
        check("late working skipped", r.debug.get("reason") == REASON_NOT_WORKING)


def test_skip_outside_window() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 13, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(_ctx(b, now, hour=13, minute=0, occupied=True))
        check("after lunch", r.intent == "")
        check("reason", r.debug.get("reason") == REASON_WINDOW)


def test_skip_empty_desk() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(_ctx(b, now, hour=10, minute=0, occupied=False))
        check("empty", r.debug.get("reason") == REASON_EMPTY)


def test_skip_dwell() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(min_dwell_s=120), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=0,
                occupied=True,
                session_started_at=now - 10,
            )
        )
        check("dwell", r.debug.get("reason") == REASON_DWELL)
        check("no commit yet", store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0)


def test_skip_no_face() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=0,
                occupied=True,
                face=None,
                identity_fresh=False,
            )
        )
        check("no intent", r.intent == "")
        check("need enrolled face", r.debug.get("reason") == REASON_NEED_FACE)
        check("no need_identity", r.need_identity is False)
        check(
            "daily untouched",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )


def test_skip_stranger() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=0,
                occupied=True,
                face=FaceIdentity(-3, "guest", True),
                identity_fresh=True,
            )
        )
        check("no intent", r.intent == "")
        check("stranger", r.debug.get("reason") == REASON_STRANGER)
        check("no need_identity", r.need_identity is False)
        check(
            "stranger does not consume daily",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )


def test_skip_wrong_primary() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=0,
                occupied=True,
                face=FaceIdentity(2, "Pat", False),
                identity_fresh=True,
            )
        )
        check("no intent", r.intent == "")
        check("non-primary", r.debug.get("reason") == REASON_STRANGER)
        check("no need_identity", r.need_identity is False)
        check(
            "daily untouched",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )


def test_skip_face_too_old() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(face_max_age_s=90), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=0,
                occupied=True,
                face=_CAM_FACE,
                identity_fresh=True,
                face_ts=now - 200.0,
            )
        )
        check("no intent", r.intent == "")
        check("face too old", r.debug.get("reason") == REASON_FACE_OLD)
        check("no need_identity", r.need_identity is False)
        check(
            "daily untouched",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )


def test_skip_dwell_kw_bypasses_dwell() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(min_dwell_s=120), store)
        ctx = _ctx(
            b,
            now,
            hour=10,
            minute=0,
            occupied=True,
            session_started_at=now - 10,
        )
        blocked = b.tick(ctx)
        check("dwell still blocks default path", blocked.debug.get("reason") == REASON_DWELL)
        check("no default intent", blocked.intent == "")
        r = b.tick(ctx, skip_dwell=True)
        check("intent despite short dwell", r.intent == WEATHER_INTENT)
        check("propose", r.debug.get("reason") == REASON_PROPOSE)
        check("no need_identity", r.need_identity is False)
        check(
            "ungated until apply",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )
        _apply(r)
        check(
            "committed after allow",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == now,
        )


def test_propose_and_speech_gated_commit() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 5, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        b = MorningWeatherBehavior(_wx_cfg(min_dwell_s=60), store)
        r = b.tick(
            _ctx(
                b,
                now,
                hour=10,
                minute=5,
                occupied=True,
                session_started_at=now - 180,
            )
        )
        check("intent set", r.intent == WEATHER_INTENT)
        check("no speak line", r.speak == "")
        check("propose", r.debug.get("reason") == REASON_PROPOSE)
        check("no need_identity", r.need_identity is False)
        check("ungated daily", store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0)
        _apply(r)
        d = store.weather_load_daily("2026-08-29")
        check("committed", d["last_fired_at"] == now)

        r2 = b.tick(
            _ctx(
                b,
                now + 10,
                hour=10,
                minute=5,
                occupied=True,
                session_started_at=now - 180,
            )
        )
        check("second tick fired", r2.debug.get("reason") == REASON_FIRED)
        check("no second intent", r2.intent == "")


def test_runtime_allow_returns_intent() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 10, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        rcfg = load_runtime_config(
            {
                "BEHAVIORS_ENABLED": "morning_weather",
                "SPEECH_MIN_GAP_S": "0",
                "SPEECH_SUPPRESS_AFTER_VOICE_S": "0",
            }
        )
        rt = BehaviorRuntime(
            rcfg,
            _wd_cfg(),
            store,
            weather_cfg=_wx_cfg(min_dwell_s=1),
            voice_ts_fn=lambda: 0.0,
        )
        _ingest_cam(rt, now)
        result = rt.tick(now)
        check("runtime intent", result.intent == WEATHER_INTENT)
        check("runtime speak empty", result.speak == "")
        check("spoke_from", (result.debug or {}).get("spoke_from") == MORNING_WEATHER_ID)
        check(
            "committed via runtime",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == now,
        )


def test_runtime_arbiter_deny_does_not_commit() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 10, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        rcfg = load_runtime_config(
            {
                "BEHAVIORS_ENABLED": "morning_weather",
                "SPEECH_MIN_GAP_S": "90",
                "SPEECH_SUPPRESS_AFTER_VOICE_S": "0",
            }
        )
        rt = BehaviorRuntime(
            rcfg,
            _wd_cfg(),
            store,
            weather_cfg=_wx_cfg(min_dwell_s=1),
            voice_ts_fn=lambda: 0.0,
        )
        rt.arbiter.record_speech(now - 10.0)
        _ingest_cam(rt, now)
        result = rt.tick(now)
        check("denied intent empty", result.intent == "")
        check(
            "daily untouched",
            store.weather_load_daily("2026-08-29")["last_fired_at"] == 0.0,
        )


def test_feature_gates() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg()
        wx = _wx_cfg(enabled=True)
        rt = BehaviorRuntime(
            load_runtime_config({"BEHAVIORS_ENABLED": "workday"}),
            wcfg,
            store,
            weather_cfg=wx,
        )
        check(
            "not in list",
            not any(getattr(x, "id", None) == MORNING_WEATHER_ID for x in rt.behaviors),
        )
        rt2 = BehaviorRuntime(
            load_runtime_config({"BEHAVIORS_ENABLED": "morning_weather"}),
            wcfg,
            store,
            weather_cfg=_wx_cfg(enabled=False),
        )
        check(
            "flag off",
            not any(getattr(x, "id", None) == MORNING_WEATHER_ID for x in rt2.behaviors),
        )
        rt3 = BehaviorRuntime(
            load_runtime_config({"BEHAVIORS_ENABLED": "morning_weather"}),
            wcfg,
            store,
            weather_cfg=wx,
        )
        check(
            "registered",
            any(getattr(x, "id", None) == MORNING_WEATHER_ID for x in rt3.behaviors),
        )


def test_status_card() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        rcfg = load_runtime_config({"BEHAVIORS_ENABLED": "morning_weather"})
        rt = BehaviorRuntime(rcfg, _wd_cfg(), store, weather_cfg=_wx_cfg())
        rt.presence.note_person_evidence(now - 200.0, source="test", face=None)
        rt.presence.note_person_evidence(now, source="test", face=_CAM_FACE)
        detail = rt.behavior_status(MORNING_WEATHER_ID, now)
        check("detail", detail is not None)
        assert detail is not None
        check("intent field", detail["intent"] == WEATHER_INTENT)
        check("reason ready", detail["reason"] == REASON_READY)
        check("enrolled primary", detail["enrolled_primary"] is True)
        check("identity fresh", detail["identity_fresh"] is True)
        check("face age present", detail["face_age_s"] is not None)
        check("face age recent", float(detail["face_age_s"]) < 1.0)
        check("face max age knob", detail["face_max_age_s"] == 90)
        idx = rt.build_state_index(now)
        check("card", MORNING_WEATHER_ID in idx["behaviors"])
        check("href", idx["behaviors"][MORNING_WEATHER_ID]["href"] == "/v1/behaviors/morning_weather")
        check("card summary ready", idx["behaviors"][MORNING_WEATHER_ID]["summary"] == REASON_READY)


def test_status_need_face_when_occupied_without_identity() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _arm_working(store, "2026-08-29", now)
        rcfg = load_runtime_config({"BEHAVIORS_ENABLED": "morning_weather"})
        rt = BehaviorRuntime(rcfg, _wd_cfg(), store, weather_cfg=_wx_cfg())
        rt.presence.note_person_evidence(now - 200.0, source="test", face=None)
        detail = rt.behavior_status(MORNING_WEATHER_ID, now)
        assert detail is not None
        check("need enrolled face", detail["reason"] == REASON_NEED_FACE)
        check("not enrolled primary", detail["enrolled_primary"] is False)
        check("identity not fresh", detail["identity_fresh"] is False)


def test_anti_patterns() -> None:
    root = Path(__file__).resolve().parent
    src = (root / "behaviors" / "morning_weather.py").read_text(encoding="utf-8")
    check("no llm_chat_once", "llm_chat_once" not in src)
    check("no httpx", "httpx" not in src)
    check("no AsyncClient", "AsyncClient" not in src)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                check("no service import", alias.name != "service")
        elif isinstance(node, ast.ImportFrom):
            check(
                "no service from",
                not (node.module and node.module.startswith("service")),
            )

#!/usr/bin/env python3
"""Unit tests for behaviors runtime + Work Day Mode.

Run (repo root or any cwd with pytest.ini):
  python3 -m pytest shared/vector-ai/test_behaviors.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from behaviors.config import WorkdayConfig, load_workday_config, parse_hhmm
from behaviors.presence import PresenceCache
from behaviors.arbiter import SpeechArbiter
from behaviors.types import (
    BehaviorContext,
    FaceIdentity,
    PresenceSnapshot,
    SpeechRequest,
    WorkdayMode,
)
from behaviors.continuity import ContinuityStore, WorkdayRecord
from behaviors.workday import WorkDayBehavior, parse_work_commands
from behaviors.runtime import BehaviorRuntime
from behaviors.config import RuntimeConfig, load_runtime_config


def check(name: str, cond: bool) -> None:
    """Assert with a label (pytest-friendly; replaces old SystemExit runner)."""
    assert cond, name


# ---------------------------------------------------------------------------
# Task 1: config
# ---------------------------------------------------------------------------

def test_parse_hhmm() -> None:
    assert parse_hhmm("09:00") == (9, 0)
    assert parse_hhmm("18:00") == (18, 0)
    try:
        parse_hhmm("9")
        raise AssertionError("should fail")
    except ValueError:
        pass


def test_load_workday_disabled_by_default() -> None:
    env = {}
    cfg = load_workday_config(env)
    check("default disabled", cfg.enabled is False)
    check("default poke 5400", cfg.poke_interval_s == 5400)
    check("default away 1800", cfg.away_s == 1800)


def test_load_workday_enabled() -> None:
    env = {
        "WORKDAY_ENABLED": "1",
        "WORKDAY_START_BEGIN": "09:00",
        "WORKDAY_START_END": "10:30",
        "WORKDAY_END": "18:00",
        "WORKDAY_TZ": "UTC",
    }
    cfg = load_workday_config(env)
    check("enabled", cfg.enabled is True)
    check("tz UTC", str(cfg.tz) == "UTC")


# ---------------------------------------------------------------------------
# Task 2: presence + arbiter
# ---------------------------------------------------------------------------

def test_presence_occupancy_without_face() -> None:
    cache = PresenceCache(face_max_age_s=120, image_max_age_s=45)
    snap = cache.update(now=1000.0, occupied=True, face=None)
    check("occupied true", snap.occupied is True)
    check("no face", snap.face is None)
    check("identity not fresh", cache.identity_fresh(1000.0) is False)


def test_presence_identity_cached() -> None:
    cache = PresenceCache(face_max_age_s=120, image_max_age_s=45)
    face = FaceIdentity(face_id=1, name="Cam", is_stranger=False)
    cache.update(now=1000.0, occupied=True, face=face)
    check("identity fresh at 1000", cache.identity_fresh(1000.0) is True)
    check("identity fresh at 1119", cache.identity_fresh(1119.0) is True)
    check("identity stale at 1121", cache.identity_fresh(1121.0) is False)


def test_arbiter_min_gap_and_quiet() -> None:
    arb = SpeechArbiter(min_gap_s=90, suppress_after_voice_s=120)
    req = SpeechRequest(text="hi", priority=80, behavior_id="workday")
    ok, why = arb.allow(req, now=1000.0, quiet=True, voice_recent_ts=0.0)
    check("quiet blocks", ok is False)
    ok, why = arb.allow(req, now=1000.0, quiet=False, voice_recent_ts=0.0)
    check("first allow", ok is True)
    arb.record_speech(1000.0)
    ok, why = arb.allow(req, now=1050.0, quiet=False, voice_recent_ts=0.0)
    check("gap blocks", ok is False)
    ok, why = arb.allow(req, now=1091.0, quiet=False, voice_recent_ts=0.0)
    check("after gap allow", ok is True)


# ---------------------------------------------------------------------------
# Task 3: continuity
# ---------------------------------------------------------------------------

def test_continuity_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "workday.db")
        rec = WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=1000.0,
            arm_source="morning",
        )
        store.save_workday(rec)
        loaded = store.load_workday("2026-07-18")
        check("loaded mode", loaded.mode == WorkdayMode.WORKING)
        check("face id", loaded.primary_face_id == 1)
        check("day strip non-empty", "working" in store.day_strip("2026-07-18").lower())


# ---------------------------------------------------------------------------
# Task 4 helpers + WorkDayBehavior
# ---------------------------------------------------------------------------

def _wd_cfg(**kwargs) -> WorkdayConfig:
    base = dict(
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
    base.update(kwargs)
    return WorkdayConfig(**base)


def _apply_speak(r) -> None:
    """Simulate arbiter allow: run deferred speech-gated commit."""
    if r.on_speak_allowed is not None:
        r.on_speak_allowed()


def _ctx(
    behavior: WorkDayBehavior,
    occupied: bool,
    face: FaceIdentity | None,
    hour: int,
    minute: int,
    now: float,
    identity_fresh: bool | None = None,
) -> BehaviorContext:
    local = datetime(2026, 7, 18, hour, minute, tzinfo=behavior.cfg.tz)
    if identity_fresh is None:
        identity_fresh = face is not None
    snap = PresenceSnapshot(
        occupied=occupied,
        face=face,
        face_ts=now if face else 0.0,
        updated_at=now,
    )
    return BehaviorContext(
        now=now,
        local_dt=local,
        presence=snap,
        quiet=False,
        config=behavior.cfg,
        identity_fresh=identity_fresh,
    )


def test_workday_disabled() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(enabled=False), store)
        face = FaceIdentity(1, "Cam", False)
        r = b.plan(_ctx(b, True, face, 9, 15, 1_000_000.0))
        check("disabled no speak", r.speak == "")
        check("disabled no need_id", r.need_identity is False)
        check("disabled reason", r.debug.get("reason") == "disabled")


def test_morning_start() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        face = FaceIdentity(1, "Cam", False)
        now = 1_800_000_000.0
        r = b.plan(_ctx(b, True, face, 9, 15, now))
        rec = store.load_workday("2026-07-18")
        check("morning -> working", rec.mode == WorkdayMode.WORKING)
        check("started_at set", rec.started_at == now)
        check("primary face", rec.primary_face_id == 1)
        check("no speak on arm", r.speak == "")
        check("arm reason", r.debug.get("reason") == "morning_start")


def test_morning_no_face_needs_identity() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        now = 1_800_000_000.0
        r = b.plan(_ctx(b, True, None, 9, 15, now, identity_fresh=False))
        rec = store.load_workday("2026-07-18")
        check("still waiting", rec.mode == WorkdayMode.WAITING_MORNING)
        check("need identity", r.need_identity is True)


def test_stranger_cannot_start() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        face = FaceIdentity(99, "Guest", is_stranger=True)
        r = b.plan(_ctx(b, True, face, 9, 15, 1_800_000_000.0, identity_fresh=True))
        rec = store.load_workday("2026-07-18")
        check("stranger no start", rec.mode == WorkdayMode.WAITING_MORNING)
        check("stranger no speak", r.speak == "")
        check("stranger no re-probe spam", r.need_identity is False)


def test_no_show_after_window() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        r = b.plan(_ctx(b, False, None, 10, 31, 1_800_000_000.0, identity_fresh=False))
        rec = store.load_workday("2026-07-18")
        check("no_show mode", rec.mode == WorkdayMode.NO_SHOW)
        check("no speak no_show", r.speak == "")


def test_late_check_and_yes() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        store.save_workday(WorkdayRecord(date="2026-07-18", mode=WorkdayMode.NO_SHOW))
        face = FaceIdentity(1, "Cam", False)
        now = 1_800_000_000.0
        r = b.plan(_ctx(b, True, face, 13, 0, now))
        rec = store.load_workday("2026-07-18")
        # Speech-gated: mode stays no_show until commit
        check("pending late_check before commit", rec.mode == WorkdayMode.NO_SHOW)
        check("late question", "afternoon" in r.speak.lower() or "morning" in r.speak.lower())
        _apply_speak(r)
        rec = store.load_workday("2026-07-18")
        check("late_check mode after commit", rec.mode == WorkdayMode.LATE_CHECK)
        b.on_afternoon_yes("2026-07-18", now=now)
        rec = store.load_workday("2026-07-18")
        check("late_working", rec.mode == WorkdayMode.LATE_WORKING)


def test_poke_interval() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(poke_interval_s=5400), store)
        start = 1_800_000_000.0
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=start,
            arm_source="morning",
            last_poke_at=start,
        ))
        # 90 minutes later, still occupied
        now = start + 5400
        r = b.plan(_ctx(b, True, None, 10, 46, now, identity_fresh=False))
        check("on-task poke speaks", len(r.speak) > 0)
        check("poke reason", r.debug.get("reason") == "on_task_poke")
        # Mid-day tick does not need identity
        check("no need_id mid-day", r.need_identity is False)


def test_away_29m_no_speak_30m_speaks() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(away_s=1800), store)
        start = 1_800_000_000.0
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=start,
            arm_source="morning",
            last_poke_at=start,
            absence_started_at=start + 100,
        ))
        # 29 minutes away
        now = start + 100 + 29 * 60
        r = b.plan(_ctx(b, False, None, 11, 0, now, identity_fresh=False))
        check("29m no away speak", r.speak == "")
        # 30 minutes away
        now2 = start + 100 + 30 * 60
        r2 = b.plan(_ctx(b, False, None, 11, 1, now2, identity_fresh=False))
        check("30m away speak", len(r2.speak) > 0)
        check("away reason", r2.debug.get("reason") == "away_scold")
        _apply_speak(r2)
        # Second tick same absence: no re-speak
        r3 = b.plan(_ctx(b, False, None, 11, 5, now2 + 300, identity_fresh=False))
        check("away speak once", r3.speak == "")


def test_pause_blocks_pokes() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(poke_interval_s=60), store)
        start = 1_800_000_000.0
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=start,
            last_poke_at=start,
            arm_source="morning",
        ))
        b.on_pause("2026-07-18", until_ts=start + 10_000)
        r = b.plan(_ctx(b, True, None, 12, 0, start + 5000, identity_fresh=False))
        check("paused no speak", r.speak == "")
        check("paused mode", r.debug.get("mode") == "paused" or r.debug.get("reason") == "paused")


def test_parse_work_commands() -> None:
    text, actions = parse_work_commands("Sure {{workAfternoon||yes}}")
    check("yes action", actions == [("afternoon", "yes")])
    check("text cleaned", "workAfternoon" not in text)
    text, actions = parse_work_commands("{{workPause||until=14:00}} ok")
    check("pause", actions[0][0] == "pause")
    check("pause until", actions[0][1] == "14:00")
    text, actions = parse_work_commands("{{workResume}}")
    check("resume", actions == [("resume", "")])
    text, actions = parse_work_commands("{{workAfternoon||no}}")
    check("no action", actions == [("afternoon", "no")])


# ---------------------------------------------------------------------------
# Task 5: runtime
# ---------------------------------------------------------------------------

def test_runtime_need_identity_and_priority() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg()
        rcfg = RuntimeConfig(
            face_cache_max_age_s=120,
            image_cache_max_age_s=45,
            speech_min_gap_s=90,
            speech_suppress_after_voice_s=120,
            behaviors_enabled=("workday",),
        )
        rt = BehaviorRuntime(
            rcfg, wcfg, store,
            quiet_fn=lambda: False,
            voice_ts_fn=lambda: 0.0,
        )
        # Occupied morning without face → need_identity
        # Use a real epoch that maps to 09:15 UTC on a known day
        # 2026-07-18 09:15 UTC
        local = datetime(2026, 7, 18, 9, 15, tzinfo=ZoneInfo("UTC"))
        now = local.timestamp()
        rt.ingest_tick_payload(now=now, occupied=True, face=None)
        result = rt.tick(now)
        check("runtime need_identity", result.need_identity is True)
        check("runtime no speak yet", result.speak == "")

        # With face → arm, still no speak
        rt.ingest_tick_payload(
            now=now + 1,
            occupied=True,
            face={"face_id": 1, "name": "Cam", "is_stranger": False},
        )
        result = rt.tick(now + 1)
        rec = store.load_workday("2026-07-18")
        check("runtime armed", rec.mode == WorkdayMode.WORKING)
        check("runtime no need_id after arm", result.need_identity is False)

        # Quiet blocks poke
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=now,
            last_poke_at=now,
            arm_source="morning",
        ))
        later = now + 5400
        rt.quiet_fn = lambda: True
        rt.ingest_tick_payload(now=later, occupied=True)
        result = rt.tick(later)
        check("quiet blocks runtime speak", result.speak == "")
        rec = store.load_workday("2026-07-18")
        check("quiet does not advance last_poke", rec.last_poke_at == now)


def test_runtime_disabled_workday_not_registered() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg(enabled=False)
        rcfg = load_runtime_config({"BEHAVIORS_ENABLED": "workday"})
        rt = BehaviorRuntime(rcfg, wcfg, store)
        check("no behaviors when disabled", len(rt.behaviors) == 0)


# ---------------------------------------------------------------------------
# Task 9: simulated full day
# ---------------------------------------------------------------------------

def test_simulated_workday() -> None:
    """
    09:15 identify Cam -> working
    10:46 poke
    unoccupied 30m -> away line
    occupied again -> clear
    18:01 -> no more pokes
    """
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg(poke_interval_s=5400, away_s=1800)
        rcfg = RuntimeConfig(
            face_cache_max_age_s=120,
            speech_min_gap_s=1,  # allow rapid simulated speech
            speech_suppress_after_voice_s=0,
            behaviors_enabled=("workday",),
        )
        rt = BehaviorRuntime(
            rcfg, wcfg, store,
            quiet_fn=lambda: False,
            voice_ts_fn=lambda: 0.0,
        )
        face = {"face_id": 1, "name": "Cam", "is_stranger": False}

        t0 = datetime(2026, 7, 18, 9, 15, tzinfo=ZoneInfo("UTC")).timestamp()
        rt.ingest_tick_payload(now=t0, occupied=True, face=face)
        r = rt.tick(t0)
        rec = store.load_workday("2026-07-18")
        check("sim: working at 09:15", rec.mode == WorkdayMode.WORKING)

        # Just before poke interval: no poke
        t_early = t0 + 5399
        rt.ingest_tick_payload(now=t_early, occupied=True)
        r = rt.tick(t_early)
        check("sim: no poke early", r.speak == "")

        # 90m later: poke
        t_poke = t0 + 5400
        rt.ingest_tick_payload(now=t_poke, occupied=True)
        r = rt.tick(t_poke)
        check("sim: poke at 90m", len(r.speak) > 0)

        # Away 30m — chipper empty is weak; clear sticky via ambient empty streak.
        t_away_start = t_poke + 100
        rt.presence.note_empty_evidence(t_away_start - 2, source="ambient")
        rt.presence.note_empty_evidence(t_away_start - 1, source="ambient")
        rt.ingest_tick_payload(now=t_away_start, occupied=False)
        rt.tick(t_away_start)
        t_away = t_away_start + 1800
        rt.ingest_tick_payload(now=t_away, occupied=False)
        r = rt.tick(t_away)
        check("sim: away line", "working" in r.speak.lower() or "gone" in r.speak.lower())

        # Return
        t_back = t_away + 60
        rt.ingest_tick_payload(now=t_back, occupied=True)
        r = rt.tick(t_back)
        rec = store.load_workday("2026-07-18")
        check("sim: absence cleared", rec.absence_started_at == 0.0)
        check("sim: absence counted", rec.absence_count >= 1)

        # After work end
        t_eod = datetime(2026, 7, 18, 18, 1, tzinfo=ZoneInfo("UTC")).timestamp()
        # Force last_poke old so poke would fire if not EOD
        rec = store.load_workday("2026-07-18")
        rec.last_poke_at = t_eod - 10_000
        rec.mode = WorkdayMode.WORKING
        store.save_workday(rec)
        rt.ingest_tick_payload(now=t_eod, occupied=True)
        r = rt.tick(t_eod)
        rec = store.load_workday("2026-07-18")
        check("sim: EOD off", rec.mode == WorkdayMode.OFF)
        check("sim: no poke after end", r.speak == "")

        # Day strip for chat
        strip = store.day_strip("2026-07-18")
        check("sim: day strip present", len(strip) > 0)


def test_clock_tick_no_show() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg()
        rcfg = RuntimeConfig(behaviors_enabled=("workday",))
        rt = BehaviorRuntime(rcfg, wcfg, store)
        t = datetime(2026, 7, 18, 10, 31, tzinfo=ZoneInfo("UTC")).timestamp()
        rt.clock_tick(t)
        rec = store.load_workday("2026-07-18")
        check("clock no_show", rec.mode == WorkdayMode.NO_SHOW)


# ---------------------------------------------------------------------------
# Review fixes: late re-ask, mode guards, face_id<=0, config, pause, voice
# ---------------------------------------------------------------------------

def test_late_no_and_timeout_no_reask() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(late_check_timeout_s=100), store)
        face = FaceIdentity(1, "Cam", False)
        now = 1_800_000_000.0
        store.save_workday(WorkdayRecord(date="2026-07-18", mode=WorkdayMode.NO_SHOW))
        r = b.plan(_ctx(b, True, face, 13, 0, now))
        _apply_speak(r)
        check("entered late_check", store.load_workday("2026-07-18").mode == WorkdayMode.LATE_CHECK)

        # User says no
        b.on_afternoon_no("2026-07-18")
        rec = store.load_workday("2026-07-18")
        check("after no -> no_show", rec.mode == WorkdayMode.NO_SHOW)
        check("late_check_done", rec.late_check_done is True)

        # Occupied again with face — must not re-ask
        r2 = b.plan(_ctx(b, True, face, 14, 0, now + 3600))
        check("no re-ask after no", r2.speak == "")
        check("still no_show after no", store.load_workday("2026-07-18").mode == WorkdayMode.NO_SHOW)

        # Timeout path
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.LATE_CHECK,
            late_check_asked_at=now,
            late_check_done=False,
            primary_face_id=1,
        ))
        r3 = b.plan(_ctx(b, True, face, 14, 0, now + 200))
        rec = store.load_workday("2026-07-18")
        check("timeout -> no_show", rec.mode == WorkdayMode.NO_SHOW)
        check("timeout sets late_check_done", rec.late_check_done is True)
        r4 = b.plan(_ctx(b, True, face, 15, 0, now + 4000))
        check("no re-ask after timeout", r4.speak == "")


def test_on_afternoon_no_mode_guard() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(), store)
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=1000.0,
            arm_source="morning",
        ))
        b.on_afternoon_no("2026-07-18")
        rec = store.load_workday("2026-07-18")
        check("no does not tear down working", rec.mode == WorkdayMode.WORKING)


def test_pause_resume_and_expiry() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        b = WorkDayBehavior(_wd_cfg(poke_interval_s=60), store)
        start = 1_800_000_000.0
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=start,
            last_poke_at=start,
            arm_source="morning",
        ))
        b.on_pause("2026-07-18", until_ts=start + 1000)
        r = b.plan(_ctx(b, True, None, 12, 0, start + 100, identity_fresh=False))
        check("paused blocks", r.speak == "")
        b.on_resume("2026-07-18")
        rec = store.load_workday("2026-07-18")
        check("resume -> working", rec.mode == WorkdayMode.WORKING)
        # Expiry path
        b.on_pause("2026-07-18", until_ts=start + 500)
        r2 = b.plan(_ctx(b, True, None, 12, 0, start + 600, identity_fresh=False))
        rec = store.load_workday("2026-07-18")
        check("pause expiry -> working", rec.mode == WorkdayMode.WORKING)


def test_arbiter_voice_suppress() -> None:
    arb = SpeechArbiter(min_gap_s=90, suppress_after_voice_s=120)
    req = SpeechRequest(text="hi", priority=80, behavior_id="workday")
    ok, why = arb.allow(req, now=1000.0, quiet=False, voice_recent_ts=950.0)
    check("recent voice blocks", ok is False and why == "recent_voice")
    ok, why = arb.allow(req, now=1000.0, quiet=False, voice_recent_ts=800.0)
    check("old voice allows", ok is True)


def test_negative_face_id_stranger() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg()
        rcfg = RuntimeConfig(
            face_cache_max_age_s=120,
            speech_min_gap_s=1,
            speech_suppress_after_voice_s=0,
            behaviors_enabled=("workday",),
        )
        rt = BehaviorRuntime(rcfg, wcfg, store)
        local = datetime(2026, 7, 18, 9, 15, tzinfo=ZoneInfo("UTC"))
        now = local.timestamp()
        # Vector stranger face_id=-3
        rt.ingest_tick_payload(
            now=now,
            occupied=True,
            face={"face_id": -3, "name": "", "is_stranger": True},
        )
        snap = rt.presence.snapshot
        check("negative face cached", snap.face is not None and snap.face.face_id == -3)
        check("negative is stranger", snap.face.is_stranger is True)
        check("identity fresh for stranger", rt.presence.identity_fresh(now) is True)
        result = rt.tick(now)
        rec = store.load_workday("2026-07-18")
        check("stranger does not arm", rec.mode == WorkdayMode.WAITING_MORNING)
        check("stranger no need_id spam", result.need_identity is False)


def test_bad_config_does_not_crash() -> None:
    cfg = load_workday_config({
        "WORKDAY_ENABLED": "1",
        "WORKDAY_START_BEGIN": "not-a-time",
        "WORKDAY_END": "25:99",
        "WORKDAY_POKE_INTERVAL_S": "nope",
        "WORKDAY_TZ": "Not/AZone",
    })
    check("bad config still loads", cfg is not None)
    check("bad HH:MM falls back start", cfg.start_begin == (9, 0))
    check("bad HH:MM falls back end", cfg.end == (18, 0))
    check("bad int falls back poke", cfg.poke_interval_s == 5400)


def test_malformed_work_tags_stripped() -> None:
    text, actions = parse_work_commands("Hi {{workAfternoon|| maybe}} {{workPause||until=25:00}} x")
    check("malformed no actions", actions == [])
    check("malformed stripped", "{{" not in text and "workAfternoon" not in text)
    text, actions = parse_work_commands("{{workPause||until=9:30}}")
    check("valid short hour pause", actions == [("pause", "09:30")])


def test_arbiter_deny_does_not_commit_poke() -> None:
    """Runtime quiet deny must not advance last_poke_at (speech-gated commit)."""
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        wcfg = _wd_cfg(poke_interval_s=100)
        rcfg = RuntimeConfig(
            speech_min_gap_s=90,
            speech_suppress_after_voice_s=120,
            behaviors_enabled=("workday",),
        )
        start = datetime(2026, 7, 18, 11, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        store.save_workday(WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=start,
            last_poke_at=start,
            arm_source="morning",
        ))
        rt = BehaviorRuntime(
            rcfg, wcfg, store,
            quiet_fn=lambda: True,
            voice_ts_fn=lambda: 0.0,
        )
        later = start + 200
        rt.ingest_tick_payload(now=later, occupied=True)
        r = rt.tick(later)
        check("denied speak empty", r.speak == "")
        rec = store.load_workday("2026-07-18")
        check("denied poke not committed", rec.last_poke_at == start)


def test_identity_reject_cooldown_after_stale_cache() -> None:
    """Stranger reject then face cache expires: still no need_identity in cooldown."""
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        cooldown = 600
        face_max = 120
        b = WorkDayBehavior(
            _wd_cfg(identity_reject_cooldown_s=cooldown),
            store,
        )
        now = 1_800_000_000.0
        stranger = FaceIdentity(face_id=-3, name="", is_stranger=True)
        # Morning window, occupied + fresh stranger identity → reject + cooldown
        r1 = b.plan(_ctx(b, True, stranger, 9, 15, now, identity_fresh=True))
        check("stranger reject no arm", r1.need_identity is False)
        rec = store.load_workday("2026-07-18")
        check("cooldown set", rec.identity_reject_until >= now + cooldown - 1)

        # Past FACE_CACHE_MAX_AGE: identity no longer fresh, but still in cooldown
        later = now + face_max + 10
        r2 = b.plan(
            _ctx(b, True, None, 9, 20, later, identity_fresh=False)
        )
        check("stale + cooldown: no need_identity", r2.need_identity is False)
        check(
            "cooldown reason",
            r2.debug.get("reason") == "identity_reject_cooldown",
        )

        # After cooldown expires → need_identity again
        after = now + cooldown + 1
        r3 = b.plan(
            _ctx(b, True, None, 9, 30, after, identity_fresh=False)
        )
        check("after cooldown need_identity", r3.need_identity is True)


def test_single_pipe_work_tags() -> None:
    text, actions = parse_work_commands("Ok {{workAfternoon|yes}}")
    check("single-pipe afternoon yes", actions == [("afternoon", "yes")])
    check("single-pipe stripped", "{{" not in text)
    text, actions = parse_work_commands("{{workPause|until=14:00}}")
    check("single-pipe pause", actions == [("pause", "14:00")])


if __name__ == "__main__":
    import pytest
    import sys

    raise SystemExit(
        pytest.main([__file__, "-q", *sys.argv[1:]])
    )

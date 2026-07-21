#!/usr/bin/env python3
"""Tests for ambient PRESENCE protocol, sticky desk occupancy, long face cache.

Run:
  python3 -m pytest shared/vector-ai/test_ambient_presence.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from behaviors.config import RuntimeConfig, load_runtime_config
from behaviors.continuity import ContinuityStore
from behaviors.presence import PresenceCache
from behaviors.runtime import BehaviorRuntime
from behaviors.types import FaceIdentity
from behaviors.config import WorkdayConfig
from routes.ambient import parse_ambient_llm_raw


# ---------------------------------------------------------------------------
# TASK-01: parser
# ---------------------------------------------------------------------------

def test_parse_person_novelty():
    raw = (
        "PRESENCE: person:Cam\n"
        "a grey hoodie appeared at the desk\n"
        "Oh. A grey hoodie has materialised. Charming."
    )
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "person"
    assert hint == "Cam"
    assert "hoodie" in spoken.lower() or "Charming" in spoken


def test_parse_person_nothing():
    raw = "PRESENCE: person\nNOTHING"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "person"
    assert hint is None
    assert spoken == ""


def test_parse_empty_nothing():
    raw = "PRESENCE: empty\nNOTHING"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "empty"
    assert hint is None
    assert spoken == ""


def test_parse_partial_body_fixture_still_person():
    """Prompt fixtures: partial body wording still parses as person."""
    raw = (
        "PRESENCE: person\n"
        "grey hoodie torso at the desk, face cut off\n"
        "A torso in a hoodie. Head optional, apparently."
    )
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "person"
    assert spoken


def test_parse_missing_presence_unknown():
    raw = "NOTHING"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "unknown"
    assert spoken == ""


def test_parse_missing_presence_free_text_unknown():
    raw = "someone is here maybe"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    # No PRESENCE line → unknown (do not force empty).
    assert kind == "unknown"


def test_parse_person_name_with_spaces():
    raw = "PRESENCE: person: Cam Smith\nNOTHING"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "person"
    assert hint == "Cam Smith"
    assert spoken == ""


def test_parse_case_insensitive_presence():
    raw = "presence: PERSON:alice\nnothing"
    kind, hint, spoken = parse_ambient_llm_raw(raw)
    assert kind == "person"
    assert hint and hint.lower() == "alice"
    assert spoken == ""


# ---------------------------------------------------------------------------
# TASK-02: sticky PresenceCache
# ---------------------------------------------------------------------------

def test_sticky_person_holds_through_nothing_glances():
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2, face_max_age_s=1800)
    t0 = 1_000_000.0
    cache.note_person_evidence(t0, source="ambient")
    assert cache.occupied_effective(t0) is True
    # Later glances with no empty evidence — still occupied within sticky.
    assert cache.occupied_effective(t0 + 600) is True
    assert cache.occupied_effective(t0 + 1799) is True
    # Sticky TTL expired without empty streak.
    assert cache.occupied_effective(t0 + 1801) is False


def test_sticky_one_empty_does_not_clear():
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    t0 = 1_000.0
    cache.note_person_evidence(t0, source="ambient")
    cache.note_empty_evidence(t0 + 10, source="ambient")
    assert cache.empty_streak == 1
    assert cache.occupied_effective(t0 + 10) is True
    assert cache.snapshot.occupied is True


def test_sticky_two_empties_clear():
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    t0 = 1_000.0
    cache.note_person_evidence(t0, source="ambient")
    cache.note_empty_evidence(t0 + 10, source="ambient")
    cache.note_empty_evidence(t0 + 20, source="ambient")
    assert cache.empty_streak == 2
    assert cache.occupied_effective(t0 + 20) is False
    assert cache.snapshot.occupied is False


def test_sleep_gap_clears():
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    t0 = 1_000.0
    cache.note_person_evidence(t0, source="ambient", name_hint="Cam")
    cache.apply_sleep_clear(t0 + 5 * 3600, 4 * 3600)
    assert cache.occupied_effective(t0 + 5 * 3600) is False
    assert cache.empty_streak == 0
    assert cache.last_person_at == 0.0


def test_face_seen_sets_person_and_identity():
    cache = PresenceCache(face_max_age_s=1800, sticky_s=1800)
    t0 = 2_000.0
    face = FaceIdentity(face_id=1, name="Cam", is_stranger=False)
    cache.note_person_evidence(t0, source="face_seen", face=face)
    assert cache.occupied_effective(t0) is True
    assert cache.identity_fresh(t0) is True
    assert cache.identity_fresh(t0 + 1799) is True
    assert cache.identity_fresh(t0 + 1801) is False
    assert cache.effective_face(t0).name == "Cam"


def test_tick_empty_does_not_clear_sticky():
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    t0 = 3_000.0
    cache.note_person_evidence(t0, source="ambient")
    # Weak empty via legacy update (chipper tick path).
    cache.update(now=t0 + 30, occupied=False)
    assert cache.occupied_effective(t0 + 30) is True


def test_soft_name_does_not_replace_fresh_enrolled():
    cache = PresenceCache(face_max_age_s=1800)
    t0 = 4_000.0
    cache.note_person_evidence(
        t0,
        source="face_seen",
        face=FaceIdentity(1, "Cam", is_stranger=False),
    )
    cache.note_person_evidence(
        t0 + 5,
        source="ambient",
        name_hint="StrangerBob",
        face=FaceIdentity(0, "StrangerBob", is_stranger=True),
    )
    assert cache.snapshot.face is not None
    assert cache.snapshot.face.name == "Cam"
    assert cache.snapshot.face.is_stranger is False


def test_ambient_soft_enrolled_does_not_replace_firmware_enrolled():
    """Soft-matched enrolled ambient must not replace still-fresh firmware face."""
    cache = PresenceCache(face_max_age_s=1800)
    t0 = 4_500.0
    cache.note_person_evidence(
        t0,
        source="face_seen",
        face=FaceIdentity(1, "Cam", is_stranger=False),
    )
    cache.note_person_evidence(
        t0 + 5,
        source="ambient",
        name_hint="Other",
        face=FaceIdentity(2, "Other", is_stranger=False),  # soft-matched enrolled
    )
    assert cache.snapshot.face is not None
    assert cache.snapshot.face.face_id == 1
    assert cache.snapshot.face.name == "Cam"


def test_sleep_clear_wipes_soft_name_and_face():
    cache = PresenceCache()
    t0 = 5_000.0
    cache.note_person_evidence(
        t0,
        source="ambient",
        name_hint="Cam",
        face=FaceIdentity(0, "Cam", is_stranger=True),
    )
    cache.apply_sleep_clear(t0 + 10_000)
    assert cache.soft_name == ""
    assert cache.snapshot.face is None
    assert cache.occupied_effective(t0 + 10_000) is False


def test_empty_streak_resets_after_person():
    cache = PresenceCache(empty_streak_clear=2, sticky_s=1800)
    t0 = 6_000.0
    cache.note_person_evidence(t0, source="ambient")
    cache.note_empty_evidence(t0 + 1, source="ambient")
    assert cache.empty_streak == 1
    cache.note_person_evidence(t0 + 2, source="ambient")
    assert cache.empty_streak == 0
    assert cache.occupied_effective(t0 + 2) is True


def test_presence_identity_cached_explicit_max_age():
    """Explicit ctor ages still work (not forced to product default)."""
    cache = PresenceCache(face_max_age_s=120, image_max_age_s=45)
    face = FaceIdentity(face_id=1, name="Cam", is_stranger=False)
    cache.update(now=1000.0, occupied=True, face=face)
    assert cache.identity_fresh(1000.0) is True
    assert cache.identity_fresh(1119.0) is True
    assert cache.identity_fresh(1121.0) is False


def test_load_runtime_config_sticky_defaults():
    cfg = load_runtime_config({})
    assert cfg.face_cache_max_age_s == 1800
    assert cfg.presence_sticky_s == 1800
    assert cfg.presence_empty_streak == 2


def test_load_runtime_config_sticky_override():
    cfg = load_runtime_config({
        "FACE_CACHE_MAX_AGE_S": "90",
        "PRESENCE_STICKY_S": "60",
        "PRESENCE_EMPTY_STREAK": "3",
    })
    assert cfg.face_cache_max_age_s == 90
    assert cfg.presence_sticky_s == 60
    assert cfg.presence_empty_streak == 3


# ---------------------------------------------------------------------------
# TASK-03: ambient endpoint writes sticky + soft match
# ---------------------------------------------------------------------------

def test_ambient_person_soft_match_enrolled(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    store.remember("likes tea", face_id=1, face_name="Cam")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)

    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rcfg = RuntimeConfig(
            face_cache_max_age_s=1800,
            presence_sticky_s=1800,
            presence_empty_streak=2,
            behaviors_enabled=(),
        )
        wcfg = WorkdayConfig(enabled=False, tz=ZoneInfo("UTC"))
        rt = BehaviorRuntime(rcfg, wcfg, cont)
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)

        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()

        async def _llm(*a, **k):
            return "PRESENCE: person:Cam\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _llm)

        result = asyncio.run(ambient(AmbientRequest(image="fakejpg")))
        assert result["text"] == ""
        assert result["presence"] == "person"
        assert result["occupied"] is True
        assert result.get("name_hint") == "Cam"
        assert rt.presence.occupied_effective(time.time()) is True
        assert rt.presence.identity_fresh(time.time()) is True
        face = rt.presence.snapshot.face
        assert face is not None
        assert face.is_stranger is False
        assert face.face_id == 1
        assert face.name == "Cam"


def test_ambient_empty_twice_clears(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)

    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rcfg = RuntimeConfig(
            presence_sticky_s=1800,
            presence_empty_streak=2,
            behaviors_enabled=(),
        )
        wcfg = WorkdayConfig(enabled=False, tz=ZoneInfo("UTC"))
        rt = BehaviorRuntime(rcfg, wcfg, cont)
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)

        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()
        now = time.time()
        rt.presence.note_person_evidence(now, source="ambient")

        async def _empty(*a, **k):
            return "PRESENCE: empty\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _empty)

        r1 = asyncio.run(ambient(AmbientRequest(image="x")))
        assert r1["presence"] == "empty"
        assert rt.presence.occupied_effective(time.time()) is True

        r2 = asyncio.run(ambient(AmbientRequest(image="x")))
        assert r2["presence"] == "empty"
        assert rt.presence.occupied_effective(time.time()) is False


def test_ambient_novelty_still_remembers(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)

    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()

        async def _novel(*a, **k):
            return (
                "PRESENCE: empty\n"
                "a rubber duck on the monitor\n"
                "A rubber duck. Of course."
            )

        monkeypatch.setattr("routes.ambient.llm_chat_once", _novel)
        result = asyncio.run(ambient(AmbientRequest(image="x")))
        assert "duck" in result["text"].lower() or "Of course" in result["text"]
        assert result["presence"] == "empty"
        obs = store.list_observations(limit=5)
        assert any("duck" in (o.get("text") or "").lower() for o in obs)


def test_ambient_llm_error_no_presence_update(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()
        rt.presence.note_person_evidence(time.time(), source="ambient")

        async def _boom(*a, **k):
            raise RuntimeError("llm down")

        monkeypatch.setattr("routes.ambient.llm_chat_once", _boom)
        result = asyncio.run(ambient(AmbientRequest(image="x")))
        assert result.get("text") == ""
        assert "error" in result
        assert "presence" not in result
        assert rt.presence.occupied_effective(time.time()) is True


# ---------------------------------------------------------------------------
# TASK-04: long FACE_RECENT_WINDOW
# ---------------------------------------------------------------------------

def test_face_recent_window_loader_default_empty_env():
    from process_state import _load_face_recent_window

    assert _load_face_recent_window({}) == 1800


def test_face_recent_window_loader_override_and_clamp():
    from process_state import _load_face_recent_window

    assert _load_face_recent_window({"FACE_RECENT_WINDOW_S": "90"}) == 90
    assert _load_face_recent_window({"FACE_RECENT_WINDOW": "45"}) == 45
    assert _load_face_recent_window({"FACE_RECENT_WINDOW_S": "0"}) == 1800
    assert _load_face_recent_window({"FACE_RECENT_WINDOW_S": "-5"}) == 1800
    assert _load_face_recent_window({"FACE_RECENT_WINDOW_S": "nope"}) == 1800


def test_current_face_long_window(monkeypatch):
    import process_state

    monkeypatch.setattr(process_state, "FACE_RECENT_WINDOW", 1800)
    process_state._face_state["enrolled_id"] = 1
    process_state._face_state["enrolled_name"] = "Cam"
    now = time.time()
    process_state._face_state["enrolled_seen"] = now - 1799
    process_state._face_state["stranger_seen"] = 0.0
    face = process_state.current_face()
    assert face is not None
    assert face["name"] == "Cam"
    assert face["is_stranger"] is False

    process_state._face_state["enrolled_seen"] = now - 1801
    assert process_state.current_face() is None


def test_current_face_enrolled_wins_over_stranger(monkeypatch):
    import process_state

    monkeypatch.setattr(process_state, "FACE_RECENT_WINDOW", 1800)
    now = time.time()
    process_state._face_state["enrolled_id"] = 1
    process_state._face_state["enrolled_name"] = "Cam"
    process_state._face_state["enrolled_seen"] = now - 10
    process_state._face_state["stranger_seen"] = now  # fresher stranger blip
    face = process_state.current_face()
    assert face is not None
    assert face["is_stranger"] is False
    assert face["name"] == "Cam"


def test_current_face_short_window_injectable(monkeypatch):
    import process_state

    monkeypatch.setattr(process_state, "FACE_RECENT_WINDOW", 2)
    now = time.time()
    process_state._face_state["enrolled_id"] = 2
    process_state._face_state["enrolled_name"] = "G"
    process_state._face_state["enrolled_seen"] = now - 1
    process_state._face_state["stranger_seen"] = 0.0
    assert process_state.current_face() is not None
    process_state._face_state["enrolled_seen"] = now - 3
    assert process_state.current_face() is None


# ---------------------------------------------------------------------------
# TASK-05: tick does not stomp sticky / identity-only reuse
# ---------------------------------------------------------------------------

def test_tick_occupied_false_keeps_warm_sticky():
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        rcfg = RuntimeConfig(
            presence_sticky_s=1800,
            presence_empty_streak=2,
            behaviors_enabled=(),
        )
        wcfg = WorkdayConfig(enabled=False, tz=ZoneInfo("UTC"))
        rt = BehaviorRuntime(rcfg, wcfg, store)
        t0 = 5_000.0
        rt.presence.note_person_evidence(t0, source="ambient")
        snap = rt.ingest_tick_payload(now=t0 + 60, occupied=False)
        assert snap.occupied is True
        assert rt.presence.occupied_effective(t0 + 60) is True
        r = rt.tick(t0 + 60)
        assert r.debug.get("occupied") is True


def test_tick_after_empty_streak_with_warm_current_face_stays_empty(monkeypatch):
    """Regression: ambient empty×2 then tick+current_face must NOT re-occupy."""
    import process_state

    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        rcfg = RuntimeConfig(
            presence_sticky_s=1800,
            presence_empty_streak=2,
            behaviors_enabled=(),
        )
        rt = BehaviorRuntime(
            rcfg, WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")), store
        )
        t0 = 7_000.0
        rt.presence.note_person_evidence(t0, source="ambient")
        rt.presence.note_empty_evidence(t0 + 10, source="ambient")
        rt.presence.note_empty_evidence(t0 + 20, source="ambient")
        assert rt.presence.occupied_effective(t0 + 20) is False
        last_person = rt.presence.last_person_at
        assert last_person == 0.0

        # Chat face still "live" for 30m window (wall-clock for current_face).
        monkeypatch.setattr(process_state, "FACE_RECENT_WINDOW", 1800)
        process_state._face_state["enrolled_id"] = 1
        process_state._face_state["enrolled_name"] = "Cam"
        process_state._face_state["enrolled_seen"] = time.time()
        process_state._face_state["stranger_seen"] = 0.0

        # Simulate tick path: occupied=false + identity from current_face.
        live = process_state.current_face()
        assert live is not None
        face_dict = {
            "face_id": int(live["face_id"] or 0),
            "name": str(live["name"] or ""),
            "is_stranger": bool(live["is_stranger"]),
        }
        now_tick = t0 + 30
        snap = rt.ingest_tick_payload(
            now=now_tick, occupied=False, face=face_dict
        )
        assert snap.occupied is False
        assert rt.presence.occupied_effective(now_tick) is False
        assert rt.presence.last_person_at == 0.0  # not refreshed


def test_identity_reuse_does_not_refresh_last_person_at():
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(presence_sticky_s=1800, behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            store,
        )
        t0 = 8_000.0
        rt.presence.note_person_evidence(t0, source="ambient")
        # One empty — still sticky warm
        rt.presence.note_empty_evidence(t0 + 5, source="ambient")
        assert rt.presence.occupied_effective(t0 + 5) is True
        lp = rt.presence.last_person_at
        rt.ingest_tick_payload(
            now=t0 + 100,
            occupied=False,
            face={"face_id": 1, "name": "Cam", "is_stranger": False},
        )
        assert rt.presence.last_person_at == lp  # not refreshed
        assert rt.presence.occupied_effective(t0 + 100) is True


def test_tick_ttl_resync_expires_sticky():
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(presence_sticky_s=60, behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            store,
        )
        t0 = 9_000.0
        rt.presence.note_person_evidence(t0, source="ambient")
        r = rt.tick(t0 + 61)
        assert r.debug.get("occupied") is False


def test_presence_debug_dict_fields():
    cache = PresenceCache(sticky_s=100, empty_streak_clear=2)
    t0 = 10.0
    cache.note_person_evidence(t0, source="ambient", name_hint="Cam")
    d = cache.debug_dict(t0)
    assert d["occupied"] is True
    assert d["last_person_at"] == t0
    assert d["empty_streak"] == 0
    assert d["presence_source"] == "ambient"
    assert d["sticky_s"] == 100


def test_runtime_zero_sticky_config_allowed():
    """presence_sticky_s=0 must not be masked by `or 1800`."""
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(presence_sticky_s=0, presence_empty_streak=0, behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            store,
        )
        assert rt.presence.sticky_s == 0
        assert rt.presence.empty_streak_clear == 0


# ---------------------------------------------------------------------------
# More ambient integration coverage
# ---------------------------------------------------------------------------

def test_ambient_unknown_presence_no_sticky_write(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()
        before = rt.presence.last_person_at

        async def _bare(*a, **k):
            return "NOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _bare)
        result = asyncio.run(ambient(AmbientRequest(image="x")))
        assert result["presence"] == "unknown"
        assert result["text"] == ""
        assert rt.presence.last_person_at == before


def test_ambient_first_empty_occupied_effective_true(tmp_path, monkeypatch):
    """JSON occupied should reflect sticky effective (still true after empty×1)."""
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(presence_empty_streak=2, behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()
        rt.presence.note_person_evidence(time.time(), source="ambient")

        async def _empty(*a, **k):
            return "PRESENCE: empty\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _empty)
        result = asyncio.run(ambient(AmbientRequest(image="x")))
        assert result["presence"] == "empty"
        assert result["occupied"] is True  # sticky still warm


def test_ambient_unmatched_soft_stranger(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()

        async def _llm(*a, **k):
            return "PRESENCE: person:NobodyKnown\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _llm)
        result = asyncio.run(ambient(AmbientRequest(image="x")))
        assert result["presence"] == "person"
        face = rt.presence.snapshot.face
        assert face is not None
        assert face.is_stranger is True
        assert face.face_id == 0
        assert face.name == "NobodyKnown"


def test_ambient_soft_match_case_insensitive(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    store.remember("tea", face_id=1, face_name="Cam")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()

        async def _llm(*a, **k):
            return "PRESENCE: person:cam\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _llm)
        asyncio.run(ambient(AmbientRequest(image="x")))
        face = rt.presence.snapshot.face
        assert face is not None
        assert face.is_stranger is False
        assert face.face_id == 1


def test_presence_line_not_in_observations(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        process_state._ambient_state["last_ambient_call"] = time.time()

        async def _novel(*a, **k):
            return (
                "PRESENCE: person\n"
                "a rubber duck on the monitor\n"
                "A duck. Lovely."
            )

        monkeypatch.setattr("routes.ambient.llm_chat_once", _novel)
        asyncio.run(ambient(AmbientRequest(image="x")))
        obs = store.list_observations(limit=10)
        assert obs
        for o in obs:
            assert "PRESENCE" not in (o.get("text") or "")


def test_ambient_sleep_gap_clears_sticky(tmp_path, monkeypatch):
    import deps
    import process_state
    from memory import MemoryStore
    from process_state import AMBIENT_SLEEP_GAP
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        process_state._set_quiet(False)
        rt.presence.note_person_evidence(time.time(), source="ambient")
        process_state._ambient_state["last_ambient_call"] = (
            time.time() - AMBIENT_SLEEP_GAP - 10
        )

        async def _person(*a, **k):
            return "PRESENCE: empty\nNOTHING"

        monkeypatch.setattr("routes.ambient.llm_chat_once", _person)
        # Sleep clear runs before glance; then empty may re-set streak from cleared state.
        asyncio.run(ambient(AmbientRequest(image="x")))
        # After sleep clear + one empty: not sticky from pre-sleep person.
        assert rt.presence.last_source in ("ambient", "sleep")
        # Occupied only if empty re-stuck somehow; after sleep last_person is 0,
        # one empty with last_person 0 clears immediately (sticky_expired).
        assert rt.presence.occupied_effective(time.time()) is False


def test_behaviors_state_sticky_fields(tmp_path, monkeypatch):
    import deps
    from routes.behaviors_http import behaviors_state

    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(presence_sticky_s=1234, behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        monkeypatch.setattr(
            deps, "_workday_cfg", WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            raising=False,
        )
        monkeypatch.setattr(deps, "_continuity", cont, raising=False)
        t0 = time.time()
        rt.presence.note_person_evidence(t0, source="ambient", name_hint="Cam")
        st = asyncio.run(behaviors_state())
        assert st["occupied"] is True
        assert st["last_person_at"] == t0
        assert st["empty_streak"] == 0
        assert st["presence_source"] == "ambient"
        assert st["presence_sticky_s"] == 1234
        assert st.get("soft_name") == "Cam"


def test_face_seen_http_notes_person(tmp_path, monkeypatch):
    import deps
    from routes.face import state_face_seen
    from routes.models import FaceSeenRequest

    with tempfile.TemporaryDirectory() as td:
        cont = ContinuityStore(Path(td) / "w.db")
        rt = BehaviorRuntime(
            RuntimeConfig(behaviors_enabled=()),
            WorkdayConfig(enabled=False, tz=ZoneInfo("UTC")),
            cont,
        )
        monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)
        asyncio.run(state_face_seen(FaceSeenRequest(face_id=1, name="Cam")))
        assert rt.presence.occupied_effective(time.time()) is True
        assert rt.presence.snapshot.face is not None
        assert rt.presence.snapshot.face.name == "Cam"
        assert rt.presence.last_source == "face_seen"

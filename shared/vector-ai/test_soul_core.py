#!/usr/bin/env python3
"""Phase 1 soul core: presence sticky via Soul, face buffer enrolled-wins, last_seen.

Run:
  python3 -m pytest shared/vector-ai/test_soul_core.py -q
"""
from __future__ import annotations

from behaviors.presence import PresenceCache
from behaviors.runtime import BehaviorRuntime
from behaviors.config import RuntimeConfig, WorkdayConfig
from behaviors.continuity import ContinuityStore
from soul import FaceService, Soul
from soul.types import FaceSighting


def test_sticky_occupancy_via_soul_note_person_empty():
    """Frozen clock: person evidence sticks; empty×2 clears after streak."""
    now = 1_000_000.0
    presence = PresenceCache(sticky_s=1800, empty_streak_clear=2, face_max_age_s=1800)
    soul = Soul(presence=presence)

    assert soul.occupied(now) is False

    soul.note_person(now, source="test")
    assert soul.occupied(now) is True
    assert soul.last_seen_at == now
    # Still occupied deep in sticky window without new evidence.
    assert soul.occupied(now + 1700) is True

    # One empty is not enough (streak clear = 2).
    soul.note_empty(now + 10, source="ambient")
    assert soul.occupied(now + 10) is True

    soul.note_empty(now + 20, source="ambient")
    assert soul.occupied(now + 20) is False


def test_face_buffer_enrolled_wins_over_stranger_blip():
    """Enrolled match survives a fresher stranger blip (process_state parity)."""
    now = 2_000_000.0
    soul = Soul(
        presence=PresenceCache(sticky_s=1800, empty_streak_clear=2),
        face=FaceService(recent_window_s=1800),
    )

    soul.note_face(
        now,
        face_id=1,
        name="Cam",
        is_stranger=False,
        source="face_seen",
    )
    cur = soul.current_face(now)
    assert cur is not None
    assert cur["name"] == "Cam"
    assert cur["is_stranger"] is False
    assert cur["face_id"] == 1

    # Stranger blip a second later must not wipe enrolled.
    soul.note_face(
        now + 1,
        face_id=99,
        name="",
        is_stranger=True,
        source="face_seen",
    )
    cur2 = soul.current_face(now + 1)
    assert cur2 is not None
    assert cur2["is_stranger"] is False
    assert cur2["name"] == "Cam"
    assert cur2["face_id"] == 1

    buf = soul.face.buffer_list()
    assert len(buf) == 2
    assert buf[0]["name"] == "Cam"
    assert buf[1]["is_stranger"] is True


def test_last_seen_at_updates_on_note_face():
    now = 3_000_000.0
    soul = Soul(presence=PresenceCache())
    assert soul.last_seen_at == 0.0

    soul.note_face(
        now + 5,
        face_id=2,
        name="G",
        is_stranger=False,
        source="greeting",
    )
    assert soul.last_seen_at == now + 5
    assert soul.face.last_seen_at == now + 5

    soul.note_face(
        now + 50,
        face_id=0,
        name="",
        is_stranger=True,
        source="face_seen",
    )
    assert soul.last_seen_at == now + 50


def test_face_buffer_max_32():
    face = FaceService(recent_window_s=1800, max_buffer=32)
    base = 4_000_000.0
    for i in range(40):
        face.note(base + i, face_id=i, name="X", is_stranger=True, source="t")
    assert len(face.buffer_list()) == 32
    # Newest retained
    assert face.buffer_list()[-1]["face_id"] == 39
    assert face.buffer_list()[0]["face_id"] == 8  # 40 - 32


def test_soul_and_runtime_share_one_presence_cache(tmp_path):
    """Composition contract: optional presence on BehaviorRuntime is shared."""
    rcfg = RuntimeConfig(behaviors_enabled=())
    wcfg = WorkdayConfig(enabled=False)
    store = ContinuityStore(tmp_path / "w.db")
    cache = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    soul = Soul(presence=cache)
    rt = BehaviorRuntime(rcfg, wcfg, store, presence=cache)

    assert rt.presence is soul.presence is cache

    now = 5_000_000.0
    soul.note_person(now, source="face_seen")
    assert rt.presence.occupied_effective(now) is True
    assert soul.occupied(now) is True


def test_snapshot_shape():
    now = 6_000_000.0
    soul = Soul(presence=PresenceCache())
    soul.note_face(now, face_id=1, name="Cam", is_stranger=False, source="t")
    soul.note_person(now, source="t")
    snap = soul.snapshot(now)
    assert snap["occupied"] is True
    assert snap["last_seen_at"] == now
    assert snap["face"]["current"]["name"] == "Cam"
    assert len(snap["face"]["buffer"]) == 1
    assert "presence" in snap
    assert "mood" in snap


def test_apply_sleep_clear():
    now = 7_000_000.0
    soul = Soul(presence=PresenceCache(sticky_s=1800, empty_streak_clear=2))
    soul.note_person(now, source="t")
    soul.note_face(now, face_id=1, name="Cam", is_stranger=False, source="t")
    assert soul.occupied(now) is True
    assert soul.current_face(now) is not None

    soul.apply_sleep_clear(now + 1)
    assert soul.occupied(now + 1) is False
    assert soul.current_face(now + 1) is None


def test_face_sighting_dataclass():
    s = FaceSighting(t=1.0, face_id=1, name="Cam", is_stranger=False, source="x")
    d = s.to_dict()
    assert d["name"] == "Cam"
    assert d["face_id"] == 1

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


def test_eye_note_frame_and_image_fresh():
    now = 8_000_000.0
    soul = Soul(presence=PresenceCache(), image_max_age_s=45)
    assert soul.image_fresh(now) is False
    d = soul.note_eye_frame(now, reason="chat", frame_path="/tmp/x.jpg", note="chat frame")
    assert d.reason == "chat"
    assert d.frame_path == "/tmp/x.jpg"
    assert soul.last_vision() is not None
    assert soul.last_vision().note == "chat frame"
    assert soul.image_fresh(now) is True
    assert soul.image_fresh(now + 46) is False
    snap = soul.snapshot(now)
    assert snap["eye"]["last_vision"]["reason"] == "chat"
    assert snap["eye"]["image_fresh"] is True


def test_eye_see_ambient_updates_presence(monkeypatch):
    """Mocked VISION_MODEL path still writes sticky person presence."""
    import asyncio

    now = 9_000_000.0
    presence = PresenceCache(sticky_s=1800, empty_streak_clear=2)
    soul = Soul(presence=presence)

    async def _llm(*a, **k):
        return "PRESENCE: person:Cam\nNOTHING"

    monkeypatch.setattr("soul.eye.llm_chat_once", _llm)
    monkeypatch.setattr("soul.eye.save_submitted_image", lambda *a, **k: None)
    monkeypatch.setattr(
        "soul.eye.image_payload_too_large", lambda *a, **k: False
    )

    digest = asyncio.run(soul.see(now, image="fakejpg", reason="ambient"))
    assert digest.presence == "person"
    assert digest.name_hint == "Cam"
    assert digest.error == ""
    assert soul.occupied(now) is True
    assert soul.last_seen_at == now
    assert soul.last_vision() is digest


def test_eye_see_oversized_skips_llm(monkeypatch):
    import asyncio

    now = 10_000_000.0
    soul = Soul(presence=PresenceCache())
    calls = []

    async def _should_not(*a, **k):
        calls.append(1)
        return "PRESENCE: empty\nNOTHING"

    monkeypatch.setattr("soul.eye.llm_chat_once", _should_not)
    # Force too-large via helper (avoid multi-MB strings).
    monkeypatch.setattr(
        "soul.eye.image_payload_too_large", lambda *a, **k: True
    )

    digest = asyncio.run(soul.see(now, image="x", reason="ambient"))
    assert digest.error == "image_too_large"
    assert calls == []
    assert soul.occupied(now) is False

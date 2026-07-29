#!/usr/bin/env python3
"""Phase 2: greeting pot gates + face_seen → soul.

Run:
  python3 -m pytest shared/vector-ai/test_soul_greeting.py -q
"""
from __future__ import annotations

import asyncio
import time

import deps
import process_state
from behaviors.presence import PresenceCache
from memory import MemoryStore
from routes.face import state_face_seen
from routes.greeting import proactive_greeting
from routes.models import FaceSeenRequest, GreetingRequest
from soul import Soul


def _wire_soul(monkeypatch, tmp_path, *, sticky_s: int = 1800) -> Soul:
    store = MemoryStore(tmp_path / "mem.db")
    presence = PresenceCache(sticky_s=sticky_s, empty_streak_clear=2)
    soul = Soul(presence=presence, memory=store)
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)
    process_state._recent_greetings.clear()
    process_state._LAST_USER_VOICE_TS = 0.0
    process_state._mood_state["text"] = ""
    return soul


def test_greeting_suppresses_when_last_spoke_recent(tmp_path, monkeypatch):
    """Empty text when soul.last_spoke_at is within GREETING_ABSENCE_GAP."""
    soul = _wire_soul(monkeypatch, tmp_path)
    now = time.time()
    # Long visual absence would otherwise count as arrived.
    soul.last_seen_at = now - 2 * 3600
    soul.note_spoke(now - 60, source="chat", text="hi")

    async def _one_shot(*a, **k):
        raise AssertionError("LLM must not run when last_spoke recent")

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == ""
    # Probe still refreshes last_seen even when suppressed.
    assert soul.last_seen_at >= now - 5


def test_greeting_suppresses_when_last_user_voice_recent(tmp_path, monkeypatch):
    soul = _wire_soul(monkeypatch, tmp_path)
    now = time.time()
    soul.last_seen_at = now - 2 * 3600
    soul.note_user_voice(now - 30)

    async def _one_shot(*a, **k):
        raise AssertionError("LLM must not run when last_user_voice recent")

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == ""


def test_greeting_suppresses_sticky_occupied_continuous(tmp_path, monkeypatch):
    """Still at desk (sticky occupied) → not arrived, even if last_seen looks old."""
    soul = _wire_soul(monkeypatch, tmp_path, sticky_s=1800)
    now = time.time()
    # Person evidence 15 min ago: sticky still holds occupied; last_seen past gap.
    soul.note_person(now - 900, source="ambient")
    soul.last_seen_at = now - 900  # > GREETING_ABSENCE_GAP (600)
    assert soul.occupied(now) is True

    async def _one_shot(*a, **k):
        raise AssertionError("LLM must not run for sticky continuous session")

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == ""


def test_greeting_candidate_after_long_absence(tmp_path, monkeypatch):
    """Returns line when last_seen is 2h ago, not occupied, no recent speech."""
    soul = _wire_soul(monkeypatch, tmp_path)
    now = time.time()
    soul.last_seen_at = now - 2 * 3600
    assert soul.occupied(now) is False

    seen = {}
    fake = "Cam. The furniture survived without you."

    async def _one_shot(*a, **k):
        seen["ok"] = True
        return fake

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == fake
    assert seen.get("ok") is True
    assert soul.last_seen_at >= now - 5
    cur = soul.current_face()
    assert cur is not None
    assert cur["name"] == "Cam"


def test_greeting_suppresses_conversed_recently(tmp_path, monkeypatch):
    soul = _wire_soul(monkeypatch, tmp_path)
    now = time.time()
    soul.last_seen_at = now - 2 * 3600
    # face_meta last_convo_at via touch + set_convo_summary
    deps.MEMORY.touch_face(1, "Cam")
    deps.MEMORY.set_convo_summary(1, "widgets")  # last_convo_at = now

    async def _one_shot(*a, **k):
        raise AssertionError("LLM must not run when last_convo recent")

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == ""


def test_face_seen_updates_soul_buffer(tmp_path, monkeypatch):
    soul = _wire_soul(monkeypatch, tmp_path)
    asyncio.run(state_face_seen(FaceSeenRequest(face_id=7, name="Cam")))
    cur = soul.current_face()
    assert cur is not None
    assert cur["face_id"] == 7
    assert cur["name"] == "Cam"
    assert cur["is_stranger"] is False
    assert soul.last_seen_at > 0
    assert soul.occupied(time.time()) is True
    buf = soul.face.buffer_list()
    assert any(e.get("source") == "face_seen" for e in buf)


def test_current_face_prefers_soul(tmp_path, monkeypatch):
    soul = _wire_soul(monkeypatch, tmp_path)
    now = time.time()
    soul.note_face(
        now, face_id=3, name="Pat", is_stranger=False, source="face_seen"
    )
    # Legacy dict empty / wrong — SOUL wins.
    process_state._face_state["enrolled_id"] = 99
    process_state._face_state["enrolled_name"] = "Wrong"
    process_state._face_state["enrolled_seen"] = 0.0
    face = process_state.current_face()
    assert face is not None
    assert face["name"] == "Pat"
    assert face["face_id"] == 3

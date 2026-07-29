#!/usr/bin/env python3
"""Phase 5 mood digest: pot last_spoke / occupied, no false long silence."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _patch_memory(monkeypatch, tmp_path):
    import deps
    from memory import MemoryStore

    store = MemoryStore(tmp_path / "mood_mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": False})(),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "BEHAVIOR_RUNTIME",
        type("RT", (), {"workday": None})(),
        raising=False,
    )
    return store


def test_mood_digest_recent_spoke_not_long_silence(tmp_path, monkeypatch):
    """Occupied + last_spoke moments ago → digest must not claim long silence."""
    import deps
    from process_state import _ambient_state, _mood_state
    from routes.mood import build_mood_digest
    from soul import Soul

    store = _patch_memory(monkeypatch, tmp_path)
    # No face_meta conversation — pot clocks alone should suffice.
    now = datetime.now()
    now_ts = now.timestamp()
    soul = Soul()
    soul.note_person(now_ts - 60, source="test")
    soul.note_spoke(now_ts - 20, source="chat", text="hey")
    soul.last_seen_at = now_ts - 30
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)
    _ambient_state["quiet"] = False
    _mood_state["text"] = ""
    _mood_state["updated"] = 0.0

    digest = build_mood_digest(now)
    assert "Someone is at the desk" in digest
    assert "last spoke" in digest.lower()
    assert "moments ago" in digest.lower() or "minute" in digest.lower()
    assert "not had a real conversation in a long time" not in digest
    # Sanity: store still empty of convos
    assert store.latest_conversation() is None


def test_mood_digest_no_speech_clocks_long_silence(tmp_path, monkeypatch):
    """With no soul speech and no convo meta, long-silence line remains."""
    import deps
    from process_state import _ambient_state, _mood_state
    from routes.mood import build_mood_digest

    _patch_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(deps, "SOUL", None, raising=False)
    _ambient_state["quiet"] = False
    _mood_state["text"] = "mildly bored"
    _mood_state["updated"] = 0.0

    digest = build_mood_digest(datetime.now())
    assert "not had a real conversation in a long time" in digest
    assert "mildly bored" in digest

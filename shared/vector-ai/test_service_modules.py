"""Smoke tests for modularized vector-ai service (import + pure helpers)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# Expected HTTP paths after Phase 6 route split (frozen contract).
EXPECTED_ROUTES = {
    "/health",
    "/v1/chat/completions",
    "/v1/mood",
    "/v1/mood/reflect",
    "/v1/memory/list",
    "/v1/memory/remember",
    "/v1/memory/forget",
    "/v1/memory/clear",
    "/v1/state/face_seen",
    "/v1/state/face",
    "/v1/sensor_reaction",
    "/v1/ambient",
    "/v1/ambient/state",
    "/v1/ambient/quiet",
    "/v1/behaviors/tick",
    "/v1/behaviors/state",
    "/v1/proactive_greeting",
}


def test_service_import_and_app():
    import service

    assert hasattr(service, "app")
    assert hasattr(service, "llm_chat_once")
    assert callable(service.llm_chat_once)


def test_llm_chat_once_reexport_identity():
    from service import llm_chat_once
    from llm import llm_chat_once as l2

    assert llm_chat_once is l2


def test_route_registry():
    import service

    paths = {
        getattr(r, "path", None)
        for r in service.app.routes
        if getattr(r, "path", None)
    }
    missing = EXPECTED_ROUTES - paths
    assert not missing, f"missing routes: {sorted(missing)}"


def test_is_vision_intent():
    from vision import is_vision_intent

    assert is_vision_intent("what do you see") is True
    assert is_vision_intent("look at this") is True
    assert is_vision_intent("what time is it") is False


def test_strip_markdown():
    from response_cleanup import strip_markdown

    assert strip_markdown("**x**") == "x"
    assert strip_markdown("# heading") == "heading"
    assert strip_markdown("`code`") == ""


def test_clean_response_strips_remember(tmp_path, monkeypatch):
    """clean_response must strip {{remember||…}} when MEMORY is a temp store."""
    import deps
    from memory import MemoryStore
    from response_cleanup import clean_response

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    # workday may be None on runtime; ensure deps has runtime with no workday
    class _RT:
        workday = None

    monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", _RT(), raising=False)
    monkeypatch.setattr(deps, "_workday_cfg", type("C", (), {"tz": None, "enabled": False})(), raising=False)

    out = clean_response("Hello {{remember||likes coffee}} world")
    assert "{{remember" not in out
    assert "likes coffee" not in out or "Hello" in out
    assert "Hello" in out and "world" in out
    # memory should have been stored
    mems = store.list_all(limit=10)
    assert any("coffee" in m.text for m in mems)


def test_clean_response_strips_forbidden_and_empty_edge():
    from response_cleanup import clean_response, strip_markdown
    import deps
    from memory import MemoryStore
    import tempfile

    # Need MEMORY for extract path even when no remember tags
    with tempfile.TemporaryDirectory() as d:
        deps.MEMORY = MemoryStore(Path(d) / "m.db")

        class _RT:
            workday = None

        deps.BEHAVIOR_RUNTIME = _RT()
        deps._workday_cfg = type("C", (), {"tz": None, "enabled": False})()

        out = clean_response("Hi {{newVoiceRequest||x}} there")
        assert "newVoiceRequest" not in out
        assert "Hi" in out and "there" in out

        assert strip_markdown("") == ""
        assert clean_response("") == ""


def test_pick_thinking_phrase_nonempty():
    from process_state import pick_thinking_phrase

    p = pick_thinking_phrase()
    assert isinstance(p, str)
    assert len(p) > 0
    # second call should also work (anti-repeat path)
    p2 = pick_thinking_phrase()
    assert isinstance(p2, str) and p2


def test_persona_nonempty():
    from persona import PERSONA

    assert isinstance(PERSONA, str)
    assert len(PERSONA) > 10


def test_no_import_cycle_behaviors():
    import service  # noqa: F401
    from behaviors import joke_sources  # noqa: F401


def test_cap_chunk_animations_edge():
    from chat_flow import cap_chunk_animations

    text = "{{playAnimation||a}} mid {{playAnimationWI||b}}"
    out, kept = cap_chunk_animations(text, 1)
    assert kept == 1
    assert out.count("{{playAnimation") == 1

    out0, kept0 = cap_chunk_animations(text, 0)
    assert kept0 == 0
    assert "{{playAnimation" not in out0

"""Smoke tests for modularized vector-ai service (import + pure helpers)."""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
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

# path -> allowed HTTP methods (business endpoints only).
EXPECTED_ROUTE_METHODS = {
    "/health": {"GET"},
    "/v1/chat/completions": {"POST"},
    "/v1/mood": {"GET"},
    "/v1/mood/reflect": {"POST"},
    "/v1/memory/list": {"GET"},
    "/v1/memory/remember": {"POST"},
    "/v1/memory/forget": {"POST"},
    "/v1/memory/clear": {"POST"},
    "/v1/state/face_seen": {"POST"},
    "/v1/state/face": {"GET"},
    "/v1/sensor_reaction": {"POST"},
    "/v1/ambient": {"POST"},
    "/v1/ambient/state": {"GET"},
    "/v1/ambient/quiet": {"POST"},
    "/v1/behaviors/tick": {"POST"},
    "/v1/behaviors/state": {"GET"},
    "/v1/proactive_greeting": {"POST"},
}

_REQUIRED_INSTALL_MODULES = (
    "paths.py",
    "logging_util.py",
    "debug_log.py",
    "llm.py",
    "persona.py",
    "process_state.py",
    "deps.py",
    "vision.py",
    "prompt_assembly.py",
    "response_cleanup.py",
    "chat_flow.py",
    "routes",
)


def _patch_deps(monkeypatch, tmp_path):
    """Isolate deps.MEMORY / runtime for extract/clean tests (no cross-test pollution)."""
    import deps
    from memory import MemoryStore

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)

    class _RT:
        workday = None

    monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", _RT(), raising=False)
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": False})(),
        raising=False,
    )
    return store


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
    business = {p for p in paths if p == "/health" or p.startswith("/v1/")}
    assert business == EXPECTED_ROUTES, (
        f"business path set mismatch: "
        f"missing={sorted(EXPECTED_ROUTES - business)} "
        f"extra={sorted(business - EXPECTED_ROUTES)}"
    )

    by_path: dict[str, set[str]] = defaultdict(set)
    for r in service.app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if path and methods:
            by_path[path] |= {m.upper() for m in methods}

    for path, want in EXPECTED_ROUTE_METHODS.items():
        got = by_path.get(path, set())
        assert want <= got, f"{path}: expected methods {want}, got {got}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what do you see", True),
        ("look at this", True),
        ("WHAT DO YOU SEE", True),
        ("what you see", True),  # VOSK mangle (dropped aux)
        ("Can you see anything?", True),
        ("what time is it", False),
        ("", False),
        ("hello there", False),
    ],
)
def test_is_vision_intent(text, expected):
    from vision import is_vision_intent

    assert is_vision_intent(text) is expected


def test_strip_markdown():
    from response_cleanup import strip_markdown

    assert strip_markdown("**x**") == "x"
    assert strip_markdown("# heading") == "heading"
    assert strip_markdown("`code`") == ""
    assert strip_markdown("") == ""
    assert strip_markdown("[label](https://example.com)") == "label"
    assert strip_markdown("- item one\n- item two") == "item one\nitem two"


def test_clean_response_strips_remember(tmp_path, monkeypatch):
    """clean_response must strip {{remember||…}} when MEMORY is a temp store."""
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)
    out = clean_response("Hello {{remember||likes coffee}} world")
    assert "{{remember" not in out
    assert "likes coffee" not in out
    assert "Hello" in out and "world" in out
    mems = store.list_all(limit=10)
    assert any("coffee" in m.text for m in mems)


def test_clean_response_strips_forbidden_and_empty_edge(tmp_path, monkeypatch):
    from response_cleanup import clean_response, strip_markdown

    _patch_deps(monkeypatch, tmp_path)

    out = clean_response("Hi {{newVoiceRequest||x}} there")
    assert "newVoiceRequest" not in out
    assert "Hi" in out and "there" in out

    out2 = clean_response("Listen {{voiceRequest||y}} now")
    assert "voiceRequest" not in out2
    assert "Listen" in out2 and "now" in out2

    assert strip_markdown("") == ""
    assert clean_response("") == ""


def test_clean_response_remember_shared_forget_and_dup(tmp_path, monkeypatch):
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)

    out = clean_response("Note {{remember-shared||household wifi is guest}} ok")
    assert "{{remember" not in out
    assert "household wifi" not in out
    shared = store.list_shared(limit=20)
    assert any("wifi" in m.text for m in shared)
    assert all(m.face_id is None for m in shared if "wifi" in m.text)

    # Duplicate remember: second insert returns None; tag still stripped.
    out_dup = clean_response("Again {{remember-shared||household wifi is guest}} end")
    assert "{{remember" not in out_dup
    assert "Again" in out_dup and "end" in out_dup
    assert sum(1 for m in store.list_shared(limit=50) if "wifi" in m.text) == 1

    # Forget by substring.
    out_f = clean_response("Bye {{forget||wifi}} done")
    assert "{{forget" not in out_f
    assert "Bye" in out_f and "done" in out_f
    assert not any("wifi" in m.text for m in store.list_all(limit=50))


def test_clean_response_quiet_mode_tag(tmp_path, monkeypatch):
    import process_state
    from response_cleanup import clean_response

    _patch_deps(monkeypatch, tmp_path)
    process_state._set_quiet(False)
    out = clean_response("Sure {{quietMode||on}} hush")
    assert "{{quietMode" not in out
    assert process_state._ambient_state["quiet"] is True
    out2 = clean_response("Ok {{quietMode||off}} talk")
    assert process_state._ambient_state["quiet"] is False
    assert "Ok" in out2


def test_clean_response_strips_work_tags_without_workday(tmp_path, monkeypatch):
    from response_cleanup import clean_response

    _patch_deps(monkeypatch, tmp_path)
    out = clean_response("Fine {{workResume}} continue")
    assert "{{work" not in out.lower()
    assert "Fine" in out and "continue" in out


def test_pick_thinking_phrase_nonempty():
    from process_state import pick_thinking_phrase

    p = pick_thinking_phrase()
    assert isinstance(p, str)
    assert len(p) > 0
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


def test_sse_chunk_shape():
    from chat_flow import sse_chunk

    raw = sse_chunk("Hello robot.", finish=None)
    assert raw.startswith("data: ")
    assert raw.endswith("\n\n")
    payload = json.loads(raw[len("data: "):].strip())
    assert payload["object"] == "chat.completion.chunk"
    assert payload["choices"][0]["delta"]["content"] == "Hello robot."
    assert payload["choices"][0]["finish_reason"] is None

    done = sse_chunk("", finish="stop")
    payload2 = json.loads(done[len("data: "):].strip())
    assert payload2["choices"][0]["finish_reason"] == "stop"
    assert payload2["choices"][0]["delta"] == {}


def test_generate_vision_intent_forces_getimage_no_llm():
    """Vision backstop must yield getImage without calling the upstream LLM."""
    from chat_flow import generate
    from vision import _GETIMAGE_PAYLOAD

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def _collect():
        chunks = []
        async for c in generate(
            [_Msg("user", "what do you see on the desk")],
            temperature=1.0,
        ):
            chunks.append(c)
        return "".join(chunks)

    joined = asyncio.run(_collect())
    assert _GETIMAGE_PAYLOAD in joined or "getImage" in joined
    assert "[DONE]" in joined


def test_ambient_quiet_short_circuits_without_llm(tmp_path, monkeypatch):
    """Quiet mode with a recent ambient call returns quiet=True and no LLM hit."""
    import process_state
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    _patch_deps(monkeypatch, tmp_path)
    process_state._set_quiet(True)
    process_state._ambient_state["last_ambient_call"] = time.time()  # no sleep gap

    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("llm should not run while quiet")

    monkeypatch.setattr("routes.ambient.llm_chat_once", _boom)

    result = asyncio.run(ambient(AmbientRequest(image="not-a-real-jpeg")))
    assert result.get("quiet") is True
    assert result.get("text", "") == ""
    assert called["n"] == 0


def test_ambient_quiet_expires_on_sleep_gap(tmp_path, monkeypatch):
    """Long gap since last ambient call lifts quiet mode (sleep-cycle expiry)."""
    import process_state
    from process_state import AMBIENT_SLEEP_GAP
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    _patch_deps(monkeypatch, tmp_path)
    process_state._set_quiet(True)
    process_state._ambient_state["last_ambient_call"] = (
        time.time() - AMBIENT_SLEEP_GAP - 10
    )

    async def _nothing(*a, **k):
        return "NOTHING"

    monkeypatch.setattr("routes.ambient.llm_chat_once", _nothing)

    result = asyncio.run(ambient(AmbientRequest(image="x")))
    assert process_state._ambient_state["quiet"] is False
    assert result.get("text", "") == ""


def test_install_scripts_list_required_modules():
    """Deploy-by-copy installers must mention every new module and routes/."""
    root = Path(__file__).resolve().parents[2]  # VectorIntelligence/
    scripts = [
        root / "linux" / "install.sh",
        root / "windows" / "install.ps1",
        root / "windows" / "setup-companion.ps1",
    ]
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        for name in _REQUIRED_INSTALL_MODULES:
            assert name in text, f"{script.name} missing deploy artifact: {name}"

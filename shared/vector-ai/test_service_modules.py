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
    "/v1/behaviors/{behavior_id}",
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
    "/v1/behaviors/{behavior_id}": {"GET"},
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


def test_behavior_detail_http_404_and_200(tmp_path, monkeypatch):
    """Thin HTTP: unknown id → 404; registered plugin → 200 + id/schema_version."""
    from datetime import datetime, timezone

    import deps
    from behaviors.config import RuntimeConfig, WorkdayConfig
    from behaviors.continuity import ContinuityStore, WorkdayRecord
    from behaviors.runtime import BehaviorRuntime
    from behaviors.types import WorkdayMode
    from fastapi.testclient import TestClient
    from zoneinfo import ZoneInfo

    import service

    cont = ContinuityStore(tmp_path / "w.db")
    wcfg = WorkdayConfig(enabled=True, tz=ZoneInfo("UTC"))
    rt = BehaviorRuntime(
        RuntimeConfig(behaviors_enabled=("workday",)),
        wcfg,
        cont,
    )
    # Seed today's UTC date so status() (uses wall clock) sees WORKING.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cont.save_workday(WorkdayRecord(date=today, mode=WorkdayMode.WORKING))
    monkeypatch.setattr(deps, "BEHAVIOR_RUNTIME", rt, raising=False)

    client = TestClient(service.app)
    r404 = client.get("/v1/behaviors/nope")
    assert r404.status_code == 404
    assert "not registered" in r404.json().get("detail", "")

    r200 = client.get("/v1/behaviors/workday")
    assert r200.status_code == 200
    body = r200.json()
    assert body.get("id") == "workday"
    assert body.get("schema_version") == 1
    assert body.get("mode") == "working"

    r_state = client.get("/v1/behaviors/state")
    assert r_state.status_code == 200
    st = r_state.json()
    assert st.get("schema_version") == 1
    assert "presence" in st and "behaviors" in st
    assert "occupied" not in st  # flat key gone


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


def test_sentence_stream_holds_tags_with_periods():
    """Periods inside unmatched {{…}} must not fracture the buffer."""
    from llm import split_sentences_from_buffer

    buf = "Noted. {{remember||Dr. Smith is Cam's dentist}} Done. Next sentence."
    parts, rest = split_sentences_from_buffer(buf)
    assert parts == [
        "Noted.",
        "{{remember||Dr. Smith is Cam's dentist}} Done.",
        "Next sentence.",
    ]
    assert rest == ""


def test_sentence_stream_abbreviations_still_split():
    from llm import split_sentences_from_buffer

    parts, rest = split_sentences_from_buffer(
        "I'll note that. Next sentence. "
    )
    assert parts == ["I'll note that.", "Next sentence."]
    assert rest == ""


def test_sentence_stream_incomplete_open_tag_held():
    from llm import split_sentences_from_buffer

    parts, rest = split_sentences_from_buffer(
        "Hi. {{remember||Dr. Smith is still open"
    )
    assert parts == ["Hi."]
    assert rest == "{{remember||Dr. Smith is still open"
    # Closing the tag later allows a sentence end after it.
    parts2, rest2 = split_sentences_from_buffer(
        rest + " now}} And done. "
    )
    assert parts2 == ["{{remember||Dr. Smith is still open now}} And done."]
    assert rest2 == ""


def test_sentence_stream_nested_tags_and_multi_outside():
    from llm import split_sentences_from_buffer

    # Nested {{ }} depth: hold until fully closed.
    parts, rest = split_sentences_from_buffer(
        "A. {{outer||inner {{x||Dr. Y}} z}} B. C. "
    )
    assert parts[0] == "A."
    assert "{{outer||inner {{x||Dr. Y}} z}} B." in parts
    assert parts[-1] == "C."
    assert rest == ""


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

    # newVoiceRequest is a real chipper command for multi-turn conversation —
    # clean_response must keep it so chipper can execute listen windows.
    out = clean_response("Hi {{newVoiceRequest||now}} there")
    assert "{{newVoiceRequest||now}}" in out
    assert "Hi" in out and "there" in out

    # Invented listen-like commands are stripped.
    out2 = clean_response("Listen {{voiceRequest||y}} now")
    assert "voiceRequest" not in out2
    assert "Listen" in out2 and "now" in out2

    out3 = clean_response("A {{listen||x}} B {{wakeWord||y}} C {{waitForUser||z}} D")
    assert "{{listen" not in out3
    assert "wakeWord" not in out3
    assert "waitForUser" not in out3
    assert "A" in out3 and "D" in out3

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


def test_remember_uses_effective_face_single_user(tmp_path, monkeypatch):
    """No live face + one enrolled profile → remember is personal for that face."""
    import prompt_assembly
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)
    # Seed one enrolled profile with a prior personal fact so distinct_faces=1.
    store.remember("already known about Cam", face_id=7, face_name="Cam")
    monkeypatch.setattr(prompt_assembly, "current_face", lambda: None)

    out = clean_response("Ok {{remember||likes dark roast}} bye")
    assert "{{remember" not in out
    owned = [m for m in store.list_all(limit=20) if "dark roast" in m.text]
    assert len(owned) == 1
    assert owned[0].face_id == 7
    assert (owned[0].face_name or "") == "Cam"


def test_remember_stranger_stays_shared(tmp_path, monkeypatch):
    """Live stranger face → personal remember falls back to shared ownership."""
    import prompt_assembly
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("Cam private fact", face_id=1, face_name="Cam")
    stranger = {"face_id": -3, "name": "", "is_stranger": True}
    monkeypatch.setattr(prompt_assembly, "current_face", lambda: stranger)

    out = clean_response("Hmm {{remember||guest mentioned rain}} ok")
    assert "{{remember" not in out
    rain = [m for m in store.list_all(limit=20) if "rain" in m.text]
    assert len(rain) == 1
    assert rain[0].face_id is None


def test_remember_zero_enrolled_stays_shared(tmp_path, monkeypatch):
    """No live face and zero enrolled profiles → shared ownership."""
    import prompt_assembly
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(prompt_assembly, "current_face", lambda: None)
    out = clean_response("Ok {{remember||orphan fact about weather}} x")
    assert "{{remember" not in out
    rows = [m for m in store.list_all(limit=10) if "weather" in m.text]
    assert len(rows) == 1
    assert rows[0].face_id is None


def test_remember_two_enrolled_stays_shared(tmp_path, monkeypatch):
    """No live face + two enrolled → cannot pick owner → shared."""
    import prompt_assembly
    from response_cleanup import clean_response

    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("Cam fact", face_id=1, face_name="Cam")
    store.remember("Sam fact", face_id=2, face_name="Sam")
    monkeypatch.setattr(prompt_assembly, "current_face", lambda: None)
    out = clean_response("Ok {{remember||ambiguous owner fact}} x")
    assert "{{remember" not in out
    rows = [m for m in store.list_all(limit=20) if "ambiguous" in m.text]
    assert len(rows) == 1
    assert rows[0].face_id is None


def test_image_payload_too_large_edges():
    from llm import image_payload_too_large

    assert image_payload_too_large("") is False
    assert image_payload_too_large(None) is False  # type: ignore[arg-type]
    assert image_payload_too_large("abc", limit=10) is False
    assert image_payload_too_large("a" * 11, limit=10) is True
    # data-URL: only payload after comma is measured
    assert image_payload_too_large("data:image/jpeg;base64," + ("x" * 5), limit=10) is False
    assert image_payload_too_large("data:image/jpeg;base64," + ("x" * 11), limit=10) is True


def test_one_shot_spoken_line_strips_and_errors(monkeypatch):
    import llm as llm_mod

    async def _ok(*a, **k):
        return '**Hi** {{remember||x}} "there"'

    monkeypatch.setattr(llm_mod, "llm_chat_once", _ok)
    out = asyncio.run(
        llm_mod.one_shot_spoken_line(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
            tag="t",
        )
    )
    assert "Hi" in out
    assert "{{" not in out
    assert "**" not in out

    async def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(llm_mod, "llm_chat_once", _boom)
    out2 = asyncio.run(
        llm_mod.one_shot_spoken_line(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
            tag="t",
        )
    )
    assert out2 == ""


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


def test_sanitize_chat_image_parts_oversized():
    """Oversized multimodal image → placeholder; not archived; has_image False."""
    from chat_flow import (
        _OVERSIZED_IMAGE_PLACEHOLDER,
        sanitize_chat_image_parts,
    )

    huge = "data:image/jpeg;base64," + ("A" * 100)
    ok_url = "data:image/jpeg;base64,abc"
    content = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": huge}},
        {"type": "image_url", "image_url": {"url": ok_url}},
    ]
    out, archive_urls, has_image = sanitize_chat_image_parts(content, limit=50)
    assert has_image is True  # ok_url remains
    assert archive_urls == [ok_url]
    assert out[0] == {"type": "text", "text": "what is this"}
    assert out[1] == {"type": "text", "text": _OVERSIZED_IMAGE_PLACEHOLDER}
    assert out[2]["type"] == "image_url"
    assert huge not in archive_urls
    # No original huge payload in any remaining image part
    for p in out:
        if isinstance(p, dict) and p.get("type") == "image_url":
            iu = p.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            assert url != huge
            assert "A" * 100 not in (url or "")

    # All oversized → no archive, no image left for LLM
    only_huge = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": huge}},
    ]
    out2, arch2, has2 = sanitize_chat_image_parts(only_huge, limit=50)
    assert has2 is False
    assert arch2 == []
    assert out2[1]["type"] == "text"
    assert _OVERSIZED_IMAGE_PLACEHOLDER in out2[1]["text"]


def test_generate_oversized_image_no_archive_placeholder_upstream(monkeypatch):
    """generate() strips huge image before archive + prepare_messages + stream.

    Use non-vision user text so the vision-intent early return does not fire
    after has_image becomes False (that path would skip prepare/stream).
    """
    from chat_flow import _OVERSIZED_IMAGE_PLACEHOLDER, generate

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    archive_calls: list = []
    prepared_seen: list = []
    stream_seen: list = []

    def _fake_archive(url, source):
        archive_calls.append((url, source))

    def _fake_prepare(messages, face):
        prepared_seen.append(list(messages))
        # No oversized payload on any message after sanitize
        blob = repr(messages)
        assert "A" * 200 not in blob
        assert huge not in blob
        content = (
            messages[-1].content
            if hasattr(messages[-1], "content")
            else messages[-1]["content"]
        )
        assert isinstance(content, list)
        assert not any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        )
        assert any(
            isinstance(p, dict)
            and p.get("type") == "text"
            and _OVERSIZED_IMAGE_PLACEHOLDER in (p.get("text") or "")
            for p in content
        )
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": content},
        ]

    stream_models: list = []

    async def _fake_stream(messages, temperature=None, filler_enabled=True, **kwargs):
        stream_seen.append(messages)
        stream_models.append(kwargs.get("model"))
        blob = json.dumps(messages)
        assert "A" * 200 not in blob
        assert huge not in blob
        yield "Hello from the stream."

    monkeypatch.setattr("chat_flow.save_submitted_image", _fake_archive)
    monkeypatch.setattr("chat_flow.prepare_messages", _fake_prepare)
    monkeypatch.setattr("chat_flow.stream_sentences_with_filler", _fake_stream)
    monkeypatch.setattr("chat_flow._effective_face", lambda: None)
    # Avoid openrouter key short-circuit
    monkeypatch.setattr("chat_flow.LLM_API_KEY", "test-key")
    # Distinct vision slug so a wrong model=VISION would be visible.
    monkeypatch.setattr("chat_flow.VISION_MODEL", "vision/must-not-use", raising=False)

    huge = "data:image/jpeg;base64," + ("A" * (5_500_000 + 10))
    # Non-vision text: "hi" must not match is_vision_intent after image strip.
    msg = _Msg(
        "user",
        [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": huge}},
        ],
    )

    async def _collect():
        out = []
        async for c in generate([msg], temperature=0.5):
            out.append(c)
        return "".join(out)

    joined = asyncio.run(_collect())
    assert archive_calls == []  # oversized not archived
    assert huge not in [u for u, _ in archive_calls]
    assert _OVERSIZED_IMAGE_PLACEHOLDER in str(msg.content)
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in msg.content
    )
    # prepare + stream path actually ran (not getImage early return)
    assert len(prepared_seen) == 1
    assert len(stream_seen) == 1
    # All images stripped → has_image False → model=None (→ MODEL), not vision.
    assert stream_models == [None], (
        f"oversized strip must stream with model=None, got {stream_models}"
    )
    assert "Hello from the stream" in joined
    assert "getImage" not in joined
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
    # Quiet short-circuit must not invent presence / occupied.
    assert "presence" not in result
    assert "occupied" not in result


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


def test_print_survives_charmap_stream():
    """Windows cp1252/charmap must not raise when printing Hangul (sensor path)."""
    import io

    from logging_util import print as vprint

    class CharmapStream(io.TextIOBase):
        encoding = "cp1252"

        def __init__(self):
            self.buf = []

        def write(self, s):
            # Simulate Windows stdout: encode with charmap, raise on Hangul.
            s.encode("cp1252")
            self.buf.append(s)
            return len(s)

        def flush(self):
            pass

    sink = CharmapStream()
    hangul = "reply with \uba7f in it"  # same class of char as production error
    # Must not raise UnicodeEncodeError
    vprint(hangul, file=sink)
    joined = "".join(sink.buf)
    assert "reply" in joined
    # Backslash-escaped for cp1252 (raw Hangul cannot be encoded)
    assert "\\uba7f" in joined or "\\u" in joined


def test_llm_chat_once_returns_non_ascii_despite_debug_print(monkeypatch):
    """Regression: Hangul in model reply used to die inside debug() print.

    Production error:
      'charmap' codec can't encode character '\\uba7f' in position 121
    which aborted /v1/sensor_reaction and forced chipper fallbacks.
    """
    import httpx
    import llm as llm_mod

    hangul_reply = "Slightly better than \uba7f elevator music."

    class _Resp:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": hangul_reply}}],
                "usage": {},
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)
    # Force DEBUG path that previously printed the reply and crashed.
    monkeypatch.setattr(llm_mod, "debug", llm_mod.debug)
    import debug_log

    monkeypatch.setattr(debug_log, "DEBUG", True)

    # Make print explode if it ever tries to encode Hangul as charmap — if
    # llm_chat_once still returns the text, the request path is safe.
    def _boom_print(*a, **k):
        msg = " ".join(str(x) for x in a)
        if "\uba7f" in msg:
            raise UnicodeEncodeError("charmap", msg, 0, 1, "maps to undefined")

    monkeypatch.setattr(debug_log, "print", _boom_print)

    text = asyncio.run(
        llm_mod.llm_chat_once(
            [{"role": "user", "content": "hi"}],
            tag="sensor_reaction",
        )
    )
    assert text == hangul_reply


def test_sensor_reaction_returns_text_with_non_ascii(monkeypatch):
    """/v1/sensor_reaction must return LLM text even when it contains non-ASCII."""
    from routes.models import SensorReactionRequest
    from routes.sensor import sensor_reaction

    hangul_reply = "Pet protocol \uba7f engaged."

    async def _fake_one_shot(*a, **k):
        return hangul_reply

    monkeypatch.setattr("routes.sensor.one_shot_spoken_line", _fake_one_shot)
    out = asyncio.run(sensor_reaction(SensorReactionRequest(event="pet")))
    assert out.get("error") in (None, "")
    assert "error" not in out or not out["error"]
    assert "Pet protocol" in out.get("text", "")


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


# -- LLM model selection (vision / summary / conversation split) ---------------


def test_health_exposes_vision_model():
    """GET /health must list vision_model equal to llm module constants."""
    import llm as llm_mod
    from routes.health import health

    body = asyncio.run(health())
    assert body.get("status") == "ok"
    assert body["model"] == llm_mod.MODEL
    assert body["vision_model"] == llm_mod.VISION_MODEL
    assert body["summary_model"] == llm_mod.SUMMARY_MODEL


def test_health_reflects_patched_vision_model(monkeypatch):
    """Health reads the routes.health.VISION_MODEL binding (restart-level env)."""
    from routes.health import health

    monkeypatch.setattr("routes.health.VISION_MODEL", "vision/health-slug")
    body = asyncio.run(health())
    assert body["vision_model"] == "vision/health-slug"


def test_ambient_passes_vision_model(tmp_path, monkeypatch):
    """Ambient LLM call must use VISION_MODEL (not conversation or summary)."""
    import process_state
    from pathlib import Path

    import routes.ambient as ambient_mod
    from routes.ambient import ambient
    from routes.models import AmbientRequest

    # Static lock: ambient must never import/use SUMMARY_MODEL for images.
    ambient_src = Path(ambient_mod.__file__).read_text(encoding="utf-8")
    assert "SUMMARY_MODEL" not in ambient_src
    assert "VISION_MODEL" in ambient_src

    _patch_deps(monkeypatch, tmp_path)
    process_state._set_quiet(False)
    process_state._ambient_state["last_ambient_call"] = 0.0

    seen = {}

    async def _llm(messages, *, model=None, tag=None, **kw):
        seen["model"] = model
        seen["tag"] = tag
        return "PRESENCE: empty\nNOTHING"

    monkeypatch.setattr("routes.ambient.llm_chat_once", _llm)
    monkeypatch.setattr("routes.ambient.VISION_MODEL", "vision/test-slug", raising=False)
    monkeypatch.setattr(
        "routes.ambient.image_payload_too_large", lambda *a, **k: False
    )
    monkeypatch.setattr("routes.ambient.save_submitted_image", lambda *a, **k: None)

    result = asyncio.run(ambient(AmbientRequest(image="fakejpg")))
    assert result.get("error") in (None, "")
    assert seen.get("model") == "vision/test-slug"
    assert seen.get("model") != "summary/test-slug"
    assert seen.get("tag") == "ambient"


def test_greeting_passes_summary_model(tmp_path, monkeypatch):
    """Proactive greeting must use SUMMARY_MODEL (cheap text path)."""
    import process_state
    from routes.greeting import proactive_greeting
    from routes.models import GreetingRequest

    _patch_deps(monkeypatch, tmp_path)
    # First-seen face → arrived; no recent conversation meta.
    process_state._face_last_seen.clear()
    process_state._recent_greetings.clear()

    seen = {}
    fake_line = "Oh. Cam. You again."

    async def _one_shot(*a, **k):
        seen["model"] = k.get("model")
        seen["tag"] = k.get("tag")
        return fake_line

    monkeypatch.setattr("routes.greeting.one_shot_spoken_line", _one_shot)
    monkeypatch.setattr(
        "routes.greeting.SUMMARY_MODEL", "summary/test-slug", raising=False
    )

    out = asyncio.run(proactive_greeting(GreetingRequest(face_id=1, name="Cam")))
    assert out.get("text") == fake_line
    assert seen.get("model") == "summary/test-slug"
    assert seen.get("tag") == "greeting"


def test_sensor_passes_summary_model(monkeypatch):
    """Sensor reaction must use SUMMARY_MODEL (cheap text path)."""
    from routes.models import SensorReactionRequest
    from routes.sensor import sensor_reaction

    seen = {}
    fake_line = "Careful with the merchandise."

    async def _one_shot(*a, **k):
        seen["model"] = k.get("model")
        seen["tag"] = k.get("tag")
        return fake_line

    monkeypatch.setattr("routes.sensor.one_shot_spoken_line", _one_shot)
    monkeypatch.setattr(
        "routes.sensor.SUMMARY_MODEL", "summary/test-slug", raising=False
    )

    out = asyncio.run(sensor_reaction(SensorReactionRequest(event="pickup")))
    assert out.get("text") == fake_line
    assert seen.get("model") == "summary/test-slug"
    assert seen.get("tag") == "sensor_reaction"


def test_generate_stream_model_text_vs_image(tmp_path, monkeypatch):
    """Text-only chat: model=None (→ MODEL). Image chat: VISION_MODEL."""
    from chat_flow import generate

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    stream_models: list = []

    async def _fake_stream(messages, temperature=None, filler_enabled=True, *, model=None, **kw):
        stream_models.append(model)
        yield "Looks fine."

    monkeypatch.setattr("chat_flow.stream_sentences_with_filler", _fake_stream)
    monkeypatch.setattr(
        "chat_flow.prepare_messages",
        lambda msgs, face: [{"role": "user", "content": "x"}],
    )
    monkeypatch.setattr("chat_flow._effective_face", lambda: None)
    monkeypatch.setattr("chat_flow.LLM_API_KEY", "test-key")
    monkeypatch.setattr("chat_flow.VISION_MODEL", "vision/stream-slug", raising=False)
    monkeypatch.setattr("chat_flow.save_submitted_image", lambda *a, **k: None)
    monkeypatch.setattr(
        "chat_flow.image_payload_too_large", lambda *a, **k: False
    )

    async def _collect(msgs):
        out = []
        async for c in generate(msgs, temperature=0.5):
            out.append(c)
        return "".join(out)

    stream_models.clear()
    joined = asyncio.run(_collect([_Msg("user", "hello there")]))
    assert "Looks fine" in joined
    assert stream_models == [None], f"text-only should pass model=None, got {stream_models}"

    stream_models.clear()
    img_msg = _Msg(
        "user",
        [
            {"type": "text", "text": "what is this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,abc"},
            },
        ],
    )
    joined2 = asyncio.run(_collect([img_msg]))
    assert "Looks fine" in joined2
    assert stream_models == ["vision/stream-slug"], (
        f"image chat should pass VISION_MODEL, got {stream_models}"
    )


def test_stream_sentences_with_filler_forwards_model(monkeypatch):
    """Filler wrapper must pass model= through to llm_sentence_stream."""
    from chat_flow import stream_sentences_with_filler

    seen = {}

    async def _fake_llm_stream(messages, temperature=None, *, model=None):
        seen["model"] = model
        yield "One."

    monkeypatch.setattr("chat_flow.llm_sentence_stream", _fake_llm_stream)

    async def _collect():
        out = []
        async for s in stream_sentences_with_filler(
            [{"role": "user", "content": "x"}],
            0.5,
            filler_enabled=False,
            model="vision/fwd-slug",
        ):
            out.append(s)
        return out

    out = asyncio.run(_collect())
    assert out == ["One."]
    assert seen.get("model") == "vision/fwd-slug"

    seen.clear()

    async def _collect_default():
        out = []
        async for s in stream_sentences_with_filler(
            [{"role": "user", "content": "x"}],
            0.5,
            filler_enabled=False,
        ):
            out.append(s)
        return out

    asyncio.run(_collect_default())
    assert seen.get("model") is None


def test_llm_sentence_stream_model_body(monkeypatch):
    """Stream request body uses model override; None/empty fall back to MODEL."""
    import llm as llm_mod

    captured: list = []

    class _StreamCtx:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *a):
            return None

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            chunk = json.dumps(
                {"choices": [{"delta": {"content": "Hello world."}}]}
            )
            yield f"data: {chunk}"
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def stream(self, method, url, headers=None, json=None):
            captured.append(json)
            return _StreamCtx(_Resp())

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _Client)

    async def _run(model=None):
        out = []
        async for s in llm_mod.llm_sentence_stream(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
            model=model,
        ):
            out.append(s)
        return out

    captured.clear()
    sentences = asyncio.run(_run("vision/slug"))
    assert captured and captured[0]["model"] == "vision/slug"
    assert any("Hello" in s for s in sentences)

    captured.clear()
    asyncio.run(_run(None))
    assert captured and captured[0]["model"] == llm_mod.MODEL

    # Empty string is falsy → same fallback as None (intentional model or MODEL).
    captured.clear()
    asyncio.run(_run(""))
    assert captured and captured[0]["model"] == llm_mod.MODEL


def test_sse_chunk_model_label():
    """SSE payload model field uses override when provided."""
    from chat_flow import sse_chunk
    import llm as llm_mod

    raw = sse_chunk("Hi.", model="vision/sse-slug")
    payload = json.loads(raw[len("data: "):].strip())
    assert payload["model"] == "vision/sse-slug"

    raw2 = sse_chunk("Hi.")
    payload2 = json.loads(raw2[len("data: "):].strip())
    assert payload2["model"] == llm_mod.MODEL


def test_vision_model_defaults_to_model():
    """VISION_MODEL binding falls back to MODEL via getenv default."""
    import llm as llm_mod

    src = Path(llm_mod.__file__).read_text(encoding="utf-8")
    assert 'VISION_MODEL = os.getenv("LLM_VISION_MODEL", MODEL)' in src
    assert isinstance(llm_mod.VISION_MODEL, str) and llm_mod.VISION_MODEL
    assert isinstance(llm_mod.MODEL, str) and llm_mod.MODEL

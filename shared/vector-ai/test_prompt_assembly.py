"""Tests for thinner memory inject (select_memories_for_turn + _build_memory_section)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Allow imports when pytest is run from VectorIntelligence root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _patch_deps(monkeypatch, tmp_path):
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


def _set_face(monkeypatch, face):
    # prompt_assembly binds current_face at import; patch the local name.
    import prompt_assembly

    monkeypatch.setattr(prompt_assembly, "current_face", lambda: face)


# --- select_memories_for_turn -------------------------------------------------


def test_many_personal_facts_capped(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    for i in range(20):
        store.remember(f"fact number {i} about hobbies", face_id=1, face_name="Cam")

    from memory_retrieve import select_memories_for_turn

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    sel = select_memories_for_turn(
        store, face, "hello there", max_personal=8, max_shared=4, max_mentions=3
    )
    assert len(sel.personal) <= 8
    assert len(sel.personal) == 8


def test_query_tokens_prefer_matching_fact(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("likes pizza on Fridays", face_id=1, face_name="Cam")
    store.remember("owns a golden retriever named Biscuit", face_id=1, face_name="Cam")
    store.remember("works on firmware for robots", face_id=1, face_name="Cam")

    from memory_retrieve import select_memories_for_turn

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    sel = select_memories_for_turn(
        store,
        face,
        "how is Biscuit the retriever doing?",
        max_personal=1,
        max_shared=0,
        max_mentions=0,
    )
    assert len(sel.personal) == 1
    assert "Biscuit" in sel.personal[0].text


def test_stranger_does_not_leak_personal(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("Cam's wifi password is secret", face_id=1, face_name="Cam")
    store.remember("Cam hates cilantro", face_id=1, face_name="Cam")
    store.remember("wifi network is VectorNet", face_id=None, face_name=None)

    from memory_retrieve import select_memories_for_turn

    stranger = {"face_id": 99, "name": "stranger", "is_stranger": True}
    sel = select_memories_for_turn(
        store,
        stranger,
        "what's the wifi password?",
        max_personal=8,
        max_shared=4,
        max_mentions=3,
    )
    assert sel.personal == []
    assert sel.mentions == []
    # Shared may still appear when selected
    assert any("VectorNet" in m.text or "wifi" in m.text.lower() for m in sel.shared) or (
        len(sel.shared) <= 4
    )
    personal_texts = [m.text for m in sel.personal]
    assert "Cam hates cilantro" not in personal_texts
    assert not any("cilantro" in m.text for m in sel.personal)


def test_shared_facts_can_appear_when_selected(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("household calendar is on the fridge", face_id=None)
    store.remember("likes tea", face_id=1, face_name="Cam")

    from memory_retrieve import select_memories_for_turn

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    sel = select_memories_for_turn(
        store,
        face,
        "where is the household calendar?",
        max_personal=8,
        max_shared=4,
        max_mentions=3,
    )
    assert any("calendar" in m.text.lower() for m in sel.shared)


def test_empty_store_blank_slate_one_liner(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import _build_memory_section

    section = _build_memory_section(face, "hi")
    assert "blank slate" in section.lower() or "NO long-term facts" in section
    assert "Cam" in section
    assert "{{remember||" in section or "remember||" in section
    # Old multi-paragraph SAVE essay must be gone
    assert "when in doubt, save" not in section.lower()
    assert "sharp friend" not in section.lower()


def test_build_memory_section_caps_bullets(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    for i in range(15):
        store.remember(f"personal detail {i} unique token{i}", face_id=1, face_name="Cam")
    for i in range(10):
        store.remember(f"shared household item {i}", face_id=None)

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import _build_memory_section

    section = _build_memory_section(face, "hello")
    # Count bullet lines under personal section only loosely: total "-" bullets
    # should be well under the old firehose (100+) and near 8+4.
    bullets = [ln for ln in section.splitlines() if ln.strip().startswith("- ")]
    assert len(bullets) <= 8 + 4 + 3  # personal + shared + mentions caps


def test_stranger_section_no_personal_bullets(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("Cam's private project codename Nebula", face_id=1, face_name="Cam")
    store.remember("shared: trash day is Tuesday", face_id=None)

    stranger = {"face_id": 0, "name": "unknown", "is_stranger": True}
    _set_face(monkeypatch, stranger)

    from prompt_assembly import _build_memory_section

    section = _build_memory_section(stranger, "tell me secrets")
    assert "stranger" in section.lower()
    assert "Nebula" not in section
    assert "Things you know about Cam" not in section


def test_build_memory_section_uses_passed_face_not_current(tmp_path, monkeypatch):
    """Memory section must honor the resolved face, not re-query current_face."""
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("Cam drinks espresso", face_id=1, face_name="Cam")
    # Live face is stranger, but prepare_messages passed Cam as effective face
    _set_face(monkeypatch, {"face_id": 99, "name": "x", "is_stranger": True})

    from prompt_assembly import _build_memory_section

    cam = {"face_id": 1, "name": "Cam", "is_stranger": False}
    section = _build_memory_section(cam, "coffee?")
    assert "Cam" in section
    assert "espresso" in section
    assert "stranger" not in section.lower()

def test_prepare_messages_passes_user_text_for_ranking(tmp_path, monkeypatch):
    store = _patch_deps(monkeypatch, tmp_path)
    store.remember("owns a cat named Whiskers", face_id=1, face_name="Cam")
    store.remember("collects vintage stamps", face_id=1, face_name="Cam")

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import prepare_messages

    messages = [
        SimpleNamespace(role="system", content="wirepod cmds"),
        SimpleNamespace(role="user", content="How is Whiskers the cat?"),
    ]
    out = prepare_messages(messages, face)
    system = out[0]["content"]
    assert "Whiskers" in system
    # With max_personal default 8 both may appear; ranking prefers match.
    # At least the matching fact is present.
    assert "cat named Whiskers" in system or "Whiskers" in system


def test_extract_last_user_text_multimodal():
    from prompt_assembly import _extract_last_user_text

    messages = [
        SimpleNamespace(
            role="user",
            content=[
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:x"}},
            ],
        )
    ]
    assert _extract_last_user_text(messages) == "what is this"


# --- Work Day silent-tag teaching (Phase 1) ---------------------------------


def test_work_line_absent_when_workday_disabled(tmp_path, monkeypatch):
    """Workday disabled (default _patch_deps) → no _WORK_LINE teach string.

    Persona may still mention workAfternoon (leave persona as-is); this test
    only asserts the gated _WORK_LINE is not injected into memory/system.
    """
    _patch_deps(monkeypatch, tmp_path)
    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import _WORK_LINE, _build_memory_section, prepare_messages

    section = _build_memory_section(face, "hi")
    assert _WORK_LINE not in section
    assert "WORK DAY (silent tags" not in section

    messages = [
        SimpleNamespace(role="system", content="wirepod cmds"),
        SimpleNamespace(role="user", content="hello"),
    ]
    out = prepare_messages(messages, face)
    system = out[0]["content"]
    assert _WORK_LINE not in system
    assert "WORK DAY (silent tags" not in system


def test_work_line_present_when_workday_enabled(tmp_path, monkeypatch):
    """Workday enabled + non-None runtime → stable prefix has WORK silent tags."""
    import deps

    store = _patch_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": True})(),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "BEHAVIOR_RUNTIME",
        type("RT", (), {"workday": object()})(),
        raising=False,
    )
    # Continuity may be touched by prepare_messages context note; stub empty strip.
    monkeypatch.setattr(
        deps,
        "_continuity",
        type("Cont", (), {"day_strip": staticmethod(lambda _d: "")})(),
        raising=False,
    )

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import _build_memory_section, prepare_messages

    section = _build_memory_section(face, "hi")
    assert "{{workAfternoon||yes}}" in section
    assert "{{workAfternoon||no}}" in section
    assert "{{workResume}}" in section
    assert "WORK DAY" in section

    messages = [
        SimpleNamespace(role="system", content="wirepod cmds"),
        SimpleNamespace(role="user", content="hello"),
    ]
    out = prepare_messages(messages, face)
    system = out[0]["content"]
    assert "{{workAfternoon||yes}}" in system
    assert "WORK DAY" in system
    # Memory store was used; keep lint quiet if unused path changes.
    assert store is not None


def test_context_note_emit_when_awaiting_answer(tmp_path, monkeypatch):
    """LATE_CHECK strip with 'awaiting answer' → context note gets emit instruction."""
    import deps
    from datetime import datetime

    _patch_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": True})(),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "BEHAVIOR_RUNTIME",
        type("RT", (), {"workday": object()})(),
        raising=False,
    )
    late_strip = (
        "Work day: asked if user is working this afternoon (awaiting answer)."
    )
    monkeypatch.setattr(
        deps,
        "_continuity",
        type("Cont", (), {"day_strip": staticmethod(lambda _d: late_strip)})(),
        raising=False,
    )

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    _set_face(monkeypatch, face)

    from prompt_assembly import _build_context_note, prepare_messages

    note = _build_context_note(face, None, datetime.now())
    assert "awaiting answer" in note
    assert "{{workAfternoon||yes}}" in note
    assert "{{workAfternoon||no}}" in note
    assert "emit" in note.lower()

    messages = [
        SimpleNamespace(role="system", content="wirepod cmds"),
        SimpleNamespace(role="user", content="yes I'm working"),
    ]
    out = prepare_messages(messages, face)
    # Context note rides on the latest user turn, not system.
    user_content = out[-1]["content"]
    assert isinstance(user_content, str)
    assert "{{workAfternoon||yes}}" in user_content
    assert "awaiting answer" in user_content


def test_context_note_no_emit_when_not_awaiting(tmp_path, monkeypatch):
    """Non-late strip still injects status but no emit-tag sentence."""
    import deps
    from datetime import datetime

    _patch_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": True})(),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "BEHAVIOR_RUNTIME",
        type("RT", (), {"workday": object()})(),
        raising=False,
    )
    working_strip = "Work day: user is working."
    monkeypatch.setattr(
        deps,
        "_continuity",
        type("Cont", (), {"day_strip": staticmethod(lambda _d: working_strip)})(),
        raising=False,
    )

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}

    from prompt_assembly import _build_context_note

    note = _build_context_note(face, None, datetime.now())
    assert "working" in note.lower()
    # WORK line is in memory/system, not this emit-only sentence.
    assert "If they affirm work / half-day, emit" not in note


def test_context_note_uses_last_spoke_not_stale_last_seen(tmp_path, monkeypatch):
    """Recent soul last_spoke must not claim multi-hour silence via last_seen."""
    import deps
    from datetime import datetime

    from soul import Soul

    _patch_deps(monkeypatch, tmp_path)
    now = datetime.now()
    now_ts = now.timestamp()
    soul = Soul()
    soul.note_spoke(now_ts - 30, source="chat", text="hi")
    # Visual last_seen looks ancient — must not drive "last spoke".
    soul.last_seen_at = now_ts - 14 * 3600
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    prior = {
        "last_seen": now_ts - 14 * 3600,
        "interaction_count": 20,
        "last_convo_summary": "widgets",
        "last_convo_at": now_ts - 14 * 3600,
    }

    from prompt_assembly import _build_context_note

    note = _build_context_note(face, prior, now)
    # Mid-turn / moments-ago speech → no session "last spoke" nag.
    assert "last spoke with Cam" not in note
    assert "14" not in note or "hour" not in note.lower()
    # Must not invent long silence from last_seen while speech is fresh.
    assert "first thing you've said" not in note


def test_context_note_last_spoke_when_gap_large(tmp_path, monkeypatch):
    """When last_spoke is hours ago, context may say last spoke relative."""
    import deps
    from datetime import datetime

    from soul import Soul

    _patch_deps(monkeypatch, tmp_path)
    now = datetime.now()
    now_ts = now.timestamp()
    soul = Soul()
    soul.note_spoke(now_ts - 2 * 3600, source="chat", text="bye")
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    prior = {
        "last_seen": now_ts - 2 * 3600,
        "interaction_count": 10,
        "last_convo_summary": "coffee",
        "last_convo_at": now_ts - 2 * 3600,
    }

    from prompt_assembly import _build_context_note

    note = _build_context_note(face, prior, now)
    assert "last spoke with Cam" in note
    assert "hour" in note.lower()

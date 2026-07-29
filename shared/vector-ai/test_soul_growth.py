"""Phase 3.5 soul growth: dream job, full llm_view, tags, debounce."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from soul.growth import (
    DREAM_DEBOUNCE_S,
    parse_dream_job_json,
    run_dream_job,
)
from soul.growth_prompts import DREAM_SYSTEM, build_dream_user_message


def _make_soul(tmp_path):
    from behaviors.presence import PresenceCache
    from soul import Soul

    soul = Soul(presence=PresenceCache())
    soul._persist_path = tmp_path / "soul.json"
    soul._auto_persist = False
    return soul


def test_dream_system_prompt_aspire_grow_goals():
    """GrowBot-adapted system prompt contains growth ethos keywords."""
    lower = DREAM_SYSTEM.lower()
    assert "aspire" in lower
    assert "grow" in lower
    assert "goals" in lower
    assert "despair" in lower or "spiral" in lower
    assert "last_night_i_dreamed_about" in DREAM_SYSTEM
    assert "three_goals_i_currently_hold" in DREAM_SYSTEM


def test_parse_dream_job_json_tolerant():
    raw = (
        "Here you go:\n```json\n"
        '{"last_night_i_dreamed_about":"gears",'
        '"three_goals_i_currently_hold":["a","b","c"],'
        '"tomorrow_try":"look left",'
        '"growth_note":"more play"}\n```'
    )
    data = parse_dream_job_json(raw)
    assert data is not None
    assert data["last_night_i_dreamed_about"] == "gears"
    assert data["three_goals_i_currently_hold"] == ["a", "b", "c"]
    assert parse_dream_job_json("not json") is None
    assert parse_dream_job_json("") is None
    assert parse_dream_job_json(None) is None  # type: ignore[arg-type]


def test_run_dream_job_full_soul_in_messages(tmp_path, monkeypatch):
    """Mock llm: messages include full soul long keys + It has been phrases."""
    soul = _make_soul(tmp_path)
    now = time.time()
    soul.note_face(now - 600, face_id=1, name="Cam", is_stranger=False, source="t")
    soul.note_person(now - 600, source="t")
    soul.note_spoke(now - 120, source="chat", text="hi")
    soul.set_mood("quietly curious", now - 300)
    soul.set_goals(["old1", "old2", "old3"], force_save=False)
    soul.set_dreamed_about("yesterday fog", force_save=False)

    captured = {}

    async def _fake_llm(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps(
            {
                "last_night_i_dreamed_about": (
                    "I dreamed Cam's mug drifted left of the monitor."
                ),
                "three_goals_i_currently_hold": [
                    "notice mug moves",
                    "ask about the project",
                    "try a desk joke",
                ],
                "tomorrow_try": "glance left",
                "growth_note": "day was social",
            }
        )

    monkeypatch.setattr("soul.growth.llm_chat_once", _fake_llm)

    result = asyncio.run(run_dream_job(soul, now=now, reason="test", force=True))
    assert result.get("skipped") is False
    user = captured["messages"][1]["content"]
    assert "It has been" in user or "It has never" in user
    assert "three_goals_i_currently_hold" in user
    assert "time_since_i_last_spoke_aloud" in user
    assert "FULL SOUL SNAPSHOT" in user
    assert "last_night_i_dreamed_about" in user
    assert captured["kwargs"].get("tag") == "soul_dream"
    assert captured["kwargs"].get("model") is not None

    assert soul.last_night_i_dreamed_about.startswith("I dreamed Cam")
    assert soul.goals[0] == "notice mug moves"
    assert len(soul.goals) == 3
    assert soul.last_dream_job_at == float(now)
    assert (tmp_path / "soul.json").exists()


def test_run_dream_job_debounce(tmp_path, monkeypatch):
    """Second dream within 20h window is skipped without LLM call."""
    soul = _make_soul(tmp_path)
    now = time.time()
    soul._last_dream_job_at = now - 60  # 1 minute ago

    called = {"n": 0}

    async def _should_not(*a, **k):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr("soul.growth.llm_chat_once", _should_not)

    result = asyncio.run(
        run_dream_job(soul, now=now, reason="test2", force=False)
    )
    assert result["skipped"] is True
    assert result["reason"] == "debounce"
    assert called["n"] == 0

    async def _ok(*a, **k):
        called["n"] += 1
        return json.dumps(
            {
                "last_night_i_dreamed_about": "forced",
                "three_goals_i_currently_hold": ["x", "y", "z"],
                "tomorrow_try": "",
                "growth_note": "",
            }
        )

    monkeypatch.setattr("soul.growth.llm_chat_once", _ok)
    result2 = asyncio.run(
        run_dream_job(soul, now=now, reason="forced", force=True)
    )
    assert result2.get("skipped") is False
    assert called["n"] == 1
    assert soul.last_night_i_dreamed_about == "forced"


def test_debounce_window_elapsed(tmp_path, monkeypatch):
    soul = _make_soul(tmp_path)
    now = time.time()
    soul._last_dream_job_at = now - DREAM_DEBOUNCE_S - 10

    async def _ok(*a, **k):
        return json.dumps(
            {
                "last_night_i_dreamed_about": "after night",
                "three_goals_i_currently_hold": ["1", "2", "3"],
                "tomorrow_try": "",
                "growth_note": "",
            }
        )

    monkeypatch.setattr("soul.growth.llm_chat_once", _ok)
    result = asyncio.run(
        run_dream_job(soul, now=now, reason="elapsed", force=False)
    )
    assert result.get("skipped") is False
    assert soul.last_night_i_dreamed_about == "after night"


def test_parse_and_strip_soul_growth_tags(tmp_path, monkeypatch):
    """{{soulDream}} and {{soulGoals||…}} stripped; goals applied."""
    import deps
    from response_cleanup import clean_response, parse_soul_growth_tags

    soul = _make_soul(tmp_path)
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)

    text = (
        "I dreamed of wires. {{soulDream}} "
        "New aims: {{soulGoals||learn faces||watch the mug||try a joke}} ok"
    )
    cleaned, actions = parse_soul_growth_tags(text)
    assert "{{soulDream}}" not in cleaned
    assert "{{soulGoals" not in cleaned
    kinds = [a[0] for a in actions]
    assert "dream" in kinds
    assert "goals" in kinds
    goals_action = next(a for a in actions if a[0] == "goals")
    assert goals_action[1] == ["learn faces", "watch the mug", "try a joke"]

    out = clean_response("Sure. {{soulGoals||a||b||c}} done")
    assert "soulGoals" not in out
    assert soul.goals == ["a", "b", "c"]
    assert "Sure" in out and "done" in out


def test_soul_growth_line_when_soul_wired(tmp_path, monkeypatch):
    """Teach line present in memory section when deps.SOUL is set."""
    import deps
    from memory import MemoryStore
    from prompt_assembly import _SOUL_GROWTH_LINE, _build_memory_section

    store = MemoryStore(tmp_path / "mem.db")
    monkeypatch.setattr(deps, "MEMORY", store, raising=False)
    monkeypatch.setattr(
        deps, "BEHAVIOR_RUNTIME", SimpleNamespace(workday=None), raising=False
    )
    monkeypatch.setattr(
        deps,
        "_workday_cfg",
        type("C", (), {"tz": None, "enabled": False})(),
        raising=False,
    )
    monkeypatch.setattr(deps, "SOUL", None, raising=False)

    face = {"face_id": 1, "name": "Cam", "is_stranger": False}
    section = _build_memory_section(face, "hi")
    assert _SOUL_GROWTH_LINE not in section
    assert "SOUL GROWTH" not in section

    soul = _make_soul(tmp_path)
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)
    section2 = _build_memory_section(face, "hi")
    assert _SOUL_GROWTH_LINE in section2
    assert "{{soulDream}}" in section2
    assert "{{soulGoals||" in section2


def test_http_dream_route(tmp_path, monkeypatch):
    """POST /v1/soul/dream handler returns applied dream + goals (no service import)."""
    import deps
    from routes.soul import SoulDreamRequest, soul_dream

    soul = _make_soul(tmp_path)
    monkeypatch.setattr(deps, "SOUL", soul, raising=False)

    async def _ok(messages, **kwargs):
        user = messages[1]["content"]
        assert "three_goals_i_currently_hold" in user
        assert "It has been" in user or "It has never" in user
        return json.dumps(
            {
                "last_night_i_dreamed_about": "http dream",
                "three_goals_i_currently_hold": ["g1", "g2", "g3"],
                "tomorrow_try": "blink",
                "growth_note": "ops",
            }
        )

    monkeypatch.setattr("soul.growth.llm_chat_once", _ok)

    body = asyncio.run(soul_dream(SoulDreamRequest(force=True)))
    assert body.get("skipped") is False
    assert body["last_night_i_dreamed_about"].startswith("http dream")
    assert body["three_goals_i_currently_hold"] == ["g1", "g2", "g3"]
    assert soul.goals == ["g1", "g2", "g3"]


def test_build_dream_user_message_includes_json():
    view = {
        "three_goals_i_currently_hold": ["a", "b", "c"],
        "time_since_i_last_spoke_aloud": (
            "It has been 12 minutes since I last spoke aloud."
        ),
    }
    msg = build_dream_user_message(view)
    assert "FULL SOUL SNAPSHOT" in msg
    assert "It has been 12 minutes" in msg
    assert "three_goals_i_currently_hold" in msg

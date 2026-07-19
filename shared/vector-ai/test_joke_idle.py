#!/usr/bin/env python3
"""Unit tests for joke_idle FSM: config, continuity, serve, refill (mocked LLM), FSM.

No network, robot, or real LLM. Time is injected; SQLite uses temp files.

Run:
  python3 -m pytest shared/vector-ai/test_joke_idle.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from behaviors.config import JokeConfig, WorkdayConfig, load_joke_config, load_runtime_config
from behaviors.continuity import ContinuityStore
from behaviors.joke_idle import JokeIdleBehavior, JOKE_IDLE_ID
from behaviors.joke_sources import (
    _MAX_GEN_BATCHES,
    joke_hash,
    parse_json_array,
    pop_line,
    refill_joke_queue,
)
from behaviors.runtime import BehaviorRuntime
from behaviors.types import BehaviorContext, FaceIdentity, PresenceSnapshot


def check(name: str, cond: bool) -> None:
    """Assert with a label (pytest-friendly; replaces old SystemExit runner)."""
    assert cond, name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _joke_cfg(**kwargs) -> JokeConfig:
    base = dict(
        enabled=True,
        audience="anyone",
        priority=15,
        min_dwell_s=1200,
        cooldown_s=9000,
        max_per_day=4,
        question_ratio=0.6,
        identity_reject_cooldown_s=1800,
        tz=ZoneInfo("UTC"),
        refill_interval_s=43200,
        queue_target=50,
        refill_low_watermark=30,
        min_score=0.55,
        novelty_min=0.4,
        generate_model="",
        critic_model="",
        seed_file="joke_seeds.txt",
        curated_ratio=0.5,
    )
    base.update(kwargs)
    return JokeConfig(**base)


def _push(store: ContinuityStore, text: str, kind: str, score: float = 0.8, now: float = 1.0) -> bool:
    return store.joke_queue_push(text, kind, "test", score, joke_hash(text), now)


def _joke_ctx(
    behavior: JokeIdleBehavior,
    now: float,
    *,
    occupied: bool = True,
    face: FaceIdentity | None = None,
    identity_fresh: bool = False,
    updated_at: float | None = None,
    voice_recent: bool = False,
) -> BehaviorContext:
    if updated_at is None:
        # Dwell fully met relative to last_spoke_at default 0.
        updated_at = now - float(behavior.cfg.min_dwell_s)
    local_dt = datetime.fromtimestamp(now, tz=behavior.cfg.tz)
    snap = PresenceSnapshot(
        occupied=occupied,
        face=face,
        face_ts=now if face is not None else 0.0,
        updated_at=updated_at,
        voice_recent=voice_recent,
    )
    return BehaviorContext(
        now=now,
        local_dt=local_dt,
        presence=snap,
        quiet=False,
        config=behavior.cfg,
        identity_fresh=identity_fresh,
    )


def _apply_speak(r) -> None:
    if r.on_speak_allowed is not None:
        r.on_speak_allowed()


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------

def test_joke_config_defaults() -> None:
    cfg = load_joke_config({})
    check("empty env no raise", cfg is not None)
    check("default disabled", cfg.enabled is False)
    check("default audience known", cfg.audience == "known")
    check("default priority 15", cfg.priority == 15)
    check("default min_dwell 1200", cfg.min_dwell_s == 1200)
    check("default cooldown 9000", cfg.cooldown_s == 9000)
    check("default max_per_day 4", cfg.max_per_day == 4)
    check("default question_ratio 0.6", abs(cfg.question_ratio - 0.6) < 1e-9)
    check("default queue_target 50", cfg.queue_target == 50)
    check("default watermark 30", cfg.refill_low_watermark == 30)
    check("default min_score 0.55", abs(cfg.min_score - 0.55) < 1e-9)


def test_joke_config_bad_audience_and_numeric() -> None:
    cfg = load_joke_config({
        "JOKE_ENABLED": "1",
        "JOKE_AUDIENCE": "everyone",  # invalid → known
        "JOKE_PRIORITY": "not-an-int",
        "JOKE_MIN_DWELL_S": "??",
        "JOKE_QUESTION_RATIO": "nope",
        "JOKE_MIN_SCORE": "abc",
        "JOKE_QUEUE_TARGET": "",
    })
    check("bad audience → known", cfg.audience == "known")
    check("bad priority → default", cfg.priority == 15)
    check("bad min_dwell → default", cfg.min_dwell_s == 1200)
    check("bad question_ratio → default", abs(cfg.question_ratio - 0.6) < 1e-9)
    check("bad min_score → default", abs(cfg.min_score - 0.55) < 1e-9)
    check("empty queue_target → default", cfg.queue_target == 50)
    check("truthy enabled", cfg.enabled is True)

    cfg2 = load_joke_config({"JOKE_AUDIENCE": "anyone"})
    check("valid anyone", cfg2.audience == "anyone")
    cfg3 = load_joke_config({"JOKE_AUDIENCE": "KNOWN"})
    check("valid known case-insensitive", cfg3.audience == "known")


# ---------------------------------------------------------------------------
# 2. Continuity
# ---------------------------------------------------------------------------

def test_joke_continuity_push_pop_daily() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        now = 1_800_000_000.0

        # Fresh daily zeros
        d = store.joke_load_daily("2026-07-18")
        check("fresh daily count 0", d["count"] == 0)
        check("fresh last_spoke 0", d["last_spoke_at"] == 0.0)
        check("fresh last_reject 0", d["last_reject_at"] == 0.0)

        t_low = "low score line about coffee"
        t_hi = "high score line about desks"
        t_mid = "mid score line about mugs"
        check("push low", _push(store, t_low, "joke", 0.2, now) is True)
        check("push hi", _push(store, t_hi, "joke", 0.95, now) is True)
        check("push mid", _push(store, t_mid, "joke", 0.5, now) is True)
        check("queue len 3", store.joke_queue_len() == 3)

        # Dedupe vs queue
        check("dedupe queue", _push(store, t_hi, "joke", 0.99, now) is False)
        check("queue still 3", store.joke_queue_len() == 3)

        # Atomic pop highest score first
        row = store.joke_queue_pop("joke")
        check("pop highest text", row is not None and row["text"] == t_hi)
        check("pop highest score", abs(row["score"] - 0.95) < 1e-9)
        check("queue len 2 after pop", store.joke_queue_len() == 2)

        # Mark served; dedupe vs served
        store.joke_mark_served(joke_hash(t_hi), t_hi, "joke", now)
        check("dedupe served", _push(store, t_hi, "joke", 0.99, now + 1) is False)

        # commit_spoke increments; mark_reject sets timestamp
        store.joke_commit_spoke("2026-07-18", now)
        d = store.joke_load_daily("2026-07-18")
        check("commit count 1", d["count"] == 1)
        check("commit last_spoke", d["last_spoke_at"] == now)
        store.joke_commit_spoke("2026-07-18", now + 10)
        d = store.joke_load_daily("2026-07-18")
        check("commit count 2", d["count"] == 2)
        check("commit last_spoke updated", d["last_spoke_at"] == now + 10)

        store.joke_mark_reject("2026-07-18", now + 50)
        d = store.joke_load_daily("2026-07-18")
        check("reject timestamp", d["last_reject_at"] == now + 50)
        check("reject does not wipe count", d["count"] == 2)

        # Other date still fresh zeros
        d2 = store.joke_load_daily("2026-07-19")
        check("other date zeros", d2 == {"count": 0, "last_spoke_at": 0.0, "last_reject_at": 0.0})


# ---------------------------------------------------------------------------
# 3. Serve (pop_line + parse_json_array)
# ---------------------------------------------------------------------------

def test_pop_line_ratio_fallback_served_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        cfg = _joke_cfg(question_ratio=0.6)
        now = 1000.0

        # Empty → None
        check("empty None", pop_line(store, cfg, 0.0) is None)

        q_text = "What would you do with a free hour?"
        j_text = "My plans left early without me."
        _push(store, q_text, "question", 0.9, now)
        _push(store, j_text, "joke", 0.9, now)

        # roll < ratio → prefer question
        line = pop_line(store, cfg, question_ratio_roll=0.1)
        check("prefer question", line is not None and line["kind"] == "question")
        check("question text", line["text"] == q_text)
        check("served after pop", joke_hash(q_text) in store.joke_all_served_hashes())
        check("queue lost question", store.joke_queue_len("question") == 0)

        # Only joke left; prefer question falls back to joke
        line2 = pop_line(store, cfg, question_ratio_roll=0.0)
        check("fallback to joke", line2 is not None and line2["kind"] == "joke")
        check("fallback text", line2["text"] == j_text)
        check("both served", len(store.joke_all_served_hashes()) == 2)
        check("queue empty", store.joke_queue_len() == 0)
        check("empty after drain", pop_line(store, cfg, 0.5) is None)

        # Prefer joke path when roll >= ratio
        _push(store, "Another joke line unique.", "joke", 0.8, now)
        _push(store, "Another question unique?", "question", 0.8, now)
        line3 = pop_line(store, cfg, question_ratio_roll=0.9)
        check("prefer joke on high roll", line3 is not None and line3["kind"] == "joke")


def test_parse_json_array_tolerant() -> None:
    fenced = 'Here you go:\n```json\n[{"text": "a", "kind": "joke"}]\n```\n'
    out = parse_json_array(fenced)
    check("fenced parse", len(out) == 1 and out[0]["text"] == "a")

    prefixed = 'Sure! [{"id": 1, "score": 0.8, "seen_before": false}] trailing noise'
    out2 = parse_json_array(prefixed)
    check("prefixed parse", len(out2) == 1 and out2[0]["id"] == 1)

    check("garbage empty", parse_json_array("not json at all") == [])
    check("empty string", parse_json_array("") == [])
    check("none-ish", parse_json_array(None) == [])  # type: ignore[arg-type]
    check("object not array", parse_json_array('{"a": 1}') == [])
    mixed = '[{"ok": true}, "skip", 3, {"b": 2}]'
    out3 = parse_json_array(mixed)
    check("non-dicts dropped", len(out3) == 2 and "ok" in out3[0])


# ---------------------------------------------------------------------------
# 4. Refill (mocked LLM)
# ---------------------------------------------------------------------------

def test_refill_filters_and_target() -> None:
    """Above-threshold novel non-dup banked; low score / seen_before / dups dropped."""
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        empty_seeds = Path(td) / "empty.txt"
        empty_seeds.write_text("# none\n", encoding="utf-8")
        cfg = _joke_cfg(
            queue_target=3,
            refill_low_watermark=10,
            curated_ratio=0.0,
            seed_file=str(empty_seeds),
            min_score=0.55,
            novelty_min=0.4,
        )

        good_a = "Observational one-liner about silent keyboards."
        good_b = "What if meetings paid us for silence instead?"
        low = "Boring low score filler line xyz."
        seen = "Classic overused joke everyone knows."
        # Pre-serve a near-duplicate of good_a tokens to force novelty drop on a twin
        twin = "Observational one-liner about silent keyboards again."
        store.joke_mark_served(joke_hash("served baseline unique zebra"), "served baseline unique zebra", "joke", 1.0)

        calls: list[str] = []

        async def fake_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            calls.append(tag or "?")
            if tag == "joke_gen" or (tag is None and calls.count("?") % 2 == 1):
                return json.dumps([
                    {"id": 0, "text": good_a, "kind": "joke", "style": "deadpan", "seed": "desk"},
                    {"id": 1, "text": good_b, "kind": "question", "style": "curious", "seed": "mug"},
                    {"id": 2, "text": low, "kind": "joke", "style": "flat", "seed": "pen"},
                    {"id": 3, "text": seen, "kind": "joke", "style": "cliche", "seed": "lamp"},
                    {"id": 4, "text": twin, "kind": "joke", "style": "repeat", "seed": "desk"},
                    {"id": 5, "text": good_a, "kind": "joke", "style": "dup", "seed": "desk"},  # hash dup
                ])
            return json.dumps([
                {"id": 0, "score": 0.92, "seen_before": False},
                {"id": 1, "score": 0.88, "seen_before": False},
                {"id": 2, "score": 0.10, "seen_before": False},
                {"id": 3, "score": 0.99, "seen_before": True},
                {"id": 4, "score": 0.90, "seen_before": False},
                {"id": 5, "score": 0.91, "seen_before": False},
            ])

        added = asyncio.run(refill_joke_queue(store, cfg, llm_chat_once=fake_llm))
        check("llm was called", len(calls) >= 2)
        # Target 3 but only 2 truly bankable (good_a, good_b); twin may or may not pass novelty
        # vs empty served of similar tokens — served baseline is dissimilar so twin may bank.
        # At least good_a and good_b; low and seen must not be present.
        qlen = store.joke_queue_len()
        check("added > 0", added > 0)
        check("queue at most target", qlen <= cfg.queue_target)
        texts = []
        # Drain queue to inspect
        while True:
            row = store.joke_queue_pop()
            if row is None:
                break
            texts.append(row["text"])
        check("good_a banked", good_a in texts)
        check("good_b banked", good_b in texts)
        check("low score not banked", low not in texts)
        check("seen_before not banked", seen not in texts)
        # Duplicate of good_a: at most one copy
        check("no hash duplicate of good_a", texts.count(good_a) == 1)


def test_refill_garbage_and_llm_down_curated() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        seeds = Path(td) / "seeds.txt"
        # Enough curated lines to hit a small target
        lines = [f"joke\tCurated line number {i} unique words here." for i in range(20)]
        seeds.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cfg = _joke_cfg(
            queue_target=8,
            refill_low_watermark=20,
            curated_ratio=0.5,
            seed_file=str(seeds),
        )

        async def garbage_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            return "totally not json {{{"

        added = asyncio.run(refill_joke_queue(store, cfg, llm_chat_once=garbage_llm))
        check("garbage no raise added>0", added > 0)
        check("garbage curated top-up", store.joke_queue_len() == cfg.queue_target)

        # LLM-down (raises): still fills from curated
        store2 = ContinuityStore(Path(td) / "joke2.db")
        async def boom_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            raise RuntimeError("network down")

        added2 = asyncio.run(refill_joke_queue(store2, cfg, llm_chat_once=boom_llm))
        check("llm-down no raise", added2 > 0)
        check("llm-down curated fills target", store2.joke_queue_len() == cfg.queue_target)


def test_refill_watermark_noop_and_batch_cap() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        empty_seeds = Path(td) / "empty.txt"
        empty_seeds.write_text("# none\n", encoding="utf-8")

        # Seed queue above watermark
        for i in range(5):
            _push(store, f"Preseeded line {i} with unique content.", "joke", 0.7, float(i))
        cfg = _joke_cfg(
            queue_target=50,
            refill_low_watermark=3,  # queue 5 > 3 → no-op
            curated_ratio=0.0,
            seed_file=str(empty_seeds),
        )
        calls: list[str] = []

        async def counting_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            calls.append(tag or "?")
            return "[]"

        added = asyncio.run(refill_joke_queue(store, cfg, llm_chat_once=counting_llm))
        check("watermark no-op added 0", added == 0)
        check("watermark no LLM calls", len(calls) == 0)
        check("watermark queue unchanged", store.joke_queue_len() == 5)

        # Batch cap: empty responses, below watermark → at most _MAX_GEN_BATCHES gen calls
        store2 = ContinuityStore(Path(td) / "joke2.db")
        cfg2 = _joke_cfg(
            queue_target=10,
            refill_low_watermark=10,
            curated_ratio=0.0,
            seed_file=str(empty_seeds),
        )
        calls2: list[str] = []

        async def empty_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            calls2.append(tag or "?")
            return "[]"

        added2 = asyncio.run(refill_joke_queue(store2, cfg2, llm_chat_once=empty_llm))
        check("empty gen added 0", added2 == 0)
        gen_calls = sum(1 for t in calls2 if t in ("joke_gen", "?"))
        # Soft-empty gen returns before critic; expect <= _MAX_GEN_BATCHES gen calls
        check("batch cap respected", len(calls2) <= _MAX_GEN_BATCHES)
        check("at least one gen attempt", len(calls2) >= 1)
        check("gen call count bound", gen_calls <= _MAX_GEN_BATCHES)


def test_refill_stops_at_queue_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        empty_seeds = Path(td) / "empty.txt"
        empty_seeds.write_text("# none\n", encoding="utf-8")
        cfg = _joke_cfg(
            queue_target=2,
            refill_low_watermark=10,
            curated_ratio=0.0,
            seed_file=str(empty_seeds),
            min_score=0.5,
            novelty_min=0.0,
        )

        async def rich_llm(messages, *, model=None, temperature=1.0, tag=None, **kw):
            if tag == "joke_critic":
                return json.dumps([
                    {"id": i, "score": 0.9, "seen_before": False} for i in range(6)
                ])
            return json.dumps([
                {
                    "id": i,
                    "text": f"Unique generated joke line number {i} about office life.",
                    "kind": "joke" if i % 2 == 0 else "question",
                    "style": "deadpan",
                    "seed": "desk",
                }
                for i in range(6)
            ])

        added = asyncio.run(refill_joke_queue(store, cfg, llm_chat_once=rich_llm))
        check("stops at target added", added == 2)
        check("queue exactly target", store.joke_queue_len() == 2)


# ---------------------------------------------------------------------------
# 5–7. FSM
# ---------------------------------------------------------------------------

def test_fsm_happy_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        cfg = _joke_cfg(
            enabled=True,
            audience="anyone",
            min_dwell_s=1200,
            cooldown_s=9000,
            max_per_day=4,
        )
        b = JokeIdleBehavior(cfg, store)
        now = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        date = "2026-07-18"
        line_text = "Desk silence is my favorite genre."
        _push(store, line_text, "joke", 0.9, now)

        r = b.tick(_joke_ctx(b, now, occupied=True, identity_fresh=False))
        check("happy speak non-empty", isinstance(r.speak, str) and len(r.speak) > 0)
        check("happy contains line", line_text in r.speak)
        check("happy on_speak_allowed set", r.on_speak_allowed is not None)
        check("happy reason spoke", r.debug.get("reason") == "spoke")
        check("happy no need_identity", r.need_identity is False)

        # Daily still zero until commit
        d = store.joke_load_daily(date)
        check("pre-commit count 0", d["count"] == 0)

        _apply_speak(r)
        d = store.joke_load_daily(date)
        check("post-commit count 1", d["count"] == 1)
        check("post-commit last_spoke", d["last_spoke_at"] == now)

        # Cooldown blocks next tick (re-seed queue so line is not the reason)
        _push(store, "Another unique joke for second tick.", "joke", 0.9, now)
        r2 = b.tick(_joke_ctx(b, now + 60, occupied=True, identity_fresh=False))
        check("cooldown blocks speak", r2.speak == "")
        check("cooldown reason", r2.debug.get("reason") == "cooldown")


def test_fsm_denied_speech_no_commit() -> None:
    """CRITICAL: not calling on_speak_allowed must leave daily state unchanged."""
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        cfg = _joke_cfg(enabled=True, audience="anyone", min_dwell_s=100, cooldown_s=9000)
        b = JokeIdleBehavior(cfg, store)
        now = datetime(2026, 7, 18, 15, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        date = "2026-07-18"
        _push(store, "A line that will be denied by arbiter sim.", "joke", 0.9, now)

        r = b.tick(_joke_ctx(b, now, occupied=True, identity_fresh=False))
        check("denied setup spoke planned", len(r.speak) > 0)
        check("denied setup callback present", r.on_speak_allowed is not None)
        # Do NOT call on_speak_allowed — simulates arbiter deny
        d = store.joke_load_daily(date)
        check("denied count unchanged", d["count"] == 0)
        check("denied last_spoke unchanged", d["last_spoke_at"] == 0.0)

        # A later tick (dwell still ok, no cooldown burned) can still plan speech
        # if we re-seed (first tick already popped the line)
        _push(store, "Second attempt line after deny.", "joke", 0.9, now)
        r2 = b.tick(_joke_ctx(b, now + 30, occupied=True, identity_fresh=False))
        check("after deny can speak again", len(r2.speak) > 0)
        d2 = store.joke_load_daily(date)
        check("still no commit without allow", d2["count"] == 0 and d2["last_spoke_at"] == 0.0)


def test_fsm_identity_gating() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        now = datetime(2026, 7, 18, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        date = "2026-07-18"
        _push(store, "Identity gate test line unique.", "joke", 0.9, now)

        # known + stale → need_identity, no speak
        b_known = JokeIdleBehavior(
            _joke_cfg(audience="known", min_dwell_s=100, identity_reject_cooldown_s=1800),
            store,
        )
        r1 = b_known.tick(
            _joke_ctx(b_known, now, occupied=True, face=None, identity_fresh=False)
        )
        check("known+stale need_identity", r1.need_identity is True)
        check("known+stale no speak", r1.speak == "")
        check("known+stale reason", r1.debug.get("reason") == "requesting_identity")

        # known + stranger → reject marked in body, no speak
        stranger = FaceIdentity(face_id=-3, name="", is_stranger=True)
        r2 = b_known.tick(
            _joke_ctx(
                b_known, now + 1, occupied=True, face=stranger, identity_fresh=True
            )
        )
        check("stranger no speak", r2.speak == "")
        check("stranger no need_id", r2.need_identity is False)
        check("stranger reason", r2.debug.get("reason") == "stranger_suppressed")
        d = store.joke_load_daily(date)
        check("stranger reject marked", d["last_reject_at"] == now + 1)

        # known + reject cooldown + stale → silent without probing
        r3 = b_known.tick(
            _joke_ctx(
                b_known,
                now + 1 + 100,
                occupied=True,
                face=None,
                identity_fresh=False,
            )
        )
        check("cooldown no need_identity", r3.need_identity is False)
        check("cooldown no speak", r3.speak == "")
        check("cooldown reason", r3.debug.get("reason") == "id_reject_cooldown")

        # anyone → never need_identity (even stale)
        b_any = JokeIdleBehavior(
            _joke_cfg(audience="anyone", min_dwell_s=100),
            store,
        )
        # Re-seed (previous ticks did not pop because identity blocked before serve
        # except stranger path also didn't pop — good, line still there? stranger
        # returned before pop; stale returned before pop; cooldown before pop.
        # So queue still has the line.
        r4 = b_any.tick(
            _joke_ctx(b_any, now + 500, occupied=True, face=None, identity_fresh=False)
        )
        check("anyone never need_identity", r4.need_identity is False)
        check("anyone can speak without face", len(r4.speak) > 0)


# ---------------------------------------------------------------------------
# 8. Feature flag off
# ---------------------------------------------------------------------------

def test_feature_flag_off() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "joke.db")
        b = JokeIdleBehavior(_joke_cfg(enabled=False), store)
        check("enabled() False", b.enabled() is False)
        now = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        _push(store, "Should not speak when disabled.", "joke", 0.9, now)
        # Even if someone calls tick, enabled gate is on the runtime; behavior
        # itself still runs if called — runtime must not register.
        rcfg = load_runtime_config({"BEHAVIORS_ENABLED": "joke_idle"})
        wcfg = WorkdayConfig(enabled=False, tz=ZoneInfo("UTC"))
        rt = BehaviorRuntime(
            rcfg,
            wcfg,
            store,
            joke_cfg=_joke_cfg(enabled=False),
        )
        check("disabled joke not registered", not any(getattr(x, "id", None) == JOKE_IDLE_ID for x in rt.behaviors))

        rt2 = BehaviorRuntime(
            rcfg,
            wcfg,
            store,
            joke_cfg=_joke_cfg(enabled=True),
        )
        check(
            "enabled joke registered",
            any(getattr(x, "id", None) == JOKE_IDLE_ID for x in rt2.behaviors),
        )

        # Missing from BEHAVIORS_ENABLED
        rcfg3 = load_runtime_config({"BEHAVIORS_ENABLED": "workday"})
        rt3 = BehaviorRuntime(
            rcfg3,
            wcfg,
            store,
            joke_cfg=_joke_cfg(enabled=True),
        )
        check(
            "not in enabled list → absent",
            not any(getattr(x, "id", None) == JOKE_IDLE_ID for x in rt3.behaviors),
        )


# ---------------------------------------------------------------------------
# Anti-patterns (greppable contracts)
# ---------------------------------------------------------------------------

def test_anti_patterns() -> None:
    root = Path(__file__).resolve().parent
    idle_src = (root / "behaviors" / "joke_idle.py").read_text(encoding="utf-8")
    check("tick file has no llm_chat_once", "llm_chat_once" not in idle_src)
    check("tick file has no httpx", "httpx" not in idle_src)
    check("tick file has no AsyncClient", "AsyncClient" not in idle_src)

    sources_path = root / "behaviors" / "joke_sources.py"
    tree = ast.parse(sources_path.read_text(encoding="utf-8"))
    top_service = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "service" or alias.name.startswith("service."):
                    top_service = True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "service" or node.module.startswith("service.")):
                top_service = True
    check("no top-level service import in joke_sources", top_service is False)


if __name__ == "__main__":
    import pytest
    import sys

    raise SystemExit(pytest.main([__file__, "-q", *sys.argv[1:]]))

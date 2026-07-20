"""Joke idle sourcing — serve side + pure prompt/JSON helpers + async refill.

Serve (`pop_line`) is synchronous and pure SQLite: no network, no LLM.
Refill (`refill_joke_queue` / `_joke_refill_loop`) runs off the tick path only.
Do not top-level import service (import cycles); inject llm_chat_once or lazy-import.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .logutil import blog, short

_log = logging.getLogger("behaviors.joke_sources")
_TAG = "joke_sources"

# Per-refill generation bound — avoid runaway API spend if critic rejects everything.
_MAX_GEN_BATCHES = 12
_GEN_BATCH_SIZE = 6

# Injectable LLM callable: async def fake(messages, *, model=None, temperature=1.0, **kw) -> str
LlmChatOnce = Callable[..., Awaitable[str]]

# Concrete noun seeds for generation prompts (rotating pool).
_SEED_NOUNS: tuple[str, ...] = (
    "desk",
    "mug",
    "stapler",
    "keyboard",
    "monitor",
    "cable",
    "notebook",
    "chair",
    "lamp",
    "calendar",
    "whiteboard",
    "pen",
    "drawer",
    "window",
    "plant",
    "backpack",
    "headphones",
    "mouse",
    "charger",
    "sticky note",
    "paperclip",
    "binder",
    "clock",
    "shelf",
    "printer",
    "badge",
    "thermos",
    "umbrella",
    "scissors",
    "folder",
    "router",
    "webcam",
    "speaker",
    "cushion",
    "coaster",
    "tape",
    "ruler",
    "envelope",
    "bookmark",
    "flashlight",
    "remote",
    "doorbell",
    "mirror",
    "hanger",
    "sock",
    "button",
    "ladder",
    "broom",
    "bucket",
    "wrench",
    "screw",
    "nail",
    "hammer",
    "sandpaper",
    "magnet",
    "battery",
    "fuse",
    "switch",
    "valve",
    "hinge",
    "knob",
    "lever",
    "spring",
    "pulley",
    "chain",
    "rope",
    "bucket",
    "funnel",
    "sieve",
    "spatula",
    "ladle",
    "whisk",
    "grater",
    "colander",
    "cutting board",
    "toaster",
    "kettle",
    "fridge magnet",
    "doormat",
    "shoe",
)

_ALLOWED_KINDS = frozenset({"joke", "question"})


def joke_hash(text: str) -> str:
    """Normalize (strip, lower, collapse whitespace) then sha1 hex digest."""
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def load_curated(path: str) -> list[dict]:
    """Parse KIND\\tTEXT lines; skip blanks/#; return [{'text','kind'}].

    Never raises: missing file → []; bad lines are skipped.
    """
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    for raw in lines:
        try:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" not in line:
                continue
            kind, text = line.split("\t", 1)
            kind = kind.strip().lower()
            text = text.strip()
            if kind not in _ALLOWED_KINDS or not text:
                continue
            out.append({"text": text, "kind": kind})
        except Exception:
            continue
    return out


def pop_line(store: Any, cfg: Any, question_ratio_roll: float) -> Optional[dict]:
    """Pure SQLite serve. Prefer 'question' if roll < cfg.question_ratio else 'joke'.

    Pop preferred kind; if empty fall back to the other; if both empty return None.
    On success mark_served and return {'text','kind','source'}. NO network, NO LLM.
    """
    ratio = float(getattr(cfg, "question_ratio", 0.6))
    if question_ratio_roll < ratio:
        preferred, fallback = "question", "joke"
    else:
        preferred, fallback = "joke", "question"

    row = store.joke_queue_pop(preferred)
    used_fallback = False
    if row is None:
        row = store.joke_queue_pop(fallback)
        used_fallback = row is not None
    if row is None:
        blog(
            _TAG,
            f"pop_line: queue empty (wanted {preferred}, then {fallback})",
            verbose=True,
        )
        return None

    text = row["text"]
    kind = row["kind"]
    source = row.get("source") or "unknown"
    text_hash = row.get("text_hash") or joke_hash(text)

    store.joke_mark_served(text_hash, text, kind, time.time())
    fb = " (fallback kind)" if used_fallback else ""
    blog(
        _TAG,
        f"pop_line: served {kind}/{source}{fb}: {short(text)!r}",
        verbose=True,
    )
    return {"text": text, "kind": kind, "source": source}


def build_generate_messages(
    seeds: list[str], want_jokes: int, want_questions: int
) -> list[dict]:
    """Chat messages for the joke/question generator (STRICT JSON array response)."""
    seed_list = ", ".join(seeds) if seeds else "(none)"
    system = (
        "You write short deadpan one-liners and conversation-starter questions "
        "for a small sarcastic desk robot. "
        "Reply with a STRICT JSON array only — no prose, no markdown fences, no commentary. "
        "Each element schema: "
        '{"text": string, "kind": "joke"|"question", "style": string, "seed": string}. '
        "Hard rules: each text under 20 words, self-contained, guest-safe and workplace-safe. "
        "FORBIDDEN: puns about atoms/skeletons/scientists; 'why did the X cross the Y'; "
        "knock-knock jokes; puns; long setups. "
        "Prefer anti-jokes, observational one-liners, and absurd literalism. "
        "You MUST use the given seeds (assign each item a seed from the list)."
    )
    user = (
        f"Seeds (use these concrete nouns): {seed_list}\n"
        f"Generate exactly {int(want_jokes)} items with kind \"joke\" and "
        f"{int(want_questions)} items with kind \"question\". "
        "Return only the JSON array."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_critic_messages(candidates: list[dict]) -> list[dict]:
    """Chat messages for the ruthless comedy critic (STRICT JSON array response)."""
    # Ensure candidates carry ids for the critic schema when missing.
    numbered: list[dict] = []
    for i, c in enumerate(candidates or []):
        if not isinstance(c, dict):
            continue
        item = dict(c)
        if "id" not in item:
            item["id"] = i
        numbered.append(item)

    system = (
        "You are a ruthless comedy editor and cynical taste filter. "
        "Rate each candidate 0.0–1.0 on originality and surprise only. "
        "Anything resembling a common, known, or cliché joke must score below 0.3. "
        "Reply with a STRICT JSON array only — no prose, no markdown fences. "
        'Each element schema: {"id": number, "score": number, "seen_before": boolean}.'
    )
    user = (
        "Score these candidates:\n"
        + json.dumps(numbered, ensure_ascii=False)
        + "\nReturn only the JSON array of verdicts."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_json_array(raw: str) -> list[dict]:
    """Tolerant parse: strip fences/prose, extract first JSON array; [] on failure."""
    if not raw or not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []

    # Strip common markdown fences (```json ... ``` or ``` ... ```).
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # Prefer locating the first '[' … matching ']' and json.loads.
    start = text.find("[")
    if start < 0:
        return []

    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []

    snippet = text[start : end + 1]
    try:
        data = json.loads(snippet)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    if not isinstance(data, list):
        return []
    # Keep only dict-like objects; coerce non-dicts away for refill safety.
    out: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def random_seeds(n: int) -> list[str]:
    """Return n concrete noun seeds from a built-in rotating list."""
    n = max(0, int(n))
    if n == 0:
        return []
    if n >= len(_SEED_NOUNS):
        # With replacement only if asked for more than the pool.
        return [random.choice(_SEED_NOUNS) for _ in range(n)]
    return random.sample(list(_SEED_NOUNS), n)


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens for lexical novelty."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def novelty(text: str, served_texts: list[str]) -> float:
    """Return 0..1, higher = more novel.

    Approach used: LEXICAL token Jaccard (A5 fallback — no embeddings in codebase).
    novelty = 1 - max_jaccard(tokens(text), tokens(served_i)) over served_texts.
    Empty served_texts → 1.0.
    """
    if not served_texts:
        return 1.0
    tokens = _tokens(text)
    if not tokens:
        # Empty/non-token text: treat as maximally similar to nothing useful → low novelty
        # but still defined; against empty served we already returned 1.0.
        return 0.0
    max_j = 0.0
    for s in served_texts:
        st = _tokens(s)
        if not st:
            continue
        inter = len(tokens & st)
        union = len(tokens | st)
        if union > 0:
            j = inter / union
            if j > max_j:
                max_j = j
    return 1.0 - max_j


def _resolve_seed_path(seed_file: str) -> str:
    """Absolute path, or relative to this module directory (behaviors/)."""
    if not seed_file:
        seed_file = "joke_seeds.txt"
    if os.path.isabs(seed_file):
        return seed_file
    return str(Path(__file__).resolve().parent / seed_file)


def _resolve_llm(llm_chat_once: Optional[LlmChatOnce]) -> Optional[LlmChatOnce]:
    """Prefer injected callable; else lazy-import service.llm_chat_once (no top-level import)."""
    if llm_chat_once is not None:
        return llm_chat_once
    try:
        # Lazy import: service is heavy and would create a cycle if imported at module load.
        from service import llm_chat_once as _llm  # type: ignore

        return _llm
    except Exception as e:
        _log.warning("joke refill: llm_chat_once unavailable (%s); curated-only fill", e)
        return None


def _push_if_novel(
    store: Any,
    text: str,
    kind: str,
    source: str,
    score: float,
    served_texts: list[str],
    novelty_min: float,
    now: float,
) -> bool:
    """Hash-dedupe via store push; drop low-novelty. Returns True if banked."""
    text = (text or "").strip()
    kind = (kind or "").strip().lower()
    if not text or kind not in _ALLOWED_KINDS:
        return False
    nov = novelty(text, served_texts)
    if nov < novelty_min:
        blog(
            _TAG,
            f"reject (low novelty {nov:.2f}<{novelty_min}): {short(text)!r}",
            verbose=True,
        )
        return False
    h = joke_hash(text)
    ok = bool(store.joke_queue_push(text, kind, source, float(score), h, now))
    if ok:
        blog(
            _TAG,
            f"banked {kind}/{source} score={score:.2f} nov={nov:.2f}: {short(text)!r}",
            verbose=True,
        )
    else:
        blog(
            _TAG,
            f"reject (dup hash): {short(text)!r}",
            verbose=True,
        )
    return ok


def _fill_curated(
    store: Any,
    curated: list[dict],
    *,
    target: int,
    limit: int,
    served_texts: list[str],
    novelty_min: float,
    now: float,
) -> int:
    """Push up to `limit` curated lines while queue_len < target. Returns count added."""
    if limit <= 0:
        return 0
    added = 0
    for item in curated:
        if added >= limit:
            break
        if store.joke_queue_len() >= target:
            break
        text = item.get("text") or ""
        kind = item.get("kind") or "joke"
        if _push_if_novel(
            store, text, kind, "curated", 1.0, served_texts, novelty_min, now
        ):
            added += 1
    return added


async def _generate_batch(
    store: Any,
    cfg: Any,
    llm: LlmChatOnce,
    *,
    target: int,
    served_texts: list[str],
    now: float,
) -> tuple[int, bool]:
    """One generate→critic→filter→push cycle.

    Returns (lines_added, hard_fail). hard_fail=True means the LLM raised;
    caller should stop generation and fall back to curated. Never raises.
    """
    if store.joke_queue_len() >= target:
        return 0, False

    q_ratio = float(getattr(cfg, "question_ratio", 0.6))
    want_questions = max(0, int(round(_GEN_BATCH_SIZE * q_ratio)))
    want_jokes = max(0, _GEN_BATCH_SIZE - want_questions)
    if want_jokes == 0 and want_questions == 0:
        want_jokes, want_questions = 3, 3

    gen_model = (getattr(cfg, "generate_model", None) or "") or None
    critic_model = (getattr(cfg, "critic_model", None) or "") or None
    min_score = float(getattr(cfg, "min_score", 0.55))
    novelty_min = float(getattr(cfg, "novelty_min", 0.4))

    try:
        seeds = random_seeds(4)
        blog(
            _TAG,
            f"generating batch: want jokes={want_jokes} questions={want_questions} "
            f"seeds={seeds} model={gen_model or 'default'}",
        )
        messages = build_generate_messages(seeds, want_jokes, want_questions)
        raw = await llm(
            messages, model=gen_model, temperature=1.0, tag="joke_gen"
        )
        cands = parse_json_array(raw if isinstance(raw, str) else "")
        if not cands:
            blog(_TAG, "generate returned no parseable candidates")
            return 0, False

        # Ensure stable ids for critic matching (position if missing).
        for i, c in enumerate(cands):
            if "id" not in c:
                c["id"] = i

        blog(_TAG, f"critic scoring {len(cands)} candidate(s) model={critic_model or 'default'}")
        scored_raw = await llm(
            build_critic_messages(cands),
            model=critic_model,
            temperature=0.2,
            tag="joke_critic",
        )
        verdicts = parse_json_array(scored_raw if isinstance(scored_raw, str) else "")
        by_id: dict[Any, dict] = {}
        for v in verdicts:
            if "id" in v:
                by_id[v["id"]] = v

        added = 0
        skipped_score = 0
        skipped_seen = 0
        skipped_no_verdict = 0
        for c in cands:
            if store.joke_queue_len() >= target:
                break
            cid = c.get("id")
            v = by_id.get(cid)
            if v is None:
                skipped_no_verdict += 1
                continue
            try:
                score = float(v.get("score", 0.0))
            except (TypeError, ValueError):
                skipped_no_verdict += 1
                continue
            seen = v.get("seen_before", False)
            if seen is True or (
                isinstance(seen, str) and seen.strip().lower() in ("true", "1", "yes")
            ):
                skipped_seen += 1
                blog(
                    _TAG,
                    f"critic rejected seen_before: {short(c.get('text'))!r}",
                    verbose=True,
                )
                continue
            if score < min_score:
                skipped_score += 1
                blog(
                    _TAG,
                    f"critic score {score:.2f}<{min_score}: {short(c.get('text'))!r}",
                    verbose=True,
                )
                continue
            text = c.get("text") or ""
            kind = (c.get("kind") or "joke").strip().lower()
            if _push_if_novel(
                store, text, kind, "generated", score, served_texts, novelty_min, now
            ):
                added += 1
        blog(
            _TAG,
            f"gen batch done: banked={added}/{len(cands)} "
            f"(low_score={skipped_score}, seen={skipped_seen}, "
            f"no_verdict={skipped_no_verdict})",
        )
        return added, False
    except Exception as e:
        _log.warning("joke gen batch failed: %s", e)
        blog(_TAG, f"gen batch hard-fail: {e}")
        return 0, True


async def refill_joke_queue(
    store: Any,
    cfg: Any,
    llm_chat_once: Optional[LlmChatOnce] = None,
) -> int:
    """Top queue to cfg.queue_target ONLY when joke_queue_len() <= cfg.refill_low_watermark.

    Else return 0 with NO LLM calls.
    Mix ~cfg.curated_ratio from curated, remainder from LLM.
    LLM-down invariant: curated may fill 100% if generation fails — fill curated FIRST
    (then always attempt curated top-up after generation).
    Returns number of lines added. Never raise out for a single bad batch.
    Max generation batches e.g. 12 per refill.
    """
    try:
        current = int(store.joke_queue_len())
        watermark = int(getattr(cfg, "refill_low_watermark", 30))
        target = int(getattr(cfg, "queue_target", 50))
        if current > watermark:
            blog(
                _TAG,
                f"refill skip: queue={current} > watermark={watermark}",
                verbose=True,
            )
            return 0
        need = target - current
        if need <= 0:
            return 0

        blog(
            _TAG,
            f"refill start: queue={current} watermark={watermark} "
            f"target={target} need={need}",
        )

        curated_ratio = float(getattr(cfg, "curated_ratio", 0.5))
        curated_ratio = max(0.0, min(1.0, curated_ratio))
        novelty_min = float(getattr(cfg, "novelty_min", 0.4))
        now = time.time()

        served_texts = list(store.joke_all_served_texts() or [])
        seed_path = _resolve_seed_path(str(getattr(cfg, "seed_file", "joke_seeds.txt")))
        curated = load_curated(seed_path)
        random.shuffle(curated)
        blog(
            _TAG,
            f"curated pool={len(curated)} seed_file={seed_path} "
            f"served_history={len(served_texts)}",
            verbose=True,
        )

        # Preference quota for curated; remainder preferred for generation.
        curated_quota = int(round(need * curated_ratio))
        # Always leave room for at least some curated attempt when need > 0 and ratio > 0.
        if curated_ratio > 0 and need > 0 and curated_quota == 0:
            curated_quota = 1
        if curated_ratio < 1.0 and need > 1 and curated_quota >= need:
            curated_quota = need - 1

        added = 0

        # 1) Curated FIRST (LLM-down invariant — partial fill even if gen never runs).
        n_cur = _fill_curated(
            store,
            curated,
            target=target,
            limit=curated_quota,
            served_texts=served_texts,
            novelty_min=novelty_min,
            now=now,
        )
        added += n_cur
        if n_cur:
            blog(_TAG, f"curated pass banked {n_cur} (quota={curated_quota})")

        # 2) Generated remainder (bounded batches).
        remaining = target - store.joke_queue_len()
        if remaining > 0:
            llm = _resolve_llm(llm_chat_once)
            if llm is not None:
                blog(_TAG, f"LLM generate for remaining {remaining} slot(s)")
                for batch_i in range(_MAX_GEN_BATCHES):
                    if store.joke_queue_len() >= target:
                        break
                    n, hard_fail = await _generate_batch(
                        store,
                        cfg,
                        llm,
                        target=target,
                        served_texts=served_texts,
                        now=now,
                    )
                    added += n
                    # LLM raised: stop generation; curated top-up fills the rest.
                    if hard_fail:
                        blog(
                            _TAG,
                            f"stopping LLM gen after batch {batch_i + 1} (hard fail)",
                        )
                        break
                    # Soft empty (garbage / all rejected): keep trying up to cap
                    # with fresh seeds.
            else:
                blog(_TAG, "LLM unavailable — curated-only fill")

        # 3) Curated top-up to target (100% curated allowed when gen failed/short).
        remaining = target - store.joke_queue_len()
        if remaining > 0:
            n_top = _fill_curated(
                store,
                curated,
                target=target,
                limit=remaining,
                served_texts=served_texts,
                novelty_min=novelty_min,
                now=now,
            )
            added += n_top
            if n_top:
                blog(_TAG, f"curated top-up banked {n_top}")

        blog(
            _TAG,
            f"refill done: added={added} queue_now={store.joke_queue_len()}/{target}",
        )
        return added
    except Exception as e:
        _log.exception("refill_joke_queue failed: %s", e)
        blog(_TAG, f"refill failed: {e}")
        return 0


async def _joke_refill_loop(store: Any, cfg: Any) -> None:
    """await asyncio.sleep(90) settle, then loop:
       try: await refill_joke_queue(store, cfg)
       except Exception: log, continue
       await asyncio.sleep(cfg.refill_interval_s)
    """
    blog(_TAG, "refill loop starting (90s settle before first check)")
    await asyncio.sleep(90)
    while True:
        try:
            n = await refill_joke_queue(store, cfg)
            if n:
                q = store.joke_queue_len()
                _log.info("joke refill banked %s line(s); queue=%s", n, q)
                blog(_TAG, f"refill loop: banked {n}; queue={q}")
            else:
                blog(
                    _TAG,
                    f"refill loop: nothing to add (queue={store.joke_queue_len()})",
                    verbose=True,
                )
        except Exception as e:
            _log.exception("joke refill loop iteration failed")
            blog(_TAG, f"refill loop error: {e}")
        interval = int(getattr(cfg, "refill_interval_s", 43200) or 43200)
        if interval < 1:
            interval = 1
        blog(_TAG, f"refill loop sleep {interval}s", verbose=True)
        await asyncio.sleep(interval)


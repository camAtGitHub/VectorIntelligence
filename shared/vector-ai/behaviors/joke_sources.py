"""Joke idle sourcing — serve side + pure prompt/JSON helpers.

Serve (`pop_line`) is synchronous and pure SQLite: no network, no LLM.
Refill pipeline (async LLM loop) is added in TASK-05; do not import service here.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any, Optional

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
    if row is None:
        row = store.joke_queue_pop(fallback)
    if row is None:
        return None

    text = row["text"]
    kind = row["kind"]
    source = row.get("source") or "unknown"
    text_hash = row.get("text_hash") or joke_hash(text)

    store.joke_mark_served(text_hash, text, kind, time.time())
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

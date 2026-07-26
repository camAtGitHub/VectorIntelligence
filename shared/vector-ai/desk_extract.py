"""Extract durable desk object → location pairs from scene text.

Server-side only: cheap SUMMARY_MODEL call with NO persona. Used after chat
vision observations and ambient novelty notes so ambient can treat moves as
news without re-roasting static clutter.

Failures return [] — same spirit as conversation summary swallow.
"""
from __future__ import annotations

import json
import re
from typing import List, Tuple

from llm import LLM_SUMMARY_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, llm_chat_once
from logging_util import print  # noqa: F401

_EXTRACT_SYSTEM = (
    "Extract durable desk objects and their short locations from the scene "
    "description. Return ONLY a JSON array of objects, each with keys "
    "\"name\" and \"location\" (both short strings). "
    "Include only physical objects on or near the desk that have a stable "
    "place (mug, keyboard, headphones, plush, lamp, etc.). "
    "Skip people, hands, faces, clothing, lighting, empty space, and vague "
    "clutter without a clear item. "
    "location is free text like \"left of monitor\" or \"on the keyboard\". "
    "If nothing qualifies, return []. No markdown, no commentary."
)


def parse_desk_items_json(raw: str) -> List[Tuple[str, str]]:
    """Tolerant parse of [{"name":"...","location":"..."}, ...] → (name, loc).

    Mirrors joke_sources.parse_json_array spirit: strip fences/prose, extract
    first JSON array; [] on any failure.
    """
    if not raw or not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

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

    out: List[Tuple[str, str]] = []
    seen_keys: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        location = item.get("location")
        if not isinstance(name, str) or not isinstance(location, str):
            continue
        name = re.sub(r"\s+", " ", name.strip())
        location = re.sub(r"\s+", " ", location.strip())
        if not name or not location:
            continue
        key = name.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((name, location))
    return out


async def extract_desk_items_from_scene_text(
    description: str,
) -> List[Tuple[str, str]]:
    """LLM extract of (name, location) pairs. No persona. [] on failure."""
    description = (description or "").strip()
    if not description:
        return []
    # Skip pure PRESENCE protocol lines if they ever leak in.
    if description.upper().startswith("PRESENCE"):
        return []

    prompt = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": description[:800]},
    ]
    try:
        raw = await llm_chat_once(
            prompt,
            model=SUMMARY_MODEL,
            temperature=LLM_SUMMARY_TEMPERATURE,
            top_p=0.9,
            timeout=_llm_timeout(connect=8.0, read=30.0),
            max_tokens=256,
            tag="desk_extract",
        )
        items = parse_desk_items_json(raw if isinstance(raw, str) else "")
        if items:
            print(f"[desk] extract {len(items)} item(s) from scene text")
        return items
    except Exception as e:
        print(f"[desk] extract failed: {e}")
        return []


async def extract_and_upsert_desk_items(description: str, memory) -> None:
    """Background-safe: extract items and upsert into MemoryStore. Swallows errors."""
    if memory is None:
        return
    try:
        items = await extract_desk_items_from_scene_text(description)
        for name, location in items:
            try:
                memory.upsert_desk_item(name, location)
            except Exception as e:
                print(f"[desk] upsert failed for {name!r}: {e}")
    except Exception as e:
        print(f"[desk] extract_and_upsert failed: {e}")

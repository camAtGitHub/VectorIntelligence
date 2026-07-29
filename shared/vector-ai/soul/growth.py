"""Soul growth jobs: dream + goals via SUMMARY_MODEL with full llm_view.

Debounced (default 20h) so automatic paths run at most about once per night.
Ops HTTP may pass force=True to override.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from llm import SUMMARY_MODEL, _llm_timeout, llm_chat_once
from logging_util import print  # noqa: F401

from .growth_prompts import DREAM_SYSTEM, build_dream_user_message

# At most one dream job per ~local night (auto + chat tags).
DREAM_DEBOUNCE_S = 20 * 3600

# Dream temperature a bit higher than pure summary extract.
_DREAM_TEMPERATURE = 0.7
_GOALS_TEMPERATURE = 0.4


def parse_dream_job_json(raw: str) -> Optional[dict]:
    """Tolerant parse of dream-job JSON object. None on failure."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    if start < 0:
        return None

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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None

    snippet = text[start : end + 1]
    try:
        data = json.loads(snippet)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _apply_dream_payload(soul: Any, data: dict, *, now: float) -> dict:
    """Apply parsed JSON to soul; set last_dream_job_at; save_now."""
    dream = data.get("last_night_i_dreamed_about")
    if not isinstance(dream, str):
        dream = str(dream or "")
    dream = dream.strip()

    goals_raw = data.get("three_goals_i_currently_hold")
    if not isinstance(goals_raw, list):
        goals_raw = []
    goals = [str(g or "").strip() for g in goals_raw[:3]]
    while len(goals) < 3:
        goals.append("")

    tomorrow = data.get("tomorrow_try")
    if isinstance(tomorrow, str):
        tomorrow = tomorrow.strip()
    else:
        tomorrow = ""
    # Fold tiny next-try into dream if present and dream is short.
    if tomorrow and dream and tomorrow.lower() not in dream.lower():
        if len(dream) < 400:
            dream = f"{dream.rstrip('.')} — try: {tomorrow}."

    growth_note = data.get("growth_note")
    if not isinstance(growth_note, str):
        growth_note = ""

    soul.set_dreamed_about(dream, force_save=False)
    soul.set_goals(goals, force_save=False)
    soul._last_dream_job_at = float(now)
    if hasattr(soul, "append_log"):
        try:
            soul.append_log(now, f"dreamed: {(dream or '')[:80]}")
        except Exception:
            pass
    if hasattr(soul, "save_now"):
        soul.save_now()

    return {
        "skipped": False,
        "last_night_i_dreamed_about": soul.last_night_i_dreamed_about,
        "three_goals_i_currently_hold": list(soul.goals),
        "tomorrow_try": tomorrow,
        "growth_note": growth_note.strip()[:80],
        "last_dream_job_at": float(soul.last_dream_job_at),
    }


async def run_dream_job(
    soul: Any,
    *,
    now: float,
    reason: str,
    force: bool = False,
    temperature: float = _DREAM_TEMPERATURE,
    tag: str = "soul_dream",
) -> dict:
    """Build full llm_view; call SUMMARY_MODEL; apply JSON; save_now().

    Returns applied fields, or ``{"skipped": True, "reason": "debounce"|...}``.
    Always uses detail=\"full\" (never lite).
    """
    if soul is None:
        return {"skipped": True, "reason": "no_soul"}

    last = float(getattr(soul, "last_dream_job_at", 0.0) or 0.0)
    if not force and last > 0 and (float(now) - last) < DREAM_DEBOUNCE_S:
        print(
            f"[soul] dream job skipped (debounce, "
            f"last={last:.0f}, now={now:.0f}, reason={reason!r})"
        )
        return {
            "skipped": True,
            "reason": "debounce",
            "last_dream_job_at": last,
            "trigger_reason": reason,
        }

    # FULL soul view — long keys + It has been phrases (anti-pattern: lite).
    if hasattr(soul, "llm_view"):
        view = soul.llm_view(float(now), detail="full")
    else:
        from .view import llm_view

        view = llm_view(soul, float(now), detail="full")

    user_content = build_dream_user_message(view)
    messages = [
        {"role": "system", "content": DREAM_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    try:
        raw = await llm_chat_once(
            messages,
            model=SUMMARY_MODEL,
            temperature=float(temperature),
            top_p=0.95,
            timeout=_llm_timeout(connect=8.0, read=60.0),
            max_tokens=512,
            tag=tag,
        )
    except Exception as e:
        print(f"[soul] dream job LLM failed ({reason}): {e}")
        return {"skipped": True, "reason": "llm_error", "error": str(e)}

    data = parse_dream_job_json(raw if isinstance(raw, str) else "")
    if not data:
        print(f"[soul] dream job parse failed ({reason}): {raw!r:.200}")
        return {"skipped": True, "reason": "parse_error"}

    applied = _apply_dream_payload(soul, data, now=float(now))
    applied["trigger_reason"] = reason
    print(
        f"[soul] dream job applied ({reason}): "
        f"dream={applied.get('last_night_i_dreamed_about', '')!r:.80} "
        f"goals={applied.get('three_goals_i_currently_hold')!r}"
    )
    return applied


async def run_reflect_goals_job(
    soul: Any,
    *,
    now: float,
    reason: str = "reflect_goals",
    force: bool = False,
) -> dict:
    """Goals-focused thin wrapper: same full soul + slightly cooler temperature."""
    return await run_dream_job(
        soul,
        now=now,
        reason=reason,
        force=force,
        temperature=_GOALS_TEMPERATURE,
        tag="soul_goals",
    )


def maybe_schedule_dream_job(
    soul: Any,
    *,
    now: float,
    reason: str,
    force: bool = False,
) -> bool:
    """Fire-and-forget create_task for dream job. Returns True if scheduled."""
    import asyncio

    if soul is None:
        return False

    async def _run() -> None:
        try:
            await run_dream_job(soul, now=now, reason=reason, force=force)
        except Exception as e:
            print(f"[soul] background dream job failed ({reason}): {e}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        print(f"[soul] dream schedule skipped (no event loop, reason={reason!r})")
        return False
    loop.create_task(_run())
    return True

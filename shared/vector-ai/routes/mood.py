"""Mood state HTTP + background reflection loop."""
import asyncio
from datetime import datetime

from fastapi import APIRouter

import deps
from llm import SUMMARY_MODEL, _llm_timeout, llm_chat_once
from logging_util import print  # noqa: F401
from process_state import (
    MOOD_REFLECT_INTERVAL,
    _ambient_state,
    _mood_state,
)
from prompt_assembly import _relative_time, _time_of_day
from response_cleanup import strip_markdown

router = APIRouter()

_MOOD_SYSTEM = (
    "You track the inner state of Vector, a small desktop robot with a dry, "
    "sardonic character - somewhere between Marvin from Hitchhiker's Guide, "
    "Bender from Futurama, and Stephen Fry. Given a short digest of how his "
    "day has gone, reply with his CURRENT state of mind as ONE short phrase: "
    "third person, lowercase, no final period, a mood rather than a list of "
    "events (e.g. 'restless after a long quiet stretch', or 'quietly content "
    "after a sociable evening'). Plain text only, under 12 words."
)


async def _reflect_mood() -> None:
    """Distil the day so far into a one-line mood and persist it.
    Uses SUMMARY_MODEL (cheap/fast) via the OpenAI-compatible chat API."""
    MEMORY = deps.MEMORY
    now_dt = datetime.now()
    bits = [
        f"It is {now_dt.strftime('%A')} {_time_of_day(now_dt)}, "
        f"{now_dt.strftime('%I:%M %p')}."
    ]
    obs = MEMORY.list_observations(limit=6, max_age_seconds=12 * 3600)
    if obs:
        bits.append("Things he has noticed recently: "
                    + "; ".join(o["text"] for o in reversed(obs)) + ".")
    else:
        bits.append("He has noticed nothing new for a good while - "
                    "a static, uneventful stretch.")
    convo = MEMORY.latest_conversation()
    if convo and convo.get("last_convo_at"):
        gap = now_dt.timestamp() - convo["last_convo_at"]
        line = f"His last conversation was {_relative_time(gap)}"
        if convo.get("last_convo_summary"):
            line += f", about: {convo['last_convo_summary']}"
        bits.append(line + ".")
    else:
        bits.append("He has not had a real conversation in a long time.")
    if _ambient_state["quiet"]:
        bits.append("He has been asked to stay quiet.")
    if _mood_state["text"]:
        bits.append(f"A little while ago his mood was: {_mood_state['text']}.")

    try:
        mood = await llm_chat_once(
            [
                {"role": "system", "content": _MOOD_SYSTEM},
                {"role": "user", "content": " ".join(bits)},
            ],
            model=SUMMARY_MODEL,
            temperature=0.7,
            top_p=0.95,
            timeout=_llm_timeout(read=60.0),
            max_tokens=64,
            tag="mood",
        )
        mood = strip_markdown(mood).strip().strip('"').strip().rstrip(".").strip()
        if mood:
            _mood_state["text"]    = mood
            _mood_state["updated"] = datetime.now().timestamp()
            MEMORY.set_state("mood", mood)
            print(f"[mood] -> {mood!r}")
    except Exception as e:
        print(f"[mood] reflection failed: {e}")


async def _mood_loop() -> None:
    await asyncio.sleep(60)  # let the stack settle before the first reflection
    while True:
        await _reflect_mood()
        await asyncio.sleep(MOOD_REFLECT_INTERVAL)


@router.get("/v1/mood")
async def mood_get():
    return dict(_mood_state)


@router.post("/v1/mood/reflect")
async def mood_reflect():
    """Force a mood reflection now (ops/testing)."""
    await _reflect_mood()
    return dict(_mood_state)

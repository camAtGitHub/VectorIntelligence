"""POST /v1/proactive_greeting."""
import random
import time as _time
from datetime import datetime

from fastapi import APIRouter

import deps
from debug_log import debug
from llm import LLM_GREETING_TEMPERATURE, MODEL, _llm_timeout, llm_chat_once
from logging_util import print  # noqa: F401
from persona import PERSONA
from process_state import (
    GREETING_ABSENCE_GAP,
    _face_last_seen,
    _mood_state,
    _recent_greetings,
)
from prompt_assembly import _relative_time, _time_of_day
from response_cleanup import _strip_for_speech
from routes.models import GreetingRequest

router = APIRouter()

# -- Proactive greeting (Phase 3a) ---------------------------------------------
# Chipper periodically probes for a known face when Vector is idle. When one
# appears, it calls here: we greet only if the person has genuinely just
# ARRIVED (not seen for a while, and not freshly out of a conversation) - so a
# person sitting at the desk all day isn't greeted over and over.

_GREETING_SYSTEM = (
    PERSONA + "\n\n"
    "Someone you know has just come into view; nobody has "
    "said anything yet. Greet them unprompted with ONE short line, in "
    "character, naming them - acknowledge their return without gushing, "
    "pleased in your own understated way, or dryly so. Vary how you open "
    "every greeting: never settle into a fixed formula such as 'Name, "
    "you've returned' - come at it from a genuinely different direction "
    "each time. Plain text only, no markdown, no quotes, no {{...}} tokens, "
    "under 20 words."
)

# Greeting variety: a random angle per greeting plus a list of recent lines to
# steer away from - without this the model mode-collapses onto one opening
# ("Name, you've returned...") on every greeting.
_GREETING_ANGLES = [
    "open on the time of day, or what the room has been like",
    "feign weary indifference to their return",
    "make a dry remark about how long they were gone",
    "be backhandedly, grudgingly pleased to see them",
    "note what their arrival has interrupted",
    "greet them with exaggerated mock formality",
    "pretend you had barely registered that they had gone",
    "be wry about the predictability of their comings and goings",
    "lead with a small complaint, then acknowledge them",
    "open with a question rather than a statement",
]


@router.post("/v1/proactive_greeting")
async def proactive_greeting(req: GreetingRequest):
    """Decide whether Vector should greet a just-seen known person, and if so
    produce the line. Returns empty text when no greeting is warranted."""
    now = _time.time()
    fid, name = req.face_id, (req.name or "").strip()
    if fid <= 0 or not name:
        return {"text": ""}

    prev_seen = _face_last_seen.get(fid, 0.0)
    _face_last_seen[fid] = now
    arrived = (prev_seen == 0.0) or (now - prev_seen > GREETING_ABSENCE_GAP)

    meta = deps.MEMORY.get_face_meta(fid)
    last_convo = (meta or {}).get("last_convo_at") or 0.0
    conversed_recently = bool(last_convo) and (now - last_convo) < GREETING_ABSENCE_GAP

    if not arrived or conversed_recently:
        return {"text": ""}

    now_dt = datetime.now()
    bits = [f"{name} has just come into view. It is {_time_of_day(now_dt)}."]
    if last_convo:
        bits.append(f"You last spoke with {name} {_relative_time(now - last_convo)}.")
        summ = (meta or {}).get("last_convo_summary")
        if summ:
            bits.append(f"That conversation was about: {summ}.")
    else:
        bits.append(f"You have not properly spoken with {name} before.")
    if _mood_state["text"]:
        bits.append(f"Your current mood: {_mood_state['text']}.")

    bits.append(f"For variety, this greeting should: {random.choice(_GREETING_ANGLES)}.")
    if _recent_greetings:
        bits.append(
            "CRITICAL: do not reuse the opening or sentence structure of your "
            "recent greetings - no shared opening words, no rephrasings of: "
            + " ; ".join(f'"{g}"' for g in _recent_greetings[-5:]) + "."
        )

    debug("HTTP RECV POST /v1/proactive_greeting", {
        "face_id": fid, "name": name, "bits": bits,
    })
    try:
        text = await llm_chat_once(
            [
                {"role": "system", "content": _GREETING_SYSTEM},
                {"role": "user", "content": " ".join(bits) + " Greet them now."},
            ],
            model=MODEL,
            temperature=LLM_GREETING_TEMPERATURE,
            top_p=0.95,
            seed=random.randint(1, 2**31 - 1),
            timeout=_llm_timeout(connect=8.0, read=30.0),
            max_tokens=128,
            tag="greeting",
        )
    except Exception as e:
        print(f"[greeting] error: {e}")
        debug(f"greeting error: {e}")
        return {"text": "", "error": str(e)}

    line = _strip_for_speech(text)
    if line:
        _recent_greetings.append(line)
        del _recent_greetings[:-6]
    print(f"[greeting] {name} (arrived) -> {line!r}")
    debug("HTTP SEND /v1/proactive_greeting", {"text": line})
    return {"text": line}

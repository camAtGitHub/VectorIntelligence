"""POST /v1/proactive_greeting."""
import random
import time as _time
from datetime import datetime

from fastapi import APIRouter

import deps
from debug_log import debug
from llm import LLM_GREETING_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, one_shot_spoken_line
from logging_util import print  # noqa: F401
from process_state import (
    GREETING_ABSENCE_GAP,
    _mood_state,
    _recent_greetings,
)
from prompt_assembly import _relative_time, _time_of_day
from prompt_voice import PERSONA_MINI
from routes.models import GreetingRequest

router = APIRouter()

# -- Proactive greeting (Phase 3a) ---------------------------------------------
# Chipper periodically probes for a known face when Vector is idle. When one
# appears, it calls here: we greet only if the person has genuinely just
# ARRIVED (not seen for a while, and not freshly out of a conversation) - so a
# person sitting at the desk all day isn't greeted over and over.
# Clocks come from deps.SOUL (melting pot), not a private greeting-only map.

_GREETING_SYSTEM = (
    PERSONA_MINI + "\n\n"
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


def _prev_seen_from_soul(soul, fid: int) -> float:
    """Last visual sighting: pot last_seen_at, or latest face-buffer hit for fid."""
    prev = float(getattr(soul, "last_seen_at", 0.0) or 0.0)
    try:
        for entry in reversed(soul.face.buffer_list()):
            if entry.get("face_id") == fid:
                t = float(entry.get("t") or 0.0)
                if t > 0:
                    # Prefer face-specific buffer ts when pot has never seen anyone
                    # else use max so either path refreshes continuous presence.
                    prev = max(prev, t) if prev > 0 else t
                break
    except Exception:
        pass
    return prev


def _spoke_recently(soul, now: float) -> bool:
    """True if user voice or robot speech was within GREETING_ABSENCE_GAP."""
    gap = GREETING_ABSENCE_GAP
    last_spoke = 0.0
    last_user_voice = 0.0
    if soul is not None:
        last_spoke = float(getattr(soul, "last_spoke_at", 0.0) or 0.0)
        last_user_voice = float(getattr(soul, "last_user_voice_at", 0.0) or 0.0)
    if last_spoke > 0 and (now - last_spoke) < gap:
        return True
    if last_user_voice > 0 and (now - last_user_voice) < gap:
        return True
    # process_state clock (chat_flow / arbiter dual-write).
    try:
        import process_state as ps

        lvt = float(getattr(ps, "_LAST_USER_VOICE_TS", 0.0) or 0.0)
        if lvt > 0 and (now - lvt) < gap:
            return True
    except Exception:
        pass
    return False


@router.post("/v1/proactive_greeting")
async def proactive_greeting(req: GreetingRequest):
    """Decide whether Vector should greet a just-seen known person, and if so
    produce the line. Returns empty text when no greeting is warranted."""
    now = _time.time()
    fid, name = req.face_id, (req.name or "").strip()
    if fid <= 0 or not name:
        return {"text": ""}

    soul = getattr(deps, "SOUL", None)

    # Capture clocks BEFORE note_face so continuous probes still compute gap.
    prev_seen = 0.0
    was_occupied = False
    if soul is not None:
        prev_seen = _prev_seen_from_soul(soul, fid)
        try:
            was_occupied = bool(soul.occupied(now))
        except Exception:
            was_occupied = False
        # Always refresh pot last_seen on greeting probe (even when speech suppressed).
        try:
            from behaviors.types import FaceIdentity

            soul.note_face(
                now,
                face_id=fid,
                name=name,
                is_stranger=False,
                source="greeting",
            )
            soul.note_person(
                now,
                source="greeting",
                name_hint=name[:64],
                face=FaceIdentity(
                    face_id=int(fid),
                    name=name[:64],
                    is_stranger=False,
                ),
            )
        except Exception as e:
            print(f"[greeting] soul note failed: {e}")

    # Arrived = genuine long visual absence, not a continuous sticky desk day.
    long_absence = (prev_seen <= 0.0) or (now - prev_seen > GREETING_ABSENCE_GAP)
    # Sticky occupied continuous session: still at desk (presence sticky) → not
    # "welcome back", even if face recognition flaked and last_seen looks old.
    sticky_continuous = bool(was_occupied)
    arrived = bool(long_absence and not sticky_continuous)

    spoke_recent = _spoke_recently(soul, now)

    meta = deps.MEMORY.get_face_meta(fid) if getattr(deps, "MEMORY", None) else None
    last_convo = (meta or {}).get("last_convo_at") or 0.0
    conversed_recently = bool(last_convo) and (now - last_convo) < GREETING_ABSENCE_GAP

    if not arrived or conversed_recently or spoke_recent:
        return {"text": ""}

    now_dt = datetime.now()
    bits = [f"{name} has just come into view. It is {_time_of_day(now_dt)}."]
    # Prefer pot speech clocks for prompt wording when available.
    spoke_gap_src = last_convo
    if soul is not None:
        pot_spoke = max(
            float(getattr(soul, "last_spoke_at", 0.0) or 0.0),
            float(getattr(soul, "last_user_voice_at", 0.0) or 0.0),
            float(last_convo or 0.0),
        )
        if pot_spoke > 0:
            spoke_gap_src = pot_spoke
    if spoke_gap_src:
        bits.append(f"You last spoke with {name} {_relative_time(now - spoke_gap_src)}.")
        summ = (meta or {}).get("last_convo_summary")
        if summ:
            bits.append(f"That conversation was about: {summ}.")
    else:
        bits.append(f"You have not properly spoken with {name} before.")

    # Lite soul strip: goals / dream / mood / desk clocks without full notify ring.
    if soul is not None:
        try:
            from soul.view import format_soul_for_prompt

            lite = format_soul_for_prompt(soul, now, detail="lite") or ""
            if lite:
                bits.append(lite)
        except Exception as e:
            print(f"[greeting] format_soul_for_prompt failed: {e}")
    # Mood one-liner only when lite strip did not already include mood.
    if _mood_state["text"]:
        mood_already = any(
            "my current inner mood" in b or "Your current mood:" in b for b in bits
        )
        if not mood_already:
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
    line = await one_shot_spoken_line(
        [
            {"role": "system", "content": _GREETING_SYSTEM},
            {"role": "user", "content": " ".join(bits) + " Greet them now."},
        ],
        model=SUMMARY_MODEL,
        temperature=LLM_GREETING_TEMPERATURE,
        top_p=0.95,
        seed=random.randint(1, 2**31 - 1),
        timeout=_llm_timeout(connect=8.0, read=30.0),
        max_tokens=128,
        tag="greeting",
    )
    # Intentional: failures → empty text only (chipper uses text; no error field).
    if line:
        _recent_greetings.append(line)
        del _recent_greetings[:-6]
        soul_out = getattr(deps, "SOUL", None)
        if soul_out is not None:
            try:
                soul_out.notify_speech(line, source="greeting")
            except Exception:
                pass
    print(f"[greeting] {name} (arrived) -> {line!r}")
    debug("HTTP SEND /v1/proactive_greeting", {"text": line})
    return {"text": line}

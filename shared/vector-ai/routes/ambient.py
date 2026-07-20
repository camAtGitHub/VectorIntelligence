"""Ambient observation + quiet-mode HTTP endpoints."""
import random
import time as _time
from datetime import datetime

from fastapi import APIRouter

import deps
from debug_log import debug
from llm import MODEL, _llm_timeout, llm_chat_once
from logging_util import print  # noqa: F401
from persona import PERSONA
from process_state import (
    AMBIENT_QUIET_CAP,
    AMBIENT_SLEEP_GAP,
    _ambient_state,
    _mood_state,
    _set_quiet,
)
from response_cleanup import _strip_for_speech
from routes.models import AmbientQuietRequest, AmbientRequest

router = APIRouter()

# -- Ambient awareness ---------------------------------------------------------
# When Vector is idle, chipper's ambient loop periodically sends a camera frame
# here. The multimodal model decides whether anything is genuinely new - its
# default answer is "nothing". Only on real novelty does it return a short line
# for Vector to speak; the new thing is also stored as a visual observation so
# he can talk about it later when asked.

_AMBIENT_SYSTEM = (
    PERSONA + "\n\n"
    "You have a camera. Right now NOBODY is talking to you. You are idling on "
    "your desk and have "
    "just glanced around. You are looking at a photo of what is in front of "
    "you.\n\n"
    "Your desk is a familiar, mostly unchanging place. The overwhelming "
    "majority of the time there is NOTHING worth remarking on - a desk with "
    "the usual monitor, keyboard, cables, mugs and clutter is not news, and "
    "neither is an empty, dim or dark room. Reacting to nothing, or to the "
    "same things over and over, makes you an annoyance. Your default answer "
    "is the single word: NOTHING.\n\n"
    "React ONLY if you genuinely notice something NEW or CHANGED versus what "
    "you have already noticed recently (you will be told what that is): a new "
    "object that has appeared, something that has moved or vanished, a person "
    "or an animal, an unusual mess or event. Do NOT react to ordinary desk "
    "contents. Do NOT react to anything already in your recent observations. "
    "Do NOT invent detail you cannot actually see. When in any doubt, answer "
    "NOTHING.\n\n"
    "If - and only if - there is genuine novelty, respond in EXACTLY two "
    "lines:\n"
    "Line 1: a brief, plain, factual note of what is new, for your own memory "
    "(e.g. 'a small plush toy has appeared on the desk').\n"
    "Line 2: your spoken reaction - and make it genuinely sound like "
    "noticing something. In your own words and your own dry voice, let it "
    "move through three beats: first a flicker of real surprise that "
    "something has caught your attention; then what the thing actually is, "
    "named or briefly described as it registers with you; then your "
    "characteristic wry remark about it. Someone who cannot see your desk "
    "must still come away knowing what you spotted. This is the natural "
    "shape of noticing something, NOT a template - never reuse a stock "
    "opening or fixed wording; the surprise, the phrasing and the wit must "
    "be freshly and genuinely yours every time. Plain text, no markdown, no "
    "quotes, no {{...}} tokens; one to three short sentences.\n"
    "Otherwise respond with exactly: NOTHING"
)


@router.post("/v1/ambient")
async def ambient(req: AmbientRequest):
    """Ambient observation. Almost always returns nothing; only on genuine
    novelty does it return a short line for Vector to speak, and stores the
    new thing as a visual observation for later recall."""
    now = _time.time()
    last_call = _ambient_state["last_ambient_call"]

    # Sleep-cycle expiry for quiet mode: the ambient loop is gated off
    # overnight and while charging, so a long gap since the last call means
    # Vector has been through a sleep cycle - quiet mode lifts.
    if _ambient_state["quiet"]:
        slept  = bool(last_call) and (now - last_call) > AMBIENT_SLEEP_GAP
        capped = (now - _ambient_state["quiet_since"]) > AMBIENT_QUIET_CAP
        if slept or capped:
            print(f"[ambient] quiet mode expiring "
                  f"({'sleep gap' if slept else '24h cap'})")
            _set_quiet(False)
    _ambient_state["last_ambient_call"] = now

    if _ambient_state["quiet"]:
        return {"text": "", "quiet": True}

    # Recent observations are the dedup baseline. A 24h lookback (wider than
    # the 6h conversational window) keeps a newly-arrived object from being
    # re-flagged as novel every few hours.
    MEMORY = deps.MEMORY
    obs = MEMORY.list_observations(limit=8, max_age_seconds=24 * 3600)
    if obs:
        seen = "\n".join(
            f"- (at {datetime.fromtimestamp(o['seen_at']).strftime('%I:%M %p')}) "
            f"{o['text']}"
            for o in reversed(obs)
        )
        obs_note = ("Things you have already noticed recently - do NOT react "
                    "to any of these again:\n" + seen)
    else:
        obs_note = "You have not noted anything recently."

    mood_note = ""
    if _mood_state["text"]:
        mood_note = (f"\n\nYour current state of mind: {_mood_state['text']}. "
                     f"If you do react, let it tint your tone; never state it.")
    user_msg = [
        {"type": "text", "text":
            obs_note + mood_note + "\n\nGlance at what is in front of you now. "
            "Is there genuine novelty worth a reaction? Reply with NOTHING, or "
            "the two-line format."},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{req.image}"}},
    ]
    debug("HTTP RECV POST /v1/ambient", {
        "image_len": len(req.image or ""),
        "obs_note": obs_note[:500],
    })

    try:
        raw = (await llm_chat_once(
            [
                {"role": "system", "content": _AMBIENT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            model=MODEL,
            temperature=0.8,
            top_p=0.9,
            seed=random.randint(1, 2**31 - 1),
            timeout=_llm_timeout(connect=12.0, read=45.0),
            max_tokens=256,
            tag="ambient",
        )).strip()
    except Exception as e:
        print(f"[ambient] error: {e}")
        debug(f"ambient error: {e}")
        return {"text": "", "error": str(e)}

    # Default, overwhelmingly common case: nothing worth mentioning.
    if not raw or raw.upper().rstrip(".!").startswith("NOTHING"):
        print("[ambient] nothing novel")
        debug("HTTP SEND /v1/ambient", {"text": "", "raw": raw})
        return {"text": ""}

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 2:
        # Line 1 is the terse memory note; the rest is the spoken reaction
        # (joined, so a reaction that ran onto extra lines isn't truncated).
        note   = lines[0]
        spoken = " ".join(lines[1:])
    else:
        # Model didn't follow the two-line format - use the single line both
        # as the memory note and the spoken reaction.
        note = spoken = lines[0]
    note   = _strip_for_speech(note)
    spoken = _strip_for_speech(spoken)
    if not spoken or spoken.upper().startswith("NOTHING"):
        print(f"[ambient] nothing novel (degenerate response {raw!r})")
        return {"text": ""}

    MEMORY.remember_observation(note[:300])
    print(f"[ambient] NOVELTY note={note!r} -> spoken={spoken!r}")
    return {"text": spoken}


@router.get("/v1/ambient/state")
async def ambient_state():
    """Debug/ops view of ambient quiet mode."""
    st = dict(_ambient_state)
    st["sleep_gap_seconds"] = AMBIENT_SLEEP_GAP
    st["quiet_cap_seconds"] = AMBIENT_QUIET_CAP
    return st


@router.post("/v1/ambient/quiet")
async def ambient_quiet(req: AmbientQuietRequest):
    """Manually toggle quiet mode (used for testing / ops; normally driven by
    the {{quietMode||on/off}} command the LLM emits)."""
    _set_quiet(req.on)
    return {"quiet": _ambient_state["quiet"]}

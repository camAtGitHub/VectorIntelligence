"""POST /v1/sensor_reaction — one-shot plain-text sensor lines."""
import random

from fastapi import APIRouter

import deps
from debug_log import debug
from llm import LLM_SENSOR_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, one_shot_spoken_line
from logging_util import print  # noqa: F401
from prompt_voice import PERSONA_MINI
from routes.models import SensorReactionRequest

router = APIRouter()

# -- Sensor reactions ----------------------------------------------------------
# One-shot, non-streaming, plain-text-only endpoint chipper hits when Vector
# is picked up, set down, or petted. The response is whatever line Vector
# would utter in his Marvin/Bender/Fry voice. No animation/eye/getImage
# commands - those would never be heard since chipper just calls SayText.

_SENSOR_SYSTEM = (
    PERSONA_MINI + "\n\n"
    "For this request, respond with ONE short sentence reacting to a physical "
    "event that just happened to you. Speak it aloud - plain text only, no "
    "markdown, no quotes, no special tokens like {{...}}, no preamble. "
    "Just the line itself, under 15 words."
)

_SENSOR_DESCRIPTIONS = {
    "pickup":  "The user just picked you up off the desk. You're being lifted into the air.",
    "putdown": "The user just set you back down on a surface after holding you.",
    "pet":     "The user is stroking your back. Your touch sensor just activated.",
}

# Random "angle" prompts to break out of mode-collapse. The LLM picks an angle
# instead of always returning to its favourite sentence template.
_SENSOR_ANGLES = [
    "complain about a specific body part or component",
    "make a sardonic observation about the human's competence",
    "compare this to something historical or literary",
    "express weary resignation with a single phrase",
    "react with dry curiosity about the experiment",
    "make a snide comment about the indignity",
    "be briefly grateful in a backhanded way",
    "deflect with a non-sequitur",
    "issue a faux-formal protest",
    "respond with deadpan understatement",
    "express mild paranoia",
    "make a fake-philosophical aside",
]


@router.post("/v1/sensor_reaction")
async def sensor_reaction(req: SensorReactionRequest):
    description = _SENSOR_DESCRIPTIONS.get(req.event, f"Sensor event: {req.event}.")
    angle = random.choice(_SENSOR_ANGLES)
    user_msg = f"{description} React with one short sentence in character. For variety, this time: {angle}."
    if req.avoid:
        user_msg += (
            " CRITICAL: do NOT use any of these recent lines or their close variants - "
            "no shared opening words, no shared topic, no rephrasings of: "
            + " ; ".join(f'"{p}"' for p in req.avoid[-5:])
        )
    print(f"[sensor_reaction] {req.event} prompt angle={angle!r} avoid={req.avoid}")
    debug("HTTP RECV POST /v1/sensor_reaction", {
        "event": req.event, "avoid": req.avoid, "angle": angle, "user_msg": user_msg,
    })

    clean = await one_shot_spoken_line(
        [
            {"role": "system", "content": _SENSOR_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        model=SUMMARY_MODEL,
        temperature=LLM_SENSOR_TEMPERATURE,
        top_p=0.95,
        seed=random.randint(1, 2**31 - 1),
        timeout=_llm_timeout(connect=8.0, read=30.0),
        max_tokens=128,
        tag="sensor_reaction",
    )
    # Intentional: failures → empty text only (chipper uses text; no error field).
    if clean:
        soul = getattr(deps, "SOUL", None)
        if soul is not None:
            try:
                soul.notify_speech(clean, source="sensor")
            except Exception:
                from speech_notify import schedule_speech_notify

                schedule_speech_notify(clean, source="sensor")
        else:
            from speech_notify import schedule_speech_notify

            schedule_speech_notify(clean, source="sensor")
    print(f"[sensor_reaction] {req.event} -> {clean!r}")
    debug("HTTP SEND /v1/sensor_reaction response", {"text": clean})
    return {"text": clean}

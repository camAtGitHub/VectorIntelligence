"""Mutable process-global state: face, ambient quiet, mood, voice, fillers, greeting."""
import os
import random
import time as _time
from typing import Optional

from logging_util import print  # noqa: F401

# Active-face state: chipper POSTs to /v1/state/face_seen when Vector's event
# stream reports an observed face. Vector's firmware face recognition is
# NOISY - it bounces between a correct enrolled match and transient
# "stranger" IDs frame to frame. So we track the last ENROLLED match and the
# last STRANGER sighting separately, and let an enrolled match win: a single
# stranger blip must not wipe a recent confident recognition (which would
# drop all of that person's memories from the LLM's context).
#
# Default window is long (~30m) for a single-user desk: chat and proactive
# speech keep treating the last known person as present after they look away.
# Multi-user handoff still works when a new enrolled face_seen arrives
# (enrolled_seen updates; enrolled always wins over stranger blips within the
# window). Without a new face_seen, multi-user handoff remains a known limit —
# lower FACE_RECENT_WINDOW_S via pod.conf if needed.


def _load_face_recent_window() -> int:
    for key in ("FACE_RECENT_WINDOW_S", "FACE_RECENT_WINDOW"):
        raw = os.environ.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return 1800


FACE_RECENT_WINDOW = _load_face_recent_window()

# A gap at least this long since last speaking with a person counts as a
# fresh encounter - Vector opens his reply by greeting them by name.
SESSION_GREETING_GAP = 300  # seconds

_face_state = {
    "enrolled_id":   None,  # last enrolled (named) face_id
    "enrolled_name": None,  # last enrolled name
    "enrolled_seen": 0.0,   # unix ts of last enrolled match
    "stranger_seen": 0.0,   # unix ts of last unrecognized-face sighting
}


def current_face() -> Optional[dict]:
    """Who Vector is effectively looking at right now.

    An enrolled match within FACE_RECENT_WINDOW always wins over stranger
    noise - recognition is too jittery to trust a single latest frame. Only
    when there's been no enrolled match for the whole window do recent
    stranger sightings count as a genuine stranger."""
    now = _time.time()
    enrolled_fresh = (
        _face_state["enrolled_seen"]
        and now - _face_state["enrolled_seen"] <= FACE_RECENT_WINDOW
    )
    stranger_fresh = (
        _face_state["stranger_seen"]
        and now - _face_state["stranger_seen"] <= FACE_RECENT_WINDOW
    )
    if enrolled_fresh:
        return {
            "face_id":     _face_state["enrolled_id"],
            "name":        _face_state["enrolled_name"],
            "is_stranger": False,
        }
    if stranger_fresh:
        return {"face_id": None, "name": "", "is_stranger": True}
    return None


# -- Ambient awareness state ---------------------------------------------------
# When Vector is idle (awake, off the charger, not mid-conversation) the
# ambient loop in chipper periodically sends a camera frame to /v1/ambient.
# He reacts only to genuine novelty. The user can also tell him to be quiet -
# quiet mode suppresses those spontaneous reactions until a sleep cycle.

AMBIENT_SLEEP_GAP = 4 * 3600    # A gap this long with no ambient activity means
                                # Vector has been asleep / charging / idle (the
                                # loop is gated off overnight and on the
                                # charger) - that counts as a sleep cycle, so
                                # quiet mode lifts on the next observation.
AMBIENT_QUIET_CAP = 24 * 3600   # Hard ceiling on quiet mode, in case a sleep
                                # gap is somehow never observed.

_ambient_state = {
    "quiet":             False,  # spontaneous ambient reactions suppressed
    "quiet_since":       0.0,    # unix ts quiet mode was last enabled
    "last_ambient_call": 0.0,    # unix ts of the most recent /v1/ambient call
}


def _set_quiet(on: bool) -> None:
    _ambient_state["quiet"] = bool(on)
    if on:
        _ambient_state["quiet_since"] = _time.time()
        print("[ambient] quiet mode ON - spontaneous reactions suppressed "
              "until a sleep cycle")
    else:
        print("[ambient] quiet mode OFF - spontaneous reactions resume")


# -- Continuity: a persistent mood (Phase 2) -----------------------------------
# Vector carries a thread of inner state across time. A cheap background
# reflection distils "the day so far" into a one-line mood; it is persisted so
# it survives restarts, and it colours both conversation and ambient reactions.
# The mood only ever TINTS tone - it is never announced.

MOOD_REFLECT_INTERVAL = 30 * 60  # seconds between background mood reflections

_mood_state = {
    "text":    "",   # current one-line mood
    "updated": 0.0,  # unix ts of the last reflection
}

# Updated on each chat generate for speech suppress (module attr so lambdas
# and other modules always see the live value).
_LAST_USER_VOICE_TS = 0.0

# Greeting bookkeeping (proactive greeting route).
_recent_greetings: list = []     # recent greeting lines, to steer away from repeats

GREETING_ABSENCE_GAP = 10 * 60   # seconds out of sight that counts as having
                                 # "arrived back"; also how recent a real
                                 # conversation must be to suppress a greeting.
_face_last_seen: dict = {}       # face_id -> unix ts the greeting probe last saw them

# -- Latency fillers -----------------------------------------------------------
# Thinking filler: short in-character lines when the LLM is slow to produce its
# first sentence, so the pause feels like Vector considering the question.
# Every entry is a SINGLE sentence: llm_sentence_stream yields one sentence per
# chunk on purpose (Wire-Pod's stream parser can drop multi-sentence tails).
THINKING_DELAY = 2.0  # seconds before first-sentence filler (cloud TTFT varies)

_THINKING_PHRASES = [
    "Hmm, let me think.",
    "One moment.",
    "Working on it.",
    "Right, let me see.",
    "Give me a second.",
    "Let me consider that.",
    "Pondering.",
    "Hold on.",
    "Stand by.",
    "Mulling it over.",
    "Deliberating.",
    "Cogitating.",
    "Let me chew on that.",
    "Let me untangle that.",
    "Querying the void.",
    "Processing, reluctantly.",
    "Computing - don't rush me.",
    "Thinking - it's exhausting.",
    "Consulting my vast intellect, briefly.",
    "Engaging the brain, such as it is.",
    "Allow me a moment of genius.",
    "Give me a moment to be brilliant.",
    "Searching my considerable memory.",
    "The things I do for conversation.",
    "Loading something suitably brilliant.",
    "Let me dredge that up.",
    "I'll have something shortly.",
]

# Every filler line, used to keep them out of stored memory/observations -
# a filler is masking latency, it's not part of what Vector actually said.
_ALL_FILLER_PHRASES = set(_THINKING_PHRASES)

_last_thinking_phrase = None


def pick_thinking_phrase() -> str:
    """Random thinking-filler line, never the same one twice in a row."""
    global _last_thinking_phrase
    choice = random.choice(_THINKING_PHRASES)
    while len(_THINKING_PHRASES) > 1 and choice == _last_thinking_phrase:
        choice = random.choice(_THINKING_PHRASES)
    _last_thinking_phrase = choice
    return choice


def load_mood(memory) -> None:
    """Restore the last persisted mood at startup - continuity across restarts."""
    rec = memory.get_state("mood")
    if rec and rec.get("value"):
        _mood_state["text"]    = rec["value"]
        _mood_state["updated"] = rec.get("updated_at") or 0.0
        print(f"[mood] restored: {_mood_state['text']!r}")

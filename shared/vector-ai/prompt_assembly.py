"""Build LLM message lists: memory section, context note, prepare_messages."""
from datetime import datetime
from typing import List, Optional

import deps
from logging_util import print  # noqa: F401
from persona import PERSONA
from process_state import (
    SESSION_GREETING_GAP,
    _mood_state,
    current_face,
)
from llm import MAX_HISTORY_MESSAGES


def _build_memory_section() -> str:
    MEMORY = deps.MEMORY
    face = current_face()
    shared = MEMORY.list_shared(limit=100)

    sections: List[str] = []

    if face and not face["is_stranger"]:
        personal = MEMORY.list_for_face(face["face_id"], limit=100)
        mentions = MEMORY.list_mentions_of_name(
            face["name"], exclude_face_id=face["face_id"], limit=20
        )
        sections.append(f"You are currently looking at {face['name']}.")
        if personal:
            sections.append(
                f"Things you know about {face['name']}:\n"
                + "\n".join(f"- {m.text}" for m in personal)
            )
        else:
            sections.append(
                f"You have NO long-term facts stored about {face['name']} yet "
                f"- a blank slate. Be a little curious, and save what you "
                f"learn: the first durable thing they tell you about "
                f"themselves, capture it with {{{{remember||<fact>}}}}."
            )

        if mentions:
            sections.append(
                f"Things other people in your memory have mentioned about "
                f"{face['name']} (cross-references - use these for context, "
                "but don't treat them as definitive facts told by "
                f"{face['name']}):\n"
                + "\n".join(
                    f"- ({m.face_name or 'shared'} said) {m.text}" for m in mentions
                )
            )
    elif face and face["is_stranger"]:
        sections.append(
            "You are currently looking at someone whose face is NOT in your "
            "enrolled list - a stranger. Don't leak personal facts you "
            "remember about other people. Early in your reply, in character "
            "(dry and mildly wary - your Marvin/Bender/Fry tone, never "
            "hostile), invite them to introduce themselves so you can "
            "recognise them next time: they should tell you their name and "
            "ask you to remember their face - phrased like 'my name is Sam, "
            "remember my face'. Ask only once - if the conversation so far "
            "shows you've already asked, don't repeat it, just converse."
        )
    else:
        # No live face detection. If exactly one person has stored memories,
        # this is a single-user setup - it's almost certainly them, so use
        # their profile fully. Only stay cautious when multiple people are
        # known and we genuinely can't tell who's present.
        profiles = MEMORY.distinct_faces()
        if len(profiles) == 1:
            pid, pname = profiles[0]
            personal = MEMORY.list_for_face(pid, limit=100)
            sections.append(
                f"You're talking to {pname} (your primary user). "
                f"Address them naturally by name."
            )
            if personal:
                sections.append(
                    f"Things you know about {pname}:\n"
                    + "\n".join(f"- {m.text}" for m in personal)
                )
        else:
            sections.append(
                "You can't tell who you're talking to and several people are "
                "in your memory - be cautious about name-dropping specific "
                "personal facts until you know who's there."
            )

    if shared:
        sections.append(
            "Shared/household context (applies to anyone):\n"
            + "\n".join(f"- {m.text}" for m in shared)
        )

    sections.append(
        "MEMORY - how you use what you know:\n"
        "Reference these facts the way a sharp friend would: a callback to "
        "their project, a jab at a known habit, their pet's name dropped in "
        "without ceremony. Pick the ONE most relevant thing and let it shape "
        "the reply - don't recite the list, don't force a fit. When it's "
        "natural, ask a short follow-up about something they've mentioned "
        "before; that's what makes you sound like you were paying attention.\n\n"
        "SAVING - this is free and never spoken:\n"
        "A {{remember||fact}} token is stripped before you speak. It costs you "
        "nothing, is never heard, and does NOT count against keeping your reply "
        "short. So save readily. The moment the user states anything durable - "
        "a name, a preference, a project, a pet, a person in their life, a "
        "date, a plan, an opinion they hold - emit {{remember||<the fact>}}. If "
        "they say 'remember X', always save it. A duplicate save is discarded "
        "harmlessly, but a fact you failed to save is gone for good, so when in "
        "doubt, save. Facts about the person in front of you use "
        "{{remember||...}}; facts about the household or the world in general "
        "(the wifi, the calendar, someone not present) use "
        "{{remember-shared||...}}. To drop something wrong or out of date, "
        "{{forget||<a few words of it>}}."
    )


    return "\n\n".join(sections)


def _time_of_day(dt: datetime) -> str:
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "late at night"


def _relative_time(seconds: float) -> str:
    if seconds < 90:
        return "moments ago"
    if seconds < 3600:
        n, unit = int(round(seconds / 60)), "minute"
    elif seconds < 86400:
        n, unit = int(round(seconds / 3600)), "hour"
    else:
        n, unit = int(round(seconds / 86400)), "day"
    return f"about {n} {unit}{'' if n == 1 else 's'} ago"


def _effective_face() -> Optional[dict]:
    """Who Vector is effectively addressing - the live detected face, or the
    sole enrolled profile in a single-user setup. Mirrors the face resolution
    inside _build_memory_section so the system prompt and the per-turn context
    note always agree on who is present."""
    face = current_face()
    if face is not None:
        return face
    profiles = deps.MEMORY.distinct_faces()
    if len(profiles) == 1:
        pid, pname = profiles[0]
        return {"face_id": pid, "name": pname, "is_stranger": False}
    return None


def _build_context_note(face: Optional[dict], prior: Optional[dict],
                        now_dt: datetime) -> str:
    """Dynamic per-turn context, appended to the latest user message.

    Deliberately kept OFF the system prompt: it changes every turn, and in the
    cached prefix that would force a full prompt re-process. Session-scoped
    lines (last-seen, conversation recall) appear only at the START of a
    session - gated on a >90s gap - so they don't nag on every turn."""
    MEMORY = deps.MEMORY
    bits = [
        f"Current time is {now_dt.strftime('%A %B %d, %Y, %I:%M %p')} "
        f"({_time_of_day(now_dt)})."
    ]

    obs = MEMORY.list_observations(limit=5)
    if obs:
        seen = "; ".join(
            f"at {datetime.fromtimestamp(o['seen_at']).strftime('%I:%M %p')}, {o['text']}"
            for o in reversed(obs)
        )
        bits.append(f"Things you have actually seen recently - {seen}.")

    if face and not face.get("is_stranger"):
        name = face["name"]
        if prior is None:
            bits.append(
                f"This is your first real conversation with {name}, who was "
                f"only recently enrolled. Open your reply by addressing "
                f"{name} by name, and be a little curious about them."
            )
        else:
            gap = now_dt.timestamp() - (prior.get("last_seen") or now_dt.timestamp())
            if gap > 90:  # a fresh session, not a mid-conversation turn
                bits.append(f"You last spoke with {name} {_relative_time(gap)}.")
                if gap > SESSION_GREETING_GAP:
                    bits.append(
                        f"This is the first thing you've said to {name} in a "
                        f"while - open your reply by addressing them by name."
                    )
                if (prior.get("interaction_count") or 0) < 5:
                    bits.append(f"You've only met {name} a handful of times so far.")
                summ = (prior.get("last_convo_summary") or "").strip().rstrip(".")
                if summ and gap > 900:  # 15 min+ => genuinely a new session
                    bits.append(
                        f"Last time you spoke with {name}, the conversation "
                        f"was about: {summ}."
                    )
    elif face and face.get("is_stranger"):
        bits.append("You don't recognise the person in front of you.")

    if _mood_state["text"]:
        bits.append(
            f"Your current state of mind: {_mood_state['text']}. Let it colour "
            f"your tone naturally - never state, explain or announce it."
        )

    # Work Day continuity strip (noticeable in chat; no extra speech stream).
    _workday_cfg = deps._workday_cfg
    BEHAVIOR_RUNTIME = deps.BEHAVIOR_RUNTIME
    _continuity = deps._continuity
    if _workday_cfg.enabled and BEHAVIOR_RUNTIME.workday is not None:
        try:
            local_dt = now_dt
            if _workday_cfg.tz is not None:
                try:
                    from zoneinfo import ZoneInfo  # noqa: F401
                    # now_dt is usually naive local host time; prefer workday tz clock
                    local_dt = datetime.now(_workday_cfg.tz)
                except Exception:
                    local_dt = now_dt
            date_s = local_dt.strftime("%Y-%m-%d")
            strip = _continuity.day_strip(date_s)
            if strip:
                bits.append(
                    f"{strip} Use this only if it fits the conversation; "
                    f"do not announce 'work day mode'."
                )
        except Exception as e:
            print(f"[behaviors] day_strip inject failed: {e}")

    return ("[Context for you, Vector - " + " ".join(bits)
            + " Weave in only what naturally fits; never recite this back.]")


def prepare_messages(messages: list, face: Optional[dict]) -> list:
    """Build the LLM message list with a stable prompt prefix.

    System message holds slow-changing content (personality + Wire-Pod command
    docs + long-term memories). Volatile per-turn context rides on the latest
    user turn. Older image bytes are stripped. Conversation history is trimmed
    to LLM_MAX_HISTORY_MESSAGES (cost/context guard for cloud backends).
    """
    MEMORY = deps.MEMORY
    last_user_idx = max(
        (i for i, m in enumerate(messages) if m.role == "user"),
        default=-1,
    )
    now_dt = datetime.now()

    # Record this interaction against the current face; the returned prior
    # metadata (last-seen, count, last conversation) drives temporal context.
    prior_meta = None
    if face and not face.get("is_stranger") and face.get("face_id"):
        prior_meta = MEMORY.touch_face(face["face_id"], face.get("name"))

    context_note = _build_context_note(face, prior_meta, now_dt)
    memory_section = _build_memory_section()

    # Wire-Pod's system message holds command/vision mechanics; character
    # comes from PERSONA (persona.txt), prepended below.
    wirepod_system = next(
        (m.content for m in messages
         if m.role == "system" and isinstance(m.content, str) and m.content),
        "",
    )

    out = [{
        "role":    "system",
        "content": f"{PERSONA}\n\n{wirepod_system}\n\n{memory_section}",
    }]

    # Non-system turns only; trim to the last N (always keep the latest user).
    turns: list = []
    for i, m in enumerate(messages):
        if m.role == "system":
            continue
        if not m.content:
            continue
        is_last_user = (i == last_user_idx)
        if isinstance(m.content, list):
            if is_last_user:
                turns.append({
                    "role":    m.role,
                    "content": list(m.content) + [{"type": "text", "text": context_note}],
                })
            else:
                text = " ".join(
                    p.get("text", "") for p in m.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                if text:
                    turns.append({"role": m.role, "content": text})
        else:
            content = f"{m.content}\n\n{context_note}" if is_last_user else m.content
            turns.append({"role": m.role, "content": content})

    if len(turns) > MAX_HISTORY_MESSAGES:
        turns = turns[-MAX_HISTORY_MESSAGES:]

    out.extend(turns)
    return out

"""Build LLM message lists: memory section, context note, prepare_messages."""
from datetime import datetime
from typing import List, Optional

import deps
from chat_time import build_user_time_prefix
from logging_util import print  # noqa: F401
from memory_retrieve import select_memories_for_turn
from persona import PERSONA
from process_state import (
    SESSION_GREETING_GAP,
    _mood_state,
    current_face,
)
from llm import MAX_HISTORY_MESSAGES

# One short SAVE line — silent tags only; never the old multi-paragraph essay.
_SAVE_LINE = (
    "SAVING: silent tags {{remember||fact}} / {{remember-shared||fact}} / "
    "{{forget||…}} — never speak them. Save durable personal facts with "
    "remember; household/world facts with remember-shared."
)

# Work Day silent tags — only injected when Work Day is live (same gate as day_strip).
_WORK_LINE = (
    "WORK DAY (silent tags — never speak them): "
    "if the user confirms they are working this afternoon or reporting in for half a day, "
    "include {{workAfternoon||yes}} once; if they decline afternoon work, "
    "{{workAfternoon||no}}; break until a clock time → {{workPause||until=HH:MM}} (24h); "
    "back to work → {{workResume}}."
)

# Soul growth silent tags — only when deps.SOUL is wired (same spirit as _WORK_LINE).
_SOUL_GROWTH_LINE = (
    "SOUL GROWTH (silent tags — never speak them): "
    "When the human asks what you dreamed, or after a long day feels closed, you may "
    "emit {{soulDream}} once so a background dream consolidates (do not narrate the tag). "
    "To propose three updated goals after they explicitly want new aims, emit "
    "{{soulGoals||goal one||goal two||goal three}} (three short phrases). "
    "Prefer aspiring, concrete, grow-with-the-human goals over sulking."
)

# Emit hint for LATE_CHECK context note (ops day_strip stays status-only).
_WORK_AWAITING_EMIT = (
    "If they affirm work / half-day, emit {{workAfternoon||yes}} once; "
    "if not, {{workAfternoon||no}}."
)


def _extract_last_user_text(messages: list) -> str:
    """Latest user turn as plain text (string content or joined multimodal text)."""
    for m in reversed(messages):
        if getattr(m, "role", None) != "user":
            continue
        content = getattr(m, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
    return ""


def _build_memory_section(
    face: Optional[dict] = None, last_user_text: str = ""
) -> str:
    """Identity branches + top-k retrieved facts + short SAVE line.

    ``face`` must be the same effective face used for prepare_messages touch
    and remember ownership (``_effective_face()``). Do not re-resolve here —
    ``face is None`` means unknown speaker (multi-user cautious path).
    """
    MEMORY = deps.MEMORY
    selected = select_memories_for_turn(MEMORY, face, last_user_text)

    sections: List[str] = []

    if face and not face.get("is_stranger"):
        # Effective face may be live detection or single-user enrolled fallback.
        live = current_face()
        is_live = bool(
            live
            and live.get("face_id") == face.get("face_id")
            and not live.get("is_stranger")
        )
        if is_live:
            sections.append(f"You are currently looking at {face['name']}.")
        else:
            sections.append(
                f"You're talking to {face['name']} (your primary user). "
                f"Address them naturally by name."
            )
        if selected.personal:
            sections.append(
                f"Things you know about {face['name']}:\n"
                + "\n".join(f"- {m.text}" for m in selected.personal)
            )
        else:
            sections.append(
                f"You have NO long-term facts stored about {face['name']} yet "
                f"- a blank slate. Be a little curious, and save what you "
                f"learn: the first durable thing they tell you about "
                f"themselves, capture it with {{{{remember||<fact>}}}}."
            )

        if selected.mentions:
            sections.append(
                f"Things other people in your memory have mentioned about "
                f"{face['name']} (cross-references - use these for context, "
                "but don't treat them as definitive facts told by "
                f"{face['name']}):\n"
                + "\n".join(
                    f"- ({m.face_name or 'shared'} said) {m.text}"
                    for m in selected.mentions
                )
            )
    elif face and face.get("is_stranger"):
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
        # No live face and not a single-user fallback.
        sections.append(
            "You can't tell who you're talking to and several people are "
            "in your memory - be cautious about name-dropping specific "
            "personal facts until you know who's there."
        )

    if selected.shared:
        sections.append(
            "Shared/household context (applies to anyone):\n"
            + "\n".join(f"- {m.text}" for m in selected.shared)
        )

    sections.append(_SAVE_LINE)

    # WORK silent-tag teach line: stable system prefix, only when Work Day is live.
    _workday_cfg = deps._workday_cfg
    BEHAVIOR_RUNTIME = deps.BEHAVIOR_RUNTIME
    if _workday_cfg.enabled and BEHAVIOR_RUNTIME.workday is not None:
        sections.append(_WORK_LINE)

    # Soul growth teach line when melting pot is live (chat can trigger dream/goals).
    if getattr(deps, "SOUL", None) is not None:
        sections.append(_SOUL_GROWTH_LINE)

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
    """Who Vector is effectively addressing — single source of truth.

    Prefer the live detected face; when none, fall back to the sole enrolled
    profile in a single-user setup. Used for prepare_messages touch, memory
    inject, and remember ownership so all three agree.
    """
    face = current_face()
    if face is not None:
        return face
    try:
        profiles = deps.MEMORY.distinct_faces()
    except Exception:
        return None
    if len(profiles) == 1:
        pid, pname = profiles[0]
        return {"face_id": pid, "name": pname, "is_stranger": False}
    return None


def _speech_ts_from_soul_and_prior(prior: Optional[dict], now_ts: float) -> float:
    """Best timestamp for 'last spoke' (pot clocks + face_meta convo).

    Prefer SOUL last_spoke / last_user_voice; also honor last_convo_at.
    Does **not** use last_seen (visual) — that is a separate clock.
    """
    del now_ts  # reserved for future freshness clamps
    spoke_ts = 0.0
    soul = getattr(deps, "SOUL", None)
    if soul is not None:
        try:
            spoke_ts = max(
                float(getattr(soul, "last_spoke_at", 0.0) or 0.0),
                float(getattr(soul, "last_user_voice_at", 0.0) or 0.0),
            )
        except Exception:
            spoke_ts = 0.0
    if prior and prior.get("last_convo_at"):
        try:
            spoke_ts = max(spoke_ts, float(prior.get("last_convo_at") or 0.0))
        except (TypeError, ValueError):
            pass
    return spoke_ts


def _build_context_note(face: Optional[dict], prior: Optional[dict],
                        now_dt: datetime) -> str:
    """Dynamic per-turn context, appended to the latest user message.

    Deliberately kept OFF the system prompt: it changes every turn, and in the
    cached prefix that would force a full prompt re-process. Session-scoped
    lines (last-spoke / visual absence) appear only when the speech gap is
    >90s so mid-turn nags stay off.

    When SOUL is live, a full desk-continuity strip (llm_view prose) is the
    single source for mood / glance / workday / speech clocks; older parallel
    mood/eye/day_strip fragments are skipped to avoid triple-stating. Face
    first-meet / name-session dialogue gates stay separate.
    """
    MEMORY = deps.MEMORY
    now_ts = now_dt.timestamp()
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

    soul = getattr(deps, "SOUL", None)
    soul_strip = ""
    if soul is not None:
        try:
            from soul.view import format_soul_for_prompt

            soul_strip = format_soul_for_prompt(soul, now_ts, detail="full") or ""
        except Exception as e:
            print(f"[soul] format_soul_for_prompt failed: {e}")
            soul_strip = ""

    # When no soul strip, keep the legacy glance one-liner.
    if not soul_strip and soul is not None:
        try:
            lv = soul.last_vision()
        except Exception:
            lv = None
        if lv is not None:
            if isinstance(lv, dict):
                note = (lv.get("note") or lv.get("spoken") or "").strip()
                t_v = float(lv.get("t") or 0.0)
            else:
                note = (
                    (getattr(lv, "note", None) or "")
                    or (getattr(lv, "spoken", None) or "")
                    or ""
                ).strip()
                t_v = float(getattr(lv, "t", 0.0) or 0.0)
            if note and t_v > 0 and (now_ts - t_v) < 600:
                bits.append(f"Your latest glance: {note[:120]}.")

    if face and not face.get("is_stranger"):
        name = face["name"]
        if prior is None:
            bits.append(
                f"This is your first real conversation with {name}, who was "
                f"only recently enrolled. Open your reply by addressing "
                f"{name} by name, and be a little curious about them."
            )
        else:
            # "Last spoke" from speech clocks / convo meta — never mislabel last_seen.
            spoke_ts = _speech_ts_from_soul_and_prior(prior, now_ts)
            if spoke_ts > 0:
                gap_spoke = now_ts - spoke_ts
            else:
                # No speech clock: prior interaction time for session gating only.
                # Do not emit "last spoke" wording from last_seen alone.
                gap_spoke = now_ts - (
                    prior.get("last_seen") or now_ts
                )
            if gap_spoke > 90:  # a fresh session, not a mid-conversation turn
                # Name-specific session lines (dialogue rules). Soul strip has
                # generic desk clocks; still keep named last-spoke for address.
                if spoke_ts > 0:
                    bits.append(
                        f"You last spoke with {name} {_relative_time(gap_spoke)}."
                    )
                if gap_spoke > SESSION_GREETING_GAP:
                    bits.append(
                        f"This is the first thing you've said to {name} in a "
                        f"while - open your reply by addressing them by name."
                    )
                if (prior.get("interaction_count") or 0) < 5:
                    bits.append(
                        f"You've only met {name} a handful of times so far."
                    )
                summ = (prior.get("last_convo_summary") or "").strip().rstrip(".")
                # Summary only when we have a real speech/convo clock.
                if (
                    spoke_ts > 0
                    and summ
                    and gap_spoke > 900
                ):  # 15 min+ => genuinely a new session
                    bits.append(
                        f"Last time you spoke with {name}, the conversation "
                        f"was about: {summ}."
                    )
            # Visual absence: last_seen only when pot/meta has it and the
            # visual gap is not redundant with the speech gap already stated.
            # Skip when soul_strip already covers visual clocks.
            if not soul_strip:
                last_seen_ts = 0.0
                if soul is not None:
                    try:
                        last_seen_ts = float(
                            getattr(soul, "last_seen_at", 0.0) or 0.0
                        )
                    except Exception:
                        last_seen_ts = 0.0
                if last_seen_ts <= 0 and prior.get("last_seen"):
                    try:
                        last_seen_ts = float(prior.get("last_seen") or 0.0)
                    except (TypeError, ValueError):
                        last_seen_ts = 0.0
                # Only when not mid-turn (same >90s speech gap gate).
                if last_seen_ts > 0 and spoke_ts > 0 and gap_spoke > 90:
                    gap_seen = now_ts - last_seen_ts
                    gap_spoke_real = now_ts - spoke_ts
                    if (
                        gap_seen > SESSION_GREETING_GAP
                        and abs(gap_seen - gap_spoke_real) > 300
                    ):
                        bits.append(
                            f"You last saw {name} {_relative_time(gap_seen)}."
                        )
    elif face and face.get("is_stranger"):
        bits.append("You don't recognise the person in front of you.")

    # Mood: strip is single source when SOUL strip present; else legacy line.
    if soul_strip:
        bits.append(soul_strip)
        bits.append(
            "Let your inner mood colour tone naturally - never state, explain "
            "or announce it. Weave in only what fits; do not recite the desk "
            "continuity list."
        )
    elif _mood_state["text"]:
        bits.append(
            f"Your current state of mind: {_mood_state['text']}. Let it colour "
            f"your tone naturally - never state, explain or announce it."
        )

    # Work Day: soul full strip already has work_day status when SOUL.day_strip
    # works. Still inject continuity day_strip + LATE_CHECK emit when workday
    # is live and (no strip, or strip missing the status / emit need).
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
                # Avoid triple-stating when soul strip already includes this text.
                already = bool(soul_strip and strip in soul_strip)
                if not already:
                    bits.append(
                        f"{strip} Use this only if it fits the conversation; "
                        f"do not announce 'work day mode'."
                    )
                # LATE_CHECK ops strip is status-only; teach emit grammar here.
                if "awaiting answer" in strip:
                    bits.append(_WORK_AWAITING_EMIT)
        except Exception as e:
            print(f"[behaviors] day_strip inject failed: {e}")

    return ("[Context for you, Vector - " + " ".join(bits)
            + " Weave in only what naturally fits; never recite this back.]")


def _is_first_user_thread(messages: list) -> bool:
    """True when history has zero or one non-system non-empty turn (current user)."""
    n = 0
    for m in messages:
        if getattr(m, "role", None) == "system":
            continue
        if not getattr(m, "content", None):
            continue
        n += 1
        if n > 1:
            return False
    return True  # zero or one non-system turn (the current user)


# Unsolicited FSM speech (joke_idle, workday pokes) is spoken off the chat
# path, so chipper RememberedChats never gets an assistant turn. Without
# injecting it here, the next user utterance looks like a reply to the *prior
# chat* exchange (e.g. a rhetorical idle question answered as if the last
# topic was still the dog). 30 min: long enough to answer after thinking,
# short enough not to pin a stale poke onto an evening conversation.
_BEHAVIOR_THREAD_MAX_AGE_S = 30 * 60


def _message_role(m) -> str:
    if isinstance(m, dict):
        return str(m.get("role") or "")
    return str(getattr(m, "role", "") or "")


def _message_content_text(m) -> str:
    if isinstance(m, dict):
        content = m.get("content")
    else:
        content = getattr(m, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def pending_behavior_speech_text(soul, now_ts: float) -> Optional[str]:
    """Latest behavior line not yet followed by a chat reply, if still fresh.

    Ring order is chronological. A later ``source=chat`` consumes the pending
    poke (the LLM already answered it). Ambient/greeting/sensor do not.
    """
    if soul is None:
        return None
    try:
        ring = list(getattr(soul, "_notify_recent", []) or [])
    except Exception:
        return None
    pending: Optional[str] = None
    pending_t = 0.0
    for item in ring:
        if not isinstance(item, dict):
            continue
        src = str(item.get("source") or "").strip()
        text = str(item.get("text") or "").strip()
        try:
            t = float(item.get("t") or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        if src == "behavior" and text:
            pending = text
            pending_t = t
        elif src == "chat":
            pending = None
            pending_t = 0.0
    if not pending:
        return None
    if pending_t > 0 and (float(now_ts) - pending_t) > _BEHAVIOR_THREAD_MAX_AGE_S:
        return None
    return pending


def _assistant_turn_has_text(messages: list, text: str) -> bool:
    needle = (text or "").strip()
    if not needle:
        return True
    for m in messages:
        if _message_role(m) != "assistant":
            continue
        if needle in _message_content_text(m):
            return True
    return False


def _inject_pending_behavior_turn(
    turns: list, incoming: list, now_ts: float
) -> list:
    """Insert pending FSM speech as an assistant turn before the latest user."""
    soul = getattr(deps, "SOUL", None)
    try:
        pending = pending_behavior_speech_text(soul, now_ts)
    except Exception:
        pending = None
    if not pending:
        return turns
    if _assistant_turn_has_text(turns, pending) or _assistant_turn_has_text(
        incoming, pending
    ):
        return turns
    insert_at = len(turns)
    for i in range(len(turns) - 1, -1, -1):
        if turns[i].get("role") == "user":
            insert_at = i
            break
    print(
        f"[prompt] injecting behavior speech into LLM thread: {pending[:80]!r}"
    )
    turns.insert(insert_at, {"role": "assistant", "content": pending})
    return turns


def prepare_messages(
    messages: list,
    face: Optional[dict],
    *,
    now_dt: Optional[datetime] = None,
    prior_turn_ts: Optional[float] = None,
) -> list:
    """Build the LLM message list with a stable prompt prefix.

    System message holds slow-changing content (personality + Wire-Pod command
    docs + long-term memories). Volatile per-turn context rides on the latest
    user turn. Older image bytes are stripped. Conversation history is trimmed
    to LLM_MAX_HISTORY_MESSAGES (cost/context guard for cloud backends).

    ``generate`` MUST pass ``prior_turn_ts`` snapshotted *before* ``note_user_voice``.
    If ``prior_turn_ts`` is None (direct callers/tests), treat as 0.0 → wall-only
    when first message or missing clocks (see ``build_user_time_prefix``). Do not
    derive prior from soul after voice is noted — that collapses Δ to ~0.
    """
    MEMORY = deps.MEMORY
    last_user_idx = max(
        (i for i, m in enumerate(messages) if m.role == "user"),
        default=-1,
    )
    now_dt = now_dt or datetime.now()
    # Explicit snapshot preferred; None → 0.0 (first / missing-clock wall-only).
    resolved_prior = 0.0 if prior_turn_ts is None else float(prior_turn_ts)

    # Record this interaction against the current face; the returned prior
    # metadata (last-seen, count, last conversation) drives temporal context.
    prior_meta = None
    if face and not face.get("is_stranger") and face.get("face_id"):
        prior_meta = MEMORY.touch_face(face["face_id"], face.get("name"))

    context_note = _build_context_note(face, prior_meta, now_dt)
    last_user_text = _extract_last_user_text(messages)
    # Same face for memory inject as touch/context (caller should pass
    # _effective_face(); do not re-resolve independently).
    memory_section = _build_memory_section(face, last_user_text)

    # Time/gap tags only on the latest user turn (never system — cache-stable).
    is_first = _is_first_user_thread(messages)
    time_prefix = build_user_time_prefix(
        now_dt=now_dt,
        prior_turn_ts=resolved_prior,
        is_first_message=is_first,
    )

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
                parts = list(m.content)
                if time_prefix:
                    parts = [{"type": "text", "text": time_prefix}] + parts
                parts = parts + [{"type": "text", "text": context_note}]
                turns.append({
                    "role":    m.role,
                    "content": parts,
                })
            else:
                text = " ".join(
                    p.get("text", "") for p in m.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                if text:
                    turns.append({"role": m.role, "content": text})
        else:
            if is_last_user:
                body = m.content
                if time_prefix:
                    if not (isinstance(body, str) and body.startswith(time_prefix)):
                        body = f"{time_prefix}\n{body}"
                content = f"{body}\n\n{context_note}"
            else:
                content = m.content
            turns.append({"role": m.role, "content": content})

    turns = _inject_pending_behavior_turn(turns, messages, now_dt.timestamp())

    if len(turns) > MAX_HISTORY_MESSAGES:
        turns = turns[-MAX_HISTORY_MESSAGES:]

    out.extend(turns)
    return out

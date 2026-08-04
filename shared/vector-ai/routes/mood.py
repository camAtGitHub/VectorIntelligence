"""Mood state HTTP + background reflection loop."""
import asyncio
from datetime import datetime

from fastapi import APIRouter

import deps
from llm import LLM_MOOD_TEMPERATURE, SUMMARY_MODEL, _llm_timeout, llm_chat_once
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
    "You track the inner state of a small desktop robot with a dry, sardonic "
    "character. Given a short digest of how his day has gone, reply with his "
    "CURRENT state of mind as ONE short phrase: third person, lowercase, no "
    "final period, a mood rather than a list of events (e.g. 'restless after a "
    "long quiet stretch', or 'quietly content after a sociable evening'). "
    "Plain text only, under 12 words."
)


def _mood_workday_strip(now_dt: datetime) -> str:
    """Work Day day_strip when enabled (same gate as prompt_assembly context)."""
    _workday_cfg = getattr(deps, "_workday_cfg", None)
    BEHAVIOR_RUNTIME = getattr(deps, "BEHAVIOR_RUNTIME", None)
    if _workday_cfg is None or BEHAVIOR_RUNTIME is None:
        return ""
    if not getattr(_workday_cfg, "enabled", False):
        return ""
    if getattr(BEHAVIOR_RUNTIME, "workday", None) is None:
        return ""
    try:
        local_dt = now_dt
        if getattr(_workday_cfg, "tz", None) is not None:
            try:
                local_dt = datetime.now(_workday_cfg.tz)
            except Exception:
                local_dt = now_dt
        date_s = local_dt.strftime("%Y-%m-%d")
        soul = getattr(deps, "SOUL", None)
        if soul is not None:
            strip = soul.day_strip(date_s)
            if strip:
                return str(strip)
        _continuity = getattr(deps, "_continuity", None)
        if _continuity is not None:
            return str(_continuity.day_strip(date_s) or "")
    except Exception as e:
        print(f"[mood] day_strip inject failed: {e}")
    return ""


def _vision_note_and_t(lv) -> tuple:
    """Extract (note, t) from VisionDigest or dict."""
    if lv is None:
        return "", 0.0
    if isinstance(lv, dict):
        note = (lv.get("note") or lv.get("spoken") or "").strip()
        t_v = float(lv.get("t") or 0.0)
        return note, t_v
    note = (
        (getattr(lv, "note", None) or "")
        or (getattr(lv, "spoken", None) or "")
        or ""
    ).strip()
    t_v = float(getattr(lv, "t", 0.0) or 0.0)
    return note, t_v


def build_mood_digest(now_dt: datetime | None = None) -> str:
    """Build the user-content digest for mood reflection (testable, no LLM)."""
    MEMORY = deps.MEMORY
    if now_dt is None:
        now_dt = datetime.now()
    now = now_dt.timestamp()
    bits = [
        f"It is {now_dt.strftime('%A')} {_time_of_day(now_dt)}, "
        f"{now_dt.strftime('%I:%M %p')}."
    ]

    soul = getattr(deps, "SOUL", None)
    if soul is not None:
        try:
            occupied = bool(soul.occupied(now))
            if occupied:
                sess = float(soul.session_started_at() or 0.0)
                if sess > 0 and (now - sess) > 0:
                    bits.append(
                        f"Someone is at the desk "
                        f"(session started {_relative_time(now - sess)})."
                    )
                else:
                    bits.append("Someone is at the desk.")
            else:
                bits.append("The desk looks empty right now.")
            last_seen = float(getattr(soul, "last_seen_at", 0.0) or 0.0)
            if last_seen > 0:
                bits.append(
                    f"He last saw a person {_relative_time(now - last_seen)}."
                )
            last_spoke = float(getattr(soul, "last_spoke_at", 0.0) or 0.0)
            last_voice = float(getattr(soul, "last_user_voice_at", 0.0) or 0.0)
            if last_spoke > 0:
                bits.append(
                    f"He last spoke {_relative_time(now - last_spoke)}."
                )
            elif last_voice > 0:
                bits.append(
                    f"He last heard the user {_relative_time(now - last_voice)}."
                )
            try:
                lv = soul.last_vision()
            except Exception:
                lv = None
            note, t_v = _vision_note_and_t(lv)
            if note and (t_v <= 0 or (now - t_v) < 3600):
                bits.append(f"Latest glance: {note[:120]}.")
        except Exception as e:
            print(f"[mood] soul digest failed: {e}")

    strip = _mood_workday_strip(now_dt)
    if strip:
        bits.append(f"Work day status: {strip}.")

    obs = MEMORY.list_observations(limit=6, max_age_seconds=12 * 3600)
    if obs:
        bits.append(
            "Things he has noticed recently: "
            + "; ".join(o["text"] for o in reversed(obs))
            + "."
        )
    else:
        bits.append(
            "He has noticed nothing new for a good while - "
            "a static, uneventful stretch."
        )

    # Conversation recency: prefer pot last_spoke / last_user_voice so a
    # recent chat does not look like "no conversation in a long time".
    last_spoke_ts = 0.0
    if soul is not None:
        try:
            last_spoke_ts = max(
                float(getattr(soul, "last_spoke_at", 0.0) or 0.0),
                float(getattr(soul, "last_user_voice_at", 0.0) or 0.0),
            )
        except Exception:
            last_spoke_ts = 0.0
    convo = MEMORY.latest_conversation()
    if convo and convo.get("last_convo_at"):
        last_spoke_ts = max(
            last_spoke_ts, float(convo["last_convo_at"] or 0.0)
        )
    if last_spoke_ts > 0:
        gap = now - last_spoke_ts
        line = f"His last conversation was {_relative_time(gap)}"
        if convo and convo.get("last_convo_summary"):
            line += f", about: {convo['last_convo_summary']}"
        bits.append(line + ".")
    else:
        bits.append("He has not had a real conversation in a long time.")

    if _ambient_state["quiet"]:
        bits.append("He has been asked to stay quiet.")
    prior_mood = ""
    if soul is not None:
        try:
            prior_mood = (soul.mood_text() or "").strip()
        except Exception:
            prior_mood = ""
    if not prior_mood:
        prior_mood = (_mood_state.get("text") or "").strip()
    if prior_mood:
        bits.append(f"A little while ago his mood was: {prior_mood}.")

    return " ".join(bits)


async def _reflect_mood() -> None:
    """Distil the day so far into a one-line mood and persist it.
    Uses SUMMARY_MODEL (cheap/fast) via the OpenAI-compatible chat API."""
    MEMORY = deps.MEMORY
    soul = getattr(deps, "SOUL", None)
    user_content = build_mood_digest()
    try:
        mood = await llm_chat_once(
            [
                {"role": "system", "content": _MOOD_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            model=SUMMARY_MODEL,
            temperature=LLM_MOOD_TEMPERATURE,
            top_p=0.95,
            timeout=_llm_timeout(read=60.0),
            max_tokens=512,
            tag="mood",
        )
        mood = strip_markdown(mood).strip().strip('"').strip().rstrip(".").strip()
        if mood:
            if soul is not None:
                try:
                    soul.set_mood(mood, datetime.now().timestamp())
                except Exception:
                    _mood_state["text"] = mood
                    _mood_state["updated"] = datetime.now().timestamp()
                    MEMORY.set_state("mood", mood)
            else:
                _mood_state["text"] = mood
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

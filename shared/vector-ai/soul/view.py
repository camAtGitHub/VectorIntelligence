"""LLM/ops-facing soul view: long snake_case keys + relative-time English.

No raw unix in time values. Prefer:
  "It has been {N} minutes|hours|days since {event}."
Never bare "3 minutes ago" without naming the event.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Tuple

Detail = Literal["full", "lite", "ops"]


def format_duration_units(seconds: float) -> Tuple[Optional[int], str]:
    """Unit breakpoints from prompt_assembly._relative_time.

    Returns (n, unit_phrase):
      - (None, "less than a minute") for < 90s
      - (n, "minute"|"minutes"|"hour"|"hours"|"day"|"days") otherwise
    """
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 0.0
    if s < 0:
        s = 0.0
    if s < 90:
        return None, "less than a minute"
    if s < 3600:
        n = max(1, int(round(s / 60)))
        return n, "minute" if n == 1 else "minutes"
    if s < 86400:
        n = max(1, int(round(s / 3600)))
        return n, "hour" if n == 1 else "hours"
    n = max(1, int(round(s / 86400)))
    return n, "day" if n == 1 else "days"


def _elapsed(now: float, ts: float) -> Optional[float]:
    """Seconds elapsed, or None if ts never recorded. Clamp mild future skew."""
    try:
        t = float(ts or 0.0)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    try:
        n = float(now)
    except (TypeError, ValueError):
        n = 0.0
    # Future clock skew > 5s → treat as moments (0).
    if t > n + 5.0:
        return 0.0
    return max(0.0, n - t)


def it_has_been_since(
    event_label: str,
    now: float,
    ts: float,
    *,
    never_phrase: Optional[str] = None,
) -> str:
    """Full sentence: It has been N units since {event_label}."""
    gap = _elapsed(now, ts)
    if gap is None:
        if never_phrase:
            return never_phrase
        return "It has never been recorded this session."
    n, unit = format_duration_units(gap)
    label = (event_label or "that event").strip()
    if n is None:
        return f"It has been {unit} since {label}."
    return f"It has been {n} {unit} since {label}."


def time_since_compact(
    event_label: str,
    now: float,
    ts: float,
    *,
    never_phrase: Optional[str] = None,
) -> str:
    """Compact list form: Time since {event}: N min|hours|days."""
    gap = _elapsed(now, ts)
    if gap is None:
        if never_phrase:
            return never_phrase
        return f"Time since {event_label or 'that event'}: never recorded"
    n, unit = format_duration_units(gap)
    label = (event_label or "that event").strip()
    if n is None:
        return f"Time since {label}: less than a minute"
    # Compact unit words for lists.
    if unit.startswith("minute"):
        u = "min" if n != 1 else "min"
        return f"Time since {label}: {n} {u}"
    if unit.startswith("hour"):
        return f"Time since {label}: {n} {'hour' if n == 1 else 'hours'}"
    return f"Time since {label}: {n} {'day' if n == 1 else 'days'}"


def was_ago(
    subject: str,
    now: float,
    ts: float,
    *,
    never_phrase: Optional[str] = None,
) -> str:
    """Past-event form: {subject} was N units ago."""
    gap = _elapsed(now, ts)
    subj = (subject or "That event").strip()
    if gap is None:
        if never_phrase:
            return never_phrase
        return f"{subj} has never been recorded this session."
    n, unit = format_duration_units(gap)
    if n is None:
        return f"{subj} was less than a minute ago."
    return f"{subj} was {n} {unit} ago."


def _wall_clock_safe(now: float) -> str:
    try:
        dt = datetime.fromtimestamp(float(now))
    except (TypeError, ValueError, OSError):
        dt = datetime.now()
    day = str(dt.day)  # no leading zero, portable
    return f"{dt.strftime('%A %B')} {day}, {dt.strftime('%Y, %I:%M %p')}"


def _mood_updated_ts(soul: Any) -> float:
    mood_updated = float(getattr(soul, "_mood_updated", 0.0) or 0.0)
    try:
        import process_state

        u = process_state._mood_state.get("updated")
        if u:
            mood_updated = float(u or 0.0)
    except Exception:
        pass
    return mood_updated


def _person_looking_at(soul: Any, now: float) -> Optional[dict]:
    cur = soul.current_face(now) if hasattr(soul, "current_face") else None
    if not cur:
        return None
    return {
        "name": cur.get("name") or "",
        "is_stranger": bool(cur.get("is_stranger")),
        "face_id": cur.get("face_id"),
    }


def _goals_list(soul: Any) -> list:
    goals = list(getattr(soul, "goals", None) or ["", "", ""])
    while len(goals) < 3:
        goals.append("")
    return [str(g or "") for g in goals[:3]]


def _dream_text(soul: Any) -> str:
    return str(getattr(soul, "last_night_i_dreamed_about", "") or "")


def _day_strip(soul: Any, now: float) -> str:
    if not hasattr(soul, "day_strip"):
        return ""
    try:
        date_s = datetime.fromtimestamp(float(now)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        date_s = datetime.now().strftime("%Y-%m-%d")
    try:
        return str(soul.day_strip(date_s) or "")
    except Exception:
        return ""


def _soft_name(soul: Any, now: float) -> str:
    try:
        dbg = soul.presence.debug_dict(now) if soul.presence is not None else {}
        if isinstance(dbg, dict):
            for key in ("soft_name", "name_hint", "soft_name_hint"):
                v = dbg.get(key)
                if v:
                    return str(v)
            # Nested shapes
            face = dbg.get("face") or {}
            if isinstance(face, dict) and face.get("name"):
                return str(face.get("name") or "")
    except Exception:
        pass
    return ""


def llm_view(
    soul: Any,
    now: float,
    *,
    detail: Detail = "full",
) -> dict[str, Any]:
    """Self-explanatory soul export for GET/ops/LLM.

    detail:
      lite — desk clocks, mood, person, goals, dream
      full — lite + glance + recent speech + workday strip
      ops  — full + soft_name (GET /v1/soul)
    """
    now = float(now)
    detail = detail if detail in ("full", "lite", "ops") else "full"

    occupied = bool(soul.occupied(now)) if hasattr(soul, "occupied") else False
    session_ts = 0.0
    if hasattr(soul, "session_started_at"):
        try:
            session_ts = float(soul.session_started_at() or 0.0)
        except Exception:
            session_ts = 0.0

    last_seen = float(getattr(soul, "last_seen_at", 0.0) or 0.0)
    last_spoke = float(getattr(soul, "last_spoke_at", 0.0) or 0.0)
    last_voice = float(getattr(soul, "last_user_voice_at", 0.0) or 0.0)
    mood_text = ""
    if hasattr(soul, "mood_text"):
        try:
            mood_text = str(soul.mood_text() or "")
        except Exception:
            mood_text = str(getattr(soul, "_mood_text", "") or "")
    else:
        mood_text = str(getattr(soul, "_mood_text", "") or "")
    mood_updated = _mood_updated_ts(soul)

    view: dict[str, Any] = {
        "snapshot_generated_at_wall_clock": _wall_clock_safe(now),
        "a_person_is_currently_at_the_desk": occupied,
        "time_since_this_continuous_desk_session_started": it_has_been_since(
            "this continuous desk session started",
            now,
            session_ts,
            never_phrase="No continuous desk session is active yet.",
        ),
        "time_since_i_last_visually_detected_a_person": it_has_been_since(
            "I last visually detected a person",
            now,
            last_seen,
            never_phrase="I have not visually detected a person yet this session.",
        ),
        "time_since_i_last_spoke_aloud": it_has_been_since(
            "I last spoke aloud",
            now,
            last_spoke,
            never_phrase="I have not spoken aloud yet this session.",
        ),
        "time_since_the_human_last_spoke_to_me": it_has_been_since(
            "the human last spoke to me",
            now,
            last_voice,
            never_phrase="The human has not spoken to me yet this session.",
        ),
        "person_i_currently_believe_i_am_looking_at": _person_looking_at(soul, now),
        "my_current_inner_mood_in_one_phrase": mood_text,
        "time_since_my_mood_was_last_reflected": it_has_been_since(
            "my mood was last reflected",
            now,
            mood_updated,
            never_phrase="My mood has not been reflected yet this session.",
        ),
        "three_goals_i_currently_hold": _goals_list(soul),
        "last_night_i_dreamed_about": _dream_text(soul),
    }

    if detail in ("full", "ops"):
        last_v = None
        if hasattr(soul, "last_vision"):
            try:
                last_v = soul.last_vision()
            except Exception:
                last_v = None
        note = ""
        glance_ts = 0.0
        glance_presence = ""
        if last_v is not None:
            note = str(getattr(last_v, "note", "") or "")
            glance_ts = float(getattr(last_v, "t", 0.0) or 0.0)
            glance_presence = str(getattr(last_v, "presence", "") or "")
            if not note and isinstance(last_v, dict):
                note = str(last_v.get("note") or "")
                glance_ts = float(last_v.get("t") or 0.0)
                glance_presence = str(last_v.get("presence") or "")

        view["what_i_noted_in_my_most_recent_camera_glance"] = note
        view["time_since_my_most_recent_camera_glance"] = it_has_been_since(
            "my most recent camera glance",
            now,
            glance_ts,
            never_phrase="I have not taken a camera glance yet this session.",
        )
        if glance_presence in ("person", "empty"):
            view["whether_that_glance_saw_a_person_or_empty_desk"] = glance_presence
        else:
            view["whether_that_glance_saw_a_person_or_empty_desk"] = (
                glance_presence or "unknown"
            )

        day = _day_strip(soul, now)
        view["work_day_accountability_status_for_today"] = day

        recent: list = []
        try:
            ring = list(getattr(soul, "_notify_recent", []) or [])
        except Exception:
            ring = []
        for item in ring:
            if not isinstance(item, dict):
                continue
            recent.append(
                {
                    "time_since_i_said_this": time_since_compact(
                        "I said this",
                        now,
                        float(item.get("t") or 0.0),
                    ),
                    "speech_source": str(item.get("source") or ""),
                    "text_preview": str(item.get("text") or "")[:120],
                }
            )
        view["recent_things_i_said_aloud"] = recent

        # Optional composite last contact (max of spoke / voice / seen).
        contact_candidates = [t for t in (last_spoke, last_voice, last_seen) if t > 0]
        if contact_candidates:
            last_contact = max(contact_candidates)
            view["time_since_last_contact_with_the_human"] = it_has_been_since(
                "last contact with the human",
                now,
                last_contact,
            )
        else:
            view["time_since_last_contact_with_the_human"] = (
                "There has been no contact with the human yet this session."
            )

    if detail == "ops":
        soft = _soft_name(soul, now)
        view["soft_name_hint_from_presence"] = soft

    return view


def format_soul_for_prompt(
    soul: Any,
    now: float,
    *,
    detail: Detail = "full",
) -> str:
    """Turn llm_view into labeled prose lines for chat / greeting context.

    Prefer labeled lines (not raw JSON). detail=full includes glance, workday,
    and a short recent-speech sample; lite omits those heavier rings.
    Empty string when soul is missing or view is unusable.
    """
    if soul is None:
        return ""
    try:
        view = llm_view(soul, float(now), detail=detail)
    except Exception:
        return ""
    if not isinstance(view, dict) or not view:
        return ""

    lines: list[str] = ["Desk continuity (do not recite verbatim):"]

    occupied = view.get("a_person_is_currently_at_the_desk")
    if occupied is not None:
        lines.append(
            f"- a person is currently at the desk: "
            f"{'yes' if occupied else 'no'}"
        )

    for key, label in (
        (
            "time_since_this_continuous_desk_session_started",
            "how long this continuous desk session has been going",
        ),
        (
            "time_since_i_last_visually_detected_a_person",
            "how long since I last visually detected a person",
        ),
        (
            "time_since_i_last_spoke_aloud",
            "how long ago I last spoke aloud",
        ),
        (
            "time_since_the_human_last_spoke_to_me",
            "how long since the human last spoke to me",
        ),
    ):
        val = (view.get(key) or "").strip()
        if val:
            lines.append(f"- {label}: {val}")

    person = view.get("person_i_currently_believe_i_am_looking_at")
    if isinstance(person, dict) and (person.get("name") or "").strip():
        who = str(person.get("name") or "").strip()
        if person.get("is_stranger"):
            lines.append(f"- person I believe I am looking at: stranger ({who})")
        else:
            lines.append(f"- person I believe I am looking at: {who}")

    mood = (view.get("my_current_inner_mood_in_one_phrase") or "").strip()
    if mood:
        lines.append(f"- my current inner mood: {mood}")
        mood_age = (view.get("time_since_my_mood_was_last_reflected") or "").strip()
        if mood_age:
            lines.append(f"- mood last reflected: {mood_age}")

    goals = view.get("three_goals_i_currently_hold") or []
    if isinstance(goals, list):
        filled = [str(g).strip() for g in goals if str(g or "").strip()]
        if filled:
            numbered = " ".join(
                f"({i}) {g}" for i, g in enumerate(filled, start=1)
            )
            lines.append(f"- three goals I currently hold: {numbered}")

    dream = (view.get("last_night_i_dreamed_about") or "").strip()
    if dream:
        lines.append(f"- last night I dreamed about: {dream[:200]}")

    if detail in ("full", "ops"):
        glance = (view.get("what_i_noted_in_my_most_recent_camera_glance") or "").strip()
        if glance:
            lines.append(f"- most recent camera glance: {glance[:120]}")
            glance_when = (
                view.get("time_since_my_most_recent_camera_glance") or ""
            ).strip()
            if glance_when:
                lines.append(f"- glance age: {glance_when}")
            saw = (
                view.get("whether_that_glance_saw_a_person_or_empty_desk") or ""
            ).strip()
            if saw and saw != "unknown":
                lines.append(f"- that glance saw: {saw}")

        day = (view.get("work_day_accountability_status_for_today") or "").strip()
        if day:
            lines.append(f"- work day status: {day}")

        recent = view.get("recent_things_i_said_aloud") or []
        if isinstance(recent, list) and recent:
            # Cap prompt noise: last 3 previews only.
            sample = recent[-3:]
            parts = []
            for item in sample:
                if not isinstance(item, dict):
                    continue
                prev = (item.get("text_preview") or "").strip()
                if not prev:
                    continue
                src = (item.get("speech_source") or "").strip()
                parts.append(f'"{prev[:80]}"' + (f" ({src})" if src else ""))
            if parts:
                lines.append(
                    "- recent things I said aloud: " + "; ".join(parts)
                )

    # Header alone is not useful.
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)

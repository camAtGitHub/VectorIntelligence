"""Ambient observation + quiet-mode HTTP endpoints."""
import random
import re
import time as _time
from datetime import datetime
from typing import Optional, Tuple

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
# here. Every glance reports machine PRESENCE (for desk occupancy). Novelty
# speech remains independent: default spoken answer is NOTHING; only genuine
# novelty yields a short line for Vector to speak (also stored as a visual
# observation for later recall).

_AMBIENT_SYSTEM = (
    PERSONA + "\n\n"
    "You have a camera. Right now NOBODY is talking to you. You are idling on "
    "your desk and have "
    "just glanced around. You are looking at a photo of what is in front of "
    "you.\n\n"
    "ALWAYS answer with this structure:\n"
    "Line 1 (machine, ALWAYS — exact tokens):\n"
    "  PRESENCE: empty\n"
    "  PRESENCE: person\n"
    "  PRESENCE: person:<NameHint>\n"
    "Use person if ANY human body evidence is visible: head, face, torso, arms, "
    "hands, legs, or clothing silhouette (e.g. a grey hoodie), even if the face "
    "or head is cut off, turned away, or occluded. Use empty only when there is "
    "NO human body evidence at all. Add :<NameHint> only if you are confident "
    "who it is; otherwise use bare person.\n"
    "Then EITHER the single word NOTHING, OR genuine novelty in EXACTLY two "
    "further lines (memory note, then spoken reaction).\n\n"
    "Your desk is a familiar, mostly unchanging place. The overwhelming "
    "majority of the time there is NOTHING worth remarking on - a desk with "
    "the usual monitor, keyboard, cables, mugs and clutter is not news, and "
    "neither is an empty, dim or dark room. Reacting to nothing, or to the "
    "same things over and over, makes you an annoyance. Your default for the "
    "speech part is the single word: NOTHING.\n\n"
    "Independence: if a person is still at the desk but you already noted them "
    "recently, still output PRESENCE: person (or person:<Name>) and then "
    "NOTHING — do not re-roast the same person every glance.\n\n"
    "React ONLY if you genuinely notice something NEW or CHANGED versus what "
    "you have already noticed recently (you will be told what that is): a new "
    "object that has appeared, something that has moved or vanished, a person "
    "or an animal arriving for the first time, an unusual mess or event. Do "
    "NOT react to ordinary desk contents. Do NOT react to anything already in "
    "your recent observations. Do NOT invent detail you cannot actually see. "
    "When in any doubt about novelty, answer NOTHING after the PRESENCE line.\n\n"
    "If - and only if - there is genuine novelty, respond with the PRESENCE "
    "line, then EXACTLY two more lines:\n"
    "Line 2: a brief, plain, factual note of what is new, for your own memory "
    "(e.g. 'a small plush toy has appeared on the desk').\n"
    "Line 3: your spoken reaction - and make it genuinely sound like "
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
    "Otherwise respond with the PRESENCE line and then exactly: NOTHING"
)

_PRESENCE_RE = re.compile(
    r"^PRESENCE\s*:\s*(empty|person(?:\s*:\s*(.+))?)\s*$",
    re.IGNORECASE,
)


def parse_ambient_llm_raw(raw: str) -> Tuple[str, Optional[str], str]:
    """Parse ambient LLM output.

    Returns (presence_kind, name_hint|None, spoken_text).

    presence_kind is ``\"empty\"``, ``\"person\"``, or ``\"unknown\"`` when the
    PRESENCE line is missing/unparseable (caller must not invent empty).
    spoken_text is \"\" for NOTHING / no novelty lines.
    """
    if not raw or not str(raw).strip():
        return "unknown", None, ""

    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    if not lines:
        return "unknown", None, ""

    presence_kind = "unknown"
    name_hint: Optional[str] = None
    body_start = 0

    # Prefer first line; also scan if model put PRESENCE later.
    for i, ln in enumerate(lines):
        m = _PRESENCE_RE.match(ln)
        if m:
            kind_raw = (m.group(1) or "").strip().lower()
            if kind_raw.startswith("person"):
                presence_kind = "person"
                hint = (m.group(2) or "").strip()
                name_hint = hint[:64] if hint else None
            elif kind_raw == "empty":
                presence_kind = "empty"
                name_hint = None
            body_start = i + 1
            break
        # Bare first-line NOTHING with no PRESENCE → unknown (no update).
        if i == 0 and ln.upper().rstrip(".!").startswith("NOTHING"):
            return "unknown", None, ""

    rest = lines[body_start:]
    if not rest:
        return presence_kind, name_hint, ""

    first = rest[0]
    if first.upper().rstrip(".!").startswith("NOTHING"):
        return presence_kind, name_hint, ""

    if len(rest) >= 2:
        spoken = " ".join(rest[1:])
    else:
        spoken = rest[0]
    spoken = _strip_for_speech(spoken)
    if not spoken or spoken.upper().startswith("NOTHING"):
        return presence_kind, name_hint, ""
    return presence_kind, name_hint, spoken


def _novelty_note_from_raw(raw: str) -> str:
    """Extract the memory note line after PRESENCE (if novelty present)."""
    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    body = []
    seen_presence = False
    for ln in lines:
        if not seen_presence and _PRESENCE_RE.match(ln):
            seen_presence = True
            continue
        if not seen_presence and ln.upper().startswith("PRESENCE"):
            seen_presence = True
            continue
        body.append(ln)
    if not body:
        return ""
    if body[0].upper().rstrip(".!").startswith("NOTHING"):
        return ""
    note = _strip_for_speech(body[0])
    if not note or note.upper().startswith("NOTHING"):
        return ""
    return note


def _soft_face_from_name_hint(name_hint: Optional[str]):
    """Map ambient name_hint to FaceIdentity; soft-match enrolled MEMORY faces."""
    from behaviors.types import FaceIdentity

    if not name_hint or not str(name_hint).strip():
        return None
    hint = str(name_hint).strip()[:64]
    # Soft-match enrolled profiles when possible (desk single-user win).
    try:
        MEMORY = deps.MEMORY
        if MEMORY is not None:
            for face_id, face_name in MEMORY.distinct_faces() or []:
                if face_name and str(face_name).strip().lower() == hint.lower():
                    fid = int(face_id)
                    if fid > 0:
                        return FaceIdentity(
                            face_id=fid, name=str(face_name)[:64], is_stranger=False
                        )
    except Exception as e:
        print(f"[ambient] name_hint soft-match failed: {e}")
    return FaceIdentity(face_id=0, name=hint, is_stranger=True)


@router.post("/v1/ambient")
async def ambient(req: AmbientRequest):
    """Ambient observation + presence.

    Always returns ``text`` for chipper (empty when nothing to say). On a real
    glance, also returns presence / occupied / name_hint for desk occupancy.
    Quiet short-circuit and LLM errors do not invent empty presence.
    """
    now = _time.time()
    last_call = _ambient_state["last_ambient_call"]

    # Sleep-cycle expiry for quiet mode: the ambient loop is gated off
    # overnight and while charging, so a long gap since the last call means
    # Vector has been through a sleep cycle - quiet mode lifts.
    sleep_gap = bool(last_call) and (now - last_call) > AMBIENT_SLEEP_GAP
    if _ambient_state["quiet"]:
        capped = (now - _ambient_state["quiet_since"]) > AMBIENT_QUIET_CAP
        if sleep_gap or capped:
            print(f"[ambient] quiet mode expiring "
                  f"({'sleep gap' if sleep_gap else '24h cap'})")
            _set_quiet(False)

    # Sleep gap clears sticky desk occupancy (new session) before this glance.
    if sleep_gap:
        try:
            rt = getattr(deps, "BEHAVIOR_RUNTIME", None)
            if rt is not None:
                rt.presence.apply_sleep_clear(now, AMBIENT_SLEEP_GAP)
                print("[ambient] sleep gap — sticky presence cleared")
        except Exception as e:
            print(f"[ambient] sleep clear failed: {e}")

    _ambient_state["last_ambient_call"] = now

    if _ambient_state["quiet"]:
        # No image processed for presence — do not invent empty/person.
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
            "First line MUST be PRESENCE: empty or PRESENCE: person "
            "(or person:<NameHint>). Then NOTHING, or the two-line novelty "
            "format. Partial people count (torso/arms/hoodie OK without face)."},
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
        # No presence update on error (do not invent empty).
        return {"text": "", "error": str(e)}

    presence_kind, name_hint, spoken = parse_ambient_llm_raw(raw)

    # Sticky presence write (only for known empty/person).
    occupied_out = presence_kind == "person"
    try:
        rt = getattr(deps, "BEHAVIOR_RUNTIME", None)
        if rt is not None and presence_kind in ("person", "empty"):
            if presence_kind == "person":
                face = _soft_face_from_name_hint(name_hint)
                rt.presence.note_person_evidence(
                    now,
                    name_hint=name_hint,
                    source="ambient",
                    face=face,
                )
            else:
                rt.presence.note_empty_evidence(now, source="ambient")
            eff = rt.presence.occupied_effective(now)
            occupied_out = eff  # sticky effective, not raw glance only
            streak = rt.presence.empty_streak
            clear_n = rt.presence.empty_streak_clear
            if presence_kind == "person":
                print(
                    f"[ambient] presence=person name_hint={name_hint!r} "
                    f"occupied_effective={eff}"
                )
            else:
                print(
                    f"[ambient] presence=empty streak={streak}/{clear_n} "
                    f"occupied_effective={eff}"
                )
        elif rt is not None:
            occupied_out = rt.presence.occupied_effective(now)
    except Exception as e:
        print(f"[ambient] presence update failed: {e}")

    if not spoken:
        print("[ambient] nothing novel")
        debug("HTTP SEND /v1/ambient", {
            "text": "", "raw": raw, "presence": presence_kind,
        })
        return {
            "text": "",
            "presence": presence_kind,
            "name_hint": name_hint,
            "occupied": occupied_out,
        }

    note = _novelty_note_from_raw(raw)
    if not note:
        note = spoken
    note = note[:300]
    MEMORY.remember_observation(note)
    print(f"[ambient] NOVELTY note={note!r} -> spoken={spoken!r}")
    return {
        "text": spoken,
        "presence": presence_kind,
        "name_hint": name_hint,
        "occupied": occupied_out,
    }


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

"""Ambient observation + quiet-mode HTTP endpoints."""
import asyncio
import time as _time
from typing import Optional

from fastapi import APIRouter

import deps
from debug_log import debug
from logging_util import print  # noqa: F401
from process_state import (
    AMBIENT_QUIET_CAP,
    AMBIENT_SLEEP_GAP,
    _ambient_state,
    _set_quiet,
)
from routes.models import AmbientQuietRequest, AmbientRequest

# Re-exports for tests / prompt checks (logic lives in soul.eye).
from soul.eye import (  # noqa: F401
    AMBIENT_SYSTEM as _AMBIENT_SYSTEM,
    novelty_note_from_raw as _novelty_note_from_raw,
    parse_ambient_llm_raw,
    soft_face_from_name_hint as _soft_face_from_name_hint,
)

router = APIRouter()

# Keep VISION_MODEL name visible in this module for static guards in tests
# (actual vision call is soul.eye → llm.VISION_MODEL).
from llm import VISION_MODEL  # noqa: F401, E402


def _resolve_soul():
    """Prefer process SOUL; else ephemeral Soul sharing runtime presence (tests)."""
    soul = getattr(deps, "SOUL", None)
    if soul is not None:
        return soul
    from soul import Soul

    rt = getattr(deps, "BEHAVIOR_RUNTIME", None)
    presence = getattr(rt, "presence", None) if rt is not None else None
    return Soul(
        presence=presence,
        memory=getattr(deps, "MEMORY", None),
    )


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
            soul = getattr(deps, "SOUL", None)
            if soul is not None:
                soul.apply_sleep_clear(now, AMBIENT_SLEEP_GAP)
                print("[ambient] sleep gap — sticky presence cleared")
                # Phase 3.5 auto dream: after sleep gap, if >20h since last dream
                # job, fire-and-forget consolidate (debounced inside run_dream_job).
                try:
                    from soul.growth import DREAM_DEBOUNCE_S, maybe_schedule_dream_job

                    last_dream = float(getattr(soul, "last_dream_job_at", 0.0) or 0.0)
                    if last_dream <= 0 or (now - last_dream) > DREAM_DEBOUNCE_S:
                        maybe_schedule_dream_job(
                            soul, now=now, reason="ambient_sleep_gap", force=False
                        )
                except Exception as de:
                    print(f"[ambient] dream schedule failed: {de}")
            else:
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

    soul = _resolve_soul()
    digest = await soul.see(now, image=req.image, reason="ambient")

    if digest.error == "image_too_large":
        print(
            f"[ambient] image payload too large "
            f"(chars={len(req.image or '')}) — skipping glance"
        )
        debug("HTTP SEND /v1/ambient image_too_large", {
            "image_len": len(req.image or ""),
        })
        return {"text": "", "error": "image_too_large"}

    if digest.error and digest.presence in ("", "unknown"):
        # LLM / eye failure: no invented presence.
        debug(f"ambient eye error: {digest.error}")
        return {"text": "", "error": digest.error}

    presence_kind = digest.presence or "unknown"
    name_hint: Optional[str] = digest.name_hint or None
    if name_hint == "":
        name_hint = None
    spoken = digest.spoken or ""
    occupied_out = bool(digest.occupied)

    if not spoken:
        print("[ambient] nothing novel")
        debug("HTTP SEND /v1/ambient", {
            "text": "", "presence": presence_kind,
        })
        return {
            "text": "",
            "presence": presence_kind,
            "name_hint": name_hint,
            "occupied": occupied_out,
        }

    note = (digest.note or spoken)[:300]
    MEMORY = deps.MEMORY
    if MEMORY is not None:
        try:
            MEMORY.remember_observation(note)
        except Exception as e:
            print(f"[ambient] remember_observation failed: {e}")
        # Update desk map from novelty note only. Background so ambient stays snappy.
        try:
            from desk_extract import extract_and_upsert_desk_items

            asyncio.create_task(extract_and_upsert_desk_items(note, MEMORY))
        except Exception as e:
            print(f"[ambient] desk extract schedule failed: {e}")
    print(f"[ambient] NOVELTY note={note!r} -> spoken={spoken!r}")
    if spoken:
        # Single egress path: soul ring + Pushover (soul/soul.py notify_speech).
        soul.notify_speech(spoken, source="ambient")
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

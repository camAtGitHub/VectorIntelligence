"""Face state HTTP endpoints."""
import time as _time

from fastapi import APIRouter

import deps
from debug_log import debug
from logging_util import print  # noqa: F401
from process_state import FACE_RECENT_WINDOW, _face_state, current_face
from routes.models import FaceSeenRequest

router = APIRouter()


def _sync_legacy_face_state(now: float, face_id: int, name: str, is_stranger: bool) -> None:
    """Keep process_state._face_state in sync for dual-read cutover / raw GET."""
    if is_stranger:
        _face_state["stranger_seen"] = now
    else:
        _face_state["enrolled_id"] = face_id
        _face_state["enrolled_name"] = name
        _face_state["enrolled_seen"] = now


@router.post("/v1/state/face_seen")
async def state_face_seen(req: FaceSeenRequest):
    name = (req.name or "").strip()
    is_stranger = (not name) or req.face_id <= 0
    now = _time.time()
    if is_stranger:
        print(f"[face] observed: id={req.face_id} (stranger)")
    else:
        print(f"[face] observed: id={req.face_id} {name!r} (enrolled)")
    debug("HTTP RECV POST /v1/state/face_seen", {
        "face_id": req.face_id, "name": name, "is_stranger": is_stranger,
    })

    # Prefer SOUL pot: face buffer + shared PresenceCache (same instance as runtime).
    soul = getattr(deps, "SOUL", None)
    if soul is not None:
        try:
            from behaviors.types import FaceIdentity

            soul.note_face(
                now,
                face_id=req.face_id,
                name=name,
                is_stranger=is_stranger,
                source="face_seen",
            )
            prev = soul.presence.snapshot
            soul.note_person(
                now,
                source="face_seen",
                name_hint=name[:64] if name else None,
                face=FaceIdentity(
                    face_id=int(req.face_id),
                    name=name[:64],
                    is_stranger=is_stranger,
                ),
                on_charger=bool(prev.on_charger),
                voice_recent=bool(prev.voice_recent),
            )
            # Dual-write legacy dict so raw GET / tests without SOUL readers stay sane.
            raw = soul.face.raw_state()
            _face_state["enrolled_id"] = raw.get("enrolled_id")
            _face_state["enrolled_name"] = raw.get("enrolled_name")
            _face_state["enrolled_seen"] = raw.get("enrolled_seen") or 0.0
            _face_state["stranger_seen"] = raw.get("stranger_seen") or 0.0
        except Exception as e:
            print(f"[face] soul write failed: {e}")
            _sync_legacy_face_state(now, req.face_id, name, is_stranger)
    else:
        # Legacy path (unit tests without SOUL wired).
        _sync_legacy_face_state(now, req.face_id, name, is_stranger)
        # Work Day / joke presence used to only update from /v1/behaviors/tick.
        # Voice-start face probes POST here and never set PresenceCache.occupied,
        # so the desk stayed "empty" and Work Day stuck in no_show. Mirror the
        # sighting into the behavior presence cache as person evidence + face.
        try:
            from behaviors.types import FaceIdentity

            rt = getattr(deps, "BEHAVIOR_RUNTIME", None)
            if rt is not None:
                prev = rt.presence.snapshot
                rt.presence.note_person_evidence(
                    now,
                    source="face_seen",
                    face=FaceIdentity(
                        face_id=int(req.face_id),
                        name=name[:64],
                        is_stranger=is_stranger,
                    ),
                    on_charger=bool(prev.on_charger),
                    voice_recent=bool(prev.voice_recent),
                )
        except Exception as e:
            print(f"[face] presence mirror failed: {e}")

    # Enrolled sighting may fire morning weather via a weather-only tick.
    # Occupancy stays note_face/note_person (no tick-payload ingest).
    # Do not return speak; chipper honors intent only.
    intent = ""
    if not is_stranger:
        rt = getattr(deps, "BEHAVIOR_RUNTIME", None)
        if rt is not None:
            from behaviors.morning_weather import MORNING_WEATHER_ID

            result = rt.tick(now, only_ids={MORNING_WEATHER_ID})
            intent = (getattr(result, "intent", None) or "")
            if intent:
                print(f"[face] intent: {intent!r}")
    return {"ok": True, "is_stranger": is_stranger, "intent": intent}


@router.get("/v1/state/face")
async def state_face():
    soul = getattr(deps, "SOUL", None)
    if soul is not None:
        return {
            "current": soul.current_face(),
            "raw": soul.face.raw_state(),
            "window_seconds": FACE_RECENT_WINDOW,
        }
    return {
        "current": current_face(),
        "raw": dict(_face_state),
        "window_seconds": FACE_RECENT_WINDOW,
    }

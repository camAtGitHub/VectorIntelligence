"""Face state HTTP endpoints."""
import time as _time

from fastapi import APIRouter

import deps
from debug_log import debug
from logging_util import print  # noqa: F401
from process_state import FACE_RECENT_WINDOW, _face_state, current_face
from routes.models import FaceSeenRequest

router = APIRouter()


@router.post("/v1/state/face_seen")
async def state_face_seen(req: FaceSeenRequest):
    name = (req.name or "").strip()
    is_stranger = (not name) or req.face_id <= 0
    now = _time.time()
    if is_stranger:
        _face_state["stranger_seen"] = now
        print(f"[face] observed: id={req.face_id} (stranger)")
    else:
        _face_state["enrolled_id"]   = req.face_id
        _face_state["enrolled_name"] = name
        _face_state["enrolled_seen"] = now
        print(f"[face] observed: id={req.face_id} {name!r} (enrolled)")
    debug("HTTP RECV POST /v1/state/face_seen", {
        "face_id": req.face_id, "name": name, "is_stranger": is_stranger,
    })
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
    return {"ok": True, "is_stranger": is_stranger}


@router.get("/v1/state/face")
async def state_face():
    return {
        "current": current_face(),
        "raw":     dict(_face_state),
        "window_seconds": FACE_RECENT_WINDOW,
    }

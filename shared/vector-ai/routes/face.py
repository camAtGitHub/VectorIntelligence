"""Face state HTTP endpoints."""
import time as _time

from fastapi import APIRouter

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
    return {"ok": True, "is_stranger": is_stranger}


@router.get("/v1/state/face")
async def state_face():
    return {
        "current": current_face(),
        "raw":     dict(_face_state),
        "window_seconds": FACE_RECENT_WINDOW,
    }

"""POST /v1/behaviors/tick and GET /v1/behaviors/state."""
import time
from datetime import datetime

from fastapi import APIRouter

import deps
import process_state
from debug_log import DEBUG
from logging_util import print  # noqa: F401
from routes.models import BehaviorTickRequest

router = APIRouter()


@router.post("/v1/behaviors/tick")
async def behaviors_tick(req: BehaviorTickRequest):
    """Chipper presence tick: occupancy every time; face only at junctures.

    Returns at most one proactive speak line and whether chipper should run a
    short face probe before the next tick (need_identity).
    """
    now = time.time()
    face_dict = None
    if req.face is not None:
        face_dict = {
            "face_id": int(req.face.face_id),
            "name": (req.face.name or "")[:64],
            "is_stranger": bool(req.face.is_stranger),
        }
    deps.BEHAVIOR_RUNTIME.ingest_tick_payload(
        now=now,
        occupied=bool(req.occupied),
        face=face_dict,
        on_charger=bool(req.on_charger),
        voice_recent=bool(req.voice_recent),
    )
    # Chipper may flag recent voice; also honor our chat-side timestamp.
    if req.voice_recent:
        process_state._LAST_USER_VOICE_TS = max(
            process_state._LAST_USER_VOICE_TS, now
        )
    result = deps.BEHAVIOR_RUNTIME.tick(now)
    if result.speak:
        print(f"[behaviors] speak: {result.speak!r} debug={result.debug}")
    elif result.need_identity:
        print(f"[behaviors] need_identity debug={result.debug}")
    out = {
        "speak": result.speak or "",
        "need_identity": bool(result.need_identity),
    }
    if DEBUG:
        out["debug"] = result.debug
    return out


@router.get("/v1/behaviors/state")
async def behaviors_state():
    """Debug/ops view of workday mode + presence cache."""
    now = time.time()
    _workday_cfg = deps._workday_cfg
    _continuity = deps._continuity
    BEHAVIOR_RUNTIME = deps.BEHAVIOR_RUNTIME
    try:
        local_dt = datetime.now(_workday_cfg.tz)
        date_s = local_dt.strftime("%Y-%m-%d")
        rec = _continuity.load_workday(date_s)
        mode = rec.mode.value
        strip = _continuity.day_strip(date_s)
    except Exception as e:
        mode, strip, date_s = "error", str(e), ""
    snap = BEHAVIOR_RUNTIME.presence.snapshot
    return {
        "workday_enabled": _workday_cfg.enabled,
        "date": date_s,
        "mode": mode,
        "day_strip": strip,
        "occupied": snap.occupied,
        "identity_fresh": BEHAVIOR_RUNTIME.presence.identity_fresh(now),
        "face": (
            {"face_id": snap.face.face_id, "name": snap.face.name,
             "is_stranger": snap.face.is_stranger}
            if snap.face else None
        ),
        "behaviors": [b.id for b in BEHAVIOR_RUNTIME.behaviors],
    }

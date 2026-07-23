"""POST /v1/behaviors/tick, GET /v1/behaviors/state (envelope v1), GET detail."""
import time

from fastapi import APIRouter, HTTPException

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
    # Chipper-attached face is hard evidence (behavior probe); current_face
    # reuse below is soft identity-only.
    hard_face = False
    if req.face is not None:
        hard_face = True
        face_dict = {
            "face_id": int(req.face.face_id),
            "name": (req.face.name or "")[:64],
            "is_stranger": bool(req.face.is_stranger),
        }
    occupied = bool(req.occupied)
    # If chipper did not attach a face this tick but a recent voice-start
    # face_seen is still current, reuse it for **identity only** (morning/late
    # arm). Do NOT force occupied=True — long FACE_RECENT_WINDOW would otherwise
    # re-arm sticky after ambient empty×2 and break workday away.
    if face_dict is None:
        live = process_state.current_face()
        if live is not None:
            face_dict = {
                "face_id": int(live.get("face_id") or 0),
                "name": str(live.get("name") or "")[:64],
                "is_stranger": bool(live.get("is_stranger")),
            }
            hard_face = False  # soft cache reuse only
    # ingest: occupied or hard_face → person evidence; soft face → identity-only;
    # occupied=False alone is weak empty.
    deps.BEHAVIOR_RUNTIME.ingest_tick_payload(
        now=now,
        occupied=occupied,
        face=face_dict,
        on_charger=bool(req.on_charger),
        voice_recent=bool(req.voice_recent),
        hard_face=hard_face,
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
    """Shared multi-FSM index (envelope v1).

    Breaking change vs flat pre-envelope shape: top-level workday keys
    (`mode`, `day_strip`, `occupied`, …) are gone. Use nested `presence` /
    `arbiter` / `behaviors.<id>` cards, plus `GET /v1/behaviors/{id}` for
    private FSM fields.
    """
    now = time.time()
    return deps.BEHAVIOR_RUNTIME.build_state_index(now)


@router.get("/v1/behaviors/{behavior_id}")
async def behavior_detail(behavior_id: str):
    """Per-FSM ops/debug detail. Does not speak or advance tick policy."""
    now = time.time()
    detail = deps.BEHAVIOR_RUNTIME.behavior_status(behavior_id, now)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"behavior not registered: {behavior_id}")
    return detail

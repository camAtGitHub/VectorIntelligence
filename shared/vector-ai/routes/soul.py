"""GET /v1/soul — ops snapshot of the desk-day melting pot (no base64 frames)."""
import time

from fastapi import APIRouter, HTTPException

import deps

router = APIRouter()


@router.get("/v1/soul")
async def soul_get():
    """Return SOUL.snapshot(now) for ops/debug.

    Keys include occupied, face, last_seen/spoke, mood, eye.last_vision
    (digest only — no frame payloads), notify.recent, presence debug.
    """
    soul = getattr(deps, "SOUL", None)
    if soul is None:
        raise HTTPException(status_code=503, detail="soul not initialized")
    return soul.snapshot(time.time())

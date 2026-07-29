"""GET/PATCH /v1/soul — LLM-readable desk-day melting pot; dream/goals jobs."""
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import deps

router = APIRouter()


class SoulPatchRequest(BaseModel):
    """Ops patch using long export names only."""

    three_goals_i_currently_hold: Optional[List[str]] = Field(
        default=None,
        description="Exactly 3 goal strings (pad/truncate on apply).",
    )
    last_night_i_dreamed_about: Optional[str] = Field(
        default=None,
        description="Dream recap string (capped).",
    )


class SoulDreamRequest(BaseModel):
    """Optional body for dream / reflect_goals ops."""

    force: bool = Field(
        default=True,
        description="Bypass 20h debounce (default true for manual ops).",
    )


@router.get("/v1/soul")
async def soul_get():
    """Return SOUL.llm_view(now, detail='ops') for ops/debug/LLM.

    Long snake_case keys; duration values are English sentences
    (\"It has been X since Y\" / \"Time since …\" / never phrases). No raw unix
    time fields, no base64 frames.
    """
    soul = getattr(deps, "SOUL", None)
    if soul is None:
        raise HTTPException(status_code=503, detail="soul not initialized")
    now = time.time()
    if hasattr(soul, "llm_view"):
        return soul.llm_view(now, detail="ops")
    return soul.snapshot(now)


@router.patch("/v1/soul")
async def soul_patch(body: SoulPatchRequest):
    """Update goals and/or dream via long field names; force-save soul.json."""
    soul = getattr(deps, "SOUL", None)
    if soul is None:
        raise HTTPException(status_code=503, detail="soul not initialized")

    changed = False
    if body.three_goals_i_currently_hold is not None:
        if not hasattr(soul, "set_goals"):
            raise HTTPException(status_code=500, detail="soul lacks set_goals")
        soul.set_goals(body.three_goals_i_currently_hold, force_save=False)
        changed = True
    if body.last_night_i_dreamed_about is not None:
        if not hasattr(soul, "set_dreamed_about"):
            raise HTTPException(status_code=500, detail="soul lacks set_dreamed_about")
        soul.set_dreamed_about(body.last_night_i_dreamed_about, force_save=False)
        changed = True

    if not changed:
        raise HTTPException(
            status_code=400,
            detail=(
                "provide three_goals_i_currently_hold and/or "
                "last_night_i_dreamed_about"
            ),
        )

    if hasattr(soul, "save_now"):
        soul.save_now()

    now = time.time()
    if hasattr(soul, "llm_view"):
        return soul.llm_view(now, detail="ops")
    return soul.snapshot(now)


@router.post("/v1/soul/dream")
async def soul_dream(body: SoulDreamRequest = SoulDreamRequest()):
    """Run dream+goals LLM job with full soul view; apply + save_now."""
    soul = getattr(deps, "SOUL", None)
    if soul is None:
        raise HTTPException(status_code=503, detail="soul not initialized")
    from soul.growth import run_dream_job

    now = time.time()
    result = await run_dream_job(
        soul, now=now, reason="http_dream", force=bool(body.force)
    )
    return result


@router.post("/v1/soul/reflect_goals")
async def soul_reflect_goals(body: SoulDreamRequest = SoulDreamRequest()):
    """Goals-focused refresh with full soul (same job, cooler temperature)."""
    soul = getattr(deps, "SOUL", None)
    if soul is None:
        raise HTTPException(status_code=503, detail="soul not initialized")
    from soul.growth import run_reflect_goals_job

    now = time.time()
    result = await run_reflect_goals_job(
        soul, now=now, reason="http_reflect_goals", force=bool(body.force)
    )
    return result

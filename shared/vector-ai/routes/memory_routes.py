"""Memory debug/ops HTTP endpoints."""
from fastapi import APIRouter

import deps
from routes.models import MemoryAddRequest, MemoryForgetRequest

router = APIRouter()


@router.get("/v1/memory/list")
async def memory_list():
    mems = deps.MEMORY.list_all(limit=200)
    return {"count": len(mems), "memories": [m._asdict() for m in mems]}


@router.post("/v1/memory/remember")
async def memory_remember(req: MemoryAddRequest):
    stored = deps.MEMORY.remember(req.text)
    if stored:
        return {"stored": True, "memory": stored._asdict()}
    return {"stored": False, "reason": "duplicate or empty"}


@router.post("/v1/memory/forget")
async def memory_forget(req: MemoryForgetRequest):
    n = deps.MEMORY.forget(req.target)
    return {"deleted": n}


@router.post("/v1/memory/clear")
async def memory_clear():
    n = deps.MEMORY.clear()
    return {"deleted": n}

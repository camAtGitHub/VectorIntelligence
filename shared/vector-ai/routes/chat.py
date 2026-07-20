"""POST /v1/chat/completions."""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from chat_flow import generate
from debug_log import _redact_messages, debug
from routes.models import ChatRequest

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    debug(
        "HTTP RECV POST /v1/chat/completions",
        {
            "model": req.model,
            "stream": req.stream,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "n_messages": len(req.messages or []),
            "messages": _redact_messages(req.messages or []),
        },
    )
    return StreamingResponse(
        generate(req.messages, req.temperature or 1.0),
        media_type="text/event-stream",
    )

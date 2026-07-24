"""POST /v1/chat/completions."""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from chat_flow import generate
from debug_log import _redact_messages, debug
from llm import LLM_TEMPERATURE
from routes.models import ChatRequest

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    # Knowledge chat sampling is owned by vector-ai .env (LLM_TEMPERATURE).
    # Wire-Pod / OpenAI clients often hardcode request temperature=1.0; that is
    # logged but does not override the brain config.
    temp = LLM_TEMPERATURE
    debug(
        "HTTP RECV POST /v1/chat/completions",
        {
            "model": req.model,
            "stream": req.stream,
            "temperature": temp,
            "request_temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "n_messages": len(req.messages or []),
            "messages": _redact_messages(req.messages or []),
        },
    )
    return StreamingResponse(
        generate(req.messages, temp),
        media_type="text/event-stream",
    )

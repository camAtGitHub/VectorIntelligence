"""POST /v1/chat/completions + server-side chat session inject (bypass STT)."""
from __future__ import annotations

import json
import re
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from chat_flow import generate
from chat_session import (
    append_message,
    clear_session,
    get_messages,
    message_count,
    session_snapshot,
)
from debug_log import _redact_messages, debug
from llm import LLM_TEMPERATURE
from routes.models import ChatRequest, ChatSessionMessageRequest, Message

router = APIRouter()


def _parse_sse_content_delta(chunk: str) -> str:
    """Extract delta.content from one OpenAI-compat SSE data line (or empty)."""
    if not chunk:
        return ""
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            return content
    return ""


def _join_reply_parts(parts: List[str]) -> str:
    """Join streamed content deltas into a single assistant reply string."""
    raw = " ".join(p for p in parts if p)
    # SSE chunks are already clean_response'd; strip any residual tags for store.
    cleaned = re.sub(r"\{\{[^}]*\}\}", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def _collect_generate_reply(
    messages: list,
    temperature: Optional[float] = None,
) -> str:
    """Run generate() to completion; return joined assistant text (no SSE)."""
    parts: List[str] = []
    async for chunk in generate(messages, temperature):
        delta = _parse_sse_content_delta(chunk)
        if delta:
            parts.append(delta)
    return _join_reply_parts(parts)


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


@router.post("/v1/chat/session/message")
async def chat_session_message(req: ChatSessionMessageRequest):
    """Append user text → same brain path as completions → append assistant.

    Non-stream default returns JSON ``{session_id, reply, message_count}``.
    ``stream=true`` yields the same SSE shape as ``/v1/chat/completions`` and
    still records the full reply into the session when the stream completes.

    Voice clock ownership: only ``generate()`` calls ``SOUL.note_user_voice``
    (and ``notify_speech``). Do not note voice here — a pre-generate note would
    collapse inter-turn gap tags to ~0 when generate snapshots prior clocks.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    session_id = (req.session_id or "default").strip() or "default"
    stream = bool(req.stream)
    temp = LLM_TEMPERATURE

    append_message(session_id, "user", text)

    messages: list[Message] = [
        Message(role=m["role"], content=m.get("content") or "")
        for m in get_messages(session_id)
    ]
    debug(
        "HTTP RECV POST /v1/chat/session/message",
        {
            "session_id": session_id,
            "stream": stream,
            "n_messages": len(messages),
            "text_preview": text[:120],
        },
    )

    if stream:

        async def _stream_and_store() -> AsyncIterator[str]:
            parts: List[str] = []
            try:
                async for chunk in generate(messages, temp):
                    delta = _parse_sse_content_delta(chunk)
                    if delta:
                        parts.append(delta)
                    yield chunk
            finally:
                reply = _join_reply_parts(parts)
                if reply:
                    append_message(session_id, "assistant", reply)

        return StreamingResponse(
            _stream_and_store(),
            media_type="text/event-stream",
        )

    reply = await _collect_generate_reply(messages, temp)
    if reply:
        append_message(session_id, "assistant", reply)
    # notify_speech already run inside generate when reply non-empty.

    return {
        "session_id": session_id,
        "reply": reply,
        "message_count": message_count(session_id),
    }


@router.get("/v1/chat/session")
async def chat_session_get(
    session_id: str = Query(default="default"),
):
    """Inspect current server-side session messages (ops)."""
    return session_snapshot(session_id)


@router.delete("/v1/chat/session")
async def chat_session_delete(
    session_id: str = Query(default="default"),
):
    """Clear a session (start fresh on next message)."""
    sid = (session_id or "default").strip() or "default"
    clear_session(sid)
    return {"session_id": sid, "cleared": True, "message_count": 0}

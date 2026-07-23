"""LLM transport: env config, headers, non-stream + sentence-stream clients."""
import json
import os
import random
import re
import time
from typing import Any, AsyncIterator, Optional

import httpx

from debug_log import (
    DEBUG_MAX_CHARS,
    _redact_body,
    _redact_content,
    debug,
)
from logging_util import print  # noqa: F401

# -- LLM backend (OpenRouter by default; OpenAI-compatible HTTP only) ----------
# Wire-Pod's knowledge path stays OpenAI-compatible into *this* service.
# Only the upstream behind us changes (was local Ollama; now OpenRouter).
LLM_BASE_URL = os.getenv(
    "LLM_BASE_URL",
    os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
).rstrip("/")
LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENROUTER_API_KEY")
    or ""
).strip()
MODEL = os.getenv("LLM_MODEL", "google/gemini-2.0-flash")
# Cheap/fast model for mood reflection + conversation summaries.
SUMMARY_MODEL = os.getenv("LLM_SUMMARY_MODEL", MODEL)
# Cap user/assistant turns forwarded to the upstream LLM (cost/context guard).
# Wire-Pod may keep a longer local chat; we only send the tail.
try:
    MAX_HISTORY_MESSAGES = max(2, int(os.getenv("LLM_MAX_HISTORY_MESSAGES", "24")))
except ValueError:
    MAX_HISTORY_MESSAGES = 24
try:
    LLM_TIMEOUT_CONNECT = float(os.getenv("LLM_TIMEOUT_CONNECT", "15"))
except ValueError:
    LLM_TIMEOUT_CONNECT = 15.0
try:
    LLM_TIMEOUT_READ = float(os.getenv("LLM_TIMEOUT_READ", "120"))
except ValueError:
    LLM_TIMEOUT_READ = 120.0
# Optional OpenRouter ranking headers (harmless if empty / other providers).
LLM_HTTP_REFERER = os.getenv(
    "LLM_HTTP_REFERER",
    os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com/VectorIntelligence"),
)
LLM_APP_TITLE = os.getenv(
    "LLM_APP_TITLE",
    os.getenv("OPENROUTER_APP_TITLE", "VectorIntelligence"),
)


def _llm_headers() -> dict:
    """Auth + optional OpenRouter attribution headers for upstream calls."""
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    if LLM_HTTP_REFERER:
        h["HTTP-Referer"] = LLM_HTTP_REFERER
    if LLM_APP_TITLE:
        # OpenRouter accepts both; docs currently prefer X-OpenRouter-Title.
        h["X-Title"] = LLM_APP_TITLE
        h["X-OpenRouter-Title"] = LLM_APP_TITLE
    return h


def _chat_completions_url() -> str:
    return f"{LLM_BASE_URL}/chat/completions"


def _llm_timeout(connect: Optional[float] = None, read: Optional[float] = None) -> httpx.Timeout:
    return httpx.Timeout(
        connect if connect is not None else LLM_TIMEOUT_CONNECT,
        read=read if read is not None else LLM_TIMEOUT_READ,
    )


def _message_content(data: dict) -> str:
    """Extract assistant text from an OpenAI-compatible non-stream response."""
    try:
        return (data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


async def llm_chat_once(
    messages: list,
    *,
    model: Optional[str] = None,
    temperature: float = 1.0,
    top_p: float = 0.95,
    seed: Optional[int] = None,
    timeout: Optional[httpx.Timeout] = None,
    max_tokens: Optional[int] = None,
    tag: str = "llm_chat_once",
) -> str:
    """Single non-streaming chat completion against the configured LLM backend."""
    body: dict[str, Any] = {
        "model": model or MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "top_p": top_p,
    }
    if seed is not None:
        body["seed"] = seed
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    url = _chat_completions_url()
    debug(f"UPSTREAM SEND [{tag}] POST {url}", _redact_body(body))
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout or _llm_timeout()) as client:
        resp = await client.post(
            url,
            headers=_llm_headers(),
            json=body,
        )
        try:
            resp.raise_for_status()
        except Exception:
            debug(
                f"UPSTREAM ERROR [{tag}] status={resp.status_code} "
                f"body={resp.text[:DEBUG_MAX_CHARS]!r}"
            )
            raise
        data = resp.json()
        text = _message_content(data)
        # Debug after we already hold `text`. Never let logging/encoding kill
        # a successful completion (Windows charmap + non-ASCII model output
        # previously aborted sensor_reaction and forced chipper fallbacks).
        try:
            debug(
                f"UPSTREAM RECV [{tag}] {time.monotonic() - t0:.2f}s "
                f"status={resp.status_code} chars={len(text)}",
                {"content": _redact_content(text), "usage": data.get("usage")},
            )
        except Exception:
            pass
        return text


# Match end of a sentence: punctuation followed by whitespace or end-of-string.
_SENTENCE_END = re.compile(r'(?<=[.!?])(?:\s+|$)')


async def llm_sentence_stream(messages: list, temperature: float = 1.0) -> AsyncIterator[str]:
    """Stream chat-completion tokens and yield complete sentences as they arrive.

    Wire-Pod's stream parser splits on punctuation but only takes splitResp[1],
    discarding splitResp[2:]. If we sent a multi-sentence response as one delta,
    trailing sentences (and any trailing {{command}}) would be lost. Yielding
    one sentence per SSE chunk sidesteps that bug entirely and also lets Vector
    start speaking before the full response has generated.

    A per-request random seed + top_p<1 keeps responses from converging on the
    same high-probability tokens turn after turn (especially noticeable on
    'tell me a joke')."""
    buffer = ""
    t0 = time.monotonic()
    first_token_seen = False
    first_sentence_seen = False
    full_reply: list[str] = []
    body = {
        "model":       MODEL,
        "messages":    messages,
        "stream":      True,
        "temperature": temperature,
        "top_p":       0.95,
        "seed":        random.randint(1, 2**31 - 1),
    }
    url = _chat_completions_url()
    debug("UPSTREAM SEND [stream] POST " + url, _redact_body(body))
    async with httpx.AsyncClient(timeout=_llm_timeout()) as client:
        async with client.stream(
            "POST",
            url,
            headers=_llm_headers(),
            json=body,
        ) as resp:
            try:
                resp.raise_for_status()
            except Exception:
                err_body = ""
                try:
                    err_body = (await resp.aread())[:DEBUG_MAX_CHARS].decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass
                debug(f"UPSTREAM ERROR [stream] status={resp.status_code} body={err_body!r}")
                raise
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    delta = json.loads(raw)["choices"][0].get("delta", {}).get("content", "")
                except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                    continue
                if not delta:
                    continue
                if not first_token_seen:
                    print(f"[vector-ai] timing: LLM first token {time.monotonic() - t0:.2f}s")
                    first_token_seen = True
                    debug(f"UPSTREAM first token at {time.monotonic() - t0:.2f}s")
                buffer += delta
                while True:
                    match = _SENTENCE_END.search(buffer)
                    if not match:
                        break
                    sentence = buffer[:match.end()].strip()
                    buffer = buffer[match.end():]
                    if sentence:
                        if not first_sentence_seen:
                            print(f"[vector-ai] timing: LLM first sentence {time.monotonic() - t0:.2f}s")
                            first_sentence_seen = True
                        full_reply.append(sentence)
                        debug(f"UPSTREAM sentence: {sentence!r}")
                        yield sentence
    # Flush any trailing content that didn't end in punctuation (often a
    # trailing {{getImage||front}} or animation command).
    if buffer.strip():
        full_reply.append(buffer.strip())
        debug(f"UPSTREAM tail: {buffer.strip()!r}")
        yield buffer.strip()
    debug(
        f"UPSTREAM RECV [stream] done {time.monotonic() - t0:.2f}s "
        f"sentences={len(full_reply)}",
        {"content": _redact_content(" ".join(full_reply))},
    )

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
# Cheap/fast text model for mood, conversation summaries, desk extract,
# proactive greetings, and sensor quips. Not for images.
SUMMARY_MODEL = os.getenv("LLM_SUMMARY_MODEL", MODEL)
# Multimodal path (ambient glances + chat getImage replies).
# Falls back to MODEL so existing .env installs behave unchanged.
VISION_MODEL = os.getenv("LLM_VISION_MODEL", MODEL)
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


def _parse_temperature(raw: Optional[str], default: float = 1.0) -> float:
    """Parse OpenAI/OpenRouter temperature; clamp to [0, 2]. Bad values → default."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if v < 0.0:
        return 0.0
    if v > 2.0:
        return 2.0
    return v


# Sampling temperatures (OpenAI/OpenRouter 0–2). Each path has its own knob so
# chat creativity can be tuned without flattening mood/ambient/sensor/jokes.
# Defaults match the previous hardcodes in each call site.
LLM_TEMPERATURE = _parse_temperature(os.getenv("LLM_TEMPERATURE"), 1.0)
LLM_SUMMARY_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_SUMMARY_TEMPERATURE"), 0.3
)
LLM_MOOD_TEMPERATURE = _parse_temperature(os.getenv("LLM_MOOD_TEMPERATURE"), 0.7)
LLM_AMBIENT_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_AMBIENT_TEMPERATURE"), 0.8
)
LLM_GREETING_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_GREETING_TEMPERATURE"), 1.3
)
LLM_SENSOR_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_SENSOR_TEMPERATURE"), 1.4
)
LLM_JOKE_GENERATE_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_JOKE_GENERATE_TEMPERATURE"), 1.0
)
LLM_JOKE_CRITIC_TEMPERATURE = _parse_temperature(
    os.getenv("LLM_JOKE_CRITIC_TEMPERATURE"), 0.2
)
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


# OpenRouter unified reasoning.effort values (see reasoning-tokens docs).
_REASONING_EFFORTS = frozenset(
    {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
)
# Sentinel: caller did not pass ``reasoning=`` → apply default_reasoning().
_REASONING_UNSET: Any = object()


def reasoning_config(
    *,
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
    enabled: Optional[bool] = None,
    exclude: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """Build an OpenRouter ``reasoning`` request object, or None if empty.

    Prefer one of ``effort`` or ``max_tokens`` (not both). ``effort="none"``
    disables reasoning on models that accept it. Models with
    ``reasoning.mandatory`` reject ``effort: "none"`` — use ``minimal`` or omit.
    """
    out: dict[str, Any] = {}
    if effort is not None:
        e = str(effort).strip().lower()
        if e:
            if e not in _REASONING_EFFORTS:
                print(f"[llm] ignoring unknown reasoning effort {effort!r}")
            else:
                out["effort"] = e
    if max_tokens is not None:
        try:
            mt = int(max_tokens)
        except (TypeError, ValueError):
            mt = 0
        if mt > 0:
            out["max_tokens"] = mt
    if enabled is not None:
        out["enabled"] = bool(enabled)
    if exclude is not None:
        out["exclude"] = bool(exclude)
    return out or None


def default_reasoning() -> Optional[dict[str, Any]]:
    """Default OpenRouter reasoning block for all LLM calls.

    Controlled by ``LLM_REASONING_EFFORT`` (default ``none`` = disable thinking).
    Set to empty / ``omit`` to leave the field off the request entirely.
    Set ``minimal`` / ``low`` / … if a model rejects ``none`` (mandatory).
    """
    s = str(os.getenv("LLM_REASONING_EFFORT", "none")).strip().lower()
    if s in ("", "omit"):
        return None
    if s in ("off", "false", "disabled", "disable"):
        s = "none"
    return reasoning_config(effort=s)


def _resolve_reasoning(reasoning: Any) -> Optional[dict[str, Any]]:
    """Map call-site arg to body value: unset → default; explicit None → omit."""
    if reasoning is _REASONING_UNSET:
        return default_reasoning()
    if reasoning is None:
        return None
    if isinstance(reasoning, dict):
        return reasoning
    print(f"[llm] ignoring non-dict reasoning arg {type(reasoning)!r}")
    return default_reasoning()


def _message_content(data: dict) -> str:
    """Extract assistant text from an OpenAI-compatible non-stream response."""
    try:
        return (data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


# Base64 JPEG payload size guard (~4 MB decoded ≈ 5.5M chars base64).
# Real Vector frames are far smaller; this rejects pathological payloads before
# they hit the LLM or blow memory.
MAX_IMAGE_B64_CHARS = 5_500_000


def image_payload_too_large(image_b64: Optional[str], *, limit: int = MAX_IMAGE_B64_CHARS) -> bool:
    """True when raw base64 (or data-URL) string exceeds the safe char cap."""
    if not image_b64:
        return False
    s = image_b64
    if isinstance(s, str) and s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return len(s) > int(limit)


async def llm_chat_once(
    messages: list,
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    top_p: float = 0.95,
    seed: Optional[int] = None,
    timeout: Optional[httpx.Timeout] = None,
    max_tokens: Optional[int] = None,
    reasoning: Any = _REASONING_UNSET,
    tag: str = "llm_chat_once",
) -> str:
    """Single non-streaming chat completion against the configured LLM backend.

    ``temperature=None`` uses ``LLM_TEMPERATURE`` from the environment.
    ``reasoning`` defaults from ``LLM_REASONING_EFFORT`` (usually ``none`` =
    disable). Pass an explicit dict to override, or ``None`` to omit the field.
    """
    temp = LLM_TEMPERATURE if temperature is None else float(temperature)
    body: dict[str, Any] = {
        "model": model or MODEL,
        "messages": messages,
        "stream": False,
        "temperature": temp,
        "top_p": top_p,
    }
    if seed is not None:
        body["seed"] = seed
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    resolved = _resolve_reasoning(reasoning)
    if resolved is not None:
        body["reasoning"] = resolved
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


async def one_shot_spoken_line(
    messages: list,
    *,
    temperature: float,
    max_tokens: int = 128,
    timeout: Optional[httpx.Timeout] = None,
    top_p: float = 0.95,
    seed: Optional[int] = None,
    model: Optional[str] = None,
    reasoning: Any = _REASONING_UNSET,
    tag: str = "one_shot",
) -> str:
    """Non-stream LLM call + speech strip; errors → \"\" (never raises).

    Shared by sensor / greeting (and similar one-line spoken paths). Preserves
    each caller's temperature / token / timeout knobs. Reasoning defaults from
    ``LLM_REASONING_EFFORT`` like ``llm_chat_once``.
    """
    try:
        from response_cleanup import _strip_for_speech

        text = await llm_chat_once(
            messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            timeout=timeout,
            max_tokens=max_tokens,
            reasoning=reasoning,
            tag=tag,
        )
        return _strip_for_speech(text or "")
    except Exception as e:
        print(f"[{tag}] error: {e}")
        debug(f"{tag} error: {e}")
        return ""


# Match end of a sentence: punctuation followed by whitespace or end-of-string.
_SENTENCE_END = re.compile(r'(?<=[.!?])(?:\s+|$)')


def _tag_open_depth(text: str) -> int:
    """Net unmatched ``{{`` open depth in text (0 = balanced / outside tags)."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            if depth > 0:
                depth -= 1
            i += 2
            continue
        i += 1
    return depth


def find_sentence_end(buffer: str) -> Optional[int]:
    """Index just past a complete sentence, or None if not yet complete.

    Does not treat ``.!?`` as sentence ends while inside an unmatched
    ``{{...}}`` tag region, so tags like ``{{remember||Dr. Smith is Cam's
    dentist}}`` stay in one chunk when possible.
    """
    if not buffer:
        return None
    for match in _SENTENCE_END.finditer(buffer):
        # Prefix ends at/after the sentence-ending punct (match starts on
        # trailing whitespace or EOS after the lookbehind).
        if _tag_open_depth(buffer[: match.start()]) == 0:
            return match.end()
    return None


def split_sentences_from_buffer(buffer: str) -> tuple[list[str], str]:
    """Pure helper: peel complete sentences off buffer; return (sentences, rest).

    Used by ``llm_sentence_stream`` and unit tests (no network).
    """
    out: list[str] = []
    while True:
        end = find_sentence_end(buffer)
        if end is None:
            break
        sentence = buffer[:end].strip()
        buffer = buffer[end:]
        if sentence:
            out.append(sentence)
    return out, buffer


async def llm_sentence_stream(
    messages: list,
    temperature: Optional[float] = None,
    *,
    model: Optional[str] = None,
    reasoning: Any = _REASONING_UNSET,
) -> AsyncIterator[str]:
    """Stream chat-completion tokens and yield complete sentences as they arrive.

    Wire-Pod's stream parser splits on punctuation but only takes splitResp[1],
    discarding splitResp[2:]. If we sent a multi-sentence response as one delta,
    trailing sentences (and any trailing {{command}}) would be lost. Yielding
    one sentence per SSE chunk sidesteps that bug entirely and also lets Vector
    start speaking before the full response has generated.

    Sentence ends inside unmatched ``{{...}}`` tags are held until the tag
    closes (or the stream ends), so remember/work tags with internal periods
    are not fractured.

    A per-request random seed + top_p<1 keeps responses from converging on the
    same high-probability tokens turn after turn (especially noticeable on
    'tell me a joke'). ``temperature=None`` uses ``LLM_TEMPERATURE`` from env.
    ``model=None`` uses ``MODEL`` (``LLM_MODEL``); pass ``VISION_MODEL`` for
    multimodal getImage reply turns. Reasoning defaults from
    ``LLM_REASONING_EFFORT`` like ``llm_chat_once``.
    """
    temp = LLM_TEMPERATURE if temperature is None else float(temperature)
    buffer = ""
    t0 = time.monotonic()
    first_token_seen = False
    first_sentence_seen = False
    full_reply: list[str] = []
    body: dict[str, Any] = {
        "model":       model or MODEL,
        "messages":    messages,
        "stream":      True,
        "temperature": temp,
        "top_p":       0.95,
        "seed":        random.randint(1, 2**31 - 1),
    }
    resolved = _resolve_reasoning(reasoning)
    if resolved is not None:
        body["reasoning"] = resolved
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
                sentences, buffer = split_sentences_from_buffer(buffer)
                for sentence in sentences:
                    if not first_sentence_seen:
                        print(f"[vector-ai] timing: LLM first sentence {time.monotonic() - t0:.2f}s")
                        first_sentence_seen = True
                    full_reply.append(sentence)
                    debug(f"UPSTREAM sentence: {sentence!r}")
                    yield sentence
    # Flush any trailing content that didn't end in punctuation (often a
    # trailing {{getImage||now}} or animation command).
    if buffer.strip():
        full_reply.append(buffer.strip())
        debug(f"UPSTREAM tail: {buffer.strip()!r}")
        yield buffer.strip()
    debug(
        f"UPSTREAM RECV [stream] done {time.monotonic() - t0:.2f}s "
        f"sentences={len(full_reply)}",
        {"content": _redact_content(" ".join(full_reply))},
    )

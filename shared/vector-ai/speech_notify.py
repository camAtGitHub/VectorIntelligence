"""Pushover notify for text VectorIntelligence is about to send to TTS.

Fire-and-forget: never blocks chat streaming or proactive speak paths.
Requires both PUSHOVER_APP_KEY (app token) and PUSHOVER_USER_KEY in .env.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import httpx

from logging_util import print  # noqa: F401

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# Pushover hard limit is 1024; keep a small margin for safety.
_MAX_MESSAGE = 1000

_SOURCE_TITLES = {
    "chat": "Vector · chat",
    "greeting": "Vector · greeting",
    "sensor": "Vector · sensor",
    "ambient": "Vector · ambient",
    "behavior": "Vector · behavior",
}


def _api_key() -> str:
    return (os.getenv("PUSHOVER_APP_KEY") or "").strip()


def _user_key() -> str:
    return (os.getenv("PUSHOVER_USER_KEY") or "").strip()


def _timeout_s() -> float:
    try:
        return max(0.5, float(os.getenv("PUSHOVER_TIMEOUT_S", "3")))
    except ValueError:
        return 3.0


def pushover_configured() -> bool:
    """True when both app token and user key are set."""
    return bool(_api_key() and _user_key())


async def send_speech_pushover(
    text: str,
    *,
    source: str = "chat",
    token: Optional[str] = None,
    user: Optional[str] = None,
) -> bool:
    """POST one Pushover message. Returns True on HTTP success. Never raises."""
    message = (text or "").strip()
    if not message:
        return False
    app_token = (token if token is not None else _api_key()).strip()
    user_key = (user if user is not None else _user_key()).strip()
    if not app_token or not user_key:
        return False
    if len(message) > _MAX_MESSAGE:
        message = message[: _MAX_MESSAGE - 1] + "…"
    title = _SOURCE_TITLES.get(source, f"Vector · {source}")
    data = {
        "token": app_token,
        "user": user_key,
        "message": message,
        "title": title,
        "timestamp": str(int(time.time())),
    }
    try:
        async with httpx.AsyncClient(timeout=_timeout_s()) as client:
            resp = await client.post(PUSHOVER_URL, data=data)
        if resp.status_code >= 400:
            print(
                f"[pushover] HTTP {resp.status_code} source={source!r}: "
                f"{(resp.text or '')[:200]!r}"
            )
            return False
        return True
    except Exception as e:
        print(f"[pushover] send failed source={source!r}: {e}")
        return False


def schedule_speech_notify(text: str, *, source: str = "chat") -> None:
    """Queue a background Pushover send if configured and text is non-empty.

    Safe from async route handlers / streaming generators. No-op when keys are
    missing, text is empty, or there is no running event loop.
    """
    message = (text or "").strip()
    if not message:
        return
    if not pushover_configured():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        print(f"[pushover] no event loop; skipped source={source!r}")
        return
    loop.create_task(send_speech_pushover(message, source=source))

"""Server-side multi-turn chat sessions for keyboard/text inject (bypass STT).

In-memory only: restart clears history. Successive POSTs to
``/v1/chat/session/message`` continue the same conversation without the client
holding a full OpenAI ``messages[]`` list.

Does **not** merge with Wire-Pod chipper ``RememberedChats`` (v2).
"""
from __future__ import annotations

from typing import Any

from llm import MAX_HISTORY_MESSAGES

# session_id -> list of {role, content} dicts (user/assistant only).
_sessions: dict[str, list[dict[str, str]]] = {}


def _norm_sid(session_id: str | None) -> str:
    return (session_id or "default").strip() or "default"


def get_messages(session_id: str) -> list[dict[str, str]]:
    """Return a shallow copy of the session history (empty list if unknown)."""
    return list(_sessions.get(_norm_sid(session_id), []))


def message_count(session_id: str) -> int:
    return len(_sessions.get(_norm_sid(session_id), []))


def clear_session(session_id: str) -> None:
    _sessions.pop(_norm_sid(session_id), None)


def clear_all() -> None:
    """Drop every session (tests / ops)."""
    _sessions.clear()


def append_message(session_id: str, role: str, content: str) -> list[dict[str, str]]:
    """Append one turn and cap history to MAX_HISTORY_MESSAGES.

    Returns the current session list (same object held in the store).
    """
    sid = _norm_sid(session_id)
    role_s = (role or "").strip()
    text = (content or "").strip()
    if not role_s:
        raise ValueError("role required")
    hist = _sessions.setdefault(sid, [])
    hist.append({"role": role_s, "content": text})
    if len(hist) > MAX_HISTORY_MESSAGES:
        del hist[:-MAX_HISTORY_MESSAGES]
    return hist


def session_snapshot(session_id: str) -> dict[str, Any]:
    """Ops GET body for one session."""
    sid = _norm_sid(session_id)
    msgs = get_messages(sid)
    return {
        "session_id": sid,
        "messages": msgs,
        "message_count": len(msgs),
    }

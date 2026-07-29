"""Tests for server-side text chat session inject (Phase 6 — bypass STT)."""
from __future__ import annotations

import json
from typing import AsyncIterator, List

import pytest


@pytest.fixture(autouse=True)
def _clear_sessions():
    import chat_session

    chat_session.clear_all()
    yield
    chat_session.clear_all()


def _fake_sse_chunks(text: str) -> List[str]:
    """Build minimal OpenAI-compat SSE lines matching chat_flow.sse_chunk shape."""
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "delta": {"content": text},
                "finish_reason": None,
            }
        ],
    }
    stop = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return [
        f"data: {json.dumps(payload)}\n\n",
        f"data: {json.dumps(stop)}\n\n",
        "data: [DONE]\n\n",
    ]


def test_chat_session_store_cap_and_clear():
    import chat_session
    from llm import MAX_HISTORY_MESSAGES

    sid = "cap-test"
    for i in range(MAX_HISTORY_MESSAGES + 5):
        chat_session.append_message(sid, "user", f"u{i}")
    assert chat_session.message_count(sid) == MAX_HISTORY_MESSAGES
    last = chat_session.get_messages(sid)[-1]["content"]
    assert last == f"u{MAX_HISTORY_MESSAGES + 4}"

    chat_session.clear_session(sid)
    assert chat_session.message_count(sid) == 0
    assert chat_session.get_messages(sid) == []


def test_two_sequential_messages_build_multi_turn(monkeypatch):
    """Second inject must see first user+assistant in the generate() messages."""
    from fastapi.testclient import TestClient

    import service

    seen: list = []
    replies = iter(["First reply about coffee.", "Second reply remembering coffee."])

    async def _fake_generate(messages, temperature=None) -> AsyncIterator[str]:
        # Snapshot roles/content as generate would see them this turn.
        snap = [
            {
                "role": getattr(m, "role", None),
                "content": getattr(m, "content", None),
            }
            for m in messages
        ]
        seen.append(snap)
        text = next(replies)
        for chunk in _fake_sse_chunks(text):
            yield chunk

    monkeypatch.setattr("routes.chat.generate", _fake_generate)
    monkeypatch.setattr("chat_flow.generate", _fake_generate)

    client = TestClient(service.app)
    r1 = client.post(
        "/v1/chat/session/message",
        json={"text": "I like coffee", "session_id": "t1"},
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["session_id"] == "t1"
    assert body1["reply"] == "First reply about coffee."
    assert body1["message_count"] == 2  # user + assistant

    r2 = client.post(
        "/v1/chat/session/message",
        json={"text": "what was I talking about?", "session_id": "t1"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["reply"] == "Second reply remembering coffee."
    assert body2["message_count"] == 4

    assert len(seen) == 2
    # First call: only the first user turn.
    assert seen[0] == [{"role": "user", "content": "I like coffee"}]
    # Second call: prior user+assistant + new user.
    assert seen[1] == [
        {"role": "user", "content": "I like coffee"},
        {"role": "assistant", "content": "First reply about coffee."},
        {"role": "user", "content": "what was I talking about?"},
    ]

    g = client.get("/v1/chat/session", params={"session_id": "t1"})
    assert g.status_code == 200
    snap = g.json()
    assert snap["message_count"] == 4
    assert snap["messages"][0]["content"] == "I like coffee"
    assert snap["messages"][1]["role"] == "assistant"


def test_empty_text_returns_400(monkeypatch):
    from fastapi.testclient import TestClient

    import service

    async def _should_not_run(*a, **k):
        raise AssertionError("generate must not run for empty text")
        yield  # pragma: no cover

    monkeypatch.setattr("routes.chat.generate", _should_not_run)

    client = TestClient(service.app)
    r = client.post(
        "/v1/chat/session/message",
        json={"text": "   ", "session_id": "empty"},
    )
    assert r.status_code == 400
    assert "text" in (r.json().get("detail") or "").lower()

    r2 = client.post(
        "/v1/chat/session/message",
        json={"text": "", "session_id": "empty"},
    )
    assert r2.status_code == 400


def test_delete_clears_session(monkeypatch):
    from fastapi.testclient import TestClient

    import service

    n = {"i": 0}

    async def _fake_generate(messages, temperature=None) -> AsyncIterator[str]:
        n["i"] += 1
        for chunk in _fake_sse_chunks(f"reply-{n['i']}"):
            yield chunk

    monkeypatch.setattr("routes.chat.generate", _fake_generate)

    client = TestClient(service.app)
    r1 = client.post(
        "/v1/chat/session/message",
        json={"text": "hello", "session_id": "wipe"},
    )
    assert r1.status_code == 200
    assert r1.json()["message_count"] == 2

    d = client.delete("/v1/chat/session", params={"session_id": "wipe"})
    assert d.status_code == 200
    assert d.json() == {
        "session_id": "wipe",
        "cleared": True,
        "message_count": 0,
    }

    g = client.get("/v1/chat/session", params={"session_id": "wipe"})
    assert g.json()["message_count"] == 0
    assert g.json()["messages"] == []

    r2 = client.post(
        "/v1/chat/session/message",
        json={"text": "fresh start", "session_id": "wipe"},
    )
    assert r2.status_code == 200
    # Fresh: only this user + new assistant (no prior turns).
    assert r2.json()["message_count"] == 2

    # generate on second message after clear must not include "hello".
    # Re-run with capture.
    seen: list = []

    async def _capture(messages, temperature=None) -> AsyncIterator[str]:
        seen.append(
            [
                {
                    "role": getattr(m, "role", None),
                    "content": getattr(m, "content", None),
                }
                for m in messages
            ]
        )
        for chunk in _fake_sse_chunks("ok"):
            yield chunk

    monkeypatch.setattr("routes.chat.generate", _capture)
    client.delete("/v1/chat/session", params={"session_id": "wipe2"})
    client.post(
        "/v1/chat/session/message",
        json={"text": "only this", "session_id": "wipe2"},
    )
    assert seen == [[{"role": "user", "content": "only this"}]]


def test_note_user_voice_called_when_soul_present(monkeypatch):
    """Route notes user voice when SOUL is wired (generate also notes)."""
    import deps
    from fastapi.testclient import TestClient

    import service

    calls: list = []

    class _Soul:
        def note_user_voice(self, now: float) -> None:
            calls.append(float(now))

        def notify_speech(self, text: str, *, source: str = "chat") -> None:
            pass

    monkeypatch.setattr(deps, "SOUL", _Soul(), raising=False)

    async def _fake_generate(messages, temperature=None) -> AsyncIterator[str]:
        for chunk in _fake_sse_chunks("hi back"):
            yield chunk

    monkeypatch.setattr("routes.chat.generate", _fake_generate)

    client = TestClient(service.app)
    r = client.post(
        "/v1/chat/session/message",
        json={"text": "hey", "session_id": "soul"},
    )
    assert r.status_code == 200
    assert r.json()["reply"] == "hi back"
    assert len(calls) >= 1


def test_completions_route_unchanged_shape(monkeypatch):
    """POST /v1/chat/completions still streams SSE (not broken by session routes)."""
    from fastapi.testclient import TestClient

    import service

    async def _fake_generate(messages, temperature=None) -> AsyncIterator[str]:
        for chunk in _fake_sse_chunks("completions ok"):
            yield chunk

    monkeypatch.setattr("routes.chat.generate", _fake_generate)

    client = TestClient(service.app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert "completions ok" in r.text
    assert "data:" in r.text

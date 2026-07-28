"""Unit tests for Pushover speech notify (no network)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import speech_notify as sn


@pytest.fixture(autouse=True)
def _clear_pushover_env(monkeypatch):
    monkeypatch.delenv("PUSHOVER_APP_KEY", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    monkeypatch.delenv("PUSHOVER_TIMEOUT_S", raising=False)


def test_pushover_configured_requires_both_keys(monkeypatch):
    assert sn.pushover_configured() is False
    monkeypatch.setenv("PUSHOVER_APP_KEY", "app-token")
    assert sn.pushover_configured() is False
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    assert sn.pushover_configured() is True
    monkeypatch.setenv("PUSHOVER_APP_KEY", "  ")
    assert sn.pushover_configured() is False


def test_send_skips_empty_message(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "t")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "u")

    async def _run():
        assert await sn.send_speech_pushover("") is False
        assert await sn.send_speech_pushover("   ") is False

    asyncio.run(_run())


def test_send_skips_when_keys_missing():
    async def _run():
        assert await sn.send_speech_pushover("hello") is False

    asyncio.run(_run())


def test_send_posts_form_urlencoded(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")
    posts: List[Dict[str, Any]] = []

    class _Resp:
        status_code = 200
        text = '{"status":1}'

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data=None, **k):
            posts.append({"url": url, "data": dict(data or {})})
            return _Resp()

    monkeypatch.setattr(sn.httpx, "AsyncClient", _Client)

    async def _run():
        ok = await sn.send_speech_pushover("Hello desk", source="chat")
        assert ok is True

    asyncio.run(_run())
    assert len(posts) == 1
    assert posts[0]["url"] == sn.PUSHOVER_URL
    data = posts[0]["data"]
    assert data["token"] == "APP"
    assert data["user"] == "USER"
    assert data["message"] == "Hello desk"
    assert data["title"] == "Vector · chat"
    assert "timestamp" in data


def test_send_truncates_long_message(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")
    captured: Dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data=None, **k):
            captured.update(data or {})
            return _Resp()

    monkeypatch.setattr(sn.httpx, "AsyncClient", _Client)
    long = "x" * 5000

    async def _run():
        assert await sn.send_speech_pushover(long, source="behavior") is True

    asyncio.run(_run())
    assert len(captured["message"]) <= sn._MAX_MESSAGE
    assert captured["message"].endswith("…")
    assert captured["title"] == "Vector · behavior"


def test_send_http_error_returns_false(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")

    class _Resp:
        status_code = 400
        text = "bad request"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data=None, **k):
            return _Resp()

    monkeypatch.setattr(sn.httpx, "AsyncClient", _Client)

    async def _run():
        assert await sn.send_speech_pushover("hi", source="sensor") is False

    asyncio.run(_run())


def test_send_network_error_returns_false(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, data=None, **k):
            raise sn.httpx.ConnectError("nope")

    monkeypatch.setattr(sn.httpx, "AsyncClient", _Client)

    async def _run():
        assert await sn.send_speech_pushover("hi") is False

    asyncio.run(_run())


def test_schedule_noop_without_keys(monkeypatch):
    scheduled = []

    async def _fake(text, *, source="chat", token=None, user=None):
        scheduled.append(text)
        return True

    monkeypatch.setattr(sn, "send_speech_pushover", _fake)

    async def _run():
        sn.schedule_speech_notify("should not send", source="chat")
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert scheduled == []


def test_schedule_noop_empty_text(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")
    scheduled = []

    async def _fake(text, *, source="chat", token=None, user=None):
        scheduled.append(text)
        return True

    monkeypatch.setattr(sn, "send_speech_pushover", _fake)

    async def _run():
        sn.schedule_speech_notify("", source="chat")
        sn.schedule_speech_notify("  ", source="chat")
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert scheduled == []


def test_schedule_creates_task(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_KEY", "APP")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "USER")
    scheduled: List[tuple] = []

    async def _fake(text, *, source="chat", token=None, user=None):
        scheduled.append((text, source))
        return True

    monkeypatch.setattr(sn, "send_speech_pushover", _fake)

    async def _run():
        sn.schedule_speech_notify("Work time?", source="behavior")
        # Let the created task run.
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert scheduled == [("Work time?", "behavior")]


def test_behaviors_route_schedules_on_speak(monkeypatch):
    """Tick with non-empty speak must call schedule_speech_notify."""
    from routes import behaviors_http
    from routes.models import BehaviorTickRequest

    class _Result:
        speak = "Time for a break."
        need_identity = False
        debug = {}

    class _Runtime:
        def ingest_tick_payload(self, **k):
            return None

        def tick(self, now):
            return _Result()

    monkeypatch.setattr("deps.BEHAVIOR_RUNTIME", _Runtime())
    calls: List[tuple] = []

    def _sched(text, *, source="chat"):
        calls.append((text, source))

    import speech_notify

    monkeypatch.setattr(speech_notify, "schedule_speech_notify", _sched)

    async def _run():
        out = await behaviors_http.behaviors_tick(
            BehaviorTickRequest(occupied=True, on_charger=False, voice_recent=False)
        )
        assert out["speak"] == "Time for a break."

    asyncio.run(_run())
    assert calls == [("Time for a break.", "behavior")]

#!/usr/bin/env python3
"""
Vector AI Service - OpenAI-compatible proxy for Wire-Pod.

Composition root: constructs deps, starts background loops, registers routers.
Logic lives in sibling modules + routes/; this file is wiring only.

Module map:
  paths, logging_util, debug_log  — foundation
  llm, persona, process_state, deps
  vision, prompt_assembly, response_cleanup, chat_flow
  routes/*                        — thin HTTP handlers
  behaviors/                      — FSMs (unchanged package)

Wire-Pod talks to this service over the OpenAI-compatible /v1 API (unchanged).
This process is the LLM backend: by default it calls OpenRouter's
OpenAI-compatible chat completions endpoint. Personality lives in persona.txt;
command/vision rules stay in Wire-Pod's openai_prompt.
"""
# dotenv must load before llm/debug_log read os.environ at import time.
from dotenv import load_dotenv
from paths import ROOT

load_dotenv(ROOT / ".env")
load_dotenv()  # also allow process env / cwd .env to override

import asyncio
import logging
import time

from fastapi import FastAPI

import deps
import process_state
from debug_log import DEBUG, DEBUG_MAX_CHARS, debug  # noqa: F401
from llm import (  # noqa: F401 — re-exports for joke_sources / tools
    LLM_API_KEY,
    LLM_BASE_URL,
    MAX_HISTORY_MESSAGES,
    MODEL,
    SUMMARY_MODEL,
    llm_chat_once,
)
from logging_util import (
    _SkipHealthAccessLog,
    _apply_log_timestamps,
    print,  # noqa: A001,F401
)
from memory import MemoryStore
from process_state import _ambient_state, load_mood
from routes import register_routes
from routes.mood import _mood_loop
from behaviors.runtime import BehaviorRuntime
from behaviors.config import (
    load_runtime_config,
    load_workday_config,
    load_joke_config,
    load_morning_weather_config,
)
from behaviors.continuity import ContinuityStore
from behaviors.joke_sources import _joke_refill_loop

# Persistent memory: SQLite next to service.py so it lives wherever vector-ai
# is installed. Survives restarts and updates.
MEMORY = MemoryStore(ROOT / "memory.db")
deps.MEMORY = MEMORY

# -- Multi-behavior runtime (Work Day Mode first passenger) --------------------
# Intelligence lives under behaviors/; this service only wires HTTP + chat hooks.
# Soul owns one shared PresenceCache; BehaviorRuntime receives the same instance.
from behaviors.presence import PresenceCache
from soul import Soul

_runtime_cfg = load_runtime_config()
_workday_cfg = load_workday_config()
JOKE_CFG = load_joke_config()
WEATHER_CFG = load_morning_weather_config()
_continuity = ContinuityStore(ROOT / "workday.db")
_sticky = getattr(_runtime_cfg, "presence_sticky_s", None)
_streak = getattr(_runtime_cfg, "presence_empty_streak", None)
_presence_cache = PresenceCache(
    face_max_age_s=_runtime_cfg.face_cache_max_age_s,
    image_max_age_s=_runtime_cfg.image_cache_max_age_s,
    sticky_s=int(1800 if _sticky is None else _sticky),
    empty_streak_clear=int(2 if _streak is None else _streak),
)
SOUL = Soul(
    presence=_presence_cache,
    memory=MEMORY,
    continuity=_continuity,
    image_max_age_s=_runtime_cfg.image_cache_max_age_s,
)
SOUL._auto_persist = True
try:
    _soul_loaded = SOUL.load_from_disk()
    if _soul_loaded:
        print(
            "[soul] restored from soul.json "
            f"(last_spoke_at={SOUL.last_spoke_at}, "
            f"goals={[g for g in SOUL.goals if g]!r}, "
            f"dream={bool(SOUL.last_night_i_dreamed_about)})"
        )
    else:
        print("[soul] no soul.json (or empty) — starting clean")
except Exception as e:
    print(f"[soul] load_from_disk failed: {e}")
deps.SOUL = SOUL
BEHAVIOR_RUNTIME = BehaviorRuntime(
    _runtime_cfg,
    _workday_cfg,
    _continuity,
    quiet_fn=lambda: bool(_ambient_state.get("quiet")),
    voice_ts_fn=lambda: process_state._LAST_USER_VOICE_TS,
    joke_cfg=JOKE_CFG,
    presence=_presence_cache,
    weather_cfg=WEATHER_CFG,
)
deps.BEHAVIOR_RUNTIME = BEHAVIOR_RUNTIME
deps._runtime_cfg = _runtime_cfg
deps._workday_cfg = _workday_cfg
deps.JOKE_CFG = JOKE_CFG
deps._continuity = _continuity

if _workday_cfg.enabled:
    print(
        f"[behaviors] Work Day Mode ON "
        f"(tz={_workday_cfg.tz}, start={_workday_cfg.start_begin}-"
        f"{_workday_cfg.start_end}, end={_workday_cfg.end})"
    )
else:
    print("[behaviors] Work Day Mode OFF (set WORKDAY_ENABLED=1 to enable)")
if JOKE_CFG.enabled:
    print("[behaviors] Joke Idle ON (set JOKE_ENABLED=0 to disable)")
else:
    print("[behaviors] Joke Idle OFF (set JOKE_ENABLED=1 to enable)")
if WEATHER_CFG.enabled:
    print(
        "[behaviors] Morning weather ON "
        f"(lunch={WEATHER_CFG.lunch_end[0]:02d}:{WEATHER_CFG.lunch_end[1]:02d})"
    )
else:
    print("[behaviors] Morning weather OFF (set MORNING_WEATHER_ENABLED=1 to enable)")

load_mood(MEMORY)

app = FastAPI()
register_routes(app)


async def _behavior_clock_loop() -> None:
    """Every 60s: clock transitions for Work Day even if presence ticks stall."""
    while True:
        try:
            await asyncio.sleep(60)
            BEHAVIOR_RUNTIME.clock_tick(time.time())
        except Exception as e:
            print(f"[behaviors] clock loop error: {e}")


@app.on_event("startup")
async def _configure_access_log() -> None:
    # Uvicorn may rebind handlers after import; re-apply timestamps.
    _apply_log_timestamps()
    logging.getLogger("uvicorn.access").addFilter(_SkipHealthAccessLog())
    if DEBUG:
        print(
            f"[vector-ai] DEBUG logging ON -> stdout (vector-ai.log) "
            f"(max_chars={DEBUG_MAX_CHARS})"
        )
    else:
        print("[vector-ai] DEBUG logging off (set VECTORAI_DEBUG=1 in .env)")
    # Clock-only workday transitions (waiting_morning → no_show) without chipper.
    asyncio.create_task(_behavior_clock_loop())


@app.on_event("startup")
async def _start_mood_loop() -> None:
    asyncio.create_task(_mood_loop())


@app.on_event("startup")
async def _start_joke_refill_loop() -> None:
    if JOKE_CFG.enabled:
        asyncio.create_task(_joke_refill_loop(store=_continuity, cfg=JOKE_CFG))

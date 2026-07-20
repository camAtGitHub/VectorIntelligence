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
from debug_log import DEBUG, DEBUG_MAX_CHARS, _DEBUG_LOG_PATH, debug  # noqa: F401
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
from behaviors.config import load_runtime_config, load_workday_config, load_joke_config
from behaviors.continuity import ContinuityStore
from behaviors.joke_sources import _joke_refill_loop

# Persistent memory: SQLite next to service.py so it lives wherever vector-ai
# is installed. Survives restarts and updates.
MEMORY = MemoryStore(ROOT / "memory.db")
deps.MEMORY = MEMORY

# -- Multi-behavior runtime (Work Day Mode first passenger) --------------------
# Intelligence lives under behaviors/; this service only wires HTTP + chat hooks.
_runtime_cfg = load_runtime_config()
_workday_cfg = load_workday_config()
JOKE_CFG = load_joke_config()
_continuity = ContinuityStore(ROOT / "workday.db")
BEHAVIOR_RUNTIME = BehaviorRuntime(
    _runtime_cfg,
    _workday_cfg,
    _continuity,
    quiet_fn=lambda: bool(_ambient_state.get("quiet")),
    voice_ts_fn=lambda: process_state._LAST_USER_VOICE_TS,
    joke_cfg=JOKE_CFG,
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
            f"[vector-ai] DEBUG logging ON -> stdout + {_DEBUG_LOG_PATH.name} "
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

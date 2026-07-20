# Split `service.py` into Manageable Modules — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan phase-by-phase. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Scope:** Mechanical modularization of `shared/vector-ai/service.py` only. Do **not** change HTTP contracts, LLM behavior, chipper Go, FSM logic under `behaviors/`, or `memory.py` schema.

**Goal:** Break the ~2105-line FastAPI brain (`service.py`) into focused modules so new features (and humans) can edit one concern at a time, while preserving every external contract.

**Architecture:** Keep `service.py` as the **composition root** and ASGI entry (`uvicorn service:app`). Extract pure helpers and domain slices into **flat sibling modules** (same style as `memory.py`) plus a small `routes/` package of FastAPI `APIRouter`s. Mirror the existing `behaviors/` doctrine: fat logic lives in modules; `service.py` wires HTTP, startup, and shared process state.

**Tech stack:** Python 3, FastAPI `APIRouter`, `httpx`, existing flat import path (`pythonpath = shared/vector-ai`), deploy-by-copy install scripts.

---

## How to execute this plan

### Orchestrator rules

1. **One phase = one sequential worker** (or one subagent). Do not merge phases.
2. **Strict order:** Phase 0 is discovery (already done below). Implement Phase 1 → 2 → … → final verification. Each phase must leave the process importable and tests green.
3. **Move code, do not rewrite.** Prefer cut/paste of existing functions with imports adjusted. No behavior changes, no new env vars, no new routes.
4. **Stable public surface (non-negotiable):**
   - `uvicorn service:app` still works (cwd = vector-ai install dir).
   - `from service import llm_chat_once` still works (lazy import in `behaviors/joke_sources.py`).
   - All HTTP paths/payloads unchanged.
5. **After every phase:** run verification for that phase + full existing suite.
6. **Path root:** all paths mean under `VectorIntelligence/shared/vector-ai/` unless noted.

### Subagent prompt template (copy per phase)

```
You are implementing Phase N of the service.py split plan.

READ FIRST:
- docs/superpowers/plans/2026-07-20-split-service-py.md — Phase 0 Allowed APIs + the full Phase N packet
- Files listed under "Bootstrap context" for that phase

WORKDIR: VectorIntelligence/shared/vector-ai/

DO:
- Move (cut/paste) only the symbols listed for this phase
- Keep service.py as composition root re-exporting any symbols other modules import from `service`
- Update install scripts only when new top-level files/packages appear

MUST NOT:
- Change route paths, request/response shapes, or LLM call semantics
- Add top-level `from service import` inside behaviors/* (keep joke_sources lazy/injected pattern)
- Invent APIs or "improve" prompts/regexes while moving them
- Convert vector-ai into an installable pip package / rename uvicorn entry
```

---

## Phase 0: Documentation Discovery (complete)

### Sources consulted

| Source | What was extracted |
|--------|-------------------|
| `shared/vector-ai/service.py` (~2105 lines) | Full symbol map, section bands, call graph, module globals |
| `shared/vector-ai/memory.py` | Prior flat-module extraction pattern |
| `shared/vector-ai/behaviors/*` + `__init__.py` | Canonical multi-file package pattern |
| `shared/vector-ai/behaviors/joke_sources.py` | Lazy `from service import llm_chat_once`; injection preference |
| `shared/supervisor.py` ~L908–914 | `uvicorn service:app` launch |
| `linux/install.sh`, `windows/install.ps1`, `windows/setup-companion.ps1` | Explicit file copy + `cp -a behaviors` |
| `docs/FSM-implementation.md` | Doctrine: service.py = HTTP wiring only |
| `AGENTS.md` | Repo layout; `service.py` described as HTTP surface |
| `pytest.ini` | `pythonpath = shared` + `shared/vector-ai` |
| `test_behaviors.py`, `test_joke_idle.py` | No imports of `service`; behavior-only tests |

### Current structure of `service.py` (line bands)

| Lines | Concern |
|------:|---------|
| 1–124 | Logging wrappers, `app = FastAPI()`, access-log filter, startup, behavior clock loop |
| 125–211 | LLM env config, `MEMORY`, `BEHAVIOR_RUNTIME` construction |
| 213–383 | Debug redaction + `llm_chat_once` |
| 385–449 | Persona load |
| 451–535 | Face state + ambient quiet state |
| 538–654 | Mood state, reflection, mood routes |
| 657–676 | Shared Pydantic chat/sensor/ambient models |
| 679–1009 | Vision intent + message assembly (`prepare_messages`) |
| 1012–1141 | Response cleanup + memory/work command tags |
| 1144–1408 | SSE, thinking fillers, sentence stream, convo summary |
| 1413–1564 | Main `generate()` + chat route |
| 1568–1611 | Health + memory HTTP |
| 1614–1737 | Face HTTP + sensor reaction |
| 1740–1903 | Ambient + quiet |
| 1906–1988 | Behaviors tick/state |
| 1991–2105 | Proactive greeting |

### Allowed APIs / patterns (use these; do not invent)

| Pattern | Source | Rule |
|---------|--------|------|
| Flat sibling module | `memory.py` | `from memory import MemoryStore` — path-based, no package prefix |
| Nested package | `behaviors/` | Package with `__init__.py`; copied as a tree by install |
| Thin service wiring | `docs/FSM-implementation.md` L29, L245 | service owns HTTP + hooks; not fat domain logic |
| FastAPI app object | `service.py` L82 | `app = FastAPI()` must remain importable as `service.app` |
| LLM one-shot | `service.py` L336–383 | Signature of `llm_chat_once` must stay identical for joke refill |
| Lazy service import | `joke_sources.py` L352–360 | Prefer inject; lazy `from service import llm_chat_once` only |
| Startup loops | `service.py` L99–112, L631–639 | `@app.on_event("startup")` + `asyncio.create_task` |
| APIRouter (stdlib FastAPI) | FastAPI docs / existing stack | `app.include_router(router)` — no new frameworks |
| Deploy copy | install scripts | Every new top-level module or package tree must be listed in Linux + Windows installers |
| Paths next to install | `Path(__file__).resolve().parent` | When code moves, **root data dir** must stay the vector-ai install directory (use a shared `ROOT` constant) |

### Anti-patterns (do NOT)

| Anti-pattern | Why |
|--------------|-----|
| Rename ASGI target away from `service:app` | Supervisor + stop scripts hard-code it |
| `pip install -e .` / invent `pyproject.toml` as part of this split | Project deploys by copy; out of scope |
| Top-level `from service import …` inside new modules that service also imports | Import cycles (joke_sources already documents this) |
| Putting a second FSM into extracted modules | FSMs stay under `behaviors/` |
| Changing route paths or chipper payloads | External HTTP contract |
| Rewriting prompts/regex "while moving" | Behavior drift; this is a pure move |
| Leaving new files out of install scripts | Runtime tree will miss modules and crash |
| Using `Path(__file__).parent` for `memory.db` / `persona.txt` from a nested file without a shared ROOT | DBs would land in wrong directory |

### External contracts that must remain stable

| Contract | Consumer |
|----------|----------|
| `service:app` | `supervisor.py` uvicorn argv |
| `llm_chat_once` re-export on `service` | `behaviors/joke_sources.py` lazy import |
| `POST /v1/chat/completions` (SSE) | Wire-Pod knowledge provider |
| `GET /health` fields | start scripts / supervisor |
| `POST /v1/sensor_reaction`, `/v1/state/face_seen` | chipper |
| `POST /v1/ambient`, `/v1/proactive_greeting` | chipper ambient loop |
| `POST /v1/behaviors/tick` → `{speak, need_identity}` | chipper behavior-tick |
| Memory / mood / ambient quiet ops routes | ops/debug |

### Confidence + gaps

- **High** confidence on symbol map, deploy model, and sole Python importer (`joke_sources`).
- **Gap:** no `TestClient` suite for HTTP today — verification relies on import smoke tests + existing behavior tests + targeted unit tests of pure extractees. Phase final adds a minimal import/route registry check.

---

## Target layout (end state)

```text
shared/vector-ai/
  service.py                 # composition root: app, include_router, startup, re-exports
  memory.py                  # unchanged
  paths.py                   # ROOT = install dir (parent of this file tree)
  logging_util.py            # timestamped print, log formatters, health access filter
  debug_log.py               # DEBUG flags, redaction, debug()
  llm.py                     # LLM env, headers, timeouts, llm_chat_once, sentence stream
  persona.py                 # PERSONA load from persona.txt
  process_state.py           # mutable process state: face, ambient, mood, voice_ts, fillers
  vision.py                  # is_vision_intent + GETIMAGE payload
  prompt_assembly.py         # prepare_messages, memory/context sections
  response_cleanup.py        # strip_markdown, extract_memory_commands, clean_response
  chat_flow.py               # generate(), summarise, fillers, animation cap, sse_chunk
  routes/
    __init__.py              # register_all(app) or explicit includes
    models.py                # shared Pydantic request models
    health.py
    chat.py
    memory_routes.py
    face.py
    mood.py
    sensor.py
    ambient.py
    behaviors_http.py
    greeting.py
  behaviors/                 # unchanged package
  test_behaviors.py
  test_joke_idle.py
  test_service_modules.py    # NEW: pure-function + import smoke tests
```

**Dependency direction (acyclic):**

```text
paths, logging_util, debug_log
        ↓
llm  (uses debug_log)
persona, process_state, vision
        ↓
prompt_assembly, response_cleanup  (use memory, process_state, behaviors.*)
        ↓
chat_flow  (uses llm, prompt_assembly, response_cleanup, process_state)
        ↓
routes/*  (thin handlers)
        ↓
service.py  (wires app, constructs MEMORY / BEHAVIOR_RUNTIME, startup)
```

`joke_sources` continues to resolve LLM via inject or `from service import llm_chat_once` (service re-exports from `llm`).

---

## Phase 1: Foundation — `paths.py`, `logging_util.py`, `debug_log.py`

### What to implement

1. **Create `paths.py`** — single source of install root:
   - Copy the idea of `Path(__file__).resolve().parent` from `service.py` L79 / L177 / L182 / L190.
   - Export `ROOT: Path` pointing at the **vector-ai directory** (same directory that holds `service.py` after deploy). Because these new modules sit next to `service.py`, `ROOT = Path(__file__).resolve().parent` is correct for flat siblings.

2. **Create `logging_util.py`** — move from `service.py`:
   - `print` wrapper (L47–63)
   - `_apply_log_timestamps` (L66–76)
   - `_SkipHealthAccessLog` (L85–96)
   - Constants `_LOG_DATEFMT`, `_LOG_FMT`, `_orig_print`

3. **Create `debug_log.py`** — move:
   - DEBUG env parsing (L164–178)
   - `_redact_content`, `_redact_messages`, `_redact_body`, `_debug_rotate`, `debug` (L213–300)
   - Use `ROOT / "vector-ai-debug.log"` for the debug log path

4. **Update `service.py`** to import these and delete moved bodies. Keep call sites working via imports.

### Documentation references

- Existing module style: `memory.py` module docstring + flat imports
- Path usage: `service.py` L79–80, L177–178, L182

### Verification checklist

- [ ] `cd shared/vector-ai && python3 -c "import service; print(service.app.title if hasattr(service.app,'title') else 'ok', service.DEBUG)"`
- [ ] `python3 -c "from service import debug, print; from debug_log import debug as d2; assert debug is d2 or callable(d2)"`
- [ ] `python3 -m pytest shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py -q`
- [ ] Grep: no duplicate `def debug(` outside `debug_log.py` (except re-exports)

### Anti-pattern guards

- Do not change DEBUG env var names (`VECTORAI_DEBUG`, `DEBUG`, `LOG_LEVEL`)
- Do not move FastAPI `app` yet
- Do not touch install scripts yet if only flat files next to service (but **do** update them if you want deploy parity — preferred in Phase 1 already; see Phase 6)

---

## Phase 2: Extract `llm.py` (transport only)

### What to implement

1. **Create `llm.py`** — move exactly:
   - LLM config constants: `LLM_BASE_URL`, `LLM_API_KEY`, `MODEL`, `SUMMARY_MODEL`, `MAX_HISTORY_MESSAGES`, timeouts, OpenRouter headers (L125–162)
   - `_llm_headers`, `_chat_completions_url`, `_llm_timeout`, `_message_content` (L303–333)
   - `llm_chat_once` (L336–383) — **identical signature and body**
   - `llm_sentence_stream` (L1221–1307) and `_SENTENCE_END` (L1218)

2. **Import `debug` / redactors from `debug_log`** inside `llm.py` (not from service).

3. **Re-export from `service.py`:**

   ```python
   from llm import llm_chat_once, MODEL, SUMMARY_MODEL, LLM_BASE_URL, LLM_API_KEY  # etc. as needed
   ```

   Required: `from service import llm_chat_once` must succeed (joke_sources).

### Documentation references

- Signature contract: `service.py` L336–346; joke plan docs call this READ ONLY historically — re-export only
- Lazy import site: `behaviors/joke_sources.py` L352–360

### Verification checklist

- [ ] `python3 -c "from service import llm_chat_once; import inspect; print(inspect.signature(llm_chat_once))"` matches prior signature
- [ ] `python3 -c "from behaviors.joke_sources import _resolve_llm; assert _resolve_llm(None) is not None"` (may require env; at least import must not cycle)
- [ ] Import cycle smoke: `python3 -c "import service; from behaviors import joke_sources; print('ok')"`
- [ ] Full behavior tests green

### Anti-pattern guards

- Do not change keyword-only args or defaults of `llm_chat_once`
- Do not top-level import `service` from `llm.py`
- Do not move fillers / `generate` yet (they belong in chat_flow)

---

## Phase 3: Extract persona + process state

### What to implement

1. **Create `persona.py`**
   - `_DEFAULT_PERSONA`, `_PERSONA_HEADER`, `_load_persona`, `PERSONA` (L391–449)
   - Persona path: `ROOT / "persona.txt"` from `paths.py`

2. **Create `process_state.py`** holding mutable process globals and their accessors:
   - Face: `FACE_RECENT_WINDOW`, `SESSION_GREETING_GAP`, `_face_state`, `current_face` (L459–504)
   - Ambient quiet: `AMBIENT_SLEEP_GAP`, `AMBIENT_QUIET_CAP`, `_ambient_state`, `_set_quiet` (L513–535)
   - Mood dict + load/reflect helpers *or* leave reflect for Phase 4 if it pulls too many deps — preferred split:
     - State dicts + `current_face` / `_set_quiet` / `_load_mood` setters in `process_state.py`
     - `_reflect_mood` / loops stay until routes/mood phase if they need `llm_chat_once` + MEMORY
   - Voice: `_LAST_USER_VOICE_TS` (L186)
   - Greeting bookkeeping: `_recent_greetings`, `_face_last_seen`, `GREETING_ABSENCE_GAP` (L2024–2029)
   - Filler state: `_THINKING_PHRASES`, `_ALL_FILLER_PHRASES`, `_last_thinking_phrase`, `pick_thinking_phrase`, `THINKING_DELAY` (L1166–1212)

3. **Wire service construction** so `BEHAVIOR_RUNTIME` still receives:

   ```python
   quiet_fn=lambda: bool(_ambient_state.get("quiet")),
   voice_ts_fn=lambda: _LAST_USER_VOICE_TS,
   ```

   Import these names from `process_state` (same lambdas as today at L195–196).

### Documentation references

- Face resolution comments L451–466 (preserve comments when moving)
- Quiet mode semantics L507–517
- BehaviorRuntime construction L191–198

### Verification checklist

- [ ] `from process_state import current_face, _set_quiet` works
- [ ] `from persona import PERSONA` is non-empty string
- [ ] Importing `service` still constructs `BEHAVIOR_RUNTIME` without error
- [ ] Behavior tests green

### Anti-pattern guards

- Do not create a second `ContinuityStore` or second `MemoryStore`
- Do not change window constants' default values
- Avoid circular imports: `process_state` must not import `service` or route modules

---

## Phase 4: Extract prompt assembly + vision + response cleanup

### What to implement

1. **Create `vision.py`**
   - `_VISION_TRIGGERS`, `_GETIMAGE_PAYLOAD`, `is_vision_intent` (L684–728)

2. **Create `prompt_assembly.py`**
   - `_build_memory_section`, `_time_of_day`, `_relative_time`, `_effective_face`, `_build_context_note`, `prepare_messages` (L731–1009)
   - Depends on: `MEMORY` (passed in or imported from a thin `deps` — **prefer injecting MEMORY via functions that close over service-set globals, or import a `deps.py` singleton module set by service**)

3. **Create `response_cleanup.py`**
   - `strip_markdown`, phrase fixes, forbidden commands, remember/forget/quiet regexes
   - `extract_memory_commands`, `_apply_work_commands`, `clean_response` (L1012–1141)
   - `_strip_for_speech` (L1674–1678) can live here too

### Dependency injection note (required reading)

Today many helpers close over module globals (`MEMORY`, `BEHAVIOR_RUNTIME`, `_mood_state`). Two acceptable patterns (pick one and stick to it):

| Pattern | How | Prefer when |
|---------|-----|-------------|
| **A. `deps.py` singleton** | `deps.MEMORY = …` set in service at import; modules `import deps` | Closest to current global style; least code churn |
| **B. Explicit parameters** | `prepare_messages(..., memory=MEMORY)` | Cleaner tests; more signature churn |

**Recommended for this codebase: Pattern A (`deps.py`)** — mirrors existing process-global style, minimizes behavior risk. Set in `service.py` immediately after constructing `MEMORY` / `BEHAVIOR_RUNTIME`.

### Documentation references

- `prepare_messages` docstring L945–950
- Memory command order (shared before personal) L1053–1063
- Work command wiring L1098–1130; `parse_work_commands` from `behaviors.workday`

### Verification checklist

- [ ] Unit-test pure helpers in `test_service_modules.py`:
  - `is_vision_intent("what do you see")` is True
  - `strip_markdown("**x**")` == `"x"`
  - `clean_response` still strips `{{remember||x}}` when MEMORY is a temp store
- [ ] No change to regex source strings (byte-identical move)
- [ ] Behavior tests green

### Anti-pattern guards

- Do not "fix" vision regex or prompt text
- Do not reorder remember-shared vs remember processing
- `prompt_assembly` must not import FastAPI

---

## Phase 5: Extract `chat_flow.py` (generate + streaming helpers)

### What to implement

1. **Create `chat_flow.py`** — move:
   - `sse_chunk` (L1146–1158)
   - `stream_sentences_with_filler` (L1310–1338)
   - `cap_chunk_animations` (L1341–1357)
   - `_summarise_conversation` (L1361–1408)
   - `generate` (L1413–1545)

2. Wire imports from `llm`, `prompt_assembly`, `response_cleanup`, `vision`, `process_state`, `deps`, `persona`.

3. Chat route stays thin (Phase 6) or temporarily remains in service calling `generate`.

### Documentation references

- Vision backstop inside `generate` L1433–1438
- Wire-Pod single-sentence SSE constraint comments on `llm_sentence_stream` L1222–1228
- Voice timestamp update L1414–1415

### Verification checklist

- [ ] `import service` succeeds
- [ ] Manual smoke (if stack available): `POST /v1/chat/completions` still streams SSE
- [ ] Grep: `async def generate` defined once
- [ ] Behavior tests green

### Anti-pattern guards

- Do not change SSE payload shape (`object`, `choices`, `finish_reason`)
- Do not alter thinking filler delay default (`THINKING_DELAY = 2.0`)
- Do not call LLM from `behaviors` tick path

---

## Phase 6: Extract FastAPI routers under `routes/`

### What to implement

1. **Create `routes/models.py`** — Pydantic models:
   - `Message`, `ChatRequest`, `SensorReactionRequest`, `AmbientRequest`
   - `MemoryAddRequest`, `MemoryForgetRequest`, `FaceSeenRequest`
   - `AmbientQuietRequest`, `FaceIn`, `BehaviorTickRequest`, `GreetingRequest`

2. **Create one router module per HTTP concern** using FastAPI `APIRouter`:

| Module | Routes (exact paths) | Source lines (approx) |
|--------|----------------------|------------------------|
| `routes/health.py` | `GET /health` | L1567–1577 |
| `routes/chat.py` | `POST /v1/chat/completions` | L1548–1564 |
| `routes/mood.py` | `GET /v1/mood`, `POST /v1/mood/reflect` + mood loop helpers if still here | L572–651 |
| `routes/memory_routes.py` | memory list/remember/forget/clear | L1580–1611 |
| `routes/face.py` | face_seen, face | L1614–1650 |
| `routes/sensor.py` | sensor_reaction | L1653–1737 |
| `routes/ambient.py` | ambient, ambient/state, ambient/quiet | L1740–1903 |
| `routes/behaviors_http.py` | behaviors/tick, behaviors/state | L1906–1988 |
| `routes/greeting.py` | proactive_greeting | L1991–2105 |

3. **Create `routes/__init__.py`** with:

   ```python
   def register_routes(app: FastAPI) -> None:
       app.include_router(health.router)
       app.include_router(chat.router)
       # ...
   ```

4. **`service.py` composition root** should shrink to roughly:
   - imports + dotenv
   - construct `MEMORY`, configs, `BEHAVIOR_RUNTIME` into `deps`
   - `app = FastAPI()`
   - startup handlers (clock, mood, joke refill, access log)
   - `register_routes(app)`
   - re-exports: `llm_chat_once`, and any symbols tests/tools need

5. **Update install scripts** to copy new modules:

| Script | Change |
|--------|--------|
| `linux/install.sh` | After `service.py` / `memory.py`, also copy `paths.py`, `logging_util.py`, `debug_log.py`, `llm.py`, `persona.py`, `process_state.py`, `vision.py`, `prompt_assembly.py`, `response_cleanup.py`, `chat_flow.py`, `deps.py` (if used), and `cp -a routes` like behaviors |
| `windows/install.ps1` | Same set via `Copy-Item` |
| `windows/setup-companion.ps1` | Same set |

   Prefer: copy entire directory of known Python modules with a small list or `cp -a` of the whole `vector-ai` code tree excluding venv/tests if easier — but **match existing style** (explicit copies) unless you deliberately improve to "copy all `*.py` + packages".

### Documentation references

- FastAPI routers: use `APIRouter()` + `@router.get/post` (standard FastAPI; project already uses FastAPI)
- Install copy pattern: `linux/install.sh` L260–266; `windows/install.ps1` L446–451
- Startup loop pattern: `service.py` L631–639

### Verification checklist

- [ ] `python3 -c "import service; paths=sorted({r.path for r in service.app.routes if hasattr(r,'path')}); print('\n'.join(paths))"` includes all prior paths
- [ ] Required paths present:
  - `/health`
  - `/v1/chat/completions`
  - `/v1/mood`, `/v1/mood/reflect`
  - `/v1/memory/list`, `/v1/memory/remember`, `/v1/memory/forget`, `/v1/memory/clear`
  - `/v1/state/face_seen`, `/v1/state/face`
  - `/v1/sensor_reaction`
  - `/v1/ambient`, `/v1/ambient/state`, `/v1/ambient/quiet`
  - `/v1/behaviors/tick`, `/v1/behaviors/state`
  - `/v1/proactive_greeting`
- [ ] `wc -l service.py` — target **≲ 250 lines** (composition root)
- [ ] Install script grep: every new top-level module name appears in all three installers
- [ ] Behavior tests green

### Anti-pattern guards

- Do not change path strings (e.g. no `/v1/behavior/tick` typos)
- Do not add auth middleware
- Do not register duplicate routes
- Keep `@app.on_event("startup")` working (on `app` in service, not lost on routers unless intentionally moved)

---

## Phase 7: Docs + AGENTS.md + optional thin re-export cleanup

### What to implement

1. Update `AGENTS.md` repo layout section (currently lists only `service.py` + `memory.py` + `behaviors/`) to list the new modules and state that `service.py` is composition-only.
2. Update `docs/FSM-implementation.md` table row for service.py if it still says "fat" incorrectly — keep "HTTP wiring only" doctrine; note chat/ambient/sensor still live in `routes/*` + `chat_flow.py` (legacy non-plugin loops).
3. Optional: add a short comment at top of `service.py` listing module map (copy style of behaviors package docstring).

### Verification checklist

- [ ] Docs mention `uvicorn service:app` still
- [ ] Docs mention install must copy new modules / `routes/`
- [ ] No doc claims FSMs live in `service.py`

### Anti-pattern guards

- Do not rewrite user-facing Work Day / Joke guides unless paths change (they should not)

---

## Phase 8: Final verification

### What to implement / run

1. **Import + route registry smoke** (add `test_service_modules.py` if not already):
   - Import `service.app`
   - Assert route path set equality against frozen expected list from Phase 6
   - Assert `service.llm_chat_once` is the same object as `llm.llm_chat_once`
   - Assert pure helpers: vision, strip_markdown, pick_thinking_phrase returns non-empty

2. **Anti-pattern greps:**

   ```bash
   # no top-level service import in behaviors except lazy inside functions
   # (existing test_joke_idle anti-pattern already covers joke_sources)

   # service.py should not still define llm_chat_once body
   # (re-export OK)

   # install scripts mention routes or each new module
   ```

3. **Full test suite from VectorIntelligence root:**

   ```bash
   python3 -m pytest -q
   ```

4. **Manual runtime checklist** (if robot/stack available):
   - Supervisor starts vector-ai
   - `GET /health` → `status=ok`
   - Voice chat still streams
   - Behavior tick still returns JSON
   - Ambient / sensor still respond

### Verification checklist

- [ ] `pytest -q` green
- [ ] `service.py` line count ≲ 250
- [ ] No module > ~400 lines preferred (soft); none > 600 without reason
- [ ] `from service import llm_chat_once` works
- [ ] `uvicorn service:app` import path works from `shared/vector-ai` cwd
- [ ] Linux + Windows install scripts copy all new artifacts
- [ ] Zero intentional behavior changes in git diff (review prompts/regexes unchanged)

### Anti-pattern guards

- Do not claim complete without running pytest
- Do not leave dead duplicate function definitions in `service.py`
- Do not break `joke_sources` lazy import

---

## Suggested phase → file ownership (parallelism)

| Phase | Creates / primarily edits | Touches service.py? |
|------:|---------------------------|---------------------|
| 1 | `paths.py`, `logging_util.py`, `debug_log.py` | Yes (imports) |
| 2 | `llm.py` | Yes (re-export) |
| 3 | `persona.py`, `process_state.py`, maybe `deps.py` | Yes |
| 4 | `vision.py`, `prompt_assembly.py`, `response_cleanup.py` | Yes |
| 5 | `chat_flow.py` | Yes |
| 6 | `routes/*`, install scripts | Yes (shrink) |
| 7 | docs | No code behavior |
| 8 | `test_service_modules.py` | Minimal |

Phases are **sequential** because each mutates `service.py`. Do not parallelize implementation phases.

---

## Size budget (guidance, not hard CI gates)

| Module | Soft max lines | Rationale |
|--------|---------------:|-----------|
| `service.py` | 250 | Composition root |
| `llm.py` | 250 | Transport only |
| `chat_flow.py` | 400 | generate + stream helpers |
| `prompt_assembly.py` | 350 | Context building |
| `response_cleanup.py` | 200 | Tag parsing |
| Each `routes/*.py` | 200 | Thin HTTP |
| `process_state.py` | 250 | State + small accessors |

---

## Out of scope (explicit)

- Migrating ambient / sensor / greeting into `behaviors/` plugins (separate design; see FSM-implementation.md)
- Adding HTTP auth, OpenAPI polish, or new endpoints
- Refactoring `memory.py` or `behaviors/*` internals
- Changing supervisor, chipper, or Wire-Pod knowledge config
- Introducing poetry/pip packaging for vector-ai

---

## Rollback

Each phase is a single git commit (recommended messages):

1. `refactor(vector-ai): extract logging and debug helpers from service`
2. `refactor(vector-ai): extract llm client module`
3. `refactor(vector-ai): extract persona and process state`
4. `refactor(vector-ai): extract prompt assembly and response cleanup`
5. `refactor(vector-ai): extract chat flow from service`
6. `refactor(vector-ai): split HTTP routes into package; update installers`
7. `docs(vector-ai): document modular service layout`
8. `test(vector-ai): add service module import and route smoke tests`

Revert one commit if a phase breaks runtime.

---

## Success criteria

1. **`service.py` is a thin composition root** (< ~250 lines) that constructs deps, starts background loops, and registers routers.
2. **All external contracts preserved** (`service:app`, `llm_chat_once`, HTTP surface).
3. **Install scripts deploy every new module** on Linux and Windows.
4. **Existing pytest suite green** plus new smoke tests for routes/import.
5. **Dependency graph is acyclic**; joke refill still lazy-imports LLM from `service`.
6. **No intentional prompt/regex/behavior edits** in the refactor diff.

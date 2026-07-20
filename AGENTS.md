# Vector Intelligence
`Make an Anki/DDL Vector a smarter desk companion — persona, memory, vision notes, ambient awareness, optional work-day accountability`

Local brain (**vector-ai**) + **Wire-Pod** + cloud LLM (**OpenRouter** by default). Wire-Pod still owns pairing, wake word, STT, and TTS; vector-ai is the OpenAI-compatible knowledge endpoint on localhost.

Tested on **Windows 10/11** and Debian-family Linux.

## What This Project Is

| Piece | Role |
|-------|------|
| **Wire-Pod (chipper)** | Robot pairing, STT, TTS, optional patched aliveness loops |
| **vector-ai** | FastAPI brain — persona, SQLite memory, mood, multi-behavior runtime |
| **LLM** | OpenRouter (OpenAI-compatible HTTPS); no required local Ollama |
| **supervisor** | Keeps vector-ai (and full-stack chipper/mDNS) alive; log rotation |

Two install modes:

| Mode | What you get |
|------|----------------|
| **Companion** | Packaged Wire-Pod + this brain (chat, persona, memory). No ambient/sensors/behavior-tick. |
| **Full** | Builds patched chipper: ambient, sensors, face probe, behavior tick, Work Day proactive speech |

Hands-on install checklist: [NEXT_STEPS.md](NEXT_STEPS.md). User Work Day guide: [docs/FSM-workday-companion.md](docs/FSM-workday-companion.md).

## Environment Notes

- **Python 3.11+** for the brain (Windows companion explicitly expects 3.11).
- Runtime deps live in `shared/vector-ai/requirements.txt` (FastAPI, uvicorn, httpx, pydantic, python-dotenv, zeroconf, tzdata).
- **Full Windows build** also needs Go, MSYS2, Git (see `windows/install.ps1`).
- **AI port defaults to 8090**, not 8000 — many tools squat on 8000 and leave vector-ai crash-looping.
- Repo tree is **source**. On Windows the scheduled task runs from `%USERPROFILE%\vector-pod\`, not from the git clone.
- Live secrets: edit **runtime** `.env` (`%USERPROFILE%\vector-pod\vector-ai\.env` or `~/vector-ai/.env`), not only the repo template under `shared/vector-ai/`.

## Run (dev / brain only)

From a checkout (or the deployed `vector-ai` copy):

```bash
cd shared/vector-ai
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env-default .env   # then set OPENROUTER_API_KEY
uvicorn service:app --host 127.0.0.1 --port 8090
# health: http://127.0.0.1:8090/health  → api_key_set: true
```

Unit tests (pytest; no robot). From repo root:

```bash
python3 -m pytest -q
# or subsets:
python3 -m pytest shared/test_supervisor_pod_conf.py shared/test_supervisor_wedge.py -q
python3 -m pytest shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py -q
```

Debian: `sudo apt install python3-pytest` (optional: `python3-pytest-asyncio`).
Config: root `pytest.ini` (`pythonpath`, asyncio loop scope).

Daily ops (installed stack): see README tables — `start-companion` / `start-vector` / `stop-vector` under `windows/` or `linux/`.

## Git Workflow

**Always commit changes before handing control back.** Treat commits as a journal
of work-in-progress, not a book of published stories.

Commit early and often — after each meaningful edit or logical step — rather than
batching a session’s worth of work into one polished commit. Small, frequent,
honest commits beat large curated ones. A WIP or “checkpoint” commit is fine and
expected; don’t wait for the change to feel finished.

**Mandatory commit boundaries:**
- At the end of any phase, stage, job, task, or logical unit of work.
- **Critical rule:** If control is about to be handed back to the user or to an
  orchestrator/agent, commit first. Never yield a dirty working tree.

Write clear commit messages that describe the completed unit of work.

## Architecture

```text
You speak  →  Wire-Pod STT  →  POST http://127.0.0.1:8090/v1/chat/completions
                                    │
                              vector-ai (persona, memory, behaviors)
                                    │
                              OpenRouter  (HTTPS)
                                    │
                              stream back  →  Wire-Pod TTS  →  Vector
```

That is a normal Wire-Pod **custom knowledge** endpoint (`knowledge.provider = custom`,
`knowledge.endpoint = http://127.0.0.1:8090/v1`), not a network MITM.

### Full install process tree

```text
Vector  <-->  supervisor
                ├── chipper (patched Wire-Pod)  :443 / web UI
                ├── vector-ai                   :8090
                │        └── OpenRouter
                └── mDNS escapepod.local
```

### Companion mode

```text
Vector  <-->  chipper (Program Files / packaged)
                    │  knowledge → http://127.0.0.1:8090/v1
                    v
              vector-ai  →  OpenRouter
```

Supervisor with `EXTERNAL_CHIPPER=1` only babysits vector-ai; it does **not**
start or stop packaged Wire-Pod.

### Windows path split (packaged Wire-Pod)

| Path | Role |
|------|------|
| `C:\Program Files\wire-pod\` | Install: chipper.exe, DLLs, web UI |
| `%APPDATA%\wire-pod\` | Live data: `apiConfig.json`, certs, jdocs, VOSK models |
| `%USERPROFILE%\vector-pod\` | **This project’s runtime** (vector-ai, supervisor, logs, `.env`) |
| This git repo | Source only — not what the scheduled task runs |

### Multi-behavior aliveness (full patched chipper)

```text
Chipper (thin body)              vector-ai (brain)
─────────────────────            ─────────────────────────────
presence tick loop               BehaviorRuntime
  occupied (cheap)          →      PresenceCache (shared)
  face only if asked        →      SpeechArbiter (shared)
  speak line if returned    ←      Behavior plugins (FSMs)
  optional face probe              Continuity / SQLite as needed
```

**Principle:** vector-ai owns policy and state. Chipper reports sensors and acts as mouth/body.

Legacy chipper loops (ambient, sensors, greeting) still run **outside** the plugin
runtime today. Work Day is the first Behavior plugin. Do not delete those loops
to “make room”; coordinate via quiet / voice / speech gap. See
[docs/FSM-implementation.md](docs/FSM-implementation.md).

## Repo layout

```text
windows/                 setup-companion, install, start/stop, apply-config
linux/                   install + start/stop + apply-config
shared/
  vector-ai/             FastAPI brain (uvicorn service:app)
    service.py           Composition root only: deps, startup loops, register_routes
    paths.py             ROOT install dir (memory.db / persona.txt / debug log)
    logging_util.py      Timestamped print + access-log filter
    debug_log.py         DEBUG flag, redaction, debug()
    llm.py               OpenRouter/OpenAI-compatible transport + llm_chat_once
    persona.py           persona.txt load
    process_state.py     Face / ambient quiet / mood / voice / filler state
    deps.py              MEMORY / BEHAVIOR_RUNTIME singletons (set by service)
    vision.py            Vision-intent regex + getImage payload
    prompt_assembly.py   prepare_messages + memory/context sections
    response_cleanup.py  strip_markdown, remember/forget/work tags
    chat_flow.py         generate() SSE stream + fillers + convo summary
    routes/              Thin FastAPI APIRouters (chat, ambient, sensor, …)
    memory.py            SQLite memories, face_meta, observations, mood state
    persona.txt          Personality prose (not commands, not API keys)
    env-default          Template for runtime .env
    requirements.txt
    test_behaviors.py    Behavior/Work Day unit tests (no robot)
    test_service_modules.py  Import/route/pure-helper smoke tests
    behaviors/
      types.py           Protocols + PresenceSnapshot / TickResult / SpeechRequest
      runtime.py         Registration, tick orchestration, clock_tick
      config.py          Env loaders (safe defaults)
      presence.py        Shared presence cache
      arbiter.py         Global proactive speech gate
      continuity.py      Durable day/self SQLite helpers
      workday.py         Reference FSM (Work Day Mode)
  patches/               Applied only by full install (chipper Go source)
  supervisor.py          Process manager / health / mDNS / wedge recovery
  config/                Wire-Pod apiConfig + intents templates
  test_supervisor_wedge.py
docs/
  FSM-workday-companion.md
  FSM-implementation.md
  superpowers/           Design + implementation plans
NEXT_STEPS.md            Companion checklist + how to compile chipper
README.md
```

Chipper patches of note (full install only): ambient, sensors, face probe,
behavior tick (`add-behavior-tick.py`), speech volume bump, connection/stream
leak fixes, wake-word mute during camera, etc.

## Config cheatsheet

### Where config should live: `pod.conf` vs `.env`

| File | Ideal contents |
|------|----------------|
| **`vector-ai/.env`** | **OpenRouter / LLM only** — API key, base URL, model IDs, LLM timeouts, optional `VECTORAI_DEBUG`. Treat as the cloud-brain secret + model file. |
| **`pod.conf`** (next to supervisor) | **Stack / robot / process knobs** — ports, `EXTERNAL_CHIPPER`, Wire-Pod paths, volume duck settings, `USE_LOCAL_OLLAMA`, and other deploy-time tunables the supervisor (and chipper child env) should own. |
| **`persona.txt`** | Personality prose only — not keys, not ports, not volume. |

**Preferred split:** leave `.env` as the OpenRouter config. Put operational tunables in **`pod.conf`**.

**How supervisor loads `pod.conf`:** `shared/supervisor.py` uses a generic
`load_pod_conf()` (all `KEY=VALUE` lines → string dict). Only a small set of
supervisor-owned keys is type-applied (`apply_supervisor_pod_conf`). **Every**
other key is still kept in `POD_CONF` and **forwarded into chipper and
vector-ai child env** via `merge_pod_conf_into_env`. Adding a new FSM knob
means putting it in `pod.conf` — not a new `if/elif` in the supervisor.

vector-ai still `load_dotenv()`s its `.env` for OpenRouter; python-dotenv does
**not** override keys already in the process env, so a value set in `pod.conf`
(and injected by the supervisor) wins over the same key in `.env`.

Work Day / Joke idle / speech / `BEHAVIORS_ENABLED` knobs live in **`pod.conf`**
(template: `shared/config/pod.conf-default`). `env-default` is OpenRouter/LLM only
and points at pod.conf. Do not grow `.env` with stack-wide settings.

**Install merge safety:** setup/install/apply-config scripts **upsert** only the
keys they manage (ports, companion paths). They never rewrite `pod.conf` to a
fixed whitelist — hand-edited `WORKDAY_*` / `JOKE_*` survive reinstall. Optional
one-shot move from an old `.env`: `windows/migrate-behavior-config.ps1` or
`linux/migrate-behavior-config.sh`.

| Want | Where |
|------|--------|
| OpenRouter key / models / history | Runtime `vector-ai/.env` (not only repo template) |
| Personality | `persona.txt` next to that `.env` |
| Knowledge endpoint | `%APPDATA%\wire-pod\apiConfig.json` (custom → `:8090/v1`) |
| Ports, companion flags, volume duck, FSM knobs | **`pod.conf`** next to supervisor |

**LLM (`.env` only — keep it that way):**

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | Required (or `LLM_API_KEY`) |
| `LLM_BASE_URL` | Default `https://openrouter.ai/api/v1` |
| `LLM_MODEL` | Main multimodal model |
| `LLM_SUMMARY_MODEL` | Mood + conversation summary |
| `LLM_MAX_HISTORY_MESSAGES` | Turns sent upstream (default 24) |
| `LLM_TIMEOUT_CONNECT` / `LLM_TIMEOUT_READ` | HTTP timeouts |
| `VECTORAI_DEBUG` | Verbose request/response logs (images redacted) |

**`pod.conf` (stack / deploy):**

| Key | Purpose |
|-----|---------|
| `WEB_PORT` | Wire-Pod UI port (e.g. `9080`) |
| `AI_PORT` | vector-ai port (default `8090`) |
| `EXTERNAL_CHIPPER=1` | Companion: do not start/stop chipper |
| `WIREPOD_DIR` / `WIREPOD_DATA_DIR` | Packaged Wire-Pod install + AppData paths |
| `USE_LOCAL_OLLAMA` | Legacy local LLM process (default off) |
| `VOLUME_DROP` / `VECTOR_VOLUME_DROP` | Speech-volume duck levels (see below) |
| `VOLUME_HANG_MS` / `VECTOR_VOLUME_HANG_MS` | Extra hold after estimated speech |
| `VECTOR_VOLUME_MS_PER_WORD` | Assumed TTS rate for hold sizing |
| `WORKDAY_*`, `JOKE_*`, `BEHAVIORS_ENABLED`, `SPEECH_*` | Behavior FSMs — preferred here, not `.env` |

Format: one `KEY=value` per line, `#` full-line comments, blank lines OK.
UTF-8 (BOM stripped if present). Unknown keys are preserved and forwarded.

**Speech volume bump** (full install + `add-speech-volume-bump.py` patch only):

Keeps Vector quiet at idle and bumps master volume only while he speaks. The
**speaking** level is whatever a human last set (web UI, “Vector, volume 4”,
official app). The patch only chooses how far to duck **between** utterances.

Chipper reads these process env vars (defaults match the Go in the patch):

| Chipper env | Default | Meaning |
|-------------|---------|---------|
| `VECTOR_VOLUME_DROP` | `2` | How many master_volume presets below the speaking level to idle. Presets: `0` Mute … `5` High. Example: speaking at 4 → idle at 2. **`0` disables the patch** (no reads/writes; volume left where the human put it). |
| `VECTOR_VOLUME_HANG_MS` | `2500` | Extra hold (ms) after the **estimated** speech length before dropping back to idle. Absorbs estimate error **and** gaps between consecutive sentence chunks of one LLM reply. Too short → level pumps mid-answer. |
| `VECTOR_VOLUME_MS_PER_WORD` | `400` | Assumed TTS rate (ms/word) used to size the hold. Vector speaks slowly; erring long only costs a little idle delay, erring short ducks him mid-sentence. |

Hold sizing note: gRPC SayText returns when the robot **accepts** the request,
not when speech finishes — there is no end-of-speech callback, so the hold is
always estimated from text length + hang margin.

**If Vector ducks mid-reply:** raise `VECTOR_VOLUME_MS_PER_WORD` and/or
`VECTOR_VOLUME_HANG_MS` until he doesn’t.

**Where to set them:** prefer **`pod.conf`** (not `.env`). Supervisor reads
`VOLUME_DROP` / `VOLUME_HANG_MS` (or the `VECTOR_VOLUME_*` names) from pod.conf
and always injects `VECTOR_VOLUME_*` into the chipper child env. Do not park
these next to the OpenRouter key.

**Work Day / Joke idle** (default off; needs full install + behavior-tick).
vector-ai loaders still take an env mapping (`load_workday_config` /
`load_joke_config`); supervisor injects **`pod.conf`** into the child env.
Edit the live file (not only the repo template):

| Platform | Live `pod.conf` |
|----------|-----------------|
| **Windows** | `%USERPROFILE%\vector-pod\pod.conf` |
| **Linux** | `~/vector-pod/pod.conf` |

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKDAY_ENABLED` | off | Master switch (also need `workday` in `BEHAVIORS_ENABLED`) |
| `WORKDAY_TZ` | host / UTC | Local schedule windows |
| `WORKDAY_START_BEGIN` / `END` / `WORKDAY_END` | 09:00 / 10:30 / 18:00 | Day windows |
| `WORKDAY_POKE_INTERVAL_S` | `5400` | On-task poke interval |
| `WORKDAY_AWAY_S` | `1800` | Away scold threshold |
| `JOKE_ENABLED` | off | Joke idle master (also need `joke_idle` in `BEHAVIORS_ENABLED`) |
| `SPEECH_MIN_GAP_S` | `90` | Global proactive speech gap |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | Quiet after chat |
| `BEHAVIORS_ENABLED` | `workday` | Plugin enable list |

Full key list + commented examples: `shared/config/pod.conf-default`.

Restart vector-ai after `pod.conf` / `.env` / `persona.txt` changes. Restart
chipper (or the full stack) after speech-volume / port `pod.conf` changes so
the patched process re-reads env.

## HTTP surface (vector-ai)

| Route | Role |
|-------|------|
| `POST /v1/chat/completions` | Wire-Pod knowledge chat (stream) |
| `GET /health` | Supervisor poll; access log suppressed |
| `GET /v1/mood`, `POST /v1/mood/reflect` | Persistent mood |
| `GET/POST /v1/memory/*` | List / remember / forget / clear |
| `POST /v1/state/face_seen`, `GET /v1/state/face` | Identity continuity |
| `POST /v1/ambient`, quiet/state | Ambient commentary + quiet mode |
| `POST /v1/sensor_reaction` | Pickup / pet / fall quips |
| `POST /v1/behaviors/tick` | Presence → BehaviorRuntime |
| `GET /v1/behaviors/state` | Debug FSM / arbiter state |
| `POST /v1/proactive_greeting` | Arrival greeting |

Localhost trust model: same class as other chipper→brain loops. Still validate
and bound inputs.

## Code Style — Python

Target **readable, explicit Python 3.11+** suitable for a long-lived desk daemon.

### Conventions

| Topic | Rule |
|-------|------|
| Formatting | Keep surrounding file style. Prefer clear names over clever one-liners. |
| Types | Use type hints on new public functions and dataclasses; match neighbors. |
| Privacy | `_leading_underscore` = module/internal; do not “publicize” without need. |
| Config | Load from env with **safe defaults**; never crash import on a bad optional value. |
| Logging | Prefer timestamped `print` / stdlib logging patterns already in `service.py`. |
| Persona vs commands | **Prose** in `persona.txt`. Command/vision rules live in Wire-Pod `openai_prompt`. API keys and models stay in `.env`. |
| LLM tags | Chat may emit structured tags (`{{remember\|\|…}}`, `{{workAfternoon\|\|yes\|no}}`, `{{workPause\|\|until=HH:MM}}`, `{{workResume}}`). **Strip before speech.** |
| Side effects | Speech-gated commits via `TickResult.on_speak_allowed` — do not advance poke/away/late timers when the arbiter denied speech. |

### Behavior plugin contract

Structural protocol in `shared/vector-ai/behaviors/types.py`:

```text
id: str
priority: int
enabled() -> bool
tick(ctx: BehaviorContext) -> TickResult
```

Optional: `min_tick_interval`, `clock_tick(now, local_dt)`, chat hooks wired thinly from `service.py`.

`TickResult` fields: `speak`, `need_identity`, `debug`, `on_speak_allowed`.

When adding an FSM: one module under `behaviors/`, config loader with defaults off,
register in `BehaviorRuntime`, document knobs in `shared/config/pod.conf-default`
(not OpenRouter `env-default`), unit tests with frozen clocks. Full checklist:
[docs/FSM-implementation.md](docs/FSM-implementation.md).

### Speech arbiter (shared — do not bypass)

Before any proactive line wins a tick:

1. Empty text → no  
2. Quiet mode → no  
3. Recent user voice within `SPEECH_SUPPRESS_AFTER_VOICE_S` → no  
4. Global min gap `SPEECH_MIN_GAP_S` → no  
5. Highest **priority** among remaining candidates; one line per tick  

Chipper must **deliver** non-empty `speak` from `/v1/behaviors/tick` without a second
“recent conversation” drop that leaves server timers advanced.

### Presence

| Layer | Use for | Do not use for |
|--------|---------|----------------|
| **Occupancy** | Desk not empty, away timers | “This is Cam” |
| **Identity** | Arming a day, personal lines | Every 60s confirmation |

Identity is expensive (firmware face streams). Request only at **junctures** via
`need_identity`. Negative `face_id` values are stranger-style IDs — do not drop them
as invalid.

### Patches (Go / chipper)

- Live under `shared/patches/*.py` — they **mutate cloned Wire-Pod source** at full install.
- Prefer thin body loops: report sensors, speak lines, close SDK connections.
- **Robot I/O via `robotsession`:** chipper owns one durable session per ESN
  (`pkg/wirepod/robotsession`). Do **not** call `vector.New` in new chipper code;
  obtain a session from `robotsession.Default` and use `WithControl` / `Unary` /
  `Say` / `SubscribeState`. **`Close` is session-owned** — callers must never
  `Close` a shared client from Get/GetRobot (that tears down voice + sensor
  streams for everyone). Full install still applies `add-sdk-close.py` so the
  third_party SDK exposes `Close()` for the session package.
- No continuous face streams for occupancy (short probes only).
- Companion mode never applies these patches — design features accordingly.

### Scripts (PowerShell / bash)

- Windows: PowerShell in `windows/`; thin `.cmd` wrappers for double-click.
- Linux: bash in `linux/`.
- Paths should come from `pod.conf` / env overrides, not hardcoded machine-specific IPs in committed source.

## Incremental Update Rule

When editing any non-trivial function or public contract:

1. Keep surrounding code consistent with the conventions above.
2. Update docstrings / comments that would otherwise lie about behavior.
3. If you change tick/chat contracts, update `docs/FSM-implementation.md` or the
   relevant companion guide in the same change.
4. If you change Work Day or arbiter behavior, extend `test_behaviors.py`.

A change that alters speech-gating or FSM mode transitions without tests will be
sent back.

## Testing

Use **pytest** (`python3 -m pytest`). Root `pytest.ini` sets `testpaths=shared`,
`pythonpath` for `supervisor` + `behaviors`, and
`asyncio_default_fixture_loop_scope=function` (quiets system pytest-asyncio).

| Suite | Command | Covers |
|-------|---------|--------|
| All unit | `python3 -m pytest -q` (repo root) | Everything below |
| Behaviors / Work Day | `python3 -m pytest shared/vector-ai/test_behaviors.py -q` | Config, presence, arbiter, modes, runtime, chat tags |
| Joke idle | `python3 -m pytest shared/vector-ai/test_joke_idle.py -q` | Joke config, queue, refill mocks, FSM |
| Supervisor pod.conf | `python3 -m pytest shared/test_supervisor_pod_conf.py -q` | load/apply/merge pod.conf → loaders |
| Supervisor wedge | `python3 -m pytest shared/test_supervisor_wedge.py -q` | SDK-wedge pattern / bounce logic |

**Design goals:**
- Unit tests run **without a robot** and without network.
- Freeze clocks / inject presence; do not sleep real wall-clock for policy.
- Load real modules (no parallel reimplementation of FSM logic in the test).
- Prefer `assert cond, "label"` (or plain `assert`); no module-level script runners.
- Add tests when you fix subtle bugs in speech-gating, mode transitions, or tag parsing.

There is no CI config in-repo by default; still run the suites before handing work back.

## What Doesn't Exist (don't invent it)

- No local Ollama requirement (optional legacy `USE_LOCAL_OLLAMA` only).
- No browser SPA / frontend for the brain.
- No house automation, calendar, or OS focus tracking.
- Companion installs do **not** get ambient / sensors / Work Day proactive speech.
- Ambient / greeting / sensors are **not** Behavior plugins yet (arbiter-ready; migration is future work).
- Do not add extra tooling (TypeScript, monorepo, Docker-first rewrite) unless asked.

## Critical Learnings

Hard-won knowledge. Read before changing chipper patches, supervisor, or behaviors.

### Source vs runtime

The git repo is not the process the scheduled task runs. Editing
`shared/vector-ai/.env` in the clone and restarting without copying to
`%USERPROFILE%\vector-pod\vector-ai\.env` (or the Linux runtime path) does nothing
useful. Same for `persona.txt`.

### Port 8090, not 8000

AI_PORT defaults to **8090** because 8000 is crowded (uvicorn defaults, static
servers, MCP tools). A squatter causes vector-ai restart storms.

### Supervisor ownership

One process owns the stack: chipper (unless `EXTERNAL_CHIPPER`), vector-ai, mDNS.
Health checks are cheap (TCP / light probes), not noisy HTTP every tick for
everything. `/health` access lines are filtered so logs stay readable.
Log rotation ~10 MB with `.old` kept.

### SDK wedge vs “network looks fine”

Vector’s gateway can wedge half-dead: TCP to :443 still connects but new gRPC
streams hang; the robot shows the Wi‑Fi icon. Chipper log lines matching
`rpc error.*(DeadlineExceeded|i/o timeout)` are the signal. Bounce chipper after
repeated strikes in a window — that closes held connections and clears the
gateway. Do **not** count vector-ai HTTP timeouts or ordinary stream resets as
wedge strikes.

### Proactive speech is scarce on purpose

Users sit at a desk 8+ hours. Spam is a product failure. Quiet mode, recent chat
suppression, and min-gap will drop lines. If timers advance when speech is denied,
users get silent state changes and “missed” check-ins. Prefer
**speech-gated commits** (`on_speak_allowed`).

### Noticeability test

If the user cannot hear or feel a difference in a normal work day, the change
failed. Pure backend state without speech or chat injection is not enough.

### Identity junctures only

Named-face streams are expensive. Occupancy is sticky/cheap and approximate.
Design FSMs that tolerate false empty/occupied. Request identity only when arming
a day, re-identifying after long away, or similar junctures.

### Work Day is opt-in

`WORKDAY_ENABLED=0` by default for holidays and guests. Pattern for new FSMs:
**list membership (`BEHAVIORS_ENABLED`) + feature flag**, both required.

### Chat tags must never be spoken

Model-emitted tags for memory and work commands are stripped server-side before
TTS. If you add a new tag family, strip it in the same pipeline as existing ones
and cover it in unit tests.

### Persona file scope

`persona.txt` shapes tone for chat **and** proactive reactions. Do not put
animation tokens, API keys, or Wire-Pod command grammar there. Command/vision
rules stay in Wire-Pod’s openai_prompt (apply-wirepod-config).

### Memory model

SQLite: durable facts (`memories`), per-face meta + conversation recaps
(`face_meta`), short visual notes (`observations` — images never stored), and
generic `state` (mood, etc.). Observations are retention-pruned; facts are
deduped case-insensitively.

### Windows timezones

`zoneinfo` needs the `tzdata` package on Windows. Work Day windows use
`WORKDAY_TZ` (IANA name), not “whatever the host clock string says,” for policy.

### Ambient / calm power modes

Ambient capture must respect robot power/sleep constraints (e.g. skip calm power
mode; single capture budget). DeadlineExceeded on calm/sleep has already bitten
this project — see ambient patch history in README troubleshooting.

### Multi-robot caution

Tick loops are per enrolled bot; vector-ai process state is global. Day/self state
needs care if multiple Vectors share one brain.

### When extending aliveness

1. Prefer intelligence in vector-ai; keep chipper thin.  
2. Do not clobber ambient/sensors by hijacking the camera every tick.  
3. Register new FSMs through BehaviorRuntime + arbiter.  
4. Document non-LLM knobs in `pod.conf` / `shared/config/pod.conf-default` (LLM stays in `env-default`) and user-facing docs if people must flip them.
5. Unit test without hardware.

## Docs map

| Doc | Audience |
|-----|----------|
| [README.md](README.md) | Features, architecture, install overview |
| [NEXT_STEPS.md](NEXT_STEPS.md) | Install & day-to-day setup |
| [docs/FSM-workday-companion.md](docs/FSM-workday-companion.md) | Live with Work Day Mode |
| [docs/FSM-implementation.md](docs/FSM-implementation.md) | Add another behavior FSM |
| [docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md](docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md) | Product design (continuity, junctures) |

## Agent checklist (before you stop)

- [ ] Meaningful commits landed; working tree clean or intentional  
- [ ] Runtime vs repo path confusion avoided for config edits  
- [ ] `python3 -m pytest -q` green if behaviors/arbiter/workday/pod.conf/supervisor touched  
- [ ] No new proactive speech path bypasses SpeechArbiter  
- [ ] New non-LLM knobs documented in `pod.conf` / `pod.conf-default` (LLM still `env-default`)  
- [ ] Persona/command/LLM concerns stayed in the right files  

---

*Built on [Wire-Pod](https://github.com/kercre123/wire-pod). Vector and the robot’s
firmware are the property of Digital Dream Labs / Anki.*

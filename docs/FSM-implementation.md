# Adding FSM modules (multi-behavior architecture)

How to extend Vector’s **aliveness** stack with new finite-state machines (behaviors), using Work Day and Joke Idle as reference passengers.

**User-facing guides:** [FSM-workday-companion.md](./FSM-workday-companion.md) · [FSM-jokes-at-idle.md](./FSM-jokes-at-idle.md)  
**Product design:** [superpowers/specs/2026-07-18-vector-aliveness-workday-design.md](./superpowers/specs/2026-07-18-vector-aliveness-workday-design.md)

This document is for **implementers**. It is about contracts, boundaries, and habits—not a line-by-line walkthrough of each FSM’s state graph.

---

## 1. Mental model

```text
Chipper (thin body)              vector-ai (brain)
─────────────────────            ─────────────────────────────
presence tick loop               BehaviorRuntime
  occupied (cheap)          →      PresenceCache (shared)
  face only if asked        →      SpeechArbiter (shared)
  speak line if returned    ←      Behavior plugins (FSMs)
  optional face probe              Continuity / SQLite as needed
                                     ↑
HTTP (ops / debug)                 routes/* thin glue
  POST /v1/behaviors/tick          ← only speak decision path for plugins
  GET  /v1/behaviors/state         ← shared index (dictated envelope)
  GET  /v1/behaviors/<id>          ← per-FSM detail + debug
```

| Layer | Owns | Does not own |
|--------|------|----------------|
| **Chipper behavior-tick** | Occupancy signals, rare face probes, actually speaking | Policy, schedules, persona lines, day state |
| **BehaviorRuntime** | Registration, shared presence, who may speak, OR of `need_identity`, building state index | Individual FSM policy |
| **One Behavior (FSM)** | Its modes, timers, templates/LLM, its config, its `status` / `status_summary` | Private camera loops, bypassing the arbiter, alternate tick/speak HTTP |
| **service.py** | Composition root only (~deps, startup loops, `register_routes`) | Fat business logic, FSM bodies |
| **routes/** | Thin FastAPI handlers (chat, ambient, face, mood, **behaviors tick/state/detail**) | FSM logic (call into `behaviors/`) |
| **Ambient / greeting / sensor** | Their own loops (legacy, not plugins yet; handlers in `routes/`) | Work Day / Joke Idle policy |

**On disk (post split):** `service.py` is a short composition root. All HTTP lives under `shared/vector-ai/routes/` (`register_routes` in `routes/__init__.py`). FSM code lives under `shared/vector-ai/behaviors/`. Older docs that treated `service.py` as a monolith are **stale**.

**Work Day was the first plugin; Joke Idle is the second.** Ambient, greeting, and sensors still run as separate chipper loops. Future work may migrate them onto the runtime; until then, **coordinate** via shared speech manners (quiet, voice activity, min gap)—do not delete them to “make room.”

---

## 2. Assumptions (read before designing a new FSM)

1. **Full patched chipper** is required for proactive presence-driven speech (behavior-tick patch). Companion-only installs get vector-ai but not the tick body.
2. **Presence is approximate.** Occupancy is sticky/cheap (face/empty heuristics), not a perfect “user at keyboard” signal. Design FSMs that tolerate false empty/occupied.
3. **Identity is expensive.** Named-face streams are firmware-heavy. Request identity only at **junctures**, never every tick.
4. **Proactive speech is scarce.** Quiet mode, recent conversation, and global min-gap will drop lines. If your FSM advances timers when speech is denied, users will feel “missed” check-ins—or worse, silent state changes. Prefer **speech-gated commits**.
5. **Timezone is config, not host clock for policy.** Work Day uses `WORKDAY_TZ`. Behavior-tick must **not** gate itself on ambient night hours for policy that lives in vector-ai.
6. **Localhost trust model.** Tick API is for chipper on the same machine (same class as ambient / face_seen). Still validate and bound inputs.
7. **Default off / opt-in.** User-facing FSMs should be safe for other people and holidays (`*_ENABLED=0` by default).
8. **Multi-robot:** tick loop is per enrolled bot; shared state in vector-ai is process-global—design day/self state carefully if multiple Vectors share one brain.
9. **Python 3 + FastAPI** for brain; Go patches for chipper; unit tests without a robot in `shared/vector-ai/test_behaviors.py` (or a sibling module).
10. **Observability is part of the contract.** New FSMs should be curl-friendly: a one-line **summary card** on the shared index and a **detail** GET under `/v1/behaviors/<id>`. Do not dump private fields onto a flat global state object.

---

## 3. Contracts you must honor

### 3.1 Behavior plugin interface

Implemented as a structural protocol in `shared/vector-ai/behaviors/types.py`:

```text
id: str                 # stable key, e.g. "workday", "joke_idle"
priority: int           # higher wins when two want to speak on one tick
enabled() -> bool
tick(ctx: BehaviorContext) -> TickResult
```

Optional but recommended:

| Piece | Role |
|--------|------|
| `min_tick_interval` | Soft throttle (seconds); runtime tracks last tick per id |
| `clock_tick(now, local_dt)` | Time-only transitions when chipper is quiet (Work Day: morning window → no_show) |
| `status_summary(now) -> str` | One short string for the shared state **card** (e.g. `"working"`, `"dwell_building"`) |
| `status(now) -> dict` | Full detail + debug for `GET /v1/behaviors/<id>` (JSON-serializable) |
| Chat hooks | e.g. workday pause tags—wired from response cleanup / routes only if needed |

**Status methods are optional at the type level** so existing plugins keep loading, but **new FSMs should implement both**. Runtime/routes should call them via `getattr` (duck typing), not require a hard Protocol break in one PR if staged carefully.

**`BehaviorContext`** (read-only sensors + time):

- `now` — epoch seconds  
- `local_dt` — timezone-aware datetime (today driven by workday TZ config; see limitations below)  
- `presence` — `PresenceSnapshot` (occupied, face, timestamps, charger, …)  
- `quiet` — ambient quiet mode  
- `identity_fresh` — face cache still valid  
- `config` — historically the workday config object is passed globally; new FSMs should prefer their own config object passed at construct time and ignore or share carefully  

**`TickResult`:**

| Field | Meaning |
|--------|---------|
| `speak` | Plain text for Vector to say (empty = silence). Prefer templates or pre-cleaned speech—no `{{…}}` tags. |
| `need_identity` | Ask chipper to run a short face probe before the next tick |
| `debug` | Structured diagnostics (mode, reason)—safe for logs / tick DEBUG |
| `on_speak_allowed` | **Optional callback** run only if the speech arbiter allows this line |

### 3.2 HTTP: tick (chipper ↔ vector-ai)

`POST /v1/behaviors/tick` is the **only** path that should advance “may I speak?” for behavior plugins. Per-FSM GETs are for **ops/debug**, not alternate speak engines.

**Request (conceptual):**

```json
{
  "occupied": true,
  "face": { "face_id": 1, "name": "Cam", "is_stranger": false },
  "on_charger": false,
  "voice_recent": false
}
```

- `occupied` every tick (cheap).  
- `face` only when a probe just ran (or cached payload chipper chooses to send).  
- Negative `face_id` = stranger-style IDs (do not drop them as “invalid”).

**Response:**

```json
{
  "speak": "… or empty …",
  "need_identity": false,
  "debug": {}
}
```

**Chipper rules (already in `add-behavior-tick.py`):**

- If `speak` is non-empty → **deliver it** (server already applied quiet/gap/voice policy). Do not second-guess with post-HTTP “recent conversation” drops that leave server timers advanced.  
- If `need_identity` → short face probe, then tick again with `face`.  
- Always `Close()` robot connections; no continuous `robot_observed_face` stream for occupancy.

### 3.3 HTTP: shared state index (dictated envelope)

`GET /v1/behaviors/state` is a **stable overview**, not a kitchen sink. As FSMs are added, **do not** bolt private fields onto a flat top-level object (the pre-envelope shape already did this for workday: `mode`, `day_strip`, …).

**Envelope v1 (target shape):**

```json
{
  "schema_version": 1,
  "now": 1784775472.3,
  "date": "2026-07-23",
  "presence": {
    "occupied": true,
    "identity_fresh": true,
    "face": { "face_id": 1, "name": "Cam", "is_stranger": false },
    "last_person_at": 1784775472.33,
    "empty_streak": 0,
    "presence_source": "face_seen",
    "soft_name": "Cam",
    "presence_sticky_s": 1800
  },
  "arbiter": {
    "quiet": false,
    "last_speech_at": null
  },
  "behaviors": {
    "workday": {
      "enabled": true,
      "summary": "working",
      "href": "/v1/behaviors/workday"
    },
    "joke_idle": {
      "enabled": true,
      "summary": "dwell_building",
      "href": "/v1/behaviors/joke_idle"
    }
  }
}
```

| Layer | Holds |
|--------|--------|
| **Top** | `schema_version`, `now`, `date` (brain calendar day for the runtime TZ) |
| **`presence`** | Shared desk occupancy + identity snapshot only |
| **`arbiter`** | Shared speech gate summary only |
| **`behaviors.<id>`** | **Card only:** `enabled`, short `summary`, optional `href` — no private dumps |

**Rules:**

1. Bump `schema_version` if you break the envelope structure.  
2. FSM-private fields live under **`GET /v1/behaviors/<id>`**, not under the index.  
3. Cards are built from registered plugins (`status_summary` / defaults), not hand-maintained if-ladders of workday-only keys.  
4. Index is diagnostic/ops-friendly; still not a forever public product API—but the **envelope** is frozen for clients/scripts once v1 ships.

### 3.4 HTTP: per-FSM detail routes

**Preferred pattern:**

```text
GET /v1/behaviors/<behavior_id>
```

Examples: `/v1/behaviors/workday`, `/v1/behaviors/joke_idle`.

| Belongs here | Does not belong here |
|--------------|----------------------|
| Modes, timers, dwell/cooldown remaining, queue depth, day strip, config snapshot | Running `tick()` / returning `speak` for chipper |
| Last skip reason, debug bags | Alternate presence ingestion |
| Fine-grained ops for that FSM only | Dumping every FSM into one blob |

**Implementation habit:**

- Logic: `YourBehavior.status(now) -> dict` in `behaviors/your_fsm.py`.  
- Glue: thin handler in `routes/behaviors_http.py` (or a small sibling router) that resolves id → instance and returns JSON.  
- Prefer one generic `GET /v1/behaviors/{behavior_id}` over N copy-pasted route functions once the protocol exists.

Ambient already has domain GETs (`/v1/ambient/state`); mood has `/v1/mood`. Behavior plugins should follow the same idea under the **`/v1/behaviors/`** namespace so the map stays scannable.

### 3.5 Speech arbiter (shared)

Before any proactive line is returned as the tick’s winner:

1. Empty text → no  
2. Quiet mode → no  
3. Recent user voice (chat) within `SPEECH_SUPPRESS_AFTER_VOICE_S` → no  
4. Global min gap `SPEECH_MIN_GAP_S` since last proactive speech → no  
5. Highest **priority** among remaining candidates wins; one line per tick  

**Expectation:** if you need a side effect only when the user actually hears the line (poke timer, “we entered late_check”), put it in `on_speak_allowed`, not before the arbiter.

### 3.6 Presence cache (shared)

| Layer | Use for | Do not use for |
|--------|---------|----------------|
| **Occupancy** | “Desk not empty,” away timers, sticky presence | “This is Cam” |
| **Identity** | Arming a day, personal greetings, binding state to a person | Every 60s confirmation |

Cache ages: `FACE_CACHE_MAX_AGE_S` (default **1800s**), `IMAGE_CACHE_MAX_AGE_S`.  
Sticky occupancy: `PRESENCE_STICKY_S` (default 1800s), `PRESENCE_EMPTY_STREAK` (default 2).  
Ambient feeds person/empty every glance (partial body counts); chipper tick empty is weak and does not clear warm sticky. Sleep gap on ambient clears the desk session.  
Chat face window `FACE_RECENT_WINDOW_S` (default 1800s) is separate from FSM face age.  
If another behavior already refreshed face/image, **reuse**—do not force a new capture.

### 3.7 Enable list

`BEHAVIORS_ENABLED=workday,joke_idle` (comma-separated).

Work Day also requires `WORKDAY_ENABLED=1`; Joke Idle requires `JOKE_ENABLED=1`. Pattern for new FSMs: **list membership + feature flag**.

---

## 4. What to do (checklist for a new FSM)

### A. Design on paper first

- [ ] Name the **modes** and **events** (one page max).  
- [ ] List **what the user notices** when it is on vs off (noticeability test).  
- [ ] List **junctures** that need identity vs pure occupancy/time.  
- [ ] Choose **priority** band (see below).  
- [ ] Decide **default off** and holiday story.  
- [ ] Decide if you need **chat commands** or only ticks.  
- [ ] Decide **summary strings** and **status fields** for ops (what would you curl at 2am?).

### B. Code layout (Python)

```text
shared/vector-ai/
  service.py              # composition root only
  routes/
    __init__.py           # register_routes
    behaviors_http.py     # tick, state index, GET /v1/behaviors/{id}
    ambient.py, chat.py, …  # other thin HTTP
  behaviors/
    types.py              # shared contracts — extend carefully
    runtime.py            # registration + tick orchestration + state index helper
    config.py             # env loaders
    presence.py           # shared cache
    arbiter.py            # shared speech gate
    continuity.py         # SQLite helpers if you need durable day/self state
    workday.py            # reference FSM
    joke_idle.py          # second reference FSM
    your_fsm.py           # NEW
  test_behaviors.py / test_your_fsm.py
```

Implement:

1. `YourBehavior` with `id`, `priority`, `enabled()`, `tick()`.  
2. Config dataclass + `load_your_config(env)` with **safe defaults** (never crash vector-ai import on bad env).  
3. Optional `clock_tick` for pure time transitions.  
4. **`status_summary(now)`** + **`status(now)`** for the shared index card and detail GET.  
5. Register in `BehaviorRuntime.__init__` when id ∈ `BEHAVIORS_ENABLED` and feature flag on.  
6. HTTP: ensure the generic behaviors routes expose your id (no FSM logic in the router). Chat tags only if needed.  
7. Document knobs in `shared/config/pod.conf-default` (not OpenRouter `.env`) + a short companion doc if user-facing.  
8. Unit tests with frozen clocks (no robot), including status shape smoke tests.

### C. Registration pattern (today)

```python
# In BehaviorRuntime.__init__ (conceptual)
enabled = set(runtime_cfg.behaviors_enabled)
if "workday" in enabled and workday_cfg.enabled:
    self.workday = WorkDayBehavior(workday_cfg, store)
    self.behaviors.append(self.workday)

if "joke_idle" in enabled and joke_cfg.enabled:
    self.behaviors.append(JokeIdleBehavior(joke_cfg, store))

# Add similarly:
if "evening" in enabled and evening_cfg.enabled:
    self.behaviors.append(EveningBehavior(evening_cfg, store))
```

Longer term, prefer a small registry map `id → factory` so `runtime.py` does not grow an if-ladder—but match current style unless you are doing a deliberate registry refactor.

### D. Suggested priority bands

| Band | Example | Notes |
|------|---------|--------|
| 100 | Immediate physical reactions (sensor) | Still mostly outside runtime today |
| 80 | Work Day accountability | Current default `WORKDAY_PRIORITY` |
| 50 | Social greeting | If migrated later |
| 30 | Ambient novelty | If migrated later |
| 10–20 | Background / low urgency (e.g. joke idle) | New soft features |

Document your chosen priority next to the feature flag.

### E. Chat integration (optional)

If users answer questions in conversation:

- Parse **structured tags** (Work Day: `{{workAfternoon||yes}}`, pause, resume)—reliable.  
- Strip near-miss tags so junk never reaches TTS.  
- **Mode-guard** every command (e.g. “no” only valid in late_check).  
- Inject a short **context strip** into chat only when your feature is enabled.

### F. Chipper

**Usually: no new Go loop.** Reuse `StartBehaviorTickLoop` and the tick API.

Only add chipper code if you need a **new sensor** the tick cannot express (and then still funnel decisions through vector-ai).

---

## 5. What not to do

| Don’t | Why |
|--------|-----|
| Start another always-on `robot_observed_face` stream | Firmware / network degradation (sensors deliberately avoid this) |
| Call `/v1/ambient` or open camera every tick for “presence” | Heavy, wrong semantics, fights ambient design |
| Advance “I already nagged” timers when arbiter denies speech | Silent state desync; user never heard the line |
| Gate chipper ticks on ambient night hours for Work Day–like policy | Host TZ ≠ `WORKDAY_TZ` |
| Put FSM **logic** inside `service.py` or fat `routes/*` | FSMs belong under `behaviors/`; routes stay thin glue |
| Add a per-FSM **POST** that returns `speak` outside the arbiter | Bypasses rationing; double-speak risk |
| Bolt private FSM fields onto a **flat** `/v1/behaviors/state` | Hot unorganized mess as plugins grow—use cards + detail GETs |
| Replace ambient/greeting/sensor to “simplify” | Out of scope; additive architecture |
| Use untyped unbounded tick payloads | Bound face name, validate types |
| Let chat commands tear down unrelated modes | Mode guards on every mutating command |
| Hardcode absolute paths / robot IPs | Follow existing VECTORAI_PORT / bot roster patterns |
| Require LLM for every tick line in v1 of a feature | Latency, cost, flaky tests—templates first is fine |
| Assume occupancy means “primary user still working” | Occupancy ≠ identity; guests and false stickiness exist |
| Skip unit tests because “it’s just prompts” | FSMs rot without clock-driven tests |

---

## 6. Expectations of a good FSM module

### Product

- **Noticeable when on**, quiet when off.  
- **Restraint** at a desk for 8+ hours (defaults, cooldowns, suppress).  
- **Graceful degradation** if chipper is missing, face fails, or LLM is down (for workday pokes: templates; for optional LLM lines: skip speak, don’t corrupt state).

### Engineering

- **Deterministic tests** for mode transitions with injected time.  
- **Idempotent** enable/disable via env + restart.  
- **Observable**: tick `debug` reasons, log lines prefixed with behavior id, **card** on `GET /v1/behaviors/state`, **detail** on `GET /v1/behaviors/<id>`.  
- **Composable**: does not assume it is the only behavior; sets `need_identity` only when required; accepts that another behavior may win the arbiter.  
- **Documented** user knobs (name, default, meaning, where to set).

### Operations

- Default **off** for anything that can annoy or surprise guests.  
- Clear restart requirement after env change.  
- Full install note if chipper patch is required.  
- Curlable status without reading logs: overview index + per-FSM detail.

---

## 7. Speech-gated side effects (pattern)

**Bad:**

```text
tick:
  last_poke_at = now
  return speak="Still working?"
# arbiter later drops the line → timer already moved
```

**Good (Work Day pattern):**

```text
tick:
  result.speak = "Still working?"
  result.on_speak_allowed = lambda: commit_last_poke(now)
# runtime runs callback only after arbiter.allow()
```

Use the same idea for: entering a “we already asked” mode, counting absences that were *announced*, etc.

State that must advance even when silent (e.g. “morning window closed → no_show”) belongs in `clock_tick` / occupancy logic, **not** in speech callbacks.

---

## 8. Identity junctures (pattern)

Request `need_identity=True` only when:

- Transition requires a **named person** (e.g. arm work day, personal late check), and  
- Identity cache is not fresh, and  
- You are not in a reject cooldown (stranger / wrong person).

Do **not** set `need_identity` every tick while `occupied` is true “just to be sure.”

After a non-primary ID, prefer a **cooldown** before asking again (Work Day: `WORKDAY_IDENTITY_REJECT_COOLDOWN_S`).

---

## 9. Shared resources and fairness

| Resource | Sharing rule |
|----------|----------------|
| Face probe | One in-flight; cache; only on `need_identity` |
| Camera image | Reuse if `IMAGE_CACHE_MAX_AGE_S` allows; coalesce |
| Speech | One line per tick; priority + min gap |
| CPU / robot gRPC | Short-lived connections + Close(); no extra EventStreams if `robot_state` already exists for charger |
| Ops HTTP | Shared index is O(plugins); detail GETs are lazy and read-only |

If every FSM wanted a photo every minute, the runtime would melt the robot. Design for **event / schedule / juncture**, not polling vanity.

---

## 10. Relationship to legacy loops

| Loop | Status | Interaction with new FSMs |
|------|--------|---------------------------|
| Ambient | Separate chipper loop | Don’t remove; MarkVoiceActivity after ambient speak helps arbiter; optional future migration |
| Greeting | Separate | Same |
| Sensor pet/pickup | Separate | Higher urgency physical reactions; leave alone |
| Behavior tick | Shared spine for plugins | Add FSMs here first |

When migrating a legacy loop onto the runtime later:

1. Port policy to a Behavior.  
2. Keep chipper as sensor + speak only.  
3. Register under `BEHAVIORS_ENABLED`.  
4. Add status card + detail GET.  
5. Remove or no-op the old Go loop only after parity tests.  
6. Do it as its own PR—not mixed with unrelated FSM features.

---

## 11. Testing expectations

Minimum for each new FSM:

- Config defaults and bad-env safety.  
- Happy-path mode transitions with frozen time.  
- At least one **denied speech** case (quiet or min-gap) proving timers do not advance incorrectly.  
- Identity juncture vs occupancy-only tick.  
- Feature flag off → no speak / no state poison.  
- **Status**: `status_summary` / `status` (or route) returns expected keys for dwell/cooldown/mode-style fields.  
- **Envelope**: shared state still validates `schema_version` + card shape when your plugin is registered.

Run:

```bash
cd VectorIntelligence
python3 -m pytest shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py shared/vector-ai/test_service_modules.py -q
```

---

## 12. Suggested implementation order for a new module

1. Spec one page: modes, noticeability, flags, priority, status fields.  
2. Config + types + empty `tick()` returning silence.  
3. Register behind `BEHAVIORS_ENABLED` + `YOUR_ENABLED=0`.  
4. Unit-test transitions.  
5. Speech + `on_speak_allowed`.  
6. Identity junctures if needed.  
7. `status_summary` + `status` + detail route exposure.  
8. Optional chat tags + day strip.  
9. Docs (companion + `shared/config/pod.conf-default`).  
10. Only then consider chipper changes.

---

## 13. Known limitations of the foundation

Documented so you do not reinvent or get surprised:

- `BehaviorContext.config` is still centered on workday config for `local_dt` TZ; multi-TZ multi-FSM may need a small runtime clock policy later.  
- Registration is an explicit if-ladder in `BehaviorRuntime`, not a plugin discovery system.  
- Ambient/greeting are **not** plugins yet; double-speak mitigation is partial and cooperative.  
- Occupancy heuristics in Go are approximate (sticky window, sparse empty probes).  
- Pre-envelope `GET /v1/behaviors/state` was a flat workday-skewed bag; migrate to **envelope v1** (cards + detail GETs) and stop growing the flat form.  
- No hot-reload of env without process restart.

Improvements that help **all** FSMs (registry, shared clock TZ, migrating ambient, envelope rollout) are welcome as focused refactors—do not block a single new FSM on a perfect platform.

---

## 14. Quick reference: files to touch

| Goal | Where |
|------|--------|
| New FSM logic | `shared/vector-ai/behaviors/<name>.py` |
| Env parsing | `shared/vector-ai/behaviors/config.py` + knobs in `pod.conf` / `shared/config/pod.conf-default` (LLM stays in `.env`) |
| Register plugin | `shared/vector-ai/behaviors/runtime.py` |
| Shared tick + state index + detail GET | `shared/vector-ai/routes/behaviors_http.py` (+ optional helpers on runtime) |
| Composition only | `shared/vector-ai/service.py` (deps, loops, `register_routes`) |
| Durable state | `continuity.py` or dedicated store |
| Chipper (rare) | `shared/patches/…` + `linux/install.sh` / `windows/install.ps1` |
| Tests | `shared/vector-ai/test_behaviors.py`, `test_joke_idle.py`, `test_service_modules.py` |
| Deploy modules | Install scripts must copy `behaviors/` + `routes/` packages |
| User / implementer docs | `docs/FSM-*.md`, README snippet |

---

## 15. One-sentence doctrine

**Chipper reports the world and moves the mouth; vector-ai behaviors decide meaning; the runtime shares presence and rationed speech so many FSMs can coexist without burning the robot or each other; HTTP exposes a shared index and per-FSM detail without becoming a second brain.**

When in doubt: copy Work Day’s shapes (speech-gated commits, juncture identity, default off, tests with frozen time) and Joke Idle’s restraint (long dwell, speech-gated daily commit)—not ambient’s camera loop. For ops, copy the **card + detail GET** pattern, not a growing flat state dump.

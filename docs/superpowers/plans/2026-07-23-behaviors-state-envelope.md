# Plan: Behaviors state envelope v1 + per-FSM status HTTP

> **For agentic workers:** Implement phase-by-phase. Each phase is self-contained with doc refs, verification, and anti-patterns. Prefer COPYING shapes from existing modules over inventing new layers.

**Goal:** Make multi-FSM ops curl-friendly without turning `GET /v1/behaviors/state` into a flat dump: freeze a **dictated envelope**, expose **workday** and **joke_idle** detail under `GET /v1/behaviors/<id>`, and add optional **`status` / `status_summary`** on behavior modules.

**Architecture:** Logic stays in `behaviors/`; thin glue in `routes/behaviors_http.py`; composition only in `service.py`. Tick remains the only speak path.

**Doc of record (updated first):** [docs/FSM-implementation.md](../../FSM-implementation.md) §3.1–3.4, §5–6, §13–15.

---

## Phase 0: Documentation discovery (Allowed APIs)

### Sources consulted

| Source | Findings |
|--------|----------|
| `docs/FSM-implementation.md` | Envelope v1 shape; card fields `enabled`/`summary`/`href`; detail `GET /v1/behaviors/<id>`; optional `status`/`status_summary`; no alternate speak POSTs |
| `routes/behaviors_http.py` | Today: `POST /v1/behaviors/tick`, flat `GET /v1/behaviors/state` (workday keys + presence + `behaviors: [ids]`) |
| `routes/__init__.py` | `register_routes` includes `behaviors_http.router` only for behaviors HTTP |
| `behaviors/types.py` | Protocol: `id`, `priority`, `enabled()`, `tick()` — **no** status methods yet |
| `behaviors/runtime.py` | `self.behaviors: list`; `self.workday`; arbiter; presence; `tick`/`clock_tick` |
| `behaviors/arbiter.py` | `self._last_speech_at`; `record_speech(now)` |
| `behaviors/presence.py` | `occupied_effective`, `identity_fresh`, `debug_dict`, `snapshot` |
| `behaviors/workday.py` | `WorkDayBehavior`; mode via continuity `load_workday` / `day_strip` |
| `behaviors/joke_idle.py` | Dwell formula: `now - max(presence.updated_at, last_spoke_at)`; gates: capped/cooldown/empty/dwell_building/… |
| `behaviors/continuity.py` | `joke_load_daily` → `{count, last_spoke_at, last_reject_at}`; `joke_queue_len()` |
| `behaviors/config.py` | `JokeConfig.min_dwell_s`, `cooldown_s`, `max_per_day`, `audience`, … |
| `test_service_modules.py` | Frozen `EXPECTED_ROUTES` **exact set** equality — dynamic path must be registered carefully |
| `routes/ambient.py` | Pattern for domain GET state (thin handler → process/domain state) |

### Allowed APIs (use these; do not invent)

| API | Location | Use |
|-----|----------|-----|
| `BEHAVIOR_RUNTIME.behaviors` | runtime | Iterate registered plugins for cards |
| `BEHAVIOR_RUNTIME.presence.*` | presence | Build `presence` block |
| `BEHAVIOR_RUNTIME.arbiter._last_speech_at` or prefer a small public getter if you add one | arbiter | `arbiter.last_speech_at` |
| `ContinuityStore.load_workday` / `day_strip` | continuity | Workday status |
| `ContinuityStore.joke_load_daily` / `joke_queue_len` | continuity | Joke status |
| `deps.BEHAVIOR_RUNTIME`, `deps._workday_cfg`, `deps.JOKE_CFG`, `deps._continuity` | deps | Route access (existing pattern) |
| `datetime.now(tz)` with workday TZ | same as current state handler | `date` field |
| FastAPI `APIRouter` + `app.include_router` | routes | HTTP only |

### Anti-patterns (do not)

- Do **not** put mode machines in `routes/behaviors_http.py`.
- Do **not** add per-FSM `POST` that returns `speak` or calls `tick` for chipper.
- Do **not** keep growing **flat** top-level keys on `/v1/behaviors/state`.
- Do **not** require hard Protocol break that breaks import if staged wrong—duck-type `getattr(b, "status", None)` is fine.
- Do **not** change chipper tick contract in this work.

### Confidence

High for shapes and call sites. Gap: whether any external scripts depend on flat keys (`mode`, `day_strip` at top level)—assume **breaking** for ops curl users; document migration in companion docs if needed. Prefer a short dual-read period only if something in-repo greps those keys.

```bash
# Phase 0 verification for consumers of flat keys
rg -n 'behaviors/state|"day_strip"|"workday_enabled"' VectorIntelligence --glob '!**/__pycache__/**'
```

---

## Phase 1: Protocol hooks + runtime index builder

### What to implement

1. **Document optional methods** on `Behavior` in `behaviors/types.py` (Protocol can list optional methods as comments or as non-required if using structural typing carefully). Prefer:

```python
# Optional observability (duck-typed by runtime/routes):
#   def status_summary(self, now: float) -> str: ...
#   def status(self, now: float) -> dict: ...
```

If adding to Protocol, keep them optional for implementers (Python Protocol optional methods are still “required” if listed—**prefer comments + duck typing** unless using `typing_extensions` runtime checks).

2. **`BehaviorRuntime.build_state_index(now: float) -> dict`** (name flexible) that returns envelope v1:

```json
{
  "schema_version": 1,
  "now": <float>,
  "date": "YYYY-MM-DD",
  "presence": { ... },
  "arbiter": { "quiet": bool, "last_speech_at": float|null },
  "behaviors": {
    "<id>": { "enabled": true, "summary": "...", "href": "/v1/behaviors/<id>" }
  }
}
```

Card rules (copy from `docs/FSM-implementation.md` §3.3):

- Only **registered** behaviors appear under `behaviors`.
- `summary` = `status_summary(now)` if present, else `"ok"` / `"enabled"`.
- `enabled` on the card is always `true` for registered instances (disabled plugins are not registered today)—optional later: list disabled ids; **out of scope** unless cheap.

3. **Presence block** — move current flat fields into nested `presence` (same values as today’s `occupied`, face, sticky keys from `presence.debug_dict`).

4. **Arbiter block** — expose `last_speech_at` (use `0` → `null` for “never”). Prefer adding `SpeechArbiter.last_speech_at` property rather than reading private `_last_speech_at` from routes.

### Documentation references

- Envelope: `docs/FSM-implementation.md` §3.3  
- Presence fields today: `routes/behaviors_http.py` ~99–117  
- Arbiter: `behaviors/arbiter.py` `_last_speech_at`, `record_speech`

### Verification checklist

- [ ] Unit test: `build_state_index` with mocked runtime has `schema_version == 1`, keys `presence`/`arbiter`/`behaviors`.
- [ ] Unit test: no top-level `mode` / `day_strip` / `occupied` (flat legacy keys gone once Phase 2 switches the route).
- [ ] `python3 -m pytest shared/vector-ai/test_behaviors.py -q` still green.

### Anti-pattern guards

- Do not call `tick()` from `build_state_index`.
- Do not import FastAPI inside `behaviors/`.

---

## Phase 2: Freeze `GET /v1/behaviors/state` on envelope v1

### What to implement

1. Replace body of `behaviors_state()` in `routes/behaviors_http.py` with:

```python
now = time.time()
return deps.BEHAVIOR_RUNTIME.build_state_index(now)
```

(plus quiet flag wiring if index needs `quiet_fn` already on runtime).

2. Update **user-facing curl examples** if they show flat keys:

- `docs/FSM-jokes-at-idle.md` (curl state)
- `docs/FSM-workday-companion.md` (curl state)
- `AGENTS.md` one-liner if it describes flat fields

3. **Breaking change note** in the route docstring: flat keys removed; use envelope + detail GETs.

### Documentation references

- Target JSON: `docs/FSM-implementation.md` §3.3  
- Current handler: `routes/behaviors_http.py` `behaviors_state`

### Verification checklist

- [ ] `curl` / TestClient: response has `schema_version`, nested `presence.occupied`, `behaviors` as **object** not list of strings.
- [ ] `test_service_modules.py` still sees `GET /v1/behaviors/state`.
- [ ] Grep in-repo for scripts assuming flat `mode` at top level; fix or document.

### Anti-pattern guards

- Do not dual-write flat + envelope “forever.” One-release compatibility shim is optional; if used, keep it under a flag and delete soon—default is **clean break** to envelope v1.

---

## Phase 3: Workday detail — card + `GET /v1/behaviors/workday`

### What to implement

1. On `WorkDayBehavior` (or via runtime if instance methods are cleaner):

```python
def status_summary(self, now: float) -> str:
    # e.g. mode value: "working", "paused", ...

def status(self, now: float) -> dict:
    # mode, day_strip, workday_enabled, date, key timers/flags useful for ops
```

Copy data sources from current flat handler:

- `deps._continuity.load_workday(date_s).mode.value`
- `deps._continuity.day_strip(date_s)`
- `deps._workday_cfg.enabled`
- Optional: primary face name, pause until, last poke—only if already available without new DB work.

2. **Generic route** (preferred once):

```python
@router.get("/v1/behaviors/{behavior_id}")
async def behavior_detail(behavior_id: str):
    ...
```

Resolve `behavior_id` against `BEHAVIOR_RUNTIME.behaviors` by `b.id`.  
- Found + has `status` → return `status(now)` (optionally wrap with `{"id", "schema_version": 1, ...}`).  
- Found without `status` → `{"id", "summary": status_summary or "ok"}`.  
- Missing → HTTP 404.

3. Workday **card** on index uses `status_summary` → e.g. `"working"`.

### Documentation references

- Detail contract: `docs/FSM-implementation.md` §3.4  
- Workday state today: `routes/behaviors_http.py` lines 86–92  
- Ambient GET pattern: `routes/ambient.py` `@router.get("/v1/ambient/state")`

### Verification checklist

- [ ] `GET /v1/behaviors/workday` returns mode + day_strip (or equivalent).
- [ ] Index card `behaviors.workday.summary` matches mode-ish string.
- [ ] 404 for unknown id.
- [ ] Update `EXPECTED_ROUTES` in `test_service_modules.py`: FastAPI registers path as `/v1/behaviors/{behavior_id}` — add that path + GET method; keep `/v1/behaviors/state` and `/v1/behaviors/tick`.

### Anti-pattern guards

- Do not reimplement workday FSM in the route.
- Do not special-case only workday forever—generic `{behavior_id}` is the goal; workday is first consumer.

---

## Phase 4: Joke idle detail — dwell / queue / cap

### What to implement

1. On `JokeIdleBehavior`:

```python
def status_summary(self, now: float) -> str:
    # Prefer last-known gate reason if you store it; else compute cheaply:
    # empty | dwell_building | cooldown | capped | voice_recent | idle_ready | ...

def status(self, now: float) -> dict:
    # enabled knobs + live counters
```

**Minimum fields for `status` (ops curl):**

| Field | Source |
|-------|--------|
| `audience` | `cfg.audience` |
| `min_dwell_s` | `cfg.min_dwell_s` |
| `cooldown_s` | `cfg.cooldown_s` |
| `max_per_day` | `cfg.max_per_day` |
| `daily_count` | `store.joke_load_daily(date)["count"]` |
| `last_spoke_at` | daily dict |
| `quiet_dwell_s` | `now - max(presence.updated_at, last_spoke_at)` (same formula as tick) |
| `dwell_remaining_s` | `max(0, min_dwell_s - quiet_dwell)` |
| `cooldown_remaining_s` | from last_spoke + cooldown |
| `queue_len` | `store.joke_queue_len()` |
| `occupied` | from presence snapshot if available on behavior (pass via runtime status helper or read presence from runtime—avoid circular imports; prefer `status(now, presence=...)` **or** store weak ref to presence/runtime already on behavior if present) |

**Presence access:** `JokeIdleBehavior.tick` already receives `ctx.presence`. For `status`, either:

- Add optional `presence` argument only used by runtime when building detail, or  
- Have runtime call `b.status(now)` after setting nothing—behavior reads `self` only and uses continuity + cfg for queue/cap; for dwell it needs presence → **runtime helper** `status_for(b, now)` that builds a minimal context is cleanest.

Recommended: **`BehaviorRuntime.behavior_status(behavior_id, now) -> dict | None`** that builds date/presence once and calls `b.status(now, ctx_bits)` or passes presence into status.

Keep it simple: `def status(self, now: float, *, presence_updated_at: float, occupied: bool) -> dict` if you want zero coupling to PresenceCache type.

2. Index card summary e.g. `"dwell_building"` / `"cooldown"` / `"ready"` / `"capped"`.

3. No new tables; read-only.

### Documentation references

- Skip reasons / dwell: `behaviors/joke_idle.py` gates + `dwell_building` log  
- Daily/queue: `continuity.py` `joke_load_daily`, `joke_queue_len`  
- Doc: `docs/FSM-jokes-at-idle.md` curl section (update to detail GET)

### Verification checklist

- [ ] Unit test with frozen time + seeded daily/queue: `status` reports expected dwell_remaining and queue_len.
- [ ] `GET /v1/behaviors/joke_idle` via TestClient when joke enabled in test runtime.
- [ ] Feature off / not registered → 404 from generic route (not empty 200 with lies).

### Anti-pattern guards

- Do not call refill LLM from status.
- Do not mutate daily/queue in status.
- Do not require DEBUG flag for basic counters.

---

## Phase 5: Wire tests, route registry, AGENTS/docs polish

### What to implement

1. **`test_service_modules.py`**

- Add `/v1/behaviors/{behavior_id}` to `EXPECTED_ROUTES` / methods GET.  
- Note: exact set equality—path is the FastAPI template string.

2. **Focused tests**

- Envelope shape test (Phase 1–2).  
- Workday status keys.  
- Joke status keys + dwell math.  
- Extend `test_behaviors.py` / `test_joke_idle.py` rather than giant new files unless needed.

3. **Docs already updated:** `FSM-implementation.md`. Also touch:

- `docs/FSM-jokes-at-idle.md` curl examples  
- `docs/FSM-workday-companion.md` curl examples  
- `AGENTS.md` endpoint table row for detail GET  

4. **Logging:** optional one-line at DEBUG when detail is hit—skip if noisy.

### Verification checklist

```bash
cd VectorIntelligence
python3 -m pytest shared/vector-ai/test_service_modules.py shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py -q
```

Manual:

```bash
curl -s http://127.0.0.1:8090/v1/behaviors/state | jq
curl -s http://127.0.0.1:8090/v1/behaviors/workday | jq
curl -s http://127.0.0.1:8090/v1/behaviors/joke_idle | jq
curl -s http://127.0.0.1:8090/v1/behaviors/nope   # expect 404
```

### Anti-pattern guards

- Do not weaken `EXPECTED_ROUTES` to a subset check without reason—keep exact set, add the new path.
- Do not document flat keys as current.

---

## Phase 6: Final verification

### Checklist

- [ ] `schema_version: 1` on index.  
- [ ] No flat `mode` / `occupied` at top level of `/v1/behaviors/state`.  
- [ ] Workday detail has mode + strip.  
- [ ] Joke detail has dwell/queue/cap.  
- [ ] Tick path unchanged (`POST /v1/behaviors/tick` contract).  
- [ ] FSM logic still only under `behaviors/`.  
- [ ] Full pytest set above green.  
- [ ] `rg` for remaining flat-state docs updated.

### Success criteria

| Item | Done when |
|------|-----------|
| (1) Envelope v1 frozen | Index matches §3.3 of FSM-implementation.md |
| (2) Workday under card + GET | Card summary + `/v1/behaviors/workday` |
| (3) Joke GET with dwell/queue/cap | `/v1/behaviors/joke_idle` fields populated |
| (4) Optional status on protocol | Documented + duck-typed; workday + joke implement |

---

## Suggested file touch list

| File | Change |
|------|--------|
| `behaviors/types.py` | Document optional status hooks |
| `behaviors/runtime.py` | `build_state_index`, `behavior_status` |
| `behaviors/arbiter.py` | Public last_speech accessor (small) |
| `behaviors/workday.py` | `status_summary`, `status` |
| `behaviors/joke_idle.py` | `status_summary`, `status` |
| `routes/behaviors_http.py` | Envelope state + generic detail GET |
| `test_service_modules.py` | Route set |
| `test_behaviors.py` / `test_joke_idle.py` | Status tests |
| `docs/FSM-jokes-at-idle.md`, `FSM-workday-companion.md`, `AGENTS.md` | Curl / endpoint table |

**Out of scope:** chipper changes, plugin auto-discovery, migrating ambient onto runtime, dual-write compatibility forever, hot reload.

---

## Execution order (for `/do` or subagent-driven work)

1. Phase 1 (runtime index + hooks)  
2. Phase 3–4 status methods (can parallelize workday vs joke once index exists)  
3. Phase 2 route switch to envelope (after cards have real summaries)  
4. Phase 3–4 generic GET (can land with Phase 2)  
5. Phase 5–6 tests + docs polish  

**Note:** `FSM-implementation.md` was refreshed **before** this plan; treat it as the contract source of truth during implementation.

# Ambient Presence + Sticky Desk + Long Face Cache — Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans. Checkbox tasks (`- [ ]`). One TASK per subagent where files collide.
>
> **Scope:** Fuse ambient vision into desk occupancy and soft identity; sticky “person is there until they aren’t”; longer chat face cache; ambient detects **partial** people (torso/arms/hoodie without face). Keep novelty speech independent of presence.
>
> **Out of scope:** Continuous firmware face EventStream; chipper camera redesign; multi-user identity arbitration beyond “soft name + enrolled face wins”; changing Work Day late_check_done day rules.

**Goal:** After implement + restart vector-ai (and chipper only if response fields need parse), Work Day / jokes see `occupied=true` when ambient repeatedly sees a person (including partial body), and `/v1/state/face` still reports the last known person for **many minutes** in a single-user desk setup—not 15 seconds.

**Architecture decision (binding):**

| Concern | Decision |
|---------|----------|
| Ambient speech | Still novelty-only (`NOTHING` or 2-line reaction). Unchanged product feel. |
| Ambient presence | **Every** glance returns machine line `PRESENCE: …` independent of novelty. |
| Partial person | Prompt: person if **any** human body evidence (head, torso, arms, hands, hoodie, legs)—not face-only. |
| Occupancy model | Sticky: last positive person evidence + hold; clear only after empty streak or sleep gap—not per-probe flapping. |
| Fusion | All fusion in **vector-ai** PresenceCache (+ process face state). Chipper may stay text-only for speak. |
| Chat face (`/v1/state/face`) | **Much longer** TTL (`FACE_RECENT_WINDOW`); single-user default. Enrolled still beats stranger blips. |
| Hard identity | Firmware `face_seen` / named face still **authoritative** when present; ambient soft name is fallback while occupied. |
| Novelty vs presence | Person still at desk after already noted → `PRESENCE: person` + `NOTHING` (no re-roast). |

**Binding discovery date:** 2026-07-21.

**User requirements (explicit):**

1. Ambient must feed presence (not novelty alone).  
2. Carry-forward presumption: person there until clear empty/sleep.  
3. Partial person counts (head hidden OK).  
4. `/v1/state/face` cache a lot longer (usually just one person).

---

## How to execute

1. Order: TASK-01 → TASK-02 → TASK-03 → TASK-04 → TASK-05 → TASK-06 → TASK-07.  
2. Only Allowed APIs (Phase 0).  
3. Verify each task checklist.  
4. Roots:
   - `VectorIntelligence/shared/vector-ai/`
   - Chipper ambient only if needed: `wire-pod/chipper/pkg/wirepod/ttr/ambient.go`
   - Docs: `AGENTS.md`, `pod.conf-default`, FSM docs

### Subagent template

```
You are implementing TASK-NN of the ambient-presence plan.

READ:
- VectorIntelligence/docs/superpowers/plans/2026-07-21-ambient-presence-sticky-face.md
  (Phase 0, TASK-NN, anti-patterns)

WORKDIR: as in TASK-NN

DO: Interface Contract only. Copy patterns from listed sources.
MUST NOT: continuous robot_observed_face long stream; invent chipper APIs;
  parse novelty free-text as sole presence; clear occupied on single empty ambient;
  keep FACE_RECENT_WINDOW at 15s as default.

VERIFY: TASK-NN Acceptance Criteria.
REPORT: sources, diffs, commands, confidence, gaps.
```

---

## Phase 0 — Documentation Discovery (completed; binding)

### Sources consulted

| Source | Finding |
|--------|---------|
| `routes/ambient.py` | Novelty-only prompt; return `{"text"}` only; NOTHING default |
| `process_state.py` | `FACE_RECENT_WINDOW=15`; `current_face()` enrolled-wins |
| `routes/face.py` | `face_seen` + PresenceCache mirror occupied=True |
| `behaviors/presence.py` | Cache overwrites occupied each update; face only if non-null; identity_fresh ≤ face_max_age_s |
| `behaviors/runtime.py` | `ingest_tick_payload` / tick |
| `routes/behaviors_http.py` | Tick reuses `current_face()` → force occupied |
| `routes/models.py` | AmbientRequest.image only |
| `behaviors/config.py` | `FACE_CACHE_MAX_AGE_S` default 120 |
| `joke_idle.py` / `workday.py` | Both gate on `presence.occupied`; known needs identity_fresh |
| Chipper `ambient.go` | Uses **only** response `text` for speech |
| Chipper face/tick | Sparse ProbeFace; sticky 2m; does not use ambient for occupancy |

### Allowed APIs (only these)

#### Ambient HTTP (extend response shape; keep `text` for chipper)

```python
# routes/ambient.py
async def ambient(req: AmbientRequest) -> dict
# Must continue to return "text" for chipper SayText path.
# May add: "presence", "occupied", "name_hint" (optional JSON fields; chipper ignores)
```

#### Presence

```python
# behaviors/presence.py
class PresenceCache:
    def update(...) -> PresenceSnapshot
    def identity_fresh(now: float) -> bool
    def effective_face(now: float) -> Optional[FaceIdentity]
```

Extend with sticky helpers (new methods OK; keep `update` compatible or supersede carefully):

```python
def note_person_evidence(now, *, name_hint: str | None = None, source: str) -> None
def note_empty_evidence(now) -> None
def apply_sleep_clear(now, sleep_gap_s: float) -> None
def occupied_effective(now) -> bool  # sticky evaluation
```

#### Face chat state

```python
# process_state.py
FACE_RECENT_WINDOW  # raise default; env-overridable if pattern exists
def current_face() -> Optional[dict]
```

#### Face / tick writers

```python
# routes/face.py  state_face_seen — keep mirror into PresenceCache
# routes/behaviors_http.py  behaviors_tick — keep face fallback
```

#### Config

```python
# behaviors/config.py RuntimeConfig / load_runtime_config
FACE_CACHE_MAX_AGE_S       # FSM identity TTL — raise default
# New env keys (document in pod.conf-default):
PRESENCE_STICKY_S          # default e.g. 1800 (30m)
PRESENCE_EMPTY_STREAK      # default 2 ambient empties to clear
FACE_RECENT_WINDOW_S       # chat face TTL; default e.g. 1800 (30m)
```

Prefer reading FACE_RECENT_WINDOW from env in process_state (or single source in config) so pod.conf can tune without rebuild.

#### Memory (unchanged)

```python
MEMORY.list_observations / remember_observation  # novelty notes only
```

### Anti-patterns (MUST NOT)

1. **Do not** use novelty speech text as the only presence signal.  
2. **Do not** require a visible face for ambient person (partial body counts).  
3. **Do not** clear sticky occupancy on a single `PRESENCE: empty`.  
4. **Do not** open continuous face EventStream on chipper.  
5. **Do not** break chipper ambient: response must still include `text` (empty string OK).  
6. **Do not** let ambient soft name override a **fresh enrolled** firmware face.  
7. **Do not** leave `FACE_RECENT_WINDOW=15` as the product default after this work.

### Confidence + gaps

| Item | Confidence |
|------|------------|
| Dual 15s vs 120s face clocks today | High |
| Ambient does not write presence | High |
| Chipper only needs `text` | High |
| Exact sticky defaults for real desks | Med — tunable via env |
| Multi-user wrong-name soft match risk | Med — document; enrolled face wins |

---

## TASK-01 — Ambient protocol: PRESENCE every glance + partial person

**WORKDIR:** `VectorIntelligence/shared/vector-ai/routes/ambient.py`  
**Optional:** unit-testable pure parser in same file or `ambient_presence.py`

### What to implement

1. Rewrite `_AMBIENT_SYSTEM` (and user glance text) to require **always**:

```text
Line 1 (machine, ALWAYS):
  PRESENCE: empty
  PRESENCE: person
  PRESENCE: person:<NameHint>     # only if confident; else person without name

Then EITHER:
  NOTHING
OR novelty (existing two-line human format):
  <memory note>
  <spoken reaction>
```

2. **Partial person rule (prompt + tests):**  
   Count as `person` if any human is visible: head, face, torso, arms, hands, legs, clothing silhouette (e.g. grey hoodie), even if face/head is cut off, turned away, or occluded.  
   `empty` only if **no** human body evidence.

3. **Independence:** Person still present after already in recent observations → still `PRESENCE: person` + `NOTHING`.

4. **Parser** (strict, pure function preferred):

```python
def parse_ambient_llm_raw(raw: str) -> tuple[str, Optional[str], str]:
    """Returns (presence_kind, name_hint|None, spoken_text).
    presence_kind in {"empty","person"}.
    spoken_text "" if NOTHING / no novelty lines.
    """
```

5. Endpoint still returns at least:

```python
{
  "text": spoken,           # chipper
  "presence": "empty"|"person",
  "name_hint": str|None,    # optional
  "occupied": bool,         # convenience = presence==person for this glance
}
```

6. On quiet short-circuit: do **not** invent presence (no image). Return text empty; omit presence or `presence: "unknown"`.

7. On LLM error: no presence update (caller won’t call sticky with fake empty).

### Documentation references

- Current prompt L32–67 and parse L145–169 in `ambient.py`.  
- Chipper `askVectorAIAmbient` only uses `.Text` — keep key name `text`.

### Verification checklist

- [ ] Unit tests for parser: full novelty+person, person+NOTHING, empty+NOTHING, partial-body wording in fixtures, missing PRESENCE line → safe default (prefer `unknown`/no-update, not forced empty).  
- [ ] Manual prompt sample strings in test file.  
- [ ] Quiet path still no LLM.

### Anti-pattern guards

- Do not store PRESENCE line in `remember_observation`.  
- Do not treat “grey hoodie” novelty failure as empty desk.

---

## TASK-02 — Sticky PresenceCache (person until empty/sleep)

**WORKDIR:** `behaviors/presence.py`, `behaviors/types.py` (if needed), `behaviors/config.py`, `runtime.py`

### What to implement

1. **RuntimeConfig / env** (defaults — tune in pod.conf later):

| Env | Default | Meaning |
|-----|---------|---------|
| `PRESENCE_STICKY_S` | `1800` | After last **person** evidence, stay occupied at least this long |
| `PRESENCE_EMPTY_STREAK` | `2` | Consecutive **empty** ambient (or empty face probes if wired) to clear before sticky expires |
| `FACE_CACHE_MAX_AGE_S` | `1800` | Raise default from 120 → 30m for FSM soft/hard identity while at desk |

2. PresenceCache state extensions (fields on snapshot or private):

```text
last_person_at: float
empty_streak: int
last_source: str   # ambient|face_seen|tick
soft_name: str     # optional ambient name hint
```

3. Semantics:

```text
note_person_evidence(now, name_hint?, source):
  occupied = True
  last_person_at = now
  empty_streak = 0
  if name_hint and no fresher enrolled face: soft identity update
  face_ts refresh for soft face if using FaceIdentity(is_stranger=True/False carefully)

note_empty_evidence(now, source):
  empty_streak += 1
  if empty_streak >= PRESENCE_EMPTY_STREAK OR now - last_person_at > STICKY:
    occupied = False
    # do not necessarily wipe last face immediately — identity ages via face_ts

occupied_effective(now):
  if last_person_at and now - last_person_at <= STICKY and empty_streak < STREAK:
    return True
  return snapshot.occupied  # after clear
```

4. **`ingest_tick_payload`:**  
   - If chipper `occupied=True` → `note_person_evidence(source="tick")` (or OR with sticky).  
   - If chipper `occupied=False` → do **not** immediately force empty; treat as weak empty or ignore if sticky still warm (prefer: only ambient empty increments streak; tick empty alone doesn’t wipe mid-sticky).  
   **Binding choice:** chipper empty is weak (no clear); ambient empty is strong (increments streak); face_seen always person.

5. **Sleep clear:** if ambient call gap > `AMBIENT_SLEEP_GAP` (4h existing), clear occupied and empty_streak (new session).

6. Workday/joke keep reading `ctx.presence.occupied` — ensure `snapshot.occupied` reflects sticky effective value after each update.

### Documentation references

- `presence.py` update/identity_fresh.  
- `runtime.ingest_tick_payload`.  
- process_state `AMBIENT_SLEEP_GAP`.

### Verification checklist

- [ ] Unit tests: person → NOTHING glances keep occupied for sticky window.  
- [ ] Two ambient empties clear; one empty does not.  
- [ ] Sleep gap clears.  
- [ ] face_seen still sets person + identity.  
- [ ] `test_presence_identity_cached` updated for new max age default or explicit ctor args.

### Anti-pattern guards

- Do not set occupied=False on every tick with chipper sticky miss.  
- Soft name must not replace enrolled primary when identity_fresh enrolled face exists.

---

## TASK-03 — Ambient endpoint writes sticky presence

**WORKDIR:** `routes/ambient.py` (+ deps.BEHAVIOR_RUNTIME)

### What to implement

After successful LLM parse (and image was processed):

```python
if presence == "person":
    BEHAVIOR_RUNTIME.presence.note_person_evidence(now, name_hint=..., source="ambient")
elif presence == "empty":
    BEHAVIOR_RUNTIME.presence.note_empty_evidence(now, source="ambient")
# also apply_sleep_clear if last_ambient gap large (before or after)
```

Log:

```text
[ambient] presence=person name_hint=… occupied_effective=…
[ambient] presence=empty streak=1/2
[ambient] nothing novel   # still OK with presence=person
```

Mirror soft name into PresenceCache face when no enrolled face fresh:

```python
FaceIdentity(face_id=0 or -1, name=hint, is_stranger=True)  # or is_stranger=False only if matches known enrolled name from MEMORY.distinct_faces()
```

**Binding:** If `name_hint` case-insensitively matches an enrolled profile name from `MEMORY.distinct_faces()`, promote to that face_id + is_stranger=False (desk single-user win). Else soft stranger with name string for personalization only (jokes known audience still need non-stranger — matching enrolled name is the path for “Cam”).

### Documentation references

- face.py presence mirror pattern.  
- joke/workday `_identified_primary` needs face_id>0 and not stranger — soft match to enrolled is required for arm/jokes without firmware face.

### Verification checklist

- [ ] Integration-style test: ambient person with name “Cam” + enrolled Cam in memory mock → identity_fresh + not stranger.  
- [ ] Ambient empty×2 → occupied false.  
- [ ] Novelty path still returns text and remember_observation.

### Anti-pattern guards

- Do not invent face_id without enrolled match.  
- Do not clear observations store on presence empty.

---

## TASK-04 — Long `/v1/state/face` cache (chat face)

**WORKDIR:** `process_state.py`, `routes/face.py` (GET window_seconds), `pod.conf-default`, `AGENTS.md`

### What to implement

1. Raise **default** `FACE_RECENT_WINDOW` from **15 → 1800** (30 minutes).  
2. Allow override: `FACE_RECENT_WINDOW_S` or `FACE_RECENT_WINDOW` env (same pattern as other ints).  
3. GET `/v1/state/face` reports actual window.  
4. Document tradeoff: single-user desk; if second person appears, firmware face_seen still updates enrolled; stranger blips still lose to enrolled within window.

5. Align comments that said “deliberately short for handoff” — handoff still works if new enrolled face_seen arrives (enrolled_seen updates). Multi-user handoff without face_seen remains a known limit.

### Documentation references

- `process_state.py` L15–22 comments — rewrite honestly.  
- `prompt_assembly` uses `current_face()` unchanged.

### Verification checklist

- [ ] Unit test: enrolled face still current at +1799s; gone at +1801s (or inject window=2 for fast test).  
- [ ] Enrolled still wins over stranger within window.

### Anti-pattern guards

- Do not use 15s default after this task.  
- Do not break stranger noise suppression (enrolled wins).

---

## TASK-05 — Tick / chipper coherence (light)

**WORKDIR:** `routes/behaviors_http.py`; optional chipper `ambient.go` / `behavior_tick.go`

### What to implement

1. **behaviors_tick:** When evaluating occupied for ingest, prefer:

```text
occupied_effective = PresenceCache sticky OR req.occupied OR current_face() live
```

After ambient sticky lands, tick must not stomp occupied=False over warm sticky (TASK-02 rule).

2. **Optional chipper:** If ambient JSON includes `occupied`, call `NoteAnyFaceSeen()` when true (keeps Go sticky aligned). Not required if all FSMs trust vector-ai cache via tick response path—but Work Day reads presence only after tick ingest. Ensure **every** tick re-ingests vector-ai sticky state (server-side) even when chipper sends occupied=false.

3. **Logging:** `/v1/behaviors/state` expose sticky fields: `last_person_at`, `empty_streak`, `presence_source` for ops.

### Documentation references

- behaviors_http.py current face fallback.  
- GET behaviors/state.

### Verification checklist

- [ ] Tick with occupied=false after ambient person within sticky → still occupied in state.  
- [ ] GET behaviors/state shows sticky debug.

### Anti-pattern guards

- Do not require chipper rebuild if server-side sticky is sufficient (prefer server-side first).

---

## TASK-06 — Docs + pod.conf

**WORKDIR:** `AGENTS.md`, `shared/config/pod.conf-default`, `docs/FSM-implementation.md` (short section if present)

### What to implement

Document:

- Ambient = novelty speech + **presence every glance**  
- Partial person counts  
- Sticky occupancy defaults  
- FACE_RECENT_WINDOW_S / FACE_CACHE_MAX_AGE_S long defaults  
- Dual path: ambient soft vs firmware hard face  
- Sleep gap clears desk  

### Verification checklist

- [ ] pod.conf-default comments for new keys.  
- [ ] AGENTS.md face window numbers match code defaults.

---

## TASK-07 — Verification (final)

### Automated

```bash
cd VectorIntelligence
# use project venv if available for fastapi/dotenv
python3 -m pytest shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py \
  shared/vector-ai/test_service_modules.py -q --tb=short
# plus new ambient presence tests
```

### Grep guards

```bash
# defaults no longer 15 for product window (allow test overrides)
grep -n 'FACE_RECENT_WINDOW' shared/vector-ai/process_state.py

# PRESENCE protocol in ambient
grep -n 'PRESENCE:' shared/vector-ai/routes/ambient.py

# sticky
grep -n 'PRESENCE_STICKY\|empty_streak\|note_person_evidence' shared/vector-ai/behaviors/
```

### Behavioral (device)

| Scenario | Pass |
|----------|------|
| Sit at desk (even face not toward Vector) | Ambient logs `presence=person`; GET behaviors/state `occupied=true` |
| Stay still 10+ min with NOTHING novelty | Still occupied |
| Leave desk 2+ empty ambients | occupied false |
| Voice face_seen | Long GET /v1/state/face still shows you after minutes |
| Work Day / jokes | Not stuck empty solely due to no firmware face |

### Anti-pattern final scan

- [ ] No continuous face stream in chipper.  
- [ ] Chipper ambient still works if extra JSON fields present.  
- [ ] Single empty ambient does not clear sticky.

---

## Suggested defaults (product)

| Knob | Old | New default |
|------|-----|-------------|
| Chat face `FACE_RECENT_WINDOW` | 15s | **1800s (30m)** |
| FSM `FACE_CACHE_MAX_AGE_S` | 120s | **1800s (30m)** |
| `PRESENCE_STICKY_S` | (none) | **1800s** |
| `PRESENCE_EMPTY_STREAK` | (immediate) | **2** |
| Ambient person | n/a | partial body OK |

Single-user desk assumption is explicit; multi-user can lower windows via pod.conf.

---

## Suggested task → subagent split

| Task | Collision | Notes |
|------|-----------|-------|
| 01 | ambient.py | First |
| 02 | presence/config | After 01 parser types known |
| 03 | ambient writes cache | After 02 |
| 04 | process_state | Parallel with 02 OK if careful |
| 05 | behaviors_http | After 02–03 |
| 06 | docs | After defaults frozen |
| 07 | verify | End |

**Recommended:** 01 → 02 → 04 (parallel ok) → 03 → 05 → 06 → 07.

---

## Done definition

- Ambient always emits/parses PRESENCE; partial person counts.  
- Sticky occupancy feeds Work Day + jokes without novelty speech.  
- `/v1/state/face` caches on the order of **tens of minutes** by default.  
- Tests cover sticky, empty streak, long face window, parser.  
- Docs and pod.conf knobs present.

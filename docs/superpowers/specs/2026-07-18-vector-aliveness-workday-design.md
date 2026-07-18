# Vector Aliveness: Work Day Mode + Continuity + Multi-Behavior Runtime

**Date:** 2026-07-18  
**Status:** Approved for implementation planning  
**Repo:** VectorIntelligence  
**Scope:** First aliveness wave — work accountability with continuous self-model, on a multi-behavior foundation

---

## 1. Goals and success criteria

### Problem

Vector already has chat, memory, mood, ambient novelty, sensor lines, and greetings. At a desk for 8+ hours, that can still feel like a smart brick: little continuous self, weak initiative, and no structured work-day presence. Full “house brain” is out of scope (Google already covers that).

### Product goals (priority order)

1. **Continuity of self (D)** — A day/self model that colors speech and chat so Vector “knows how today went.”
2. **Agency / initiative (C)** — Work Day Mode: structured, toggleable accountability pokes (not free-form chatter).
3. **Social presence (A)** — Better conversation and late-arrival dialogue via the same model; not a separate feature wave.

### Noticeability test

If the user cannot **hear or feel** a difference in a normal work day, the change failed. Pure backend state without speech or chat injection is not enough.

**v1 must produce at least one of these on an enabled work day:**

- On-task speech on a schedule after work start is detected.
- Away-from-desk speech after a long absence during work hours.
- Late-arrival dialogue when morning start was missed.
- Noticeably better chat context about the shape of the day when the user speaks.

### Hard constraints

- User works **8+ hours at the desk** with Vector on the back half — **must not spam**.
- Work behavior is **fully toggleable** (other users, holidays).
- Prefer **vector-ai intelligence**; **minimal firmware** changes.
- Architecture must support **multiple behaviors (FSMs)** later without clobbering each other or over-scheduling the robot (especially camera).

### Non-goals (v1)

- House/calendar/lights integration.
- Reading browser tabs, OS focus, or computer activity.
- Companion-mode stock chipper autonomy (same as today: full patched chipper for presence loops).
- Migrating ambient/greeting into the new runtime in the same PR (arbiter-ready; migration optional follow-up).
- Perfect “focus tracking” — only presence, clocks, and user declarations.

---

## 2. Architecture overview

```text
┌──────────────────────────────────────────────────────────────┐
│ chipper (thin)                                               │
│  - PresenceTick: cheap occupancy (+ rare ID at junctures)    │
│  - Optional: serve latest camera frame when asked            │
│  - Speak line when runtime returns non-empty text            │
│  - MarkVoiceActivity (existing)                              │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ vector-ai                                                    │
│  BehaviorRuntime                                             │
│    - PresenceSnapshot cache (face, image, ts)                │
│    - Speech arbiter (priority, min gap, quiet, conversation) │
│    - Registered Behavior plugins                             │
│         WorkDayBehavior  (v1)                                │
│         (future: ambient, greeting, evening wrap, …)         │
│  ContinuityStore (SQLite)                                    │
│  Existing: chat, memory, mood, ambient endpoints             │
└──────────────────────────────────────────────────────────────┘
```

**Principle:** vector-ai owns all decision logic. Chipper reports sensors and acts as the mouth/body.

---

## 3. Multi-behavior runtime (foundation)

Work Day Mode is the **first passenger** on a small plugin runtime so later FSMs load/unload cleanly.

### 3.1 Behavior interface

Each behavior implements roughly:

```text
id: str                    # e.g. "workday"
enabled: from config
priority: int              # higher wins speech contention
tick(ctx: BehaviorContext) -> Optional[SpeechRequest]
on_chat_command(...)       # optional: pause, yes/no late arm
on_presence(snapshot)      # optional push path
```

`BehaviorContext` exposes:

- Clock (local TZ)
- Read-only **PresenceSnapshot** (see cache)
- ContinuityStore read/write scoped keys
- Config for this behavior
- Whether speech is currently allowed (pre-check)

### 3.2 PresenceSnapshot cache (shared sensors)

Expensive signals are **shared**, not owned by one FSM.

Two layers of presence (do not collapse them):

| Layer | Meaning | Cost | How often |
|--------|---------|------|-----------|
| **Occupancy** (`occupied`) | Someone/something is at the desk zone — “not empty” | Cheap (motion, person blob, last tick occupancy, or light scene check) | Every presence tick |
| **Identity** (`face_id`, `name`, `is_stranger`) | *Which* enrolled person | Expensive face stream / enroll match | **Only at key junctures** (see §4.5) |

| Field | Source | Freshness policy |
|--------|--------|------------------|
| `occupied` | Cheap desk occupancy (v1: any person-like presence, recent voice from user path, or “scene not empty” if used) | every tick; age ≤ tick interval |
| `face_id`, `name`, `is_stranger` | face probe / face_seen | use if age ≤ `FACE_CACHE_MAX_AGE` (default **120s** after a juncture probe — not re-probed every tick) |
| `at_desk` | **Occupancy-based** while work day is already armed; identity only required at junctures | derived — see §4.5 |
| `image_jpeg` / base64 | last ambient or workday capture | use if age ≤ `IMAGE_CACHE_MAX_AGE` (default 45s) |
| `on_charger` | if available from chipper | best-effort |
| `last_voice_activity` | existing MarkVoiceActivity path | absolute ts |

**Rules:**

- If behavior A needs a photo and cache image is newer than max age, **do not** open another capture.
- **Do not** run a named-face probe on every Work Day tick. Identity is requested only when a behavior declares `need_identity=true` for this tick (junctures).
- If identity was resolved recently (within `FACE_CACHE_MAX_AGE`), reuse it without a new stream.
- Cache miss for identity **at a juncture only** → runtime may request **one** face probe; result goes to cache for everyone.
- Concurrent capture/probe requests coalesce (single in-flight).

### 3.3 Speech arbiter

Only one proactive utterance at a time.

**Inputs to allow/deny:**

1. Global quiet mode (existing ambient quiet).
2. Mid-conversation / recent voice activity (`SPEECH_SUPPRESS_AFTER_VOICE_S`, default 120s).
3. Minimum gap between **any** proactive speeches (`SPEECH_MIN_GAP_S`, default 90s).
4. Behavior priority if two request speech on the same tick.
5. Explicit pause from Work Day (or future behaviors).

**Priority bands (initial):**

| Priority | Behavior |
|----------|----------|
| 100 | User-facing reactive (sensor pet/pickup) — may stay outside runtime initially |
| 80 | Work Day (accountability) |
| 50 | Proactive greeting |
| 30 | Ambient novelty |

v1: Work Day goes through the arbiter. Ambient/greeting may keep current code paths but **should** call the same “may I speak?” helper so gaps are global. Full migration of ambient/greeting into Behavior plugins is a follow-up.

### 3.4 Scheduling

- Runtime tick: driven by chipper **presence tick** (e.g. every 60–120s) and/or lightweight vector-ai internal timer for clock-only transitions (10:30 no-show).
- No behavior runs its own hammering camera loop.
- Each behavior declares `min_tick_interval`; runtime won’t invoke `tick` more often than that.

### 3.5 Enable / disable behaviors

Config list, e.g.:

```text
BEHAVIORS_ENABLED=workday
# later: BEHAVIORS_ENABLED=workday,ambient,greeting
```

Disabled behaviors are not registered → zero ticks, zero speech.

---

## 4. Work Day Mode (first behavior)

### 4.1 Modes (state machine)

| Mode | Meaning |
|------|---------|
| `off` | Feature disabled (config or end of calendar day) |
| `waiting_morning` | In/before morning start window; watching for at-desk |
| `working` | Morning start detected; pokes allowed |
| `no_show` | Start window ended with no start; **no pokes** (e.g. sick day) |
| `late_check` | First at-desk after no_show; waiting for user’s yes/no |
| `late_working` | User confirmed afternoon work; pokes until end hour |
| `paused` | User paused until a timestamp or “resume” |

Terminal daily: after `WORK_END` → treat as idle until next local midnight resets to `waiting_morning` (if still enabled) or stay `off`.

### 4.2 Transitions

```text
[enabled at local day start]
    → waiting_morning

waiting_morning + occupied + identified primary user during [WORK_START_BEGIN, WORK_START_END]
    → working  (record started_at, primary_face_id)   # juncture: identity required

waiting_morning + clock > WORK_START_END without start
    → no_show

no_show + occupied + identified primary user (first time)
    → late_check                                      # juncture: identity required
    → speak once: morning miss + “working this afternoon?”

late_check + user yes (chat or short affirm)
    → late_working  (armed until WORK_END)

late_check + user no / ignore timeout (configurable, default 15m)
    → no_show  (stay silent; may re-ask once next day only)

working | late_working + verbal pause
    → paused  until until_ts or resume command

paused + until_ts passed | resume
    → previous working/late_working

working | late_working + clock >= WORK_END
    → off (for the day) / idle

config WORKDAY_ENABLED=0
    → off always
```

### 4.3 Proactive speech rules

**A. On-task poke**

- Only in `working` or `late_working`.
- Interval: `WORKDAY_POKE_INTERVAL_S` (default **90 minutes**).
- Timer starts from `started_at` or last poke, not from midnight.
- Line: short, persona-flavored, continuity-tinted (“still on task?”).
- Subject to speech arbiter + suppress rules.

**B. Away scold**

- Only in `working` or `late_working`.
- During `[WORK_AWAY_WINDOW_BEGIN, WORK_END]` (default **09:30–18:00**).
- Continuously not **occupied** for ≥ `WORKDAY_AWAY_S` (default **30 minutes**).
- Speak once per absence stretch; reset when occupied again.
- **No identity re-check** required for away/return while already `working` / `late_working` (occupancy only).
- Line: “Shouldn’t you be working?” class, continuity-tinted.

**C. Late-arrival check**

- On enter `late_check`, one question; no 90m pokes until yes.
- Example: “Didn’t see you this morning. What happened? Working this afternoon?”

### 4.4 Suppress and pause (required)

Even when timers fire, **do not speak** if:

1. `WORKDAY_ENABLED` false or mode `off` / `no_show` / `waiting_morning` (except late_check ask).
2. Global quiet mode on.
3. Recent user↔Vector conversation within suppress window.
4. Speech arbiter min-gap not elapsed.
5. Mode `paused`.

**Verbal pause (chat commands),** parsed like existing `{{…}}` or natural phrases:

- Pause: “break until 2”, “meeting”, “Vector pause work until 14:00”.
- Resume: “back to work”, “resume work mode”.
- Implementation: structured tags preferred for reliability, e.g. `{{workPause||until=14:00}}`, `{{workResume}}`, `{{workAfternoon||yes}}`, with natural-language hints in persona/system for the main user.

### 4.5 Occupancy vs identity (v1) — key junctures only

**Problem this solves:** Face ID every tick is firmware-heavy, flaky when the user faces the monitor, and overkill for “still at the desk.”

**Split:**

| Concept | Definition (v1) | Used for |
|---------|-----------------|----------|
| **Occupied** | Desk zone not empty: person-like presence, or continued occupancy after arming (chipper occupancy signal / “someone there”). Does **not** require a name. | 90m poke eligibility (you’re still around), away timer, return-from-away |
| **Identified primary user** | Enrolled named face matching the household primary (or first face that armed the day) | **Only at key junctures** |

**Key junctures (identity required):**

1. **Morning start** — `waiting_morning` → `working` (must be the right person, not a guest).
2. **Late arrival arm** — `no_show` → `late_check` (same).
3. **Optional: first poke after a very long absence** (e.g. away ≥ 2× `WORKDAY_AWAY_S`) — re-confirm identity once so a guest doesn’t inherit the scold schedule. Default **on**; can disable via config.
4. **Not junctures:** routine ticks while `working` / `late_working`, 90m on-task timer, normal away/return, end-of-day.

**While armed (`working` / `late_working`):**

- Treat **occupied** as “at desk” for timers and away detection.
- Do **not** demand a fresh named-face match every tick.
- If occupancy is lost long enough → away scold path; when occupied again → clear away (no ID).

**Strangers / guests:**

- Cannot trigger morning start or late_check (identity gate).
- If work day already armed and only a stranger is seen, occupancy may still read true — acceptable risk; optional juncture #3 limits guest inheritance after long gaps.

**Limitations (document for users):**

- If primary user never faces Vector during the morning window, start may miss → no_show + late_check (by design).
- Occupancy without a camera person detector may be approximate in v1 (implementation plan picks best cheap signal available from Vector/chipper without a per-tick face stream).

---

## 5. Continuity store

Persisted (SQLite), survives restarts.

### 5.1 Work-day record (per local date)

| Field | Purpose |
|--------|---------|
| `date` | Local calendar date |
| `mode` | Current mode |
| `primary_face_id` | Who armed the day (set at juncture only) |
| `started_at` | When work armed |
| `arm_source` | `morning` \| `late_yes` |
| `last_poke_at` | On-task timer |
| `absence_started_at` | When away stretch began |
| `absence_count` | Completed long absences today |
| `total_away_s` | Accumulated |
| `late_check_asked_at` | Debounce |
| `pause_until` | If paused |
| `notes` | Optional short LLM or rule notes |

### 5.2 How continuity bubbles up (noticeable)

| Surface | Effect |
|---------|--------|
| On-task / away lines | Templates + day stats (absence count, late arm, last answer) |
| Late-check script | Uses no morning start as fact |
| Chat `prepare_messages` | Inject **day strip** into context note when Work Day enabled |
| Mood reflection | Optional: include absence/start stats in mood inputs (existing mood loop) |

Continuity does **not** add a second random speech stream.

---

## 6. Configuration (`pod.conf` / vector-ai env)

Supervisor already maps install config into vector-ai env. All knobs are env-driven (no rebuild for tuning).

| Variable | Default | Meaning |
|----------|---------|---------|
| `WORKDAY_ENABLED` | `0` | Master switch (default **off** for other users) |
| `WORKDAY_TZ` | host local / `TZ` | Timezone for windows |
| `WORKDAY_START_BEGIN` | `09:00` | Morning detect window start |
| `WORKDAY_START_END` | `10:30` | Morning detect window end |
| `WORKDAY_AWAY_WINDOW_BEGIN` | `09:30` | Away scolds not before this |
| `WORKDAY_END` | `18:00` | End of workday pokes |
| `WORKDAY_POKE_INTERVAL_S` | `5400` | 90 minutes |
| `WORKDAY_AWAY_S` | `1800` | 30 minutes |
| `WORKDAY_LATE_CHECK_TIMEOUT_S` | `900` | 15 min wait for yes/no |
| `FACE_CACHE_MAX_AGE_S` | `120` | Reuse identity after a juncture probe (not every tick) |
| `WORKDAY_REID_AFTER_AWAY_S` | `3600` | Optional re-ID after long absence (0 = never) |
| `IMAGE_CACHE_MAX_AGE_S` | `45` | Shared photo snapshot |
| `SPEECH_MIN_GAP_S` | `90` | Global proactive gap |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` | After conversation |
| `BEHAVIORS_ENABLED` | `workday` when workday on | Explicit list optional |

Document in README / NEXT_STEPS: holiday = set `WORKDAY_ENABLED=0` and restart vector-ai (or hot-reload if already supported).

---

## 7. Data flow

### 7.1 Presence tick (chipper → vector-ai)

```text
POST /v1/behaviors/tick
{
  "occupied": true/false,           // cheap; every tick
  "face": {"face_id": 1, "name": "Cam", "is_stranger": false} | null,
                                    // only when chipper just ran an ID probe
  "on_charger": false,
  "voice_recent": false
}
→
{
  "speak": "Still on task, or touring the kitchen?",  // or ""
  "need_identity": false,           // true → chipper should face-probe before next tick
  "actions": []  // future: investigate, etc.
}
```

Server: update PresenceSnapshot → run enabled behaviors’ `tick` → arbiter → return at most one line.

If any behavior sets `need_identity`, chipper runs **one** short face probe (or reuses cache if still fresh) and includes `face` on the following tick — not on every tick.

### 7.2 Chat path

Existing `/v1/chat/completions`:

- Parse work pause / resume / afternoon yes-no.
- Inject continuity day strip into `prepare_messages` context.
- Do not block chat on work timers.

### 7.3 Clock-only transitions

If no tick arrives at 10:31, vector-ai background task (or next tick) still moves `waiting_morning` → `no_show`.

---

## 8. Chipper changes (minimal)

New small loop that:

1. Estimates **occupancy** cheaply each tick.
2. POSTs `/v1/behaviors/tick`.
3. Speaks if `speak` non-empty.
4. Only if `need_identity` → run a short face probe and tick again with `face`.

**Avoid:** separate 90m timer in Go; separate camera loop for workday; **named-face stream every tick**.

**Reuse:** face probe patterns (junctures only), `sayText` / `ambientReact`, `MarkVoiceActivity`, `VECTORAI_PORT`.

Idempotent patch script under `shared/patches/` (e.g. `add-behavior-tick.py`) launching `StartBehaviorTickLoop()` once from startserver.

---

## 9. Error handling

| Failure | Behavior |
|---------|----------|
| vector-ai down | Chipper: no speak (no static nag pool for work — avoid wrong accountability) |
| LLM failure on line generation | Skip poke; keep timers; log; do not fallback to aggressive canned spam more than once per hour optional soft template |
| Face false negative all morning | no_show → late_check when seen; user can arm afternoon |
| Face false positive (someone else) | Identity only at junctures; strangers don’t start/late-arm workday |
| Face flaky while typing | Occupancy keeps work timers; no per-tick ID required |
| Clock/TZ wrong | Document; use explicit `WORKDAY_TZ` |
| Double speak ambient + work | Arbiter min-gap + priorities |

---

## 10. Testing

### Unit (vector-ai)

- Mode transitions: morning start, no_show, late yes/no, pause/resume, end of day.
- Poke interval boundaries.
- Away stretch: 29m no speak, 30m speak once, return resets.
- Suppress: quiet, recent voice, min-gap, disabled.
- Presence cache: occupancy every tick; identity only when `need_identity`; face/image reuse within max age.
- Juncture tests: start/late_check require named primary; mid-day ticks do not.

### Integration

- Mock tick sequence for a simulated day (09:15 start → 10:45 poke → away → return).
- Chat command pause then resume.

### Manual

- Enable on desk machine; verify holiday off switch.
- Confirm no pokes after 18:00 and on no_show sick day.

---

## 11. Implementation phases (for planning skill)

1. **Runtime skeleton** — PresenceSnapshot, arbiter, Behavior interface, tick endpoint, config.
2. **ContinuityStore** — per-date work record + chat day-strip injection.
3. **WorkDayBehavior** — full state machine + line generation (templates first; light LLM optional).
4. **Chipper tick patch** — presence + speak.
5. **Wire suppress** to quiet mode + voice activity.
6. **Docs** — pod.conf / env cheatsheet; default off.
7. **Follow-up** — migrate ambient/greeting to Behavior plugins; richer LLM lines; EOD optional beat.

---

## 12. Decisions log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Intelligence home | vector-ai | Matches ambient/greeting; tunable without rebuild |
| Firmware | Thin tick + speak | Leave robot firmware alone as much as possible |
| Multi-FSM | BehaviorRuntime first | Work Day is first of several; share photo/face; no clobber |
| Default | Work Day **off** | Other users / holiday safe |
| At desk | Occupancy continuous; named face only at junctures | Avoid per-tick face streams; firmware-safe; still gate start/late-arm |
| No morning start | No pokes until late yes | Sick day must stay quiet |
| House brain | Out | Google already owns it |
| Continuity | Seasoning + chat strip | Noticeable without extra spam stream |

---

## 13. Open points for implementation plan (not blockers)

- Exact natural-language vs `{{work…}}` command parsing robustness.
- Whether on-task lines are pure templates or small LLM calls (recommend templates + continuity slots first for cost/latency).
- Whether chipper tick is new loop vs piggyback on greeting loop (plan should pick one).
- Hot-reload of `WORKDAY_ENABLED` without restart (nice-to-have).

---

## 14. Approval

Design approved in brainstorming session (2026-07-18):

- Goals: Work Day Mode + Continuity, desk-safe, toggleable.
- Architecture: vector-ai brain, thin chipper.
- Multi-behavior runtime with shared presence cache and speech arbitration.
- Config, suppress D (soft + verbal pause), continuity fields.
- Data flow, errors, tests, non-goals.

**Amendment (same day):** Occupancy vs identity — named/identified face only at key junctures (morning start, late arm, optional long-absence re-ID); routine at-desk ticks use cheap occupancy only.

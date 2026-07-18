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
│  - PresenceTick: face / empty (reuse short face probe)       │
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

| Field | Source | Freshness policy |
|--------|--------|------------------|
| `face_id`, `name`, `is_stranger` | face probe / face_seen | use if age ≤ `FACE_CACHE_MAX_AGE` (default 30s) |
| `at_desk` | enrolled face in view **or** explicit policy (v1: known face) | derived |
| `image_jpeg` / base64 | last ambient or workday capture | use if age ≤ `IMAGE_CACHE_MAX_AGE` (default 45s) |
| `on_charger` | if available from chipper | best-effort |
| `last_voice_activity` | existing MarkVoiceActivity path | absolute ts |

**Rules:**

- If behavior A needs a photo and cache image is newer than max age, **do not** open another capture.
- If face was probed 25s ago, Work Day uses that for “at desk” instead of a new stream.
- Cache miss → runtime may request **one** capture/probe; result goes to cache for everyone.
- Concurrent requests coalesce (single in-flight capture).

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

waiting_morning + at_desk during [WORK_START_BEGIN, WORK_START_END]
    → working  (record started_at)

waiting_morning + clock > WORK_START_END without start
    → no_show

no_show + at_desk (first time)
    → late_check
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
- Continuously not `at_desk` for ≥ `WORKDAY_AWAY_S` (default **30 minutes**).
- Speak once per absence stretch; reset when `at_desk` again.
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

### 4.5 “At desk” definition (v1)

- **At desk** = enrolled (named) face observed within face cache freshness.
- Stranger-only does **not** count as the primary user at desk (avoids guests arming work mode).
- Empty / no face = away.
- Limitation: if the user faces away from Vector all morning, start may be missed → falls into no_show + late_check path (acceptable; document it).

---

## 5. Continuity store

Persisted (SQLite), survives restarts.

### 5.1 Work-day record (per local date)

| Field | Purpose |
|--------|---------|
| `date` | Local calendar date |
| `mode` | Current mode |
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
| `FACE_CACHE_MAX_AGE_S` | `30` | Shared face snapshot |
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
  "face": {"face_id": 1, "name": "Cam", "is_stranger": false} | null,
  "at_desk": true/false,   // optional; server may derive
  "on_charger": false,
  "voice_recent": false
}
→
{
  "speak": "Still on task, or touring the kitchen?",  // or ""
  "actions": []  // future: investigate, etc.
}
```

Server: update PresenceSnapshot → run enabled behaviors’ `tick` → arbiter → return at most one line.

### 7.2 Chat path

Existing `/v1/chat/completions`:

- Parse work pause / resume / afternoon yes-no.
- Inject continuity day strip into `prepare_messages` context.
- Do not block chat on work timers.

### 7.3 Clock-only transitions

If no tick arrives at 10:31, vector-ai background task (or next tick) still moves `waiting_morning` → `no_show`.

---

## 8. Chipper changes (minimal)

New small loop **or** extend greeting probe interval to also POST `/v1/behaviors/tick` with last face result (prefer **one** loop that updates shared presence and asks runtime for speech).

**Avoid:** separate 90m timer in Go, separate camera loop for workday.

**Reuse:** face probe patterns, `sayText` / `ambientReact`, `MarkVoiceActivity`, `VECTORAI_PORT`.

Idempotent patch script under `shared/patches/` (e.g. `add-behavior-tick.py`) launching `StartBehaviorTickLoop()` once from startserver.

---

## 9. Error handling

| Failure | Behavior |
|---------|----------|
| vector-ai down | Chipper: no speak (no static nag pool for work — avoid wrong accountability) |
| LLM failure on line generation | Skip poke; keep timers; log; do not fallback to aggressive canned spam more than once per hour optional soft template |
| Face false negative all morning | no_show → late_check when seen; user can arm afternoon |
| Face false positive (someone else) | Named face only; strangers don’t start workday |
| Clock/TZ wrong | Document; use explicit `WORKDAY_TZ` |
| Double speak ambient + work | Arbiter min-gap + priorities |

---

## 10. Testing

### Unit (vector-ai)

- Mode transitions: morning start, no_show, late yes/no, pause/resume, end of day.
- Poke interval boundaries.
- Away stretch: 29m no speak, 30m speak once, return resets.
- Suppress: quiet, recent voice, min-gap, disabled.
- Presence cache: stale image/face not reused; fresh shared.

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
| At desk | Named face | Best signal without OS integration |
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

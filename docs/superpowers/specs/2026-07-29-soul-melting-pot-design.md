# Soul melting pot — design

**Date:** 2026-07-29  
**Branch:** `growBotHarness`  
**Repo:** VectorIntelligence (+ monorepo `wire-pod/` only where body must change)  
**Status:** approved for planning / brave rewrite on feature branch  
**Companion plan:** `docs/plans/2026-07-29-soul-melting-pot.md`  
**Prior art:** GrowBot soul snapshot harness (constitution / identity / goals / scratch / log / dream / body / notify); deferred debt “Full presence-clock unification” in `docs/plans/2026-07-26-vector-ai-quality-hardening.md`

---

## 1. Problem

Work Day, chat, greeting, and mood disagree about whether you are here and whether you have been talking.

| System | Clock it trusts today | Symptom |
|--------|----------------------|---------|
| Work Day | `PresenceCache` sticky occupancy | Correctly knows “working” |
| Chat | `face_meta.last_seen` via `touch_face` + `current_face()` | Context can say “last spoke” from wrong field |
| Greeting | in-memory `_face_last_seen` + `last_convo_at` | “Welcome back after ~14 hours” mid-day |
| Mood | observations + `latest_conversation()` only | “Stagnant / long quiet” while chatting |
| Sensor | chipper 20s per `(esn,event)` only | Hot-day phantom pet/pickup spam |
| Eye (vision) | ambient + chat getImage, separate pipelines | Frames not first-class continuity |
| Notify | fire-and-forget Pushover on speech | Not part of continuity / ops ring |

**User-facing pain:** *“Workday knows I’m working, I’ve been talking in chat, yet greeting and mood act like I haven’t been seen in 14 hours.”* Plus sensor TTS chatter on hot days.

**Root cause:** multiple notebooks of “desk truth,” not one.

---

## 2. Goals and success criteria

### Product goals

1. **One desk-day story.** Presence, face, vision digests, last speech, mood tint, workday strip, and notify egress share one hot state (“scratch”).
2. **Greeting only on true return.** No “you’ve been gone 14 hours” when sticky occupancy, recent face, recent chat, or recent vision says otherwise.
3. **Mood reflects the real day.** Digest includes workday strip, presence/session, recent chat, recent vision notes — not only empty observations + stale `last_convo_at`.
4. **One eye.** Ambient and chat capture go through a shared vision path (`eye.see`); digests feed scratch + log; full JPEG stays disk archive.
5. **One face service.** All readers use one effective face; one ring buffer of sightings; firmware face_seen and ambient soft faces both `note()`.
6. **Notify harness.** All spoken lines still egress via one path; pot records recent outbound speech; Pushover remains non-blocking.
7. **Sensor TTS cooldown.** Configurable, stronger hush against thermal phantom edges (chipper-side; empty AI text must not still fallback-speak when we mean silence).

### Noticeability test

| On | Off / broken |
|----|----------------|
| After hours of desk + chat, Vector does **not** re-greet as if you vanished overnight | Greeting still uses private `_face_last_seen` only |
| Mood after a social morning is not “long uneventful stretch” | Mood only sees old convo summary |
| Ambient glance updates “what I last saw” for chat/mood without re-capturing when fresh | Three image pipelines |
| Hot-day phantom touch → at most rare lines (cooldown + optional cross-event gap) | Pet every 20s per event forever |
| Pushover titles still label chat/greeting/sensor/ambient/behavior | Notify blocks or invents second TTS path |

### Constraints (AGENTS.md)

- **Brain vs body:** vector-ai owns policy/state; chipper is sensors + mouth.
- **`service.py` = composition root only**; leaf modules use `deps`, never import `service`.
- **No new `shared/patches/*.py`** — edit monorepo `wire-pod/` Go + rebuild chipper when body changes.
- **LLM knobs → `.env` / `env-default`; stack/FSM knobs → `pod.conf` / `pod.conf-default`.**
- **SpeechArbiter** remains for proactive *behavior* speech; sensor/greeting loops stay chipper-owned TTS unless a phase explicitly moves them.
- **Tests without robot**; freeze clocks; no live OpenRouter required for unit tests.
- **Brave on `growBotHarness`:** prefer clear module boundaries over endless shims; keep thin compatibility wrappers only where routes would break mid-cutover.

### Non-goals (this wave)

- Port GrowBot body/servos/body-code/reflex JSON.
- Full dream/identity-patch sleep gardener (schema reserved; implement later phase optional).
- Continuous `robot_observed_face` EventStream (still forbidden).
- Multi-user perfect arbitration (enrolled-wins + soft buffer is enough).
- Replacing Work Day / Joke Idle plugin architecture.
- Changing Wire-Pod OpenAI prompt command list for this feature.

---

## 3. Architecture overview

```text
                    ┌──────────────────────────────────────┐
                    │  SOUL (vector-ai)                    │
                    │  constitution_ref → persona packs    │
                    │  identity (optional living string)   │
                    │  goals (optional wants / next_try)   │
                    │  scratch:                            │
                    │    presence (sticky occupancy)       │
                    │    face {current, raw, buffer, ttl}  │
                    │    eye  {last_vision, frame_ref, buf}│
                    │    last_spoke_at / last_user_voice   │
                    │    mood {text, updated}              │
                    │    day  (workday strip fields)       │
                    │    traces[]  (recent u/a digests)    │
                    │  log[] day diary                     │
                    │  harness.notify (pushover + ring)    │
                    └──────────────────────────────────────┘
                         ▲ note / see / speak / tick
     face_seen · ambient · tick · chat · sensor · greeting
                         │
                         ▼ read-only policies
              greeting · mood · workday · joke · chat ctx
                         │
                         ▼ egress
              chipper SayText / stream  +  Pushover
```

**Principle:** constitution is cold; scratch is hot; policies read scratch; **no private clocks**.

GrowBot mapping (what we steal):

| GrowBot | Vector melting pot |
|---------|-------------------|
| constitution | `persona.txt` / `PERSONA_MINI` (unchanged packs) |
| identity | optional short living string (later dream) |
| goals | optional; workday stays separate accountability |
| scratch | **new** `soul/` hot state (this design’s core) |
| traces | recent chat digests + face/eye events |
| log | day diary append (workday events, looks, notable speech) |
| dream | reserved; mood loop becomes first consumer of pot digest |
| body | chipper / SDK (stay out of soul JSON) |
| harness | models (existing env) + **notify** |

---

## 4. Soul schema (v1)

Python module package: `shared/vector-ai/soul/` (new).

### 4.1 Public facade

```python
# Conceptual API — implement against this surface; names may match exactly.

class Soul:
    def note_face(self, now, *, face_id, name, is_stranger, source: str) -> None: ...
    def note_person(self, now, *, source, name_hint=None, face=None, image_ref=None, ...) -> None: ...
    def note_empty(self, now, *, source) -> None: ...
    def note_spoke(self, now, *, source: str, text: str = "") -> None: ...  # user or robot speech edges
    def note_user_voice(self, now) -> None: ...
    def see(self, now, *, frame_b64_or_url, reason: str) -> VisionDigest: ...  # archive + vision LLM + scratch
    def last_vision(self) -> Optional[VisionDigest]: ...
    def current_face(self) -> Optional[dict]: ...
    def occupied(self, now) -> bool: ...
    def session_started_at(self) -> float: ...
    def last_seen_at(self) -> float: ...
    def last_spoke_at(self) -> float: ...
    def mood_text(self) -> str: ...
    def set_mood(self, text: str, now: float) -> None: ...
    def day_strip(self, date_s: str) -> str: ...  # delegate ContinuityStore while workday lives there
    def append_log(self, now, txt: str, *, amb: bool = False) -> None: ...
    def notify_speech(self, text: str, *, source: str) -> None: ...  # pot ring + schedule_speech_notify
    def snapshot(self, now) -> dict: ...  # ops GET
    def apply_sleep_clear(self, now) -> None: ...
```

### 4.2 Scratch fields (hot)

| Field | Meaning | Writers |
|-------|---------|---------|
| `occupied` / sticky | desk not empty | ambient, face_seen, tick |
| `session_started_at` | empty→occupied edge | presence |
| `last_person_at` / `last_seen_at` | any person evidence | face, ambient, chat-with-face |
| `face.current` | effective enrolled/stranger | face service |
| `face.buffer[]` | last N sightings | face.note |
| `eye.last_vision` | last vision digest | eye.see |
| `eye.frame_ref` | archive path + ts | eye / archive |
| `last_spoke_at` | robot or user conversational speech | chat, optional greeting |
| `last_user_voice_at` | user chat generate | chat_flow (already has process_state ts) |
| `mood` | one-line tint | mood reflect |
| `traces[]` | last N short u/a or event digests | chat end, optional ambient novelty |
| `notify.recent[]` | last N outbound speech previews | notify harness |

### 4.3 Eye (vision) — first-class

| Reason | When | Model |
|--------|------|-------|
| `ambient` | idle glance (today) | `VISION_MODEL` |
| `chat` | getImage reply (today) | `VISION_MODEL` |
| `return` | optional later: true return before greeting | `VISION_MODEL` |
| `reid` | optional later: workday long-away | `VISION_MODEL` |

**Rules:**

- Full JPEG → `image_archive.save_submitted_image(data, reason)` (existing API).
- Pot stores **path/ref + text digest + presence kind**, not multi-frame b64 in SQLite.
- `IMAGE_CACHE_MAX_AGE_S` / `image_fresh` gates reuse; do not re-capture for policies that only need “still here.”
- Quiet ambient: no `see()` (preserve today’s short-circuit).
- `eye.see` is the **only** place that should call vision for desk glances; chat multimodal stream may still pass images to the chat model, but must **also** archive + note digest into soul.

### 4.4 Face — one service

Merge responsibilities of:

- `process_state._face_state` + `current_face()` + `FACE_RECENT_WINDOW`
- `PresenceCache` face / `identity_fresh` / `FACE_CACHE_MAX_AGE_S`
- greeting `_face_last_seen`

Into:

- `soul.note_face` / `soul.current_face` / `soul.face_buffer`
- Single primary TTL knob (prefer one env; alias others during migration).
- Enrolled-wins-over-stranger-blip rules **copy** from `current_face()` and PresenceCache `_apply_face` (firmware-wins for ambient soft).

**Delete after cutover:** greeting-private `_face_last_seen` dict.

### 4.5 Notify harness

Extend `speech_notify.py` (or `soul/notify.py` wrapping it):

1. `Soul.notify_speech(text, source=...)` records ring entry then `schedule_speech_notify`.
2. Sources remain: `chat`, `greeting`, `sensor`, `ambient`, `behavior` (+ later `dream`).
3. Never block; never raise into chat stream.
4. Optional later: state-event webhooks (`workday.armed`, `presence.return`) — same adapter interface; **not required for v1**.

### 4.6 Persistence

| Data | Store |
|------|--------|
| Mood text | existing `MEMORY.set_state("mood")` |
| face_meta last_seen / last_convo | existing `memory.db` |
| workday day | existing `workday.db` / ContinuityStore |
| face buffer / eye digests / notify ring / traces | **process memory first**; optional SQLite table in phase if restart continuity needed |
| image bytes | disk `ROOT/images` only |

v1 may keep workday ContinuityStore as-is and **project** into `snapshot()` rather than merge DBs.

---

## 5. Policy rewires (who reads the pot)

### 5.1 Greeting (`POST /v1/proactive_greeting`)

**Suppress speak when any of:**

- `not arrived` where `arrived` = `now - last_seen_at > GREETING_ABSENCE_GAP` **and** not sticky-occupied with recent session
- `now - last_spoke_at < GREETING_ABSENCE_GAP` or `now - last_user_voice_at < GREETING_ABSENCE_GAP`
- `now - last_convo_at < GREETING_ABSENCE_GAP` (keep as extra)
- optional: `occupied_effective` and `session_started_at` age < large threshold → treat as continuous desk day, not arrival

**Prompt bits:** use pot `last_spoke_at` / last vision note / workday strip — never invent 14h absence when pot says otherwise.

### 5.2 Mood (`_reflect_mood`)

Digest bits (in order):

1. Time of day (existing)
2. Presence: occupied, session age, last_seen relative
3. Workday `day_strip` if enabled
4. Recent eye digests / observations
5. Recent chat: `last_spoke_at` + latest summary
6. Quiet flag
7. Previous mood

### 5.3 Chat context (`_build_context_note`)

- “Last spoke” uses **`last_spoke_at` / last_user_voice**, not mislabeled `last_seen`.
- `last_seen` may appear as “last saw you” only when visual gap is real.
- Inject short last vision note when fresh and useful.
- Keep day_strip inject.

### 5.4 Work Day / Joke

Keep plugins. They already use PresenceCache — **PresenceCache becomes the presence engine inside soul** (own or wrap; do not fork a second sticky).

### 5.5 Sensor TTS cooldown (body + optional brain)

**Primary gate is chipper** (`wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go`):

- Today: `sensorCooldownDuration = 20 * time.Second` per `"esn:event"`.
- Change: env `SENSOR_TTS_COOLDOWN_S` (default **120** or **180** for hot-day relief; document default choice in plan).
- Add **cross-event** cooldown `SENSOR_TTS_GLOBAL_COOLDOWN_S` (default e.g. **60**) so pet/pickup cannot interleave every 20s.
- Keep rising-edge detection.

**Important:** empty AI text still triggers **static fallback** + `SayText`. If brain ever wants silence, chipper must treat empty as silence **or** cooldown must fire before HTTP. Prefer **cooldown before HTTP** (existing pattern).

Optional: also consult soul quiet later — not required for v1.

---

## 6. Config

### pod.conf (stack)

| Variable | Default (proposed) | Purpose |
|----------|-------------------|---------|
| `FACE_CACHE_MAX_AGE_S` | 1800 | identity fresh (keep) |
| `FACE_RECENT_WINDOW_S` | 1800 | alias → same face TTL during merge |
| `IMAGE_CACHE_MAX_AGE_S` | 45 | eye frame reuse |
| `PRESENCE_STICKY_S` | 1800 | sticky desk |
| `PRESENCE_EMPTY_STREAK` | 2 | empty clear |
| `GREETING_ABSENCE_GAP_S` | 600 | true absence for greeting (export existing constant) |
| `SENSOR_TTS_COOLDOWN_S` | **120** | per-event chipper hush (was hard 20s) |
| `SENSOR_TTS_GLOBAL_COOLDOWN_S` | **60** | cross-event hush |
| existing `SPEECH_*` | unchanged | behavior arbiter only |

### env / LLM

Unchanged: `LLM_MODEL`, `LLM_VISION_MODEL`, `LLM_SUMMARY_MODEL`, temperatures, Pushover keys.

---

## 7. Data flow (target)

```text
Chipper face probe ──POST /v1/state/face_seen──► soul.note_face + note_person
Chipper ambient    ──POST /v1/ambient──────────► soul.see(ambient) → presence + digest
Chipper tick       ──POST /v1/behaviors/tick───► soul presence ingest (existing runtime)
User chat          ──POST /v1/chat/completions─► soul.note_user_voice; touch face; traces
getImage reply     ── multimodal ──────────────► soul.see(chat) + VISION_MODEL stream
Sensor edge        ── chipper cooldown ────────► POST /v1/sensor_reaction → SayText
Any spoken line    ── soul.notify_speech ──────► Pushover + ring
Greeting probe     ──POST /v1/proactive_greeting► read soul; maybe SUMMARY line
Mood loop          ── _reflect_mood ───────────► read soul digest; set_mood
```

---

## 8. Migration strategy

1. **Introduce `soul/` with PresenceCache wrapped or moved inside** — routes write through soul; old helpers become thin wrappers.
2. **Point greeting + mood + chat context at soul** — delete private greeting clock.
3. **Extract ambient vision into `soul.eye.see`** — ambient route becomes thin HTTP.
4. **Notify always via soul.notify_speech** — call sites updated.
5. **Sensor cooldown in monorepo Go** — rebuild chipper; document env; update archived patch text only if still used as mirror (prefer note in patch README: in-tree is source of truth).
6. **Ops:** `GET /v1/soul` or expand health/behaviors state for snapshot.
7. **Optional dream phase** later.

Compatibility: keep HTTP paths stable (`/v1/ambient`, `/v1/proactive_greeting`, `/v1/state/face_seen`, etc.).

---

## 9. Testing strategy

| Area | Tests |
|------|--------|
| Soul presence sticky | port/adapt `test_ambient_presence.py` |
| Face buffer + enrolled wins | unit tests with frozen clock |
| Greeting suppress when last_spoke recent | `test_service_modules` / new `test_soul_greeting.py` |
| Mood digest includes presence/workday | mock soul snapshot |
| Eye archive + digest without network | mock `llm_chat_once` |
| Notify ring + schedule | extend `test_speech_notify.py` |
| Sensor cooldown Go | if no Go test harness, document manual + small pure duration parse test in Go if package allows |
| Regression full | `pytest shared/vector-ai/ -q` |

---

## 10. Risks

| Risk | Mitigation |
|------|------------|
| Big rewrite breaks live desk | Feature branch; phase cutovers; wrappers; full pytest |
| Double presence updates | Single write path per event |
| Vision cost | Reuse image_fresh; quiet skip; don’t vision every greeting by default in v1 |
| Sensor still spam if only Python cooldown | **Must** change chipper Go |
| Empty sensor text still speaks | Document; optional chipper “empty = silence” follow-up |

---

## 11. Decisions log

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | New package `soul/` under vector-ai | Clear melting pot boundary |
| D2 | PresenceCache lives inside soul (wrap or move) | Don’t fork sticky logic |
| D3 | Eye digests in process memory; JPEG on disk | Cost + privacy + existing archive |
| D4 | Sensor cooldown in chipper, not SpeechArbiter | Sensor never hits arbiter today |
| D5 | Raise default sensor cooldown 20s → 120s + global 60s | Hot-day phantoms |
| D6 | Greeting uses last_seen_at + last_spoke + sticky session | Fixes 14h false return |
| D7 | Dream/identity patch deferred but schema-reserved | Scope control |
| D8 | Bold delete of `_face_last_seen` after soul face buffer | Private clocks are the bug |

---

## 12. Open points (resolve in plan if needed)

1. Exact default for `SENSOR_TTS_COOLDOWN_S` (120 vs 180).
2. Whether chat multimodal path must always call full ambient-style PRESENCE parse or only archive + short note.
3. Persist face buffer across restarts? (v1: no.)
4. `GET /v1/soul` vs fold into existing state endpoints.

---

## 13. Approval

Brave rewrite on branch **`growBotHarness`**. Revert is fine. Executable phases: `docs/plans/2026-07-29-soul-melting-pot.md`.

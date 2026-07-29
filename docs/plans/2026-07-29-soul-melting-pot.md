# Soul melting pot (presence · face · eye · notify · sensor cooldown)

**Date:** 2026-07-29  
**Branch:** `growBotHarness`  
**Repo:** VectorIntelligence (`shared/vector-ai/`) + monorepo chipper for sensor TTS only  
**Spec:** `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md`  
**Goal:** One desk-day continuity brain so workday, chat, greeting, mood, vision, and notify share the same facts; stop false “14 hour absence” greetings/moods; tame hot-day phantom `sensor_reaction` TTS.  
**Execute with:** `/do` or sequential phase agents after review. Be bold on this branch.

---

## Phase 0 — Documentation discovery (complete)

### Problem (evidence-backed)

| Fact | Source |
|------|--------|
| Presence sticky lives in `PresenceCache` (1800s, empty×2) | `behaviors/presence.py` L21–71; `behaviors/config.py` FACE/PRESENCE env |
| Chat face is separate: `current_face()` + `FACE_RECENT_WINDOW` (1800s) | `process_state.py` L25–86 |
| Greeting “arrived” uses **private** `_face_last_seen` only updated on greeting probe | `routes/greeting.py` L68–77; `process_state.py` L140–143 |
| Greeting “last spoke” uses `face_meta.last_convo_at` (summary job), not live chat | `greeting.py` L72–74; `memory.set_convo_summary` only from `chat_flow` |
| Chat context says “last spoke” but gap is from `prior.last_seen` (touch_face) | `prompt_assembly.py` L220–222 |
| Mood digest: observations + `latest_conversation` + quiet + prior mood — **no** presence/workday/vision | `routes/mood.py` L31–59 |
| Ambient already sends JPEG → `VISION_MODEL` → PRESENCE + sticky write | `routes/ambient.py` L287–331 |
| Chat getImage + multimodal uses `VISION_MODEL`; archive `save_submitted_image` | `vision.py`; `chat_flow.py`; `image_archive.py` |
| Pushover: `schedule_speech_notify` sources chat/greeting/sensor/ambient/behavior | `speech_notify.py` |
| Deferred debt: “Full presence-clock unification” | `docs/plans/2026-07-26-vector-ai-quality-hardening.md` L31 |
| Sensor TTS gate is chipper **20s per (esn,event)** only; Python has **no** cooldown | `wire-pod/.../sensor_reactions.go` L29, L135–144; `routes/sensor.py` |
| Empty sensor AI text → chipper **static fallback still SayText** | `sensor_reactions.go` sensorReact |
| SpeechArbiter / quiet / SPEECH_* do **not** apply to sensor | `behaviors/arbiter.py`; ambient quiet only |

**Root cause:** fragmented clocks + sensor cooldown too short and per-event only.

### Allowed APIs (use only these — copy, don’t invent)

| API | Location | Use for |
|-----|----------|---------|
| `PresenceCache.note_person_evidence` / `note_empty_evidence` / `apply_sleep_clear` / `occupied_effective` / `identity_fresh` / `effective_face` / `debug_dict` | `behaviors/presence.py` | Presence engine inside soul (wrap/move, don’t fork) |
| `FaceIdentity`, `PresenceSnapshot` | `behaviors/types.py` | Types |
| `BehaviorRuntime.ingest_tick_payload` / presence field | `behaviors/runtime.py` | Tick ingest remains |
| `current_face`, `FACE_RECENT_WINDOW`, `SESSION_GREETING_GAP`, `GREETING_ABSENCE_GAP`, `_mood_state`, `_LAST_USER_VOICE_TS`, `load_mood` | `process_state.py` | Migrate into soul; thin wrappers OK mid-cutover |
| `MEMORY.touch_face` / `get_face_meta` / `set_convo_summary` / `latest_conversation` / `list_observations` / `set_state` | `memory.py` | Persist face_meta + mood |
| `ContinuityStore.day_strip` / workday load | `behaviors/continuity.py` | Day strip for mood/chat |
| `save_submitted_image` | `image_archive.py` | Eye archive |
| `llm_chat_once`, `one_shot_spoken_line`, `VISION_MODEL`, `SUMMARY_MODEL`, `image_payload_too_large` | `llm.py` | Vision + one-shots |
| `is_vision_intent`, `_GETIMAGE_PAYLOAD` | `vision.py` | Chat capture force |
| `schedule_speech_notify`, `pushover_configured`, `send_speech_pushover` | `speech_notify.py` | Notify egress |
| `parse_ambient_llm_raw` + ambient system prompt patterns | `routes/ambient.py` | Eye ambient reason |
| `deps.*` composition | `deps.py` / `service.py` | Wire soul singleton |
| Chipper `sensorOnCooldown` / `sensorReact` / `vectorAIBase` env pattern | `wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go` | Sensor TTS cooldown |
| Env parse style `VECTORAI_PORT` in same Go file | same | Copy for `SENSOR_TTS_COOLDOWN_S` |

### Anti-patterns (do not)

- Invent a second sticky presence parallel to `PresenceCache`.
- Keep greeting `_face_last_seen` after soul face is live (delete it).
- Put sensor TTS mute **only** in Python without changing chipper (empty text still speaks).
- Route sensor through SpeechArbiter (no hook today; wrong layer for rising-edge body events).
- Continuous `robot_observed_face` stream.
- New files under `shared/patches/` for production (edit monorepo Go).
- Fat logic in `service.py` or routes that bypass soul writers.
- Import `service` from leaf modules.
- Store full multi-frame JPEG blobs in SQLite.
- Put `SENSOR_*` cooldown only in `.env` if chipper never reads it — chipper needs process env from supervisor/`VECTORAI_*` style or its own env; document supervisor inject if using pod.conf.

### Architectural target

```text
soul/  (new)
  __init__.py      Soul facade + snapshot
  presence.py      thin re-export OR moved PresenceCache owner
  face.py          note/current/buffer (merges process_state face + greeting clock)
  eye.py           see/archive/digest (ambient + chat reasons)
  notify.py        ring + schedule_speech_notify
  types.py         VisionDigest, FaceSighting, SoulSnapshot

routes/*           write/read via deps.SOUL
prompt_assembly    context from soul
mood/greeting      digest/gates from soul
chipper sensor     longer + global cooldown env
```

**Principle:** constitution cold (`persona*`); scratch hot (`soul`); policies read; chipper mouth.

### GrowBot prior art (what we steal)

| Idea | Use in Vector |
|------|----------------|
| scratch as hot state | `Soul` scratch fields |
| traces | recent digests ring |
| log | `append_log` for day diary (optional v1) |
| eye as real sense | `soul.eye.see` → VISION_MODEL |
| harness notify | Pushover + ring |
| dream gardener | **deferred** (schema note only) |

---

## Phase 1 — Soul package + composition root (foundation)

### What to implement

1. Create `shared/vector-ai/soul/` package with:
   - `types.py`: `FaceSighting`, `VisionDigest` (dataclass: t, reason, presence, name_hint, note, spoken, model, frame_path), `SoulSnapshot` dict builder helpers.
   - `face.py`: ring buffer (max 32); `note(now, face_id, name, is_stranger, source)`; `current(now)` enrolled-wins logic **copied from** `process_state.current_face` + PresenceCache `_apply_face` rules.
   - `presence_bridge.py` or own `PresenceCache` instance: construct with same config as `BehaviorRuntime` today (`RuntimeConfig` face/image/sticky/empty from `behaviors/config.py`).
   - `soul.py` / `__init__.py`: class `Soul` holding presence + face + mood pointers + timestamps (`last_seen_at`, `last_spoke_at`, `last_user_voice_at`); methods listed in design §4.1 (stub `see`/`notify` for later phases if needed).
   - `snapshot(now) -> dict` for ops.

2. Wire in `service.py` (composition root only):
   ```python
   from soul import Soul
   SOUL = Soul(presence=..., face=..., memory=MEMORY, ...)
   deps.SOUL = SOUL
   ```
   Prefer **one** PresenceCache: either BehaviorRuntime receives `SOUL.presence` or Soul wraps `BEHAVIOR_RUNTIME.presence`. **Do not create two caches.**

3. Add `deps.SOUL = None` in `deps.py`.

4. Unit tests `shared/vector-ai/test_soul_core.py`:
   - frozen clock sticky occupancy via soul
   - face buffer + enrolled wins over stranger blip
   - `last_seen_at` updates on `note_face` / person evidence

### Documentation references

- Design §3–4: `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md`
- PresenceCache ctor: `behaviors/presence.py` L21–32
- Runtime wiring: `behaviors/runtime.py` L41–48
- Composition: `service.py` + `deps.py` (pattern from ContinuityStore)

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_soul_core.py -q`
- [ ] Existing `test_ambient_presence.py` and `test_behaviors.py` still pass (shared presence instance)
- [ ] `rg "deps.SOUL" shared/vector-ai/service.py shared/vector-ai/deps.py`

### Anti-pattern guards

- Do not leave BehaviorRuntime and Soul with separate PresenceCache instances.
- Do not put HTTP routes in this phase beyond optional dry-run.

---

## Phase 2 — Face writers unified (face_seen + chat + greeting clock kill)

### What to implement

1. **`routes/face.py`:** after updating any legacy `_face_state` (or instead), call `deps.SOUL.note_face(...)` + ensure person evidence still feeds presence (today L41–51). Prefer SOUL owns both; keep `_face_state` as shim calling SOUL if needed.

2. **`process_state.current_face`:** implement as `return deps.SOUL.current_face()` when SOUL set, else legacy (tests).

3. **`prompt_assembly._effective_face` / `prepare_messages`:** on chat turn with face, call `SOUL.note_face` or `note_person` and `SOUL.note_user_voice(now)`; set `last_spoke_at` when assistant finishes in chat_flow (Phase 4 can complete speech side).

4. **Greeting:** stop using `_face_last_seen`. In `routes/greeting.py`:
   - `prev_seen = SOUL.last_seen_at` or face-buffer last for that fid
   - `arrived` from pot + sticky session rules (design §5.1)
   - suppress if `last_user_voice` / `last_spoke` recent within `GREETING_ABSENCE_GAP`
   - on greeting probe sighting: `SOUL.note_face(..., source="greeting")` **before** arrive calc (so continuous probes refresh last_seen even when no speak)

5. Delete or stop writing `process_state._face_last_seen` once tests green.

6. Tests: greeting returns empty when last_spoke 60s ago; returns candidate when last_seen 2h ago and no sticky/session; face_seen updates soul buffer.

### Documentation references

- `routes/greeting.py` L59–77 (gates to replace)
- `routes/face.py` full
- Design §5.1

### Verification checklist

- [ ] `pytest shared/vector-ai/test_service_modules.py -k greeting -q`
- [ ] New greeting/soul tests for suppress-on-recent-chat
- [ ] `rg "_face_last_seen" shared/vector-ai` → only comments or gone
- [ ] Full `pytest shared/vector-ai/test_ambient_presence.py shared/vector-ai/test_soul_core.py -q`

### Anti-pattern guards

- Do not compute arrived only from greeting probe map.
- Do not skip `note_face` on greeting probe when suppressing speech (must still refresh last_seen).

---

## Phase 3 — Eye module (vision melt)

### What to implement

1. `soul/eye.py`:
   ```python
   async def see(self, now, *, image, reason: str, ...) -> VisionDigest:
       # 1) image_payload_too_large guard (llm.py)
       # 2) save_submitted_image(image, reason)
       # 3) if reason == "ambient": existing ambient multimodal prompt + parse_ambient_llm_raw
       #    if reason == "chat": may only archive + optional short note (chat stream still owns reply)
       # 4) write SOUL presence from PRESENCE line when ambient
       # 5) store last_vision + buffer (text digests, max ~16)
       # 6) return digest
   ```

2. **Refactor `routes/ambient.py`** to call `await deps.SOUL.eye.see(...)` (or `SOUL.see`) for the vision+presence path; keep quiet short-circuit and sleep clear **on soul** (`SOUL.apply_sleep_clear`).

3. **Chat path:** on multimodal user image, after sanitize, `save_submitted_image` already runs — also `SOUL.note_eye_frame` / light digest (can skip second LLM if chat model already describes; at minimum archive path + `last_seen` if face known). Prefer: always archive via eye helper; ambient-style PRESENCE parse **not** required on every chat image in v1.

4. Config: keep `IMAGE_CACHE_MAX_AGE_S`; expose `eye.image_fresh(now)`.

5. Tests: ambient path with mocked `llm_chat_once` still updates presence; archive called with reason ambient; oversized image skips.

### Documentation references

- Ambient vision body: `routes/ambient.py` L228–340 (copy prompt + parse)
- `image_archive.save_submitted_image`
- `llm.VISION_MODEL`, `image_payload_too_large`
- Design §4.3

### Verification checklist

- [ ] `pytest shared/vector-ai/test_ambient_presence.py shared/vector-ai/test_image_archive.py -q`
- [ ] Ambient route tests still pass in `test_service_modules.py -k ambient`
- [ ] `rg "VISION_MODEL" shared/vector-ai/soul shared/vector-ai/routes/ambient.py`

### Anti-pattern guards

- Do not send ambient frames to `SUMMARY_MODEL`.
- Do not invent presence on quiet short-circuit.
- Do not store raw b64 rings in SQLite.

---

## Phase 4 — Notify harness + speech clocks

### What to implement

1. `soul/notify.py` or methods on Soul:
   - `notify_speech(text, source)` → append `notify.recent` (cap 20: t, source, preview[:120]) → `schedule_speech_notify(text, source=source)`.

2. Replace direct `schedule_speech_notify` call sites with `deps.SOUL.notify_speech` (or keep schedule as implementation detail called only from soul):
   - `routes/greeting.py`
   - `routes/sensor.py`
   - `routes/ambient.py` (novelty speak)
   - `chat_flow.py`
   - behaviors speak path (`routes/behaviors_http.py` or runtime)

3. **Speech clocks:**
   - `chat_flow` start: `SOUL.note_user_voice(now)` (and keep process_state `_LAST_USER_VOICE_TS` in sync for arbiter if still read there).
   - After successful assistant reply: `SOUL.note_spoke(now, source="chat", text=reply preview)`.
   - Greeting/sensor/ambient when they produce text: `note_spoke` with their source.

4. Tests: notify ring length; schedule still invoked (monkeypatch); chat sets last_user_voice.

### Documentation references

- `speech_notify.py` full API
- Call sites: `rg schedule_speech_notify shared/vector-ai`
- Arbiter voice suppress uses `_LAST_USER_VOICE_TS` — keep updated

### Verification checklist

- [ ] `pytest shared/vector-ai/test_speech_notify.py -q`
- [ ] `rg "schedule_speech_notify" shared/vector-ai` → only soul/notify (+ tests)
- [ ] Greeting suppress test still green using last_spoke from soul

### Anti-pattern guards

- Do not await Pushover on the stream critical path.
- Do not add a second TTS HTTP.

---

## Phase 5 — Mood + chat context rewire (kill 14h fiction)

### What to implement

1. **`routes/mood.py` `_reflect_mood`:** build digest from SOUL snapshot:
   - time of day (existing helpers `_time_of_day`)
   - occupied / session age / last_seen relative (`_relative_time`)
   - workday `day_strip` if enabled (copy inject gate from `prompt_assembly` L245–260)
   - last vision note if any
   - observations (existing)
   - last conversation summary + **last_spoke relative**
   - quiet
   - prior mood  
   Persist via `SOUL.set_mood` + `MEMORY.set_state`.

2. **`prompt_assembly._build_context_note`:**
   - Fix wording: use last_spoke/last_user_voice for “last spoke”; use last_seen only for visual absence.
   - If `gap_spoke <= 90`: omit session nag (existing mid-turn idea).
   - Inject last vision one-liner when `eye` fresh and useful.
   - Keep day_strip + mood tint.

3. Optionally bump `face_meta` interaction via existing `touch_face` (keep).

4. Tests in `test_prompt_assembly.py` + mood unit test with fake SOUL.

### Documentation references

- `routes/mood.py` L31–81
- `prompt_assembly.py` L189–273
- Design §5.2–5.3

### Verification checklist

- [ ] `pytest shared/vector-ai/test_prompt_assembly.py -q`
- [ ] Mood unit: when last_spoke moments ago and occupied, user content to LLM must not claim “no conversation in a long time”
- [ ] Manual read of assembled mood bits in test asserts

### Anti-pattern guards

- Do not announce mood in spoken lines.
- Do not remove workday day_strip inject.

---

## Phase 6 — Sensor TTS cooldown (chipper + docs)

### What to implement

1. **Edit monorepo** `wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go`:
   - Replace hard `const sensorCooldownDuration = 20 * time.Second` with env-loaded duration:
     - `SENSOR_TTS_COOLDOWN_S` — default **120**
     - Copy env parse style from `VECTORAI_PORT` in same file (L35–41)
   - Add **global** cooldown map or second key `esn:*` / `lastAnySensorSpeech`:
     - `SENSOR_TTS_GLOBAL_COOLDOWN_S` — default **60**
   - `sensorOnCooldown(esn, event)` returns true if per-event **or** global window active; store both timestamps when accepting a reaction.
   - Keep rising-edge only; keep fallback phrases when AI empty (document limitation).

2. **Supervisor / pod.conf:** add commented keys to `shared/config/pod.conf-default`. If supervisor already injects all pod.conf keys into chipper env, good; else document that chipper reads process env (set in service file / start scripts). Prefer inject via existing pod.conf → child env path used for `AI_PORT`/`VECTORAI_PORT`.

3. **Update** `shared/patches/add-sensor-reactions.py` **only if** it is still applied anywhere — AGENTS says patches archived; add a one-line note in `shared/patches/README.md` pointing to monorepo Go as source of truth for cooldown.

4. **Optional brain:** if event arrives during cooldown, chipper never HTTP — no Python change required. Do **not** rely on empty text.

5. Document in `docs/FSM-implementation.md` short “sensor spam” note + defaults.

### Documentation references

- `sensor_reactions.go` L29, L135–144, L201–214
- Design §5.5, D4–D5
- Ambient quiet is **not** a template for sensor silence without Go change

### Verification checklist

- [ ] Go builds: `cd wire-pod/chipper && go build ./...` (or project’s usual chipper build)
- [ ] Defaults: 120s per-event, 60s global when env unset
- [ ] `rg "sensorCooldownDuration|SENSOR_TTS" wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go`
- [ ] pod.conf-default comments present

### Anti-pattern guards

- Do not set default still at 20s and call it fixed.
- Do not implement cooldown only in `routes/sensor.py`.
- Do not claim quiet mode mutes sensors without wiring.

---

## Phase 7 — Ops surface + docs glue

### What to implement

1. `GET /v1/soul` (or `/v1/state/soul`) returning `SOUL.snapshot(now)` — face current, occupied, last_seen, last_spoke, last_vision preview, mood, notify.recent count, presence debug.

2. Update:
   - `docs/FSM-implementation.md` §3.6 presence → “soul owns presence/face/eye clocks”
   - `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` decisions if defaults changed
   - `AGENTS.md` Docs map row for this design + plan
   - `NEXT_STEPS.md` brief pointer if it lists ambient/greeting gaps

3. Mark quality-hardening deferred item as addressed by this plan (note in quality plan or AGENTS).

### Documentation references

- Ambient state GET pattern: `routes` health/mood GET
- AGENTS.md docs map

### Verification checklist

- [ ] Route registered; test returns keys occupied/face/mood
- [ ] Docs cross-links resolve

### Anti-pattern guards

- Do not dump full base64 frames in JSON snapshot.

---

## Phase 8 — Final verification (agent-completable)

### What to run

```bash
cd VectorIntelligence
python3 -m pytest shared/vector-ai/ -q
# if chipper changed:
cd ../wire-pod/chipper && go build -o /tmp/chipper-build ./...
```

### Grep anti-pattern checks

```bash
cd VectorIntelligence
# Private greeting clock should be gone
rg "_face_last_seen" shared/vector-ai --glob '*.py' || true
# schedule_speech_notify only via soul/notify (allow speech_notify.py def + tests)
rg "schedule_speech_notify" shared/vector-ai --glob '*.py'
# No second PresenceCache construction without sharing
rg "PresenceCache\(" shared/vector-ai --glob '*.py'
# Sensor cooldown env present in Go
rg "SENSOR_TTS_COOLDOWN" ../wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go
```

### Success criteria

1. **Noticeability:** mid-day chat + sticky desk → proactive greeting suppressed; mood digest mentions recent talk/presence when those are warm.
2. **Eye:** ambient still PRESENCE-updates desk; frames archived; digests on soul.
3. **Face:** one current_face path; greeting probes refresh last_seen even when silent.
4. **Notify:** Pushover still fire-and-forget; ring in snapshot.
5. **Sensor:** default hush ≥60s global and ≥120s per-event; env-overridable; chipper rebuild required for body change.
6. **Tests:** full vector-ai pytest green; go build green if Go touched.
7. **HTTP paths unchanged** for chipper clients.

---

## Human / environment steps (agent cannot complete)

| # | Step | Notes |
|---|------|-------|
| 1 | Rebuild/restart chipper after Phase 6 | sensor cooldown live |
| 2 | Restart vector-ai after soul merge | supervisor reload |
| 3 | Live desk day soak | confirm no 14h greeting; hot-day pet rate |
| 4 | Confirm pod.conf → chipper env for SENSOR_* | platform-specific start scripts |

### Out of scope (explicit)

- GrowBot body/servos/reflexes
- Dream identity patch sleep job (reserved only)
- Continuous face EventStream
- SpeechArbiter for sensors
- Changing Work Day poke intervals
- Multi-robot soul sync

---

## Suggested implementation order

| Order | Phase | Risk | Why |
|-------|-------|------|-----|
| 1 | Phase 1 Soul core | medium | Foundation; shared presence |
| 2 | Phase 2 Face + greeting | medium | Fixes core annoyance |
| 3 | Phase 5 Mood + chat ctx | medium | Completes continuity fiction fix |
| 4 | Phase 4 Notify + speech clocks | low | Supports greeting suppress |
| 5 | Phase 3 Eye | medium | Bigger ambient refactor |
| 6 | Phase 6 Sensor Go | low–med | Independent; ship early if hot-day pain |
| 7 | Phase 7 Ops/docs | low | Glue |
| 8 | Phase 8 Verify | — | Gate |

**Note:** Phase 6 (sensor) can run **in parallel** with Phases 1–5 (different tree).

**Brave option:** implement Phase 6 first for immediate hot-day relief while soul lands.

---

## Files likely touched

| File | Change |
|------|--------|
| `shared/vector-ai/soul/*` | **NEW** package |
| `shared/vector-ai/deps.py` | `SOUL` |
| `shared/vector-ai/service.py` | construct SOUL |
| `shared/vector-ai/process_state.py` | thin wrappers / delete greeting map |
| `shared/vector-ai/routes/face.py` | soul writers |
| `shared/vector-ai/routes/greeting.py` | pot gates |
| `shared/vector-ai/routes/mood.py` | pot digest |
| `shared/vector-ai/routes/ambient.py` | eye.see |
| `shared/vector-ai/routes/sensor.py` | notify via soul only |
| `shared/vector-ai/prompt_assembly.py` | last_spoke vs last_seen |
| `shared/vector-ai/chat_flow.py` | voice/spoke clocks; eye note |
| `shared/vector-ai/speech_notify.py` | maybe ring helper |
| `shared/vector-ai/test_soul_*.py` | **NEW** |
| `shared/config/pod.conf-default` | SENSOR_* + comments |
| `wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go` | cooldown env + global |
| `docs/FSM-implementation.md` | presence/soul |
| `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` | already written |
| `AGENTS.md` | docs map |

**Prefer not touched:** joke_idle logic, workday mode math (only presence source), Wire-Pod openai_prompt.

---

## Commit sequence (suggested)

1. `feat(soul): core package + shared presence wiring`
2. `feat(soul): unify face writers; greeting reads pot`
3. `feat(soul): mood + chat context use last_spoke/presence`
4. `feat(soul): notify harness + speech clocks`
5. `feat(soul): eye.see ambient vision path`
6. `feat(chipper): SENSOR_TTS cooldown env + global gap`
7. `docs: soul melting pot ops + FSM notes`

---

## Appendix — Clock map after cutover

| Fact | Single owner |
|------|----------------|
| Desk occupied | `Soul.presence` (PresenceCache) |
| Who | `Soul.face.current` |
| Last visual person evidence | `Soul.last_seen_at` |
| Last user chat | `Soul.last_user_voice_at` |
| Last robot speech (any path) | `Soul.last_spoke_at` |
| Last vision digest | `Soul.eye.last_vision` |
| Mood | `Soul.mood` + memory state |
| Workday day shape | ContinuityStore (projected in snapshot) |
| Sensor TTS rate | chipper cooldown env |
| Pushover | `Soul.notify_speech` → speech_notify |

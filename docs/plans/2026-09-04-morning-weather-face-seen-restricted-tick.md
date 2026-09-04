# Morning weather: enrolled `face_seen` → restricted tick → firmware weather

**Status:** implemented 2026-09-04 (Python + chipper). Docs Phase 4 done. Live-robot (Phase 5) unchecked.  
**Date:** 2026-09-04  
**Repo:** VectorIntelligence (`shared/vector-ai/`) **and** monorepo `wire-pod/chipper/`  
**Parent:** [2026-08-29-morning-weather-intent.md](./2026-08-29-morning-weather-intent.md) · delivery [2026-09-02-fireweather-webviz-cloud-intent.md](./2026-09-02-fireweather-webviz-cloud-intent.md) · owner [FSM-morning-weather.md](../FSM-morning-weather.md)  
**Goal:** Net effect: enrolled sensor event → identity already in the presence cache → **weather-only** speak decision → chipper `FireWeatherIntent` (firmware weather anim) **within seconds of** `[face] observed: id=N 'Cam' (enrolled)`. Occupancy policy and Work Day stay on the ~60s `POST /v1/behaviors/tick`.  
**Execute with:** `/do` (or sequential phase agents) after review.

**Out of scope:** afternoon/evening weather; LLM weather lines; AppIntent; continuous `robot_observed_face`; denser-than-sparse idle face cadence (2 min stays unless a follow-up); vector-ai HTTP back to chipper; arming Work Day from `face_seen`; joke idle on this path.

---

## Phase 0 — Documentation discovery (complete)

### Sources consulted

| Source | Findings |
|--------|----------|
| `docs/FSM-morning-weather.md` | Dual enable; Work Day must be `WORKING`; window 09:00–12:30 exclusive lunch; dwell 60s; delivery is chipper WebViz cloud inject, not AppIntent |
| `docs/FSM-implementation.md` §2–3.2 | Tick is the only “may I speak?” path; `TickResult.intent`; speech-gated commits; identity only at junctures; no continuous named-face stream |
| `docs/FAQ.md` | `face_seen` sets occupancy+identity, does **not** tick; restricted tick is design-only; do not ingest occupied=true from long face cache |
| `docs/plans/2026-08-29-morning-weather-intent.md` | Suggested “identity same manners as other morning pokes” — **not shipped** |
| `docs/plans/2026-09-02-fireweather-webviz-cloud-intent.md` | `FireWeatherIntent` + `InjectCloudIntentWebViz`; **Go-only**; Python proposing `intent` without chipper fire is a no-op |
| `behaviors/morning_weather.py` | Gates: WORKING, window, daily lock, occupied, voice_recent, occupancy-session dwell. **No** `identity_fresh` / enrolled / `face_ts` |
| `behaviors/joke_idle.py` L258–277 | COPY: `audience=="known"` → `need_identity` or stranger reject |
| `behaviors/workday.py` `_identified_primary` L104–115 | COPY: named, `face_id>0`, not stranger, match primary once set |
| `behaviors/workday.py` `_tick_waiting_morning` L413–449 | Morning arm is **not** speech-gated — must not run from `face_seen` |
| `behaviors/workday.py` `clock_tick` L652–670 | Presence-free calendar transitions; already run from `service.py` 60s loop |
| `behaviors/runtime.py` `tick` L315–454 | Always `clock_tick` then **every** plugin then arbiter. **No** allowlist API |
| `behaviors/runtime.py` `ingest_tick_payload` L230–303 | Soft `current_face` reuse must not re-occupy |
| `behaviors/arbiter.py` | `allow` / `record_speech`; deny `quiet` / `recent_voice` / `min_gap` |
| `routes/face.py` | Enrolled: `face_id>0` and non-empty name. Returns `{ok, is_stranger}` only |
| `routes/behaviors_http.py` | Ingest then full `tick()`; JSON `{speak, need_identity, intent}` |
| `wire-pod/.../ttr/sensor_reactions.go` `notifyFaceSeen` | POST `{face_id,name}`; **discards body**; 5s rate limit; **no `esn`** |
| `wire-pod/.../ttr/face_probe.go` `ObserveFaceBriefly` | **Only in-tree caller** of `notifyFaceSeen`; voice-start; `MarkVoiceActivity()` first |
| `wire-pod/.../ttr/behavior_tick.go` | 60s tick; sparse `ProbeFace` every 2 min → tick JSON `face`, **not** `face_seen`; `intent==intent_weather_extend` → `FireWeatherIntent(esn)` |
| `robotsession/stream.go` | Long state stream **must not** whitelist `robot_observed_face`; short `ProbeFace` only |
| Tests | `test_morning_weather.py` helpers; `test_joke_idle.py` `test_fsm_identity_gating`; `test_ambient_presence.py` `test_face_seen_http_notes_person`; **no** Go tests for `notifyFaceSeen` / `FireWeatherIntent` |

### The blocking fact (must not skip)

Today `[face] observed: … (enrolled)` is **not** an idle-desk sensor. `notifyFaceSeen` is only called from `ObserveFaceBriefly` at **voice-start**. Idle occupancy glances go to `POST /v1/behaviors/tick` with `face` and never hit `/v1/state/face_seen`. Hooking weather only to the current `face_seen` would fire at “hey Vector”, then the arbiter would deny (`MarkVoiceActivity` + `SPEECH_SUPPRESS_AFTER_VOICE_S` default 120s).

A restricted tick is useless at the desk unless chipper also POSTs `face_seen` from the **idle** enrolled `ProbeFace` (existing sparse/need_identity glance). Do **not** restore a continuous `robot_observed_face` stream.

### Allowed APIs (use only these)

| API | Location | Use |
|-----|----------|-----|
| `MorningWeatherBehavior.tick(ctx)` | `behaviors/morning_weather.py` | Propose `intent_weather_extend`; commit only in `on_speak_allowed` |
| `WEATHER_INTENT = "intent_weather_extend"` | same | Only intent chipper will fire |
| `_identified_primary(face, rec)` | `behaviors/workday.py` L104–115 | Enrolled + primary match |
| Joke known-audience block | `behaviors/joke_idle.py` L258–277 | Copy identity juncture shape |
| `SpeechArbiter.allow` / `record_speech` | `behaviors/arbiter.py` L17–53 | Same manners as full tick |
| `SpeechRequest(text, priority, behavior_id, reason)` | `behaviors/types.py` | `text` = intent string when speak empty (`runtime.py` L370–408) |
| `BehaviorRuntime.tick` arbiter loop | `runtime.py` L338–454 | Copy; filter plugins (new `only_ids`) |
| `soul.note_face` / `soul.note_person` | `routes/face.py` L44–63 | Keep as the presence write; do **not** add `ingest_tick_payload` |
| `ContinuityStore.weather_commit_fired` | `behaviors/continuity.py` | Only from `on_speak_allowed` |
| `ContinuityStore.load_workday` | same | Read `WORKING`; do not save from weather |
| `FireWeatherIntent(esn)` | `behavior_tick.go` L270–326 | Only Go delivery |
| `InjectCloudIntentWebViz` | `ttr/webviz_intent.go` | Already used by `FireWeatherIntent` |
| `notifyFaceSeen` | `sensor_reactions.go` L261–285 | Extend: pass `esn`, decode `{intent}` |
| `behaviorProbeAnyFace` | `behavior_tick.go` L145–167 | After enrolled hit, call `notifyFaceSeen` |
| `FaceSeenRequest` | `routes/models.py` | Still only `face_id` + optional `name` |
| Direct handler tests | `test_ambient_presence.py` L952–978; `test_morning_weather.py` L42–123, L226–320 | Copy helpers; do not invent TestClient POST as the primary style |

### Anti-patterns (do not)

- Call full `BehaviorRuntime.tick()` from `routes/face.py` (runs Work Day arm/poke/late-check + joke idle).
- Call `ingest_tick_payload` / synthesize `occupied=true` from `face_seen` (re-arm sticky after ambient empty×2).
- Run `WorkDayBehavior.tick` / `plan` / `_tick_waiting_morning` on this path (morning arm is **not** speech-gated).
- Restore continuous `robot_observed_face` on the long state stream (`stream.go`, `kgsim_interrupt_test.go`).
- Deliver weather via `AppIntent`, `/triggerIntent`, `/api-sdk/cloud_intent`, or Python TTS/`ambientReact`.
- Invent `FireWeatherIntent` in Python or HTTP from vector-ai → chipper (chipper is always the client).
- Commit `weather_commit_fired` unless the arbiter allowed (and chipper will see `intent` in the HTTP response).
- Treat `identity_fresh` (default **1800s**) as “just now.”
- `MarkVoiceActivity` on the idle ProbeFace path.
- `ambientReact` / honor `speak` from `notifyFaceSeen` (jokes/pokes stay on the 60s tick).
- Handle `need_identity` inside `notifyFaceSeen`.
- Change `FaceSeenRequest` to add `is_stranger` / `esn` (Python already derives stranger; `esn` stays in Go).

### Product decisions (binding)

| # | Decision |
|---|----------|
| **D1** | Restricted tick allowlist is **only** `morning_weather`. Occupancy ingest, Work Day presence FSM, and joke idle stay on `POST /v1/behaviors/tick`. |
| **D2** | Do **not** `ingest_tick_payload` from `face_seen`. Presence write stays `note_face` + `note_person` (`source="face_seen"`). |
| **D3** | Do **not** `clock_tick` on the restricted path (`service.py` and the 60s tick already do). |
| **D4** | Weather still requires `WorkdayMode.WORKING`. Restricted tick **cannot** arm the day. If not yet `WORKING`, skip (no intent). |
| **D5** | Enrolled gate (both 60s tick and restricted): copy `_identified_primary` — named, `face_id>0`, not stranger, match Work Day primary when set. Stranger `face_seen` does not tick weather. |
| **D6** | Recent-face gate: new knob `MORNING_WEATHER_FACE_MAX_AGE_S` default **90**. Propose only if `now - presence.face_ts` is within that window. Restricted path is ~0s after the write. 60s tick is a fallback if the same probe also attaches `face` on tick. |
| **D7** | Restricted path **skips occupancy-session dwell** (`min_dwell_s`). The enrolled POST is the event. Keep `voice_recent` skip + arbiter (`quiet`, `recent_voice`, `min_gap`). 60s tick **keeps** `min_dwell_s`. |
| **D8** | `notifyFaceSeen(esn, faceID, name)`: decode JSON like `behaviorTickOnce`; if `intent=="intent_weather_extend"` call `FireWeatherIntent(esn)`. Ignore `speak`. Do not probe on `need_identity`. |
| **D9** | Idle enrolled sighting: after `behaviorProbeAnyFace` sees `fid>0` and non-empty name, call `notifyFaceSeen` **before** `askVectorAIBehaviorTick`. Weather may play, daily lock commits, then the full tick runs Work Day without a second weather. |
| **D10** | Keep `ObserveFaceBriefly` → `notifyFaceSeen`. Voice-start still `MarkVoiceActivity` first; arbiter `recent_voice` denies. Do not special-case skip voice suppress. |
| **D11** | Commit on arbiter allow **before** returning JSON, same contract as `behaviors_tick`. No new chipper ack API. If chipper then fails to inject, the day is burned (same as today’s 60s path). |
| **D12** | `POST /v1/state/face_seen` response becomes `{ok, is_stranger, intent}` (`intent` `""` when skip). Do **not** return `speak`. Update `EXPECTED_ROUTES` only if the path/method changes (it should not). |
| **D13** | No continuous face stream. First idle log line is still bounded by sparse ProbeFace (**≤2 min**) or a `need_identity` probe. “Within seconds” means **of that log line**, not of walking into view. Denser morning glances are a follow-up. |
| **D14** | Speech-gated: denied restricted tick does **not** consume the daily lock. |

### Confidence

High on shipped APIs and the “don’t full-tick from `face_seen`” rule. Restricted tick and idle `notifyFaceSeen` are **new**. Gap: no Go tests for weather inject; Phase 3 verification is compile + a pure decode helper test if extracted, plus Python HTTP-handler tests.

---

## Phase 1 — Enrolled + recent-face gate on `MorningWeatherBehavior`

**What to implement**

1. Add `face_max_age_s: int = 90` to `MorningWeatherConfig` / `load_morning_weather_config` via `MORNING_WEATHER_FACE_MAX_AGE_S` (copy `_int` usage from `load_morning_weather_config` in `behaviors/config.py` L245–276).
2. In `MorningWeatherBehavior.tick`, after occupied / before or after voice_recent:
   - If `not ctx.identity_fresh` or no face: skip with a new reason constant (e.g. `"need enrolled face"`). Do **not** set `need_identity` on this plugin (Work Day already requests probes at morning arm; D13 uses existing sparse/need_identity ProbeFace).
   - If not `_identified_primary(face, rec)`: skip `"stranger or non-primary"`.
   - If `(ctx.now - face_ts) > cfg.face_max_age_s`: skip `"enrolled face too old"`.
3. Keep occupancy-session dwell on this `tick()` (60s-tick path). Restricted path (Phase 2) will pass a flag **or** a dedicated entry — prefer a `tick()` kw-only `skip_dwell: bool = False` used only by the allowlist runtime so there is one gate function.
4. Ops snapshot / `status()`: include `face_age_s`, enrolled/primary, new reasons so `GET /v1/behaviors/morning_weather` stays honest.

**Copy from**

- Config load: `behaviors/config.py` `MorningWeatherConfig` + `load_morning_weather_config`
- Identity: `joke_idle.py` L258–277; primary: `workday.py` `_identified_primary` L104–115
- Skip/propose shape: `morning_weather.py` L130–186
- Tests: `test_morning_weather.py` `_wx_cfg`, `_arm_working`, `_ctx`, `_apply`; extend `_ctx` with optional `face` / `identity_fresh` / `face_ts` like `test_joke_idle.py` `_joke_ctx` L86–121

**Verification**

- [ ] `python3 -m pytest shared/vector-ai/test_morning_weather.py -q` from VectorIntelligence root
- [ ] Occupied + WORKING + dwell, **no** face / stranger / wrong primary / `face_ts` 200s old → no intent
- [ ] Occupied + WORKING + dwell + Cam primary + `face_ts` now → intent, commit only after `_apply` / arbiter allow
- [ ] `test_runtime_arbiter_deny_does_not_commit` still holds
- [ ] Default `load_morning_weather_config({})` still `enabled=False`; new knob defaults to 90

**Anti-patterns**

- Do not use `FACE_CACHE_MAX_AGE_S` (1800) as the weather face window.
- Do not `joke_mark_reject`-style consume the daily weather lock on stranger.
- Do not set `need_identity` from morning weather (would steal the next probe cadence from Work Day without a chipper change on this phase).

---

## Phase 2 — Restricted tick API + enrolled `face_seen` hook (Python)

**What to implement**

1. Add `BehaviorRuntime.tick(self, now, *, only_ids: Optional[set[str]] = None)` (or an equivalent keyword on the existing method — **do not** add a second arbiter implementation).
   - If `only_ids` is set: **skip** `clock_tick`; iterate `self.behaviors` but only those with `b.id in only_ids` and `b.enabled()`; same `SpeechRequest` / `arbiter.allow` / `on_speak_allowed` / `record_speech` as L403–444.
   - If `only_ids == {MORNING_WEATHER_ID}`: call weather `tick(..., skip_dwell=True)` (D7).
   - Full tick (`only_ids is None`) unchanged: `clock_tick` + all plugins + dwell on.
2. `state_face_seen` **after** the existing soul/presence write:
   - If `is_stranger`: return `{ok: True, is_stranger: True, intent: ""}` (no runtime tick).
   - Else if `deps.BEHAVIOR_RUNTIME` is set: `result = rt.tick(now, only_ids={MORNING_WEATHER_ID})`; return `{ok: True, is_stranger: False, intent: (result.intent or "")}`.
   - Never put `speak` on this response. Never call `ingest_tick_payload`.
3. If weather is not registered, `only_ids` loop is empty → `intent ""` (safe).
4. Tests (direct handlers + runtime), copy styles from listed files:
   - Enrolled Cam + WORKING + recent face → `state_face_seen` returns `intent == intent_weather_extend`; daily committed; **workday mode unchanged** if started in `WAITING_MORNING` (proves D4/D1).
   - Same but `WAITING_MORNING` → `intent == ""`, workday still `waiting_morning`.
   - Stranger `face_id<=0` or empty name → no tick / `intent ""`.
   - Arbiter `record_speech(now-10)` + `SPEECH_MIN_GAP_S=90` → `intent ""`, daily 0.
   - After restricted commit, full `rt.tick(now)` does not fire weather again and does not need to arm.
   - Monkeypatch or spy: `WorkDayBehavior.tick` / `joke` not called when `only_ids` is weather-only.
   - Occupancy: `test_face_seen_http_notes_person` still `last_source=="face_seen"`; add assert that restricted path does not call `ingest_tick_payload`.

**Copy from**

- Arbiter loop: `runtime.py` L315–454
- Face handler write + return: `routes/face.py` L25–99
- HTTP face_seen test: `test_ambient_presence.py` L952–978 (`asyncio.run(state_face_seen(FaceSeenRequest(...)))` + `deps.SOUL` / `BEHAVIOR_RUNTIME` monkeypatch)
- Runtime allow/deny weather: `test_morning_weather.py` L264–320
- Route freeze: `test_service_modules.py` `EXPECTED_ROUTES` — path/method unchanged; GET cards still work

**Verification**

- [ ] `python3 -m pytest shared/vector-ai/test_morning_weather.py shared/vector-ai/test_ambient_presence.py shared/vector-ai/test_behaviors.py shared/vector-ai/test_service_modules.py -q`
- [ ] `rg "ingest_tick_payload" shared/vector-ai/routes/face.py` → no matches
- [ ] `rg "only_ids" shared/vector-ai/behaviors/runtime.py` → filter present; default path still iterates all `self.behaviors`
- [ ] Restricted tick with workday+weather registered never changes `WorkdayRecord.mode` in the WAITING_MORNING test

**Anti-patterns**

- Do not `return deps.BEHAVIOR_RUNTIME.tick(now)` without `only_ids`.
- Do not return `speak` from `face_seen`.
- Do not notify soul `notify_speech` for weather intent (chipper fires firmware; `behaviors_http` only notifies on `result.speak`).
- Do not add a new HTTP path; extend the existing POST body **response** only.

---

## Phase 3 — Chipper: idle `face_seen` + honor `{intent}`

**What to implement** (monorepo `wire-pod/`, not archived patches)

1. Change `notifyFaceSeen` to `func notifyFaceSeen(esn string, faceID int32, name string)` in `sensor_reactions.go`.
   - Keep 5s per `"<id>:<name>"` limiter and 3s HTTP timeout.
   - `json.NewDecoder(resp.Body).Decode` into the **same shape** as `behaviorTickResponse` (`Speak`, `NeedIdentity`, `Intent`) — or a tiny struct with `Intent string` only. Copy decode from `askVectorAIBehaviorTick` L126–134.
   - If `strings.TrimSpace(result.Intent) == "intent_weather_extend"`: `MarkVoiceActivity()` + `lastAmbientReaction` like `behaviorTickOnce` L233–239, then `FireWeatherIntent(esn)`.
   - Ignore `Speak` and `NeedIdentity`.
   - Log decode errors; still `resp.Body.Close()`.
2. Update the only existing caller: `ObserveFaceBriefly` L40 → `notifyFaceSeen(esn, faceID, name)` (`esn` is already the function arg).
3. In `behaviorTickOnce`, after building enrolled `facePayload` (`fid>0` && `fname!=""`) and **before** `askVectorAIBehaviorTick`: `notifyFaceSeen(esn, fid, fname)`. Stranger / no-face: do not notify for weather (presence on tick still works as today).
4. Do **not** call `MarkVoiceActivity` for the idle ProbeFace itself; only if weather intent is actually fired (step 1).
5. Optional small extract: shared `type vectorAITickResult struct { Speak, Intent string; NeedIdentity bool }` used by both POST clients — only if it avoids copy-paste without a new package.

**Copy from**

- Decode + fire: `behavior_tick.go` L102–135 and L228–241
- `FireWeatherIntent`: same file L270–326 (already WebViz; do not reintroduce AppIntent)
- Probe enrolled branch: `behavior_tick.go` L196–203
- `notifyFaceSeen` POST: `sensor_reactions.go` L261–285
- Anti-continuous-face: `robotsession/stream.go` comments L62, L182; `kgsim_interrupt_test.go` `TestInterruptSourceHasNoRobotObservedFace`

**Verification**

- [ ] Chipper package compiles (`go test` / build the ttr package as this repo already does for other ttr tests)
- [ ] `rg 'func notifyFaceSeen' -n` → signature includes `esn string`
- [ ] `rg 'notifyFaceSeen\\(' wire-pod/chipper` → `ObserveFaceBriefly` and `behaviorTickOnce` only
- [ ] `rg 'AppIntent' wire-pod/chipper/pkg/wirepod/ttr/behavior_tick.go` → still no weather AppIntent
- [ ] `rg 'robot_observed_face' wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go` → still “never continuous”
- [ ] If a decode helper is extracted: table test JSON `{"ok":true,"is_stranger":false,"intent":"intent_weather_extend"}` → fire; `intent:""` → no fire; extra `speak` ignored
- [ ] Idle path: enrolled ProbeFace → POST face_seen → (Python) intent → `FireWeatherIntent`; then tick HTTP still runs for Work Day

**Anti-patterns**

- Do not `ambientReact` from `notifyFaceSeen`.
- Do not open `robot_observed_face` on `StartStateStream`.
- Do not skip the full 60s tick after a successful weather fire (Work Day still needs that cycle).
- Do not POST `face_seen` for strangers from the sparse probe (avoids restricted-tick no-ops + extra HTTP). Voice-start may still notify strangers (identity cache); Python skips tick.

---

## Phase 4 — Docs

**What to implement**

1. `docs/FSM-morning-weather.md`: add the enrolled-face trigger; `MORNING_WEATHER_FACE_MAX_AGE_S`; restricted path skips dwell; Work Day still 60s; delivery still `FireWeatherIntent` via WebViz; first idle sighting ≤ sparse 2 min then seconds to anim.
2. `docs/FSM-implementation.md` §3.2: document the **narrow exception** — enrolled `POST /v1/state/face_seen` may run `tick(only_ids={morning_weather})` and return `intent`; still not a general speak engine; GETs still ops-only.
3. `docs/ENDPOINTS.md` `POST /v1/state/face_seen`: response `{ok, is_stranger, intent}`; enrolled may fire weather intent; does not arm Work Day.
4. `docs/FAQ.md` replace the “not implemented” restricted-tick answers with the shipped behavior (D1–D14 in one short Q&A). Keep “do not full-tick Work Day from face_seen”.
5. Parent plan 2026-08-29: note identity gate + restricted tick shipped (link this file).
6. Mark **this** plan status implemented when Phases 1–3 + tests are green (leave a live-robot checkbox).

**Copy from**

- Owner-guide table style: `FSM-morning-weather.md` knobs table
- FAQ Q&A style: `FAQ.md` morning weather sections
- Fireweather docs phase: `2026-09-02-fireweather-webviz-cloud-intent.md` Phase 3

**Verification**

- [x] Docs do not claim AppIntent
- [x] Docs do not claim continuous face stream
- [x] Docs state weather cannot arm Work Day
- [x] Ops curls still `GET /v1/behaviors/morning_weather` and `GET /v1/behaviors/state`

**Anti-patterns**

- Do not document a Python weather-fire API.
- Do not leave FAQ saying restricted tick is design-only after ship.

---

## Phase 5 — Verification (final)

**What to implement**

Run and record:

```bash
# VectorIntelligence repo root
python3 -m pytest shared/vector-ai/test_morning_weather.py \
  shared/vector-ai/test_joke_idle.py \
  shared/vector-ai/test_behaviors.py \
  shared/vector-ai/test_ambient_presence.py \
  shared/vector-ai/test_service_modules.py \
  shared/vector-ai/test_soul_greeting.py -q
```

Chipper: compile ttr; existing `go test` in `chipper/pkg/wirepod/ttr/` (interrupt / volume tests) still pass.

**Grep guards**

- [ ] `rg "ingest_tick_payload" shared/vector-ai/routes/face.py` empty
- [ ] `rg "only_ids" shared/vector-ai/behaviors/runtime.py` present
- [ ] `rg "AppIntent" wire-pod/chipper/pkg/wirepod/ttr/behavior_tick.go` no weather AppIntent
- [ ] `rg "func notifyFaceSeen\\(esn" wire-pod/chipper/pkg/wirepod/ttr/sensor_reactions.go` matches
- [ ] Morning weather still speech-gated: deny does not `weather_commit_fired`

**Live robot (optional, after deploy)**

- [ ] Work Day already `WORKING`, morning window, look at Vector until sparse/need_identity ProbeFace: log `[face] observed: id=… 'Cam' (enrolled)` then weather anim within seconds, **without** a Work Day poke on that HTTP.
- [ ] `GET /v1/behaviors/workday` mode still `working` (not re-armed from face_seen).
- [ ] “Hey Vector” face probe does **not** talk over the user (arbiter `recent_voice`).
- [ ] Second enrolled sighting same morning: no second weather.

**Anti-patterns**

- Do not claim done without the pytest command output.
- Do not skip the WAITING_MORNING “does not arm” test because a live robot “looked fine.”

---

## Execution note

Two trees in one workspace:

1. Phases 1–2, 4 (docs), Python tests → `VectorIntelligence/`
2. Phase 3 → `wire-pod/chipper/` (in-tree Go; restart chipper after rebuild)

Deploy both or idle `face_seen` never happens and/or `{intent}` is discarded again.

**Follow-up (not this plan):** shorter morning ProbeFace interval if 2 min to first log line feels slow. That is a glance-cadence change, not the restricted-tick contract.

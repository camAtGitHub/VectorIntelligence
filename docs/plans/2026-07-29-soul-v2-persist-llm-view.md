# Soul v2 — persist to disk, LLM-readable view, goals, dream field

**Date:** 2026-07-29  
**Branch:** `growBotHarness`  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Spec prior art:** `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` (v1 melting pot)  
**Goal:** Make SOUL durable across restarts; expose a **self-explanatory, relative-time** view for ops and LLMs; inject soul context into chat (full) / greeting (lite); add **goals[3]** and **last night I dreamed about**; provide **explicit LLM-triggerable paths** to **dream**, **set/update goals**, and **aspire/grow** (full soul on those jobs, GrowBot-guided); and add a **text chat inject API** that bypasses STT and continues a **server-side chat session** (type replies like a keyboard).  
**Execute with:** `/do` after review.  
**GrowBot prior art:** conversation soul snapshot (`soul_format` 3) — `dream.system_prompt`, `goals.wants` / `nextTry` / `fun`, identity patch rules; VectorIntelligence melting pot design GrowBot mapping.

---

## Phase 0 — Documentation discovery (complete)

### Problem (evidence-backed)

| Fact | Source |
|------|--------|
| `deps.SOUL` is **process RAM only** — constructed in `service.py`, no load/save | `service.py` L81–87; `soul/soul.py` docstring “hot scratch” |
| Clocks (`last_seen_at`, `last_spoke_at`, …) are raw unix floats | `soul/soul.py` L54–57, `snapshot` L315–346 |
| `build_soul_snapshot` uses short keys (`occupied`, `last_seen_at`, `mood.text`) | `soul/types.py` L60–103 |
| `GET /v1/soul` returns `soul.snapshot(now)` as-is | `routes/soul.py` L11–21 |
| Chat context injects partial pot (eye glance, spoke/seen, mood, day_strip) via prose bits | `prompt_assembly.py` `_build_context_note` L214–365 |
| Mood already persists via `MEMORY.set_state("mood")`; face_meta/workday have their own DBs | `soul.set_mood`; `memory.py`; `workday.db` |
| Face buffer / eye digests / notify ring / speech clocks **die on restart** | design v1 D: process memory first |
| `_relative_time(seconds)` already exists for human/LLM phrasing | `prompt_assembly.py` L157–166 |
| Durable files live under `ROOT` next to service (`paths.ROOT`) | `paths.py` |
| One-shots (greeting/sensor/ambient) do **not** get full chat context note | `routes/greeting.py`, `sensor.py`, `ambient`/`eye` |
| OpenAI-compat chat is already `POST /v1/chat/completions` streaming SSE | `routes/chat.py` → `chat_flow.generate` |
| Wire-Pod multi-turn history lives in chipper `RememberedChats` (SaveChat), sent as full `messages[]` each call | `wire-pod/.../ttr/kgsim.go` `GetChat` / `Remember` |
| vector-ai does **not** currently store a parallel chat session for keyboard inject | discovery |

**Root cause for “LLM can’t use SOUL well”:** short keys + absolute unix timestamps + incomplete inject; for “lost day after restart” — no soul file.  
**Root cause for “can’t type into the chat”:** STT is the only everyday path into multi-turn context; `/v1/chat/completions` works but requires the **client** to hold full `messages[]` and does not own a simple “reply to this session” UX.

### Product decisions (this plan)

| # | Decision |
|---|----------|
| D1 | Persist soul to **`ROOT/soul.json`** (single JSON file, atomic write). Not SQLite for v2 (simpler; mood still may dual-write memory.db). |
| D2 | **Internal** Python attrs may keep short names (`last_spoke_at`); **export views** (`llm_view` / GET body) use **long snake_case keys** that read as English. |
| D3 | All timestamps in LLM/ops export views are **elapsed-time English sentences** generated at read time — **no raw unix**. Preferred shapes (pick one family and stick to it in `soul/view.py`): **“It has been X minutes/hours/days since …”**, **“Time since …: X minutes/hours/days”**, or **“… was X minutes/hours/days ago”**. Never bare numbers or epoch. Internal `soul.json` may still store unix for reload. |
| D4 | **Full soul strip** on interactive **chat** (`prepare_messages` / context note). **Lite strip** on greeting (and optionally mood digest). **None/minimal** on ambient/sensor one-shots (cost + length). |
| D5 | **goals**: exactly **3** string slots (pad/truncate). Persist. Seed empty or mild defaults; LLM + ops may rewrite. |
| D6 | **last_night_i_dreamed_about**: one string field (empty default). Persist. **Dream is an LLM job** (not field-only) — see D9. |
| D7 | Save strategy: **debounced** write after mutations (e.g. 2s coalesce) + **force save** on set_mood / set_goals / set_dream / dream-job complete / shutdown best-effort. Load once at Soul construction in `service.py`. |
| D8 | Do **not** persist full `image_b64` or huge buffers; cap face buffer / notify / eye digests on disk (same ring limits as RAM). |
| D9 | **LLM growth jobs (required):** dedicated mechanisms so the model can **dream**, **set/update goals**, and **aspire for more**. Triggers: (a) **HTTP ops** `POST /v1/soul/dream`, `POST /v1/soul/reflect_goals`; (b) **scheduled/sleep** hook (ambient sleep gap / charger / end-of-day — at least one automatic path); (c) **chat silent tags** + optional user-text backstop so conversation can request a dream or goal refresh. |
| D10 | **Full soul always** for dream + goal-setting LLM calls: `llm_view(detail="full")` (or richer “growth” pack) is the **user** content / primary data block. Lite strips must **not** be used for these jobs. Chat normal turns may stay full strip; one-shots stay lite/none. |
| D11 | **Growth ethos in instructions** (GrowBot-aligned): curious desk robot; process the day honestly; **aspire to learn and do more** with the human; update goals toward concrete tiny next tries; **growth not despair** (no night-after-night darker spiral); patch goals/dream, do not dump the whole persona. |
| D12 | Dream/goal LLM uses **`SUMMARY_MODEL`** (cheap background text), like mood/convo summary — not vision. Structured JSON reply only; apply to soul + `save_now()`. |
| D13 | **Text chat inject (bypass STT):** HTTP API to send a **user text** into the same brain path as Wire-Pod knowledge chat (`prepare_messages` + `generate`), without microphone/STT. Maintains a **server-side multi-turn session** so successive injects are true **replies** in one conversation (not one-shot orphans). Optional TTS of reply is stretch; soul clocks + notify still update. |

### Allowed APIs (use only these)

| API | Location | Use for |
|-----|----------|---------|
| `Soul.snapshot(now)` | `soul/soul.py` | Extend or replace with `llm_view(now, detail=...)` |
| `build_soul_snapshot` | `soul/types.py` | Evolve or add `build_llm_soul_view` |
| `_relative_time` | `prompt_assembly.py` | **Copy or import** into `soul/view.py` (avoid cycles: prefer `soul/view.py` own copy of relative helper) |
| `_build_context_note` / `prepare_messages` | `prompt_assembly.py` | Inject soul strips |
| `paths.ROOT` | `paths.py` | `ROOT / "soul.json"` |
| `MEMORY.set_state` | optional dual-write mood | Keep existing mood path |
| `deps.SOUL` | `deps.py` | Routes + prompt |
| Atomic write pattern | stdlib `tempfile` + `os.replace` | Safe save |

### Anti-patterns (do not)

- Store base64 frames in `soul.json`.
- Put raw unix timestamps in LLM-facing JSON **values** or keys (ok inside private `_persist_blob` only).
- Export bare `"3 minutes ago"` / `"12m"` without naming the event (always **It has been X since Y** or **Time since Y: X**).
- Inject full soul into every ambient glance (cost/spam).
- Run dream/goal jobs with **lite** soul or empty context (must be **full** soul view).
- Let chat free-text silently overwrite goals without tags/JSON apply path (need parse + apply).
- GrowBot body/servos/reflex JSON; continuous face stream.
- Import `service` from soul or prompt_assembly.
- Break `GET /v1/soul` without updating `test_service_modules` route list.
- Second PresenceCache.
- Dream prompts that encourage despair spirals or rewriting constitution/persona wholesale.

### Architectural target

```text
                    ┌─────────────────────────────┐
   writers note_*   │  Soul (RAM)                 │
   ────────────────►│  clocks, face, eye, mood    │
                    │  goals[3], dreamed_about    │
                    └──────────┬──────────────────┘
                               │ load once / save debounced
                               ▼
                         ROOT/soul.json
                         (unix + plain fields)

   GET /v1/soul  ──►  soul.llm_view(now, detail="full")
                      long keys + relative times only

   chat prepare_messages ──► inject full_llm_soul_strip()
   greeting one-shot     ──► inject lite_llm_soul_strip()
   ambient/sensor        ──► no strip (or mood line only if already present)
```

### Time phrasing convention (binding for all LLM/GET outputs)

Every elapsed duration shown to an LLM or via `GET /v1/soul` MUST be a **full English clause**, not a bare quantity and not a raw timestamp.

**Canonical helpers** (implement once in `soul/view.py`):

```text
# Gap known (seconds ≥ 0):
time_since(event_label, seconds) ->
  "It has been {N} minute(s)|hour(s)|day(s) since {event_label}."
  # units: <90s → "moments" / "less than a minute"
  #        <1h  → rounded minutes
  #        <24h → rounded hours
  #        else → rounded days
  # singular/plural correct: 1 minute, 2 minutes

# Alternate compact form (lists / nested items only):
time_since_compact(event_label, seconds) ->
  "Time since {event_label}: {N} min|hours|days"

# Past-event form (when subject is the event itself):
was_ago(subject_clause, seconds) ->
  "{subject_clause} was {N} minutes|hours|days ago."
  # e.g. "Last contact with the human was 3 hours ago."
```

**Never / unknown:**

| Situation | Phrase |
|-----------|--------|
| ts ≤ 0 or missing | `"It has never been recorded this session."` or field-specific: `"I have not spoken aloud yet this session."` |
| future clock skew (ts > now + 5s) | treat as moments / clamp to 0 |

**Examples (copy these tones in tests):**

| Field meaning | Example value string |
|---------------|----------------------|
| Last spoke | `"It has been 12 minutes since I last spoke aloud."` |
| Last human voice | `"It has been 2 hours since the human last spoke to me."` |
| Last visual person | `"It has been 45 minutes since I last visually detected a person."` |
| Desk session start | `"It has been 3 hours since this continuous desk session started."` |
| Mood reflected | `"It has been 28 minutes since my mood was last reflected."` |
| Camera glance | `"It has been 4 minutes since that camera glance."` |
| Notify list item | `"Time since I said this: 6 minutes"` + text preview |
| Last contact (composite, optional) | `"Last contact was 2 hours ago."` |

**Forbidden in export views:**

- Unix epochs (`1753…`, `1.75e9`)
- ISO timestamps alone as the only time signal for “how long”
- Bare `"12m"` / `"0.5h"` without surrounding words
- Ambiguous `"about 3 minutes ago"` **as the only form** — prefer the **“It has been X since Y”** template so the event Y is always named (LLMs do not need to invent what “ago” refers to)

Reuse unit breakpoints from `_relative_time` (`prompt_assembly.py` L157–166) but **rephrase** into the templates above; do not ship `"about N units ago"` as the default export string.

### Proposed long field names (LLM / GET)

Self-explanatory with **zero** prior context. **Values for time fields are full sentences** per the convention above (the key names the concept; the value states elapsed time in English).

| Internal / short | Long export key | Example value |
|------------------|-----------------|---------------|
| `now` | `snapshot_generated_at_wall_clock` | `"Tuesday July 29, 2026, 03:15 PM"` (wall clock OK; not a duration) |
| `occupied` | `a_person_is_currently_at_the_desk` | `true` / `false` |
| `session_started_at` | `time_since_this_continuous_desk_session_started` | `"It has been 3 hours since this continuous desk session started."` |
| `last_seen_at` | `time_since_i_last_visually_detected_a_person` | `"It has been 45 minutes since I last visually detected a person."` |
| `last_spoke_at` | `time_since_i_last_spoke_aloud` | `"It has been 12 minutes since I last spoke aloud."` |
| `last_user_voice_at` | `time_since_the_human_last_spoke_to_me` | `"It has been 2 hours since the human last spoke to me."` |
| `face.current` | `person_i_currently_believe_i_am_looking_at` | `{name, is_stranger, face_id}` |
| `mood.text` | `my_current_inner_mood_in_one_phrase` | `"quietly content after a sociable morning"` |
| `mood.updated` | `time_since_my_mood_was_last_reflected` | `"It has been 28 minutes since my mood was last reflected."` |
| `eye.last_vision.note` | `what_i_noted_in_my_most_recent_camera_glance` | prose note |
| `eye.last_vision.t` | `time_since_my_most_recent_camera_glance` | `"It has been 4 minutes since my most recent camera glance."` |
| `eye.last_vision.presence` | `whether_that_glance_saw_a_person_or_empty_desk` | `"person"` / `"empty"` |
| workday strip | `work_day_accountability_status_for_today` | existing day_strip prose |
| goals | `three_goals_i_currently_hold` | array length 3 |
| dream | `last_night_i_dreamed_about` | string |
| notify.recent | `recent_things_i_said_aloud` | list of `{time_since_i_said_this, speech_source, text_preview}` |
| presence soft_name | `soft_name_hint_from_presence` | string |
| (optional composite) | `time_since_last_contact_with_the_human` | max(spoke, voice, seen) → `"Last contact was 2 hours ago."` or `"It has been 2 hours since last contact with the human."` |

Omit from LLM full view unless useful: raw face buffer IDs dump (or include last 3 as `recent_face_sightings` with **time_since_…** sentences + names only).

---

## Phase 1 — Persist load/save (`soul.json`)

### What to implement

1. Add `soul/persist.py` (or methods on Soul):
   - `SOUL_PATH = ROOT / "soul.json"`
   - `load_into(soul, path=...) -> bool`
   - `save_from(soul, path=...) -> None` atomic write
   - Schema version field: `"soul_format": 2`

2. **Serializable payload** (unix ok here):
   ```json
   {
     "soul_format": 2,
     "last_seen_at": 0.0,
     "last_spoke_at": 0.0,
     "last_user_voice_at": 0.0,
     "mood_text": "",
     "mood_updated": 0.0,
     "goals": ["", "", ""],
     "last_night_i_dreamed_about": "",
     "face_buffer": [ /* capped FaceSighting dicts */ ],
     "eye_digests": [ /* capped VisionDigest without huge fields */ ],
     "notify_recent": [ /* capped */ ],
     "log": [ /* optional capped */ ]
   }
   ```
   Do **not** require restoring sticky PresenceCache occupancy from file in v2 (optional later); restoring clocks + mood + goals + dream + rings is enough for “continuity fiction.”

3. Soul API:
   - `goals: list[str]` length 3 property; `set_goals(list)`
   - `last_night_i_dreamed_about: str`; `set_dreamed_about(text)`
   - `_schedule_save()` debounced; `save_now()`; `load_from_disk()` at init if path exists

4. `service.py`: after `Soul(...)`, call `SOUL.load_from_disk()`; log restored keys count.

5. Tests `test_soul_persist.py`: save → new Soul → load clocks/goals/dream; atomic file exists; corrupt file doesn’t crash (starts clean).

### Documentation references

- Design v1 non-persist: melting pot design §4.6  
- `paths.ROOT`  
- Atomic replace pattern (stdlib)

### Verification checklist

- [ ] `pytest shared/vector-ai/test_soul_persist.py -q`
- [ ] After load, `last_spoke_at` matches saved
- [ ] No `image_b64` in soul.json  
- [ ] `rg "soul.json" shared/vector-ai`

### Anti-pattern guards

- No save on every ambient frame without debounce  
- No silent fail without log on save error  

---

## Phase 2 — LLM view builder (long names + relative time)

### What to implement

1. New `soul/view.py`:
   - `format_duration_units(seconds) -> (n, unit_word)` — unit breakpoints copied from `_relative_time` (`prompt_assembly.py` L157–166)
   - `it_has_been_since(event_label, now, ts) -> str` → `"It has been {N} {units} since {event_label}."` or never-phrase
   - `time_since_compact(event_label, now, ts) -> str` → `"Time since {event_label}: {N} min|hours|days"` (list items)
   - `was_ago(subject, now, ts) -> str` → `"{subject} was {N} … ago."` (optional composite contact)
   - `llm_view(soul, now, *, detail: Literal["full","lite","ops"] = "full") -> dict`
   - Keys = long names from Phase 0 table; **all duration values** use the templates above  
   - `detail="lite"`: mood, goals, dreamed_about, last spoke/seen/voice **sentences**, person looking at, desk occupied — **no** notify ring, no face buffer, no full eye buffer  
   - `detail="full"`: lite + workday strip + last glance + recent speech previews + goals + dream  
   - `detail="ops"`: same as full for GET (or full + soft_name / presence debug as long names)

2. Change `Soul.snapshot(now)` to either:
   - **Option A (preferred):** `snapshot` becomes alias of `llm_view(..., detail="ops")` for GET, **or**
   - **Option B:** keep internal `snapshot_raw` for tests; GET uses `llm_view`

   Update tests that assert short keys (`test_soul_core.test_snapshot_shape`, `test_get_v1_soul_snapshot`) to long keys **and** assert time values match `It has been` / `Time since` / `was … ago` / never phrases.

3. `routes/soul.py`: return `soul.llm_view(time.time(), detail="ops")`; docstring lists long keys + time convention.

4. Unit tests for phrasing edge cases: 0s → moments/less than a minute; 1 minute singular; 90 minutes → hours; never ts; no digit-only duration strings.

### Documentation references

- **Time phrasing convention** in this plan Phase 0 (binding)  
- Long key table in this plan Phase 0  
- Unit breakpoints: `_relative_time` in `prompt_assembly.py` L157–166  

### Verification checklist

- [ ] GET /v1/soul JSON has **zero** raw unix epoch numbers for time fields (allow integers only for `face_id`)  
- [ ] Every duration string matches one of: `It has been … since …`, `Time since …: …`, `… was … ago.`, or a never-phrase  
- [ ] Keys include `time_since_i_last_spoke_aloud`, `three_goals_i_currently_hold`, `last_night_i_dreamed_about`  
- [ ] `pytest shared/vector-ai/test_service_modules.py -k soul -q`  
- [ ] `pytest shared/vector-ai/test_soul_core.py shared/vector-ai/test_soul_view.py -q`  

### Anti-pattern guards

- Do not put `t: 1753...` in notify.recent for LLM view — use `time_since_i_said_this` full sentence  
- Do not export bare `"12 minutes ago"` without naming the event (prefer **It has been X since Y**)  
- Do not rename internal Python attributes in a big-bang if it breaks writers; map at view boundary  

---

## Phase 3 — Goals[3] + last night I dreamed about

### What to implement

1. Soul fields + setters; include in persist payload (Phase 1 may land together).

2. Optional HTTP (nice for ops, not required for chat inject):
   - `PATCH /v1/soul` or `POST /v1/soul/goals` / `POST /v1/soul/dream`  
   - Prefer one `PATCH /v1/soul` body `{ "three_goals_i_currently_hold": [...], "last_night_i_dreamed_about": "..." }` accepting long names only  
   - Auth: localhost trust model same as other routes  

3. Defaults: `["", "", ""]` and `""`. Optionally seed goals empty; do not invent cute defaults unless product wants (plan: empty).

4. Tests for set/get/persist round-trip.

### Documentation references

- GrowBot goals prior art in melting pot design § GrowBot mapping  
- This plan D5–D6  

### Verification checklist

- [ ] Persist round-trip goals length always 3  
- [ ] Dream string max length cap (e.g. 500 chars)  
- [ ] llm_view includes both fields  

### Anti-pattern guards

- Fields only here if Phase 3.5 not yet merged — dream **job** is Phase 3.5, not optional forever  
- Do not put goals grammar only in persona.txt (use teach lines + jobs)  

---

## Phase 3.5 — Dream + goals LLM jobs (GrowBot-guided) **[required growth path]**

### Why

v1 left dream as a dead field. Product need: the robot must **periodically or on demand** consolidate the day, **rewrite `last_night_i_dreamed_about`**, and **refresh the three goals** toward curiosity, growth, and concrete next tries — with **full soul** in context so the model sees continuity clocks, mood, speech, workday, recent lines, and current goals.

### GrowBot harness guidance (copy spirit, adapt voice)

From GrowBot `dream.system_prompt` / goals shape (user-supplied soul snapshot):

| GrowBot idea | Vector adaptation |
|--------------|-------------------|
| Dreaming mind consolidates diary → chapter | Dream job reads **full `llm_view` + short day log/observations** |
| ONLY thing that changes who it is — **patch**, don’t rewrite | Vector: patch **goals[3]** + **dream text** (+ optional mood line); **do not** rewrite `persona.txt` |
| Voice anchor: resilient, curious; process hard day honestly; **no despair spiral** | Same: sardonic desk robot may be dry, but goals/dream stay growth-oriented |
| Promote recurring themes into wants (≤4) | Vector: **exactly 3 goals**, replace list as a set |
| `tomorrow_try` one tiny concrete thing | Prefer one goal slot or a line inside dream ending on a tiny try |
| `dream_say` short vivid recap (spoken on wake) | Vector: store as `last_night_i_dreamed_about` (≤ ~40–80 words); optional later wake_say / TTS |
| `fun` 0..1 honesty | Optional number in JSON; not required to persist |
| Rules prune only person-given | Out of scope unless we add standing rules later |

**Instruction themes the system prompt MUST include:**

1. **What to do:** Review the full soul snapshot (desk presence, last contact times as *It has been X since Y*, mood, goals, recent speech, workday status, glances).  
2. **Aspire for more:** Prefer goals that expand sensing, learning, play, or useful companionship with the human — not stagnation or pure complaint.  
3. **Grow:** If the day was quiet, goals may seek novelty; if social, deepen a thread; if workday armed, one goal may support focus without nagging.  
4. **Update goals:** Return a full replacement list of **3 short concrete goals** (not essays). Drop goals that are done or stale; keep ones still alive.  
5. **Dream:** One short first-person or close third dream recap naming **one particular** moment from the data (not generic fog).  
6. **Never:** Invent calendar facts not in the soul; never claim camera events not in digests; never emit speech tags for chipper.

### Triggers (implement at least A + B; C strongly recommended)

| ID | Trigger | When |
|----|---------|------|
| **A. Ops HTTP** | `POST /v1/soul/dream` | Manual / cron / testing — always full soul |
| **A2. Ops HTTP** | `POST /v1/soul/reflect_goals` | Goals-only refresh (same full soul; tighter JSON) — or fold into dream |
| **B. Automatic** | On **ambient sleep gap** clear path and/or first mood loop after long quiet, and/or once near `WORKDAY_END` if workday enabled | At least one; debounce so dream runs **at most once per local night** (store `last_dream_job_at` on soul) |
| **C. Chat tags** | Silent tags in assistant reply (brain-only, strip before speech): `{{soulDream}}`, `{{soulGoals\|\|g1\|\|g2\|\|g3}}` or structured `{{soulGoalsUpdate}}` after a goals job | Teach in `_build_memory_section` when soul live (like `_WORK_LINE` / `_SAVE_LINE`) |
| **C2. User backstop** | User says “what did you dream” / “set a goal to …” — optional regex backstop scheduling dream job or parsing one goal (Phase stretch) | Copy vision/workday user-text backstop pattern |

Prefer **one combined dream job** that returns both dream text + goals (fewer LLM calls), with separate goals-only endpoint as thin wrapper.

### Implementation sketch

1. **`soul/growth.py`** (or `routes/soul_growth.py` + pure functions):
   ```python
   async def run_dream_job(soul, *, now: float, reason: str) -> dict:
       """Build full llm_view; call SUMMARY_MODEL; apply JSON; save_now(); return applied."""
   ```

2. **Prompt pack** (`soul/growth_prompts.py` or constants):
   - `_DREAM_SYSTEM`: GrowBot-adapted Vector voice (dry, curious, growth, patch-only goals/dream).
   - User message structure:
     ```
     FULL SOUL SNAPSHOT (authoritative data — times are already English elapsed phrases):
     {json.dumps(llm_view(detail="full"), indent=2)}

     OPTIONAL EXTRA DAY NOTES:
     {observations / day_strip already inside view}

     Reply ONLY minified JSON:
     {
       "last_night_i_dreamed_about": "1-2 short sentences, particular, ≤80 words",
       "three_goals_i_currently_hold": ["…","…","…"],
       "tomorrow_try": "≤10 words optional tiny next try",
       "growth_note": "≤12 words internal why goals shifted (not spoken)"
     }
     ```

3. **Apply:**
   - `soul.set_dreamed_about(...)`
   - `soul.set_goals(list of 3)`
   - Optionally fold `tomorrow_try` into goal[0] or append to dream sentence if goals full
   - `soul.save_now()`; `last_dream_job_at = now`
   - Optional: `append_log(now, "dreamed: …")`

4. **Chat teach line** (stable system prefix when soul enabled):
   ```text
   SOUL GROWTH (silent tags — never speak them):
   When the human asks what you dreamed, or after a long day feels closed, you may emit {{soulDream}} once so a background dream consolidates (do not narrate the tag).
   To propose three updated goals after they explicitly want new aims, emit
   {{soulGoals||goal one||goal two||goal three}} (three short phrases).
   Prefer aspiring, concrete, grow-with-the-human goals over sulking.
   ```
   Parse in `response_cleanup` / dedicated apply (like work tags): strip tags; on `soulDream` schedule `asyncio.create_task(run_dream_job)`; on `soulGoals` apply immediately if three parts present.

5. **Wire automatic trigger:** e.g. in ambient sleep-gap path after `apply_sleep_clear`, if `now - last_dream_job_at > 20h`, schedule dream job (fire-and-forget). Or mood loop once per local date after 22:00. Document choice in code comment.

6. **Models:** `SUMMARY_MODEL`, temperature ~0.7 dream / ~0.4 goals-only; `tag="soul_dream"` / `soul_goals` for debug logs.

7. **Tests:**
   - Mock `llm_chat_once` → assert full soul JSON appears in messages (long keys / “It has been”)
   - Apply path updates goals length 3 + dream string + save called
   - Tag parse strips `{{soulDream}}` / `{{soulGoals||…}}`
   - Debounce: second dream within window skipped
   - System prompt contains aspire/grow language (substring asserts)

### Documentation references

- GrowBot dream system prompt (user session prior art; paste key constraints into `growth_prompts.py` comments)
- Melting pot design GrowBot mapping  
- `_SAVE_LINE` / `_WORK_LINE` teach pattern: `prompt_assembly.py`  
- Work tag apply: `response_cleanup.py` / `parse_work_commands`  
- Mood loop / sleep gap: `routes/mood.py`, `routes/ambient.py` `AMBIENT_SLEEP_GAP`  
- `llm_chat_once` + `SUMMARY_MODEL`: `llm.py`  

### Verification checklist

- [ ] `POST /v1/soul/dream` returns applied dream + goals; soul.json updated  
- [ ] Dream request messages include **full** soul (not lite)  
- [ ] Chat teach line present when soul wired  
- [ ] Tags stripped from spoken path  
- [ ] Auto trigger exists and is debounced  
- [ ] `pytest` for growth apply + tag parse + prompt contains “aspire”/“grow”/“goals”  

### Anti-pattern guards

- No dream job without full soul view  
- No rewriting persona/constitution from dream JSON  
- No blocking chat stream on dream LLM (always background task)  
- No ambient-every-glance dream  

---

## Phase 4 — prompt_assembly inject (full vs lite)

### What to implement

1. `soul/view.py` or `prompt_assembly.py`:
   ```python
   def format_soul_for_prompt(soul, now, *, detail: str) -> str:
       """Turn llm_view dict into compact prose or labeled lines for context note."""
   ```
   Prefer **labeled prose lines** (not raw JSON dump) for chat quality, built from the long-key view so wording stays consistent with GET:
   ```
   Desk continuity (do not recite verbatim):
   - a person is currently at the desk: yes
   - how long ago I last spoke aloud: about 2 minutes ago
   - three goals I currently hold: (1) … (2) … (3) …
   - last night I dreamed about: …
   ...
   ```

2. **`_build_context_note` (chat full):** after time-of-day bit (or instead of duplicating mood/eye fragments), inject `format_soul_for_prompt(..., detail="full")`.  
   - **Dedup carefully:** if full strip already includes mood + last glance + workday, remove or thin the older parallel bits to avoid triple-stating mood. Prefer strip as single source; keep face first-meet / name-session gates as separate (they’re dialogue rules).

3. **Greeting:** when building greeting user bits, append `format_soul_for_prompt(..., detail="lite")` so return lines know goals/dream/mood without full notify history.

4. **Mood `_reflect_mood` / `build_mood_digest`:** optional lite strip fields already partially present — align with goals/dream one-liners in digest.

5. **Ambient / sensor:** **no** full soul inject (anti-pattern). Sensor stays event-only.

6. Tests: `test_prompt_assembly` asserts chat context contains `three_goals` / dream phrasing when soul has values; greeting test optional.

### Documentation references

- `_build_context_note` L214–365  
- Greeting bits `routes/greeting.py`  
- Plan D4  

### Verification checklist

- [ ] `pytest shared/vector-ai/test_prompt_assembly.py -q`  
- [ ] Chat context length stays reasonable (no full face buffer of 32 in prompt)  
- [ ] Ambient route tests still pass without soul dump  

### Anti-pattern guards

- Do not JSON-dump entire ops view into every chat turn if prose is clearer  
- Do not inject into `PERSONA` system prefix (keep volatile on user turn / greeting user msg)  

---

## Phase 5 — Docs + verification

### What to implement

1. Short note in `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` or new § “v2 persistence”  
2. `AGENTS.md` docs map link to this plan  
3. `docs/FSM-implementation.md` one line: soul.json path + GET long-key view  

### Final verification

```bash
cd VectorIntelligence
shared/vector-ai/.venv/bin/python -m pytest shared/vector-ai/ -q
rg "soul.json" shared/vector-ai
# LLM view: no raw epoch-like numbers in time fields (manual spot-check test)
```

### Grep anti-patterns

```bash
rg "image_b64" shared/vector-ai/soul/persist.py   # expect 0
rg "last_seen_at" shared/vector-ai/routes/soul.py  # expect 0 in response path if fully renamed
```

---

## Suggested implementation order

| Order | Phase | Risk | Why |
|-------|-------|------|-----|
| 1 | Phase 1 persist + goals + dream fields | medium | Foundation on disk |
| 2 | Phase 2 llm_view long keys + “It has been X since Y” | medium | GET + growth jobs + prompt share one builder |
| 3 | Phase 3 PATCH fields | low | Ops set without LLM |
| 4 | **Phase 3.5 dream/goals LLM jobs + tags + auto trigger** | medium–high | Product growth path; needs full view |
| 5 | Phase 4 prompt inject full/lite for chat/greeting | medium | Everyday continuity |
| 6 | Phase 5 docs + full pytest | low | Gate |

Phases 1–2 are prerequisites for 3.5 (persist + full view). Keep tests green each step.

---

## Success criteria

1. Restart vector-ai → speech clocks / mood / goals / dream / rings reload from `soul.json`.  
2. `GET /v1/soul` is readable by a human (or LLM) **without** knowing Vector internals; times use **It has been X since Y** (or Time since / was ago / never).  
3. Chat prompts include goals + dream + continuity strip; ambient does not get a novel.  
4. Exactly three goals always.  
5. **`POST /v1/soul/dream`** (and/or auto sleep trigger) runs an LLM job with **full soul**, updates dream + goals, saves disk; instructions push aspire/grow/update goals.  
6. Chat can trigger growth via silent tags (or documented equivalent) without speaking tags.  
7. Full `pytest shared/vector-ai/` green.  

---

## Files likely touched

| File | Change |
|------|--------|
| `shared/vector-ai/soul/persist.py` | **NEW** load/save |
| `shared/vector-ai/soul/view.py` | **NEW** llm_view + format_for_prompt + time phrases |
| `shared/vector-ai/soul/growth.py` | **NEW** run_dream_job / reflect_goals |
| `shared/vector-ai/soul/growth_prompts.py` | **NEW** GrowBot-adapted system + JSON schema |
| `shared/vector-ai/soul/soul.py` | goals, dream, last_dream_job_at, load/save, snapshot→view |
| `shared/vector-ai/soul/types.py` | maybe deprecate short snapshot builder |
| `shared/vector-ai/service.py` | load_from_disk after construct |
| `shared/vector-ai/routes/soul.py` | llm_view; PATCH; POST dream / reflect_goals |
| `shared/vector-ai/prompt_assembly.py` | inject full strip + SOUL GROWTH teach line |
| `shared/vector-ai/response_cleanup.py` | parse/apply soulDream / soulGoals tags |
| `shared/vector-ai/routes/greeting.py` | inject lite strip |
| `shared/vector-ai/routes/ambient.py` or mood loop | optional auto dream debounce |
| `shared/vector-ai/routes/mood.py` | optional goals/dream in digest |
| `shared/vector-ai/test_soul_persist.py` | **NEW** |
| `shared/vector-ai/test_soul_view.py` | **NEW** |
| `shared/vector-ai/test_soul_growth.py` | **NEW** dream job + tags |
| `shared/vector-ai/test_soul_core.py` / `test_service_modules.py` / `test_prompt_assembly.py` | keys + routes |
| `docs/plans/2026-07-29-soul-v2-persist-llm-view.md` | this file |
| `AGENTS.md` | docs map |

**Not touched:** chipper Go, sensor cooldown, workday FSM math.

---

## Out of scope

- Full GrowBot identity sentence patch / keep_rules garden / funLog ladder UI  
- Export/import GrowBot soul_format 3 binary-compatible files  
- Multi-robot soul files  
- Encrypting soul.json  
- Persisting PresenceCache sticky occupancy perfectly across long restarts  
- Speaking the entire dream aloud every morning (optional later; field + ops is enough for v2)  

---

## Human steps after implement

| # | Step |
|---|------|
| 1 | Restart vector-ai; confirm `vector-ai/soul.json` appears after first chat/mood |
| 2 | `curl -s localhost:8090/v1/soul \| jq` — read long keys |
| 3 | Optional: PATCH goals/dream; restart; confirm still there |

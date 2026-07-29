# Soul v2 — persist to disk, LLM-readable view, goals, dream field

**Date:** 2026-07-29  
**Branch:** `growBotHarness`  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Spec prior art:** `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` (v1 melting pot)  
**Goal:** Make SOUL durable across restarts; expose a **self-explanatory, relative-time** view for ops and LLMs; inject more soul context into chat (full) and thinner slices into one-shots; add **goals[3]** and **last night I dreamed about**.  
**Execute with:** `/do` after review.

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

**Root cause for “LLM can’t use SOUL well”:** short keys + absolute unix timestamps + incomplete inject; for “lost day after restart” — no soul file.

### Product decisions (this plan)

| # | Decision |
|---|----------|
| D1 | Persist soul to **`ROOT/soul.json`** (single JSON file, atomic write). Not SQLite for v2 (simpler; mood still may dual-write memory.db). |
| D2 | **Internal** Python attrs may keep short names (`last_spoke_at`); **export views** (`llm_view` / GET body) use **long snake_case keys** that read as English. |
| D3 | All timestamps in LLM/ops export views are **relative phrases** generated at read time (“about 3 minutes ago”, “never”, “moments ago”) via existing `_relative_time` pattern — **no raw unix** in those views. Internal file may still store unix for reload. |
| D4 | **Full soul strip** on interactive **chat** (`prepare_messages` / context note). **Lite strip** on greeting (and optionally mood digest). **None/minimal** on ambient/sensor one-shots (cost + length). |
| D5 | **goals**: exactly **3** string slots (pad/truncate). Persist. Editable later via API optional in phase; seed empty or sensible defaults. |
| D6 | **last_night_i_dreamed_about**: one string field (empty default). Persist. No dream LLM job in this plan — field only (populate later or manually via POST). |
| D7 | Save strategy: **debounced** write after mutations (e.g. 2s coalesce) + **force save** on set_mood / set_goals / set_dream / shutdown best-effort. Load once at Soul construction in `service.py`. |
| D8 | Do **not** persist full `image_b64` or huge buffers; cap face buffer / notify / eye digests on disk (same ring limits as RAM). |

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
- Put raw unix timestamps in LLM-facing JSON keys (ok inside private `_persist_blob` only).
- Inject full soul into every ambient glance (cost/spam).
- Import `service` from soul or prompt_assembly.
- Break `GET /v1/soul` without updating `test_service_modules` route list.
- GrowBot body/dream LLM sleep job (field only this plan).
- Second PresenceCache.

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

### Proposed long field names (LLM / GET)

Self-explanatory with **zero** prior context:

| Internal / short | Long export key |
|------------------|-----------------|
| `now` | `snapshot_generated_at_wall_clock` (human local string, e.g. `"Tuesday July 29, 2026, 03:15 PM"`) — optional |
| `occupied` | `a_person_is_currently_at_the_desk` |
| `session_started_at` | `how_long_ago_this_continuous_desk_session_started` (relative or `"unknown"`) |
| `last_seen_at` | `how_long_ago_i_last_visually_detected_a_person` |
| `last_spoke_at` | `how_long_ago_i_last_spoke_aloud` |
| `last_user_voice_at` | `how_long_ago_the_human_last_spoke_to_me` |
| `face.current` | `person_i_currently_believe_i_am_looking_at` `{name, is_stranger, face_id}` |
| `mood.text` | `my_current_inner_mood_in_one_phrase` |
| `mood.updated` | `how_long_ago_my_mood_was_last_reflected` |
| `eye.last_vision.note` | `what_i_noted_in_my_most_recent_camera_glance` |
| `eye.last_vision.t` | `how_long_ago_that_camera_glance_was` |
| `eye.last_vision.presence` | `whether_that_glance_saw_a_person_or_empty_desk` |
| workday strip | `work_day_accountability_status_for_today` |
| goals | `three_goals_i_currently_hold` (array len 3) |
| dream | `last_night_i_dreamed_about` |
| notify.recent | `recent_things_i_said_aloud` (list of `{how_long_ago, source, text_preview}`) |
| presence soft_name | `soft_name_hint_from_presence` |

Omit from LLM full view unless useful: raw face buffer IDs dump (or include last 3 as `recent_face_sightings` with relative times + names only).

**Relative never:** if ts ≤ 0 → `"never"` or `"not yet this session"`.

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
   - `relative_phrase(now, ts) -> str` (copy `_relative_time` logic + never)
   - `llm_view(soul, now, *, detail: Literal["full","lite","ops"] = "full") -> dict`
   - Keys = long names from Phase 0 table  
   - `detail="lite"`: mood, goals, dreamed_about, last spoke/seen/voice relatives, person looking at, desk occupied — **no** notify ring, no face buffer, no full eye buffer  
   - `detail="full"`: lite + workday strip + last glance + recent speech previews + goals + dream  
   - `detail="ops"`: same as full for GET (or full + soft_name / presence debug as long names)

2. Change `Soul.snapshot(now)` to either:
   - **Option A (preferred):** `snapshot` becomes alias of `llm_view(..., detail="ops")` for GET, **or**
   - **Option B:** keep internal `snapshot_raw` for tests; GET uses `llm_view`

   Update tests that assert short keys (`test_soul_core.test_snapshot_shape`, `test_get_v1_soul_snapshot`) to long keys.

3. `routes/soul.py`: return `soul.llm_view(time.time(), detail="ops")`; docstring lists long keys.

### Documentation references

- Long key table in this plan Phase 0  
- `_relative_time` in `prompt_assembly.py` L157–166  

### Verification checklist

- [ ] GET /v1/soul JSON has **zero** values that look like raw unix epoch numbers for time fields (allow integers only for face_id)  
- [ ] Keys include `how_long_ago_i_last_spoke_aloud`, `three_goals_i_currently_hold`, `last_night_i_dreamed_about`  
- [ ] `pytest shared/vector-ai/test_service_modules.py -k soul -q`  
- [ ] `pytest shared/vector-ai/test_soul_core.py -q`  

### Anti-pattern guards

- Do not put `t: 1753...` in notify.recent for LLM view — use `how_long_ago`  
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

- Do not run dream LLM in this phase  
- Do not put goals in persona.txt  

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
| 2 | Phase 2 llm_view long keys + relative | medium | GET + prompt share one builder |
| 3 | Phase 3 HTTP PATCH optional | low | Can merge with 1 |
| 4 | Phase 4 prompt inject full/lite | medium | Product feel |
| 5 | Phase 5 docs + full pytest | low | Gate |

Phases 1–3 can ship as one PR if small; keep tests green each step.

---

## Success criteria

1. Restart vector-ai → speech clocks / mood / goals / dream / rings reload from `soul.json`.  
2. `GET /v1/soul` is readable by a human (or LLM) **without** knowing Vector internals; times are relative.  
3. Chat prompts include goals + dream + continuity strip; ambient does not get a novel.  
4. Exactly three goals always.  
5. Full `pytest shared/vector-ai/` green.  

---

## Files likely touched

| File | Change |
|------|--------|
| `shared/vector-ai/soul/persist.py` | **NEW** load/save |
| `shared/vector-ai/soul/view.py` | **NEW** llm_view + format_for_prompt |
| `shared/vector-ai/soul/soul.py` | goals, dream, load/save hooks, snapshot→view |
| `shared/vector-ai/soul/types.py` | maybe deprecate short snapshot builder |
| `shared/vector-ai/service.py` | load_from_disk after construct |
| `shared/vector-ai/routes/soul.py` | llm_view; optional PATCH |
| `shared/vector-ai/prompt_assembly.py` | inject full strip |
| `shared/vector-ai/routes/greeting.py` | inject lite strip |
| `shared/vector-ai/routes/mood.py` | optional goals/dream in digest |
| `shared/vector-ai/test_soul_persist.py` | **NEW** |
| `shared/vector-ai/test_soul_view.py` | **NEW** |
| `shared/vector-ai/test_soul_core.py` / `test_service_modules.py` / `test_prompt_assembly.py` | key renames |
| `docs/plans/2026-07-29-soul-v2-persist-llm-view.md` | this file |
| `AGENTS.md` | docs map |

**Not touched:** chipper Go, sensor cooldown, workday FSM math.

---

## Out of scope

- Dream generation sleep job  
- Export/import GrowBot soul_format 3  
- Multi-robot soul files  
- Encrypting soul.json  
- Persisting PresenceCache sticky occupancy perfectly across long restarts  

---

## Human steps after implement

| # | Step |
|---|------|
| 1 | Restart vector-ai; confirm `vector-ai/soul.json` appears after first chat/mood |
| 2 | `curl -s localhost:8090/v1/soul \| jq` — read long keys |
| 3 | Optional: PATCH goals/dream; restart; confirm still there |

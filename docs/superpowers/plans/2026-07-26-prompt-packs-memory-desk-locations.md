# Plan: Mini prompt packs · thinner memory inject · desk location memory

**Date:** 2026-07-26  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Goal:** High-leverage code-side work so non-chat LLM calls stop paying full persona cost, chat memory inject stops being a firehose, and ambient can notice **moved/vanished/appeared** desk items via a structured location map.

**Out of scope for this plan**

- Chipper / Wire-Pod Go changes (`CreatePrompt`, `ValidLLMCommands`) — already improved separately  
- Rewriting user-owned `persona.txt` / `openai_prompt` content (user already revised those)  
- Embedding / vector DB  
- Migrating ambient/greeting/sensor into Behavior plugins  
- Multi-robot isolation  

**Success (noticeability)**

1. Ambient / greeting / sensor OpenRouter payloads use a **short voice line**, not full `PERSONA`.  
2. Chat system memory block injects **top-k retrieved facts**, not up to ~200 bullets + multi-paragraph SAVE essay.  
3. After vision (chat and/or ambient), Bandit can **update** “mug left of monitor” and ambient can treat a **move** as novelty without re-roasting static clutter.

---

## Phase 0 — Documentation Discovery (completed; binding)

### Sources consulted

| Path | Findings used |
|------|----------------|
| `shared/vector-ai/persona.py` | `PERSONA = _load_persona()` at import; `ROOT / "persona.txt"` |
| `shared/vector-ai/routes/ambient.py` | `_AMBIENT_SYSTEM = PERSONA + task`; `list_observations(8, 24h)`; novelty → `remember_observation` |
| `shared/vector-ai/routes/greeting.py` | `_GREETING_SYSTEM = PERSONA + task` |
| `shared/vector-ai/routes/sensor.py` | `_SENSOR_SYSTEM = PERSONA + task` |
| `shared/vector-ai/prompt_assembly.py` | `prepare_messages` = `PERSONA + wirepod_system + memory_section`; facts limits 100/100/20; SAVE essay always appended |
| `shared/vector-ai/memory.py` | `memories`, `observations` (7d prune, free text, no structure), no desk-item table |
| `shared/vector-ai/chat_flow.py` | vision store: `has_image and len(reply) >= 25` → `remember_observation(reply[:300])` |
| `shared/vector-ai/vision.py` | `_GETIMAGE_PAYLOAD = "{{getImage||front}}."` (align to `now` is a one-line fix, optional in Phase 1) |
| `shared/vector-ai/test_ambient_presence.py` | PRESENCE / sticky / novelty contracts **must not break** |
| `shared/vector-ai/test_service_modules.py` | route smoke, clean_response tags, ambient quiet, sensor non-ASCII |
| `AGENTS.md` | persona = prose; tags stripped; observations ≠ durable facts; no invent ambient empty |
| `docs/superpowers/plans/2026-07-21-ambient-presence-sticky-face.md` | binding presence architecture |

### Allowed APIs (use these; do not invent alternatives)

**Persona / packs (today)**

- `from persona import PERSONA` — full prose only (import-time)  
- Private strings: `_AMBIENT_SYSTEM`, `_GREETING_SYSTEM`, `_SENSOR_SYSTEM`  

**New modules this plan introduces (copy patterns from existing modules)**

- Pattern for module-level constants: `routes/sensor.py` L21–27  
- Pattern for optional env ints: `behaviors/config.py` `_int` / `load_*_config`  
- Pattern for SQLite schema + lock: `memory.py` `_init_schema` / `MemoryStore`  

**Memory (existing)**

```text
MemoryStore.remember(text, face_id=None, face_name=None) -> Optional[Memory]
MemoryStore.list_for_face(face_id, limit=100) -> List[Memory]
MemoryStore.list_shared(limit=100) -> List[Memory]
MemoryStore.list_mentions_of_name(name, exclude_face_id=None, limit=50) -> List[Memory]
MemoryStore.remember_observation(text, face_id=None) -> None
MemoryStore.list_observations(limit=5, max_age_seconds=21600) -> List[dict]
# dict keys: id, seen_at, face_id, text
```

**Ambient contracts (locked by tests)**

```text
parse_ambient_llm_raw(raw) -> (presence_kind, name_hint|None, spoken)
presence_kind in {empty, person, unknown}
POST /v1/ambient always returns "text"
Do not invent presence on quiet / LLM error / unknown
```

**Tags (response_cleanup only)**

```text
{{remember||…}}  {{remember-shared||…}}  {{forget||…}}  {{quietMode||on|off}}
```

### Anti-patterns (global — reject any task that does these)

1. **Do not** put command/vision grammar into `persona.txt` or mini persona.  
2. **Do not** invent embeddings / vector search for v1 retrieval (keyword + recency only).  
3. **Do not** break PRESENCE first-line protocol or store PRESENCE lines as observations.  
4. **Do not** call `/v1/ambient` from behavior tick.  
5. **Do not** replace sticky presence with “desk item map” (orthogonal: occupancy vs layout).  
6. **Do not** re-introduce “never say X” ban lists in new ambient system text (positive framing).  
7. **Do not** dump full desk map + full fact list into ambient system (caps only).  
8. **Do not** invent new HTTP unless a phase explicitly requires it (desk map is internal SQLite first).  
9. **Do not** change chipper JSON shape for ambient: keep `text` (and existing optional presence fields).  
10. **Do not** use full `PERSONA` for ambient/greeting/sensor after Phase 1 ships (tests must assert mini length / absence of calibration block).

### Verified live anchors (copy from these)

| Need | Copy from |
|------|-----------|
| System string join | `routes/sensor.py` `_SENSOR_SYSTEM` |
| Ambient LLM + parse + observe | `routes/ambient.py` `ambient`, `parse_ambient_llm_raw` |
| Memory section inject | `prompt_assembly.py` `_build_memory_section` |
| Chat vision observe | `chat_flow.py` post-stream block |
| SQLite store pattern | `memory.py` `MemoryStore` |
| Config env load | `behaviors/config.py` `_int`, `_truthy` |
| Test style ambient | `test_ambient_presence.py` |
| Test style service | `test_service_modules.py` |

---

## Phase 1 — Mini prompt packs (ambient / greeting / sensor)

### What to implement

Introduce a small **prompt voice helper** used only by non-chat spoken one-shots. Chat (`prepare_messages`) keeps full `PERSONA`.

**TASK-01 — `prompt_voice.py` (new)**

Create `shared/vector-ai/prompt_voice.py` with:

```python
# Copy load rules spirit from persona.py:_load_persona (comment strip, ROOT)
# But expose TWO strings:

PERSONA_FULL   # existing behavior: same as today's PERSONA (re-export or move load here)
PERSONA_MINI   # short voice line for non-chat

def load_persona_full() -> str: ...
def load_persona_mini() -> str: ...
```

**Mini derivation rules (implement exactly one of these; prefer A):**

| Option | Behavior |
|--------|----------|
| **A (recommended)** | Fixed constant mini ~1–2 sentences: dry desk robot voice only — **not** auto-derived from full file (full file has calibration examples that must never hit ambient). |
| **B** | Optional `persona.mini.txt` next to `persona.txt`; if missing, fall back to constant mini. |

**Do not** auto-truncate full Bandit prose (calibration examples would still leak).

Keep `persona.py` as thin re-export for back-compat:

```python
# persona.py — copy pattern: other modules import PERSONA
from prompt_voice import PERSONA_FULL as PERSONA
```

Or leave `persona.py` as loader of full only and have packs import `PERSONA_MINI` from `prompt_voice`.

**TASK-02 — Wire ambient / greeting / sensor**

In each file, change system construction:

| File | Today | After |
|------|--------|--------|
| `routes/ambient.py` | `PERSONA + ambient task` | `PERSONA_MINI + ambient task` |
| `routes/greeting.py` | `PERSONA + greeting task` | `PERSONA_MINI + greeting task` |
| `routes/sensor.py` | `PERSONA + sensor task` | `PERSONA_MINI + sensor task` |

**Rewrite ambient task body (positive framing)** — keep PRESENCE protocol and NOTHING/novelty shape from `ambient.py` L34–69, but:

- Remove any reliance on full character novel  
- Explicit: no `{{…}}` tokens; plain speech for Line B  
- Optional one line: “dry, short, Bandit-like” via mini only  

**Do not** change `parse_ambient_llm_raw` signatures or return rules.

**TASK-03 — Tests for packs**

Add `shared/vector-ai/test_prompt_voice.py`:

- `PERSONA_MINI` length under a cap (e.g. ≤ 400 chars)  
- `PERSONA_MINI` does not contain `"CALIBRATION"` or multi-example `User:` blocks  
- ambient/greeting/sensor system strings (import module constants) contain mini, not full calibration  

Add or extend service tests so imports still resolve (`from persona import PERSONA` still works for chat).

### Documentation references

- Copy load path pattern: `persona.py` L36–69, `paths.ROOT`  
- Copy system join: `sensor.py` L21–27  
- Ambient protocol must match: `test_ambient_presence.py` parser tests  

### Verification checklist

```bash
cd VectorIntelligence
python3 -m pytest shared/vector-ai/test_prompt_voice.py shared/vector-ai/test_ambient_presence.py shared/vector-ai/test_service_modules.py -q
```

- Grep: `routes/ambient.py` must not import full persona for system (only mini).  
- Grep: `prepare_messages` still uses full persona.  

### Anti-pattern guards

- Do not hot-reload persona without restart (existing product rule).  
- Do not put Work Day tags in mini.  
- Do not change ambient HTTP response shape.

---

## Phase 2 — Thinner memory inject (chat `prepare_messages`)

### What to implement

**TASK-04 — Retrieval helper**

Add to `prompt_assembly.py` or `memory_retrieve.py` (prefer new file to keep assembly readable):

```python
def select_memories_for_turn(
    memory: MemoryStore,
    face: Optional[dict],
    last_user_text: str,
    *,
    max_personal: int = 8,
    max_shared: int = 4,
    max_mentions: int = 3,
) -> list[str]:  # or list[Memory]
    """Rank durable facts for this turn. No embeddings."""
```

**Ranking (v1, deterministic, documented):**

1. Load candidates with **existing** APIs only:  
   - known face → `list_for_face(face_id, limit=100)` + `list_shared(100)` + `list_mentions_of_name(..., limit=20)`  
   - no face / single primary → same branches as today’s `_build_memory_section` identity logic (copy branch structure from `prompt_assembly.py` L16–85)  
2. Score each fact:  
   - token overlap with `last_user_text` (alphanumeric lower tokens, stopword-light)  
   - small boost if fact text appears in recent conversation turns (optional; if expensive skip)  
   - stable tie-break: lower `id` or newer `created_at` — pick one and document  
3. Take top `max_personal` / `max_shared` / `max_mentions` separately then merge for display  

**Env knobs** (optional, defaults as above):

```text
MEMORY_INJECT_PERSONAL=8
MEMORY_INJECT_SHARED=4
MEMORY_INJECT_MENTIONS=3
```

Parse like `behaviors/config.py` `_int` (clamp ≥ 0).

**TASK-05 — Slim `_build_memory_section`**

Refactor `_build_memory_section` to accept optional `last_user_text: str = ""` (or change `prepare_messages` to pass it).

**Structure after:**

```text
[who you are looking at / stranger / primary-user branch — KEEP short identity rules from L16–85]
[Things you know — only selected bullets, or "no durable facts yet" one-liner]
[ONE short save line — not the multi-paragraph SAVE essay]

SAVING (one line):
Silent tags {{remember||fact}} / {{remember-shared||fact}} / {{forget||…}}; never speak them.
```

**Delete or move** the long “Reference these facts the way a sharp friend…” + multi-paragraph SAVE block (today L82–103) to at most **4 lines total**.

**Wire `prepare_messages`:**

- Extract last user text the same way `chat_flow.generate` does (string content; if multimodal, join text parts).  
- Pass into memory section builder.  
- Keep system assembly: `f"{PERSONA}\n\n{wirepod_system}\n\n{memory_section}"`  
- Keep context note on latest user only (L160–241 unchanged except observation list can stay).

**TASK-06 — Tests for retrieval**

New `test_prompt_assembly.py` (currently **missing** — discovery confirmed no coverage):

- Many personal facts → inject ≤ `max_personal`  
- Query tokens match one fact → that fact preferred over unrelated  
- Stranger branch still does not leak other people’s personal lists (copy intent from L40–50)  
- Shared facts still appear when selected  
- Empty store → blank-slate one-liner still present for known face  

### Documentation references

- Branch logic copy: `prompt_assembly.py` L16–85  
- `prepare_messages` L244–310  
- Tag surface: `response_cleanup.py` (do not change tag regex in this phase)  
- AGENTS.md memory model  

### Verification checklist

```bash
python3 -m pytest shared/vector-ai/test_prompt_assembly.py shared/vector-ai/test_service_modules.py -q
```

- Manual log check (optional): with VECTORAI_DEBUG, prepared system memory section length drops vs pre-change.  
- `clean_response` remember/forget tests still pass.  

### Anti-pattern guards

- Do not invent `MemoryStore.search()` without implementing it.  
- Do not remove stranger / multi-user caution branches.  
- Do not move context note onto system prompt (cache comment L164–167).  
- Do not re-expand SAVE essay “for safety.”

---

## Phase 3 — Desk location memory

### Product rule

| Event | Store | Ambient novelty |
|-------|--------|-----------------|
| Object stable at known place | update `last_seen` only | NOTHING (plus PRESENCE as today) |
| Object moved | update location | optional spoken + note |
| Object gone | mark missing / drop | optional novelty |
| New durable object | insert | optional novelty |

**Occupancy (person/empty) stays in `PresenceCache`.** Location map is separate.

### What to implement

**TASK-07 — Schema + store API on `MemoryStore`**

Extend `memory.py` `_init_schema` with:

```sql
CREATE TABLE IF NOT EXISTS desk_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name_key    TEXT NOT NULL COLLATE NOCASE,  -- normalized key, unique
    name_display TEXT NOT NULL,
    location    TEXT NOT NULL,                 -- short free text, e.g. "left of monitor"
    last_seen   REAL NOT NULL,
    missing_since REAL,                        -- NULL if present
    updated_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_desk_items_name ON desk_items(name_key);
```

**Methods (add only these):**

```python
def upsert_desk_item(self, name: str, location: str, now: float | None = None) -> None
def mark_desk_item_missing(self, name: str, now: float | None = None) -> None
def list_desk_items(self, *, present_only: bool = True, limit: int = 24) -> list[dict]
def desk_map_prompt_lines(self, limit: int = 12) -> str
# returns short bullet list for prompts, empty string if none
```

Normalization: strip, collapse whitespace, lower for `name_key`; keep display casing from first sighting or latest.

**Retention:** optional prune `last_seen` older than 30d on write (mirror `_OBSERVATION_RETENTION` pattern). Prefer longer than observations (layout is semi-stable).

**TASK-08 — Extract locations (server-side, no speech tags required)**

Prefer **deterministic/light LLM extract after vision**, not new spoken tags:

**8a — After chat vision** (`chat_flow.py` where `remember_observation` already runs):

- Keep storing free-text observation as today (conversation recall).  
- Additionally call a small helper:

```python
async def extract_desk_items_from_scene_text(description: str) -> list[tuple[str, str]]:
    # either:
    # 1) llm_chat_once with tag="desk_extract", NO persona, STRICT JSON
    #    [{"name": "...", "location": "..."}, ...]
    # or v1-lite: skip LLM and only parse structured LOC lines if you add them later
```

**Recommended v1:** cheap `llm_chat_once` / `SUMMARY_MODEL` with **no PERSONA**, max_tokens small, tag=`desk_extract`. On failure, no-op (same spirit as `_summarise_conversation`).

**8b — After ambient novelty** (`ambient.py` when note stored):

- If novelty note looks like object change, run same extract **or** simpler rule: upsert from note only when extract returns items.  
- Do **not** write PRESENCE line into desk_items.

**TASK-09 — Ambient prompt uses desk map**

In `ambient()` user text construction (today obs_note from `list_observations`):

Also append when non-empty:

```text
Known desk layout (update only on real change; stable items → NOTHING after PRESENCE):
- mug: left of monitor
- headphones: right edge
```

from `MEMORY.desk_map_prompt_lines(limit=12)`.

**Tighten ambient task system (positive framing):**

- After PRESENCE, NOTHING unless something **appeared, moved, or vanished** relative to known layout / recent notes.  
- Static clutter and unchanged known items are not news.  

**Optional structured novelty (v1 keep free text):** still use Line A note + Line B spoken; after parse, run extract on Line A to upsert/missing.

**Compare logic (code, not only LLM):**

After ambient LLM (or before): if extract says location changed for existing `name_key`, that is a move even if model said NOTHING — **v1 can trust model only** to ship faster; document:

| Sub-option | Behavior |
|------------|----------|
| **3A ship** | Model sees map; model decides NOTHING vs novelty; extract updates map from note when novelty spoken |
| **3B later** | Code diffs extract(current image description) vs DB and forces novelty — needs always-on scene extract cost |

**Implement 3A in this plan.** Leave 3B as follow-up in risk register.

**TASK-10 — Tests**

- `test_memory_desk_items.py`: upsert, unique name_key, missing, list, prompt lines cap  
- `test_ambient_presence.py` or new: ambient user prompt includes layout when items exist (mock MEMORY)  
- Extract unit: JSON parse tolerant like `joke_sources.parse_json_array` (copy that pattern from `behaviors/joke_sources.py` `parse_json_array`)  
- Existing ambient presence tests still green  

### Documentation references

- Observation write pattern: `memory.py` `remember_observation` / `list_observations`  
- Ambient obs_note construction: `ambient.py` L~220–250  
- Background LLM no-persona: `chat_flow._summarise_conversation`, `routes/mood.py` `_MOOD_SYSTEM`  
- JSON array parse: `behaviors/joke_sources.py` `parse_json_array`  
- Presence must not regress: `test_ambient_presence.py`  

### Verification checklist

```bash
python3 -m pytest shared/vector-ai/test_memory_desk_items.py \
  shared/vector-ai/test_ambient_presence.py \
  shared/vector-ai/test_prompt_assembly.py \
  shared/vector-ai/test_service_modules.py -q
```

- New DB table appears on fresh `MemoryStore` init.  
- Manual: after a vision describe with a mug, `desk_items` row exists; ambient prompt includes it.  

### Anti-pattern guards

- Do not store images.  
- Do not merge desk_items into `memories` unique-text table (different lifecycle).  
- Do not use desk map for Work Day / identity.  
- Do not invent empty presence from layout.  
- Do not put full Bandit PERSONA on `desk_extract` calls.

---

## Phase 4 — Docs + small hygiene

### What to implement

**TASK-11 — AGENTS.md / env-default**

- Document `PERSONA_FULL` vs `PERSONA_MINI` (chat vs one-shots).  
- Document memory inject caps env vars.  
- Document `desk_items` as semi-stable layout (not chat fact dump).  
- Optional: change `vision.py` `_GETIMAGE_PAYLOAD` to `{{getImage||now}}.` to match chipper catalog (one-line; test if any test asserts `front`).

**TASK-12 — NEXT_STEPS or README one-liner**

User-facing: “Ambient can notice when desk objects move” only after Phase 3 verified.

### Verification

- Grep AGENTS for mini persona + desk_items.  
- Full unit suite green (below).  

---

## Final Phase — Full verification

```bash
cd VectorIntelligence
python3 -m pytest -q
```

### Grep anti-pattern checks

```bash
# Non-chat routes must not build system from full PERSONA calibration
rg -n "PERSONA" shared/vector-ai/routes/ambient.py shared/vector-ai/routes/greeting.py shared/vector-ai/routes/sensor.py
# Expect PERSONA_MINI or prompt_voice import — not long persona for system

# Memory section must not still contain the old multi-paragraph SAVE block
rg -n "when in doubt, save" shared/vector-ai/prompt_assembly.py && exit 1 || true

# desk_items exists
rg -n "desk_items" shared/vector-ai/memory.py
```

### Contract checklist

| Contract | Proof |
|----------|--------|
| PRESENCE parser unchanged | `test_ambient_presence` parser tests |
| Sticky presence unchanged | sticky tests same file |
| Chat still has full character | `prepare_messages` uses full persona |
| Tags still work | `test_service_modules` clean_response |
| Ambient always has `text` | ambient HTTP tests |

---

## Build order (one line)

`prompt_voice + wire routes → retrieve + slim memory_section → desk_items schema → extract after vision → ambient map inject → docs → full pytest`

---

## Risk register

| Risk | Mitigation |
|------|------------|
| Mini voice drifts from Bandit | Keep mini explicit; user can later add `persona.mini.txt` (Option B) |
| Retrieval misses “remember that thing about X” | Caps are defaults; env raise; token overlap is v1 — document |
| desk_extract hallucinates objects | SUMMARY_MODEL + low max_tokens; only upsert on novelty path first; ignore empty JSON |
| Ambient more expensive | Map inject is text-only; extract only on novelty or vision, not every NOTHING glance in 3A |
| DB migration on live memory.db | `CREATE TABLE IF NOT EXISTS` only; no destructive migrate |
| getImage `front` vs `now` | Optional hygiene task; chipper accepts param or align both |

---

## File map (expected touch list)

| File | Phases |
|------|--------|
| `shared/vector-ai/prompt_voice.py` | **new** P1 |
| `shared/vector-ai/persona.py` | P1 re-export / thin |
| `shared/vector-ai/routes/ambient.py` | P1, P3 |
| `shared/vector-ai/routes/greeting.py` | P1 |
| `shared/vector-ai/routes/sensor.py` | P1 |
| `shared/vector-ai/prompt_assembly.py` | P2 |
| `shared/vector-ai/memory_retrieve.py` | **new optional** P2 |
| `shared/vector-ai/memory.py` | P3 |
| `shared/vector-ai/chat_flow.py` | P3 extract hook |
| `shared/vector-ai/desk_extract.py` | **new** P3 |
| `shared/vector-ai/vision.py` | P4 optional `now` |
| `shared/vector-ai/test_prompt_voice.py` | **new** P1 |
| `shared/vector-ai/test_prompt_assembly.py` | **new** P2 |
| `shared/vector-ai/test_memory_desk_items.py` | **new** P3 |
| `shared/vector-ai/test_ambient_presence.py` | extend P3 |
| `AGENTS.md` | P4 |
| `shared/vector-ai/env-default` | P2/P4 knobs |

---

## How to execute this plan

- Run **one phase per session** when using subagents; land green tests before next phase.  
- Phase 1 is independently shippable and immediately reduces ambient token cost.  
- Phase 2 improves conversation without hardware.  
- Phase 3 needs a robot or mocked ambient image tests for full confidence; unit tests cover store + prompt injection without hardware.

**One-sentence doctrine:** Full Bandit only for real conversation; one-shots get a whisper of voice; memory injects what matters this turn; the desk map is a small structured layer so motion is news and clutter is not.

# Plan: vector-ai quality hardening (from code-quality audit)

**Date:** 2026-07-26  
**Scope:** `shared/vector-ai/` (intelligence brain only — not monorepo chipper Go)  
**Goal:** Fix high-conviction correctness bugs and structural debt that preserve (or strictly improve) product behavior. Overnight-safe, test-backed.

## Audit verdict (summary)

The brain is already well modularized (composition root, routes, behaviors plugins). The main issues are not giant files — they are **contract violations** (speech-gated joke queue), **identity inconsistency** (remember vs effective face), **stream tag fragility**, and a few pure refactors.

### In scope (implement)

| ID | Sev | Issue | Safe overnight? |
|----|-----|-------|-----------------|
| P1 | blocker | Joke queue consumed before arbiter win | No→yes once fixed (correctness) |
| P2 | high | Remember owner uses `current_face()` not effective face | Mostly (edge fix) |
| P3 | high | Memory section re-resolves face independently of prepare_messages | Yes |
| P4 | high | Sentence stream splits inside `{{…}}` tags | Careful + tests |
| P5 | medium | `clean_response` mixes pure strip + side effects | Yes (extract, same order) |
| P6 | high | `min_tick_interval` is dead code (pass) | Yes — **delete** dead claim |
| P7 | medium | Workday clock transitions duplicated in plan + clock_tick | Yes (extract) |
| P8 | medium | chat_flow last-user text ignores multimodal lists | Yes |
| P9 | medium | Unbounded ambient image payload | Yes if cap ≥ real frames |
| P10 | medium | Duplicated one-shot LLM boilerplate (sensor/greeting/ambient) | Yes |
| P11 | low | Unreachable face branch in workday.plan | Yes |

### Explicitly out of scope (defer)

- Global UNIQUE on `memories(text)` schema change / multi-user ownership index
- Forget substring → exact-match overhaul (would change ops + LLM forget UX)
- Full presence-clock unification (chat / PresenceCache / greeting)
- Shared httpx client lifecycle (nice; not correctness)
- Ambient route full service extraction (optional if time)
- Implementing `min_tick_interval` throttle (would change tick frequency)

---

## Phase 0 — Patterns & allowed APIs (from tree)

**Read before coding:**

| Pattern | Source |
|---------|--------|
| Speech-gated commits | `AGENTS.md`, `docs/FSM-implementation.md`, `on_speak_allowed` on `TickResult` |
| Behavior plugin contract | `behaviors/types.py`, `BehaviorRuntime.tick` |
| Joke queue store | `continuity.py` `joke_queue_pop` / `joke_mark_served` / `joke_queue_len` |
| Face resolution | `prompt_assembly._effective_face`, `process_state.current_face` |
| Tag strip + apply | `response_cleanup.clean_response` / `extract_memory_commands` |
| Work command parse | `behaviors/workday.parse_work_commands` |
| One-shot LLM | `llm.llm_chat_once` + `_strip_for_speech` in ambient/greeting/sensor |
| Tests | `test_behaviors.py`, `test_joke_idle.py`, `test_service_modules.py`, `test_ambient_presence.py` |

**Anti-patterns:**

- Do **not** advance joke queue / served on plan when arbiter may deny
- Do **not** bypass SpeechArbiter
- Do **not** put Work Day knobs in `env-default` (pod.conf only)
- Do **not** invent new chipper patches — brain only
- Do **not** merge desk_items into memories table
- Prefer small pure helpers over new frameworks

---

## Phase 1 — Joke speech-gated queue commit (P1) **[blocker]**

### What

1. Add **peek** (non-destructive) of preferred/fallback kind on the queue:
   - Either `joke_queue_peek(kind)` + keep `joke_queue_pop` for actual remove, **or**
   - `pop_line` becomes `select_line` that returns row without delete/mark_served
2. In `joke_idle.tick`: select line without consuming; set `r.speak` and in `on_speak_allowed`:
   - `joke_queue_pop` by id/hash (or remove matching row)
   - `joke_mark_served`
   - existing `joke_commit_spoke`
3. On arbiter deny or workday priority win: queue + served unchanged
4. Update `pop_line` API or add `peek_line` / `commit_line` in `joke_sources.py`

### Tests

- Arbiter deny after joke planned → queue length unchanged, served not marked
- Workday higher priority speaks → joke queue unburned
- Happy path still pops + marks served + advances daily count once
- Update `test_joke_idle.py` comments that currently accept collateral pop

### Verify

```bash
python3 -m pytest shared/vector-ai/test_joke_idle.py shared/vector-ai/test_behaviors.py -q
```

---

## Phase 2 — Single face resolver for chat inject / touch / remember (P2, P3, P8)

### What

1. Export one helper (e.g. keep `_effective_face` or move to `process_state` / small `face_resolve.py`):
   - Prefer `current_face()` when set
   - Single enrolled non-stranger fallback when no face (existing prepare_messages logic)
2. `prepare_messages`: resolve face once; pass into `_build_memory_section(face, last_user_text)` and `select_memories_for_turn`
3. `extract_memory_commands` / remember ownership: use same effective face (not bare `current_face()`)
4. `chat_flow.generate`: use `_extract_last_user_text` (or shared public alias) instead of local str-only scan

### Tests

- No live face + one enrolled → remember personal ownership for that face
- Stranger face → shared / not personal
- Multimodal last user content still extracts text for logging/vision intent

### Verify

```bash
python3 -m pytest shared/vector-ai/test_service_modules.py shared/vector-ai/test_prompt_assembly.py -q
```

---

## Phase 3 — Tag-safe sentence stream + parse/apply split (P4, P5)

### What

1. In `llm_sentence_stream`: do not treat `.!?` as sentence end while inside an unmatched `{{` … `}}` region. Prefer holding buffer until tag closes or stream ends.
2. Split `response_cleanup`:
   - Pure strip path for speech (markdown, forbidden cmds, tag strip, phrase fixes, `||` scrub)
   - Side-effect apply path for remember/forget/quiet/work (still called from stream path in same order as today: apply tags when chunk is cleaned)
3. Keep call order from `chat_flow`: clean each emitted sentence so tags still fire mid-stream when a full tag is in a chunk; tag-safe splitter ensures whole tags land in one chunk when possible.

### Tests

- Stream buffer with `{{remember||Dr. Smith is Cam's dentist}}` yields one piece containing full tag before clean
- Abbreviations outside tags still split (`I'll note that. Next sentence.`)
- Existing workday tag tests still pass

### Verify

```bash
python3 -m pytest shared/vector-ai/test_service_modules.py shared/vector-ai/test_behaviors.py -q -k "tag or work or remember or clean or stream" 
# plus full suites if filters miss
python3 -m pytest shared/vector-ai/ -q --ignore=shared/vector-ai/.venv
```

---

## Phase 4 — Runtime / workday cleanup (P6, P7, P11)

### What

1. **Delete** dead `min_tick_interval` skip body (the `pass`) **or** remove attribute + docs claims. Preferred: remove the no-op check block and doc note that interval is unused until multi-FSM needs it; stop advertising it as active. Optionally keep attribute for future but do not pretend it throttles.
2. Extract `_apply_clock_transitions(rec, now, local_dt) -> dirty` used by both `clock_tick` and start of `plan`.
3. Delete unreachable face reassignment in `workday.plan`.

### Tests

Existing workday clock / EOD / pause tests must stay green.

### Verify

```bash
python3 -m pytest shared/vector-ai/test_behaviors.py -q
```

---

## Phase 5 — Defense + one-shot helper (P9, P10)

### What

1. Bound ambient (and optionally chat archive) image base64: reject / skip LLM when decoded size > ~3–4 MB (or raw string length cap ~5.5M chars). Prefer empty reaction + log, not 500 crash.
2. Add small helper e.g. `async def one_shot_spoken_line(messages, *, temperature, max_tokens, timeout=..., tag="") -> str` wrapping `llm_chat_once` + `_strip_for_speech` + error → `""`. Use from sensor, greeting, ambient (and mood if trivial).

### Tests

- Ambient with absurdly large image string does not call LLM (mock) and returns safe empty
- Sensor/greeting still return text shape

### Verify

```bash
python3 -m pytest shared/vector-ai/test_ambient_presence.py shared/vector-ai/test_service_modules.py -q
```

---

## Phase 6 — Final verification

```bash
cd /home/cam-test/repos/vectorStuff/VectorIntelligence
python3 -m pytest -q
```

- Commit after each logical phase (AGENTS.md: frequent WIP commits OK)
- Working tree clean before handoff
- No SpeechArbiter bypass
- No new pod.conf keys unless truly needed (image cap can be constant)

---

## Success criteria

1. Joke lines never leave the queue unless speech was allowed
2. Remember ownership matches who chat inject believes is present (single-user desk)
3. Tags with internal `.` are not fractured by the sentence streamer
4. Full pytest suite green
5. No schema migrations; no forget-semantic rewrite; no chipper Go changes

---

## Implementation notes for agent

- Follow existing style in each module; type-hint new public helpers
- Prefer pure functions for parse/select; IO at apply/commit boundaries
- When updating docs, only touch `docs/FSM-implementation.md` if min_tick or joke commit contract changes
- Runtime path is `~/vector-pod/` on Linux — do not edit live runtime; source only

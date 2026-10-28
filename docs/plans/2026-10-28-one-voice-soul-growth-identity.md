# One voice, soul tags that fire, living identity

**Date:** 2026-10-28
**Repo for edits:** `VectorIntelligence` only
**Readonly evidence:** `../VectorIntelligence-Production_Snapshot/` (runtime persona, `soul.json`, `memory.db`, `vector-ai.log`). Do not modify that tree.
**Execute with:** `/do`

Three product changes:

1. Ambient, greeting, and sensor one-shots speak in the same character as the runtime `persona.txt`.
2. Chat can store a fact or refresh goals/dream when the human actually asks, without a second copy of a teach-block the model already ignores.
3. The dream job keeps one short living-identity line. It still must not rewrite `persona.txt`.

---

## Phase 0 — Documentation discovery (complete)

### Allowed APIs (dev tree)

| API | Where | Use |
|-----|--------|-----|
| `load_persona_full()` / `load_persona_mini()` | `shared/vector-ai/prompt_voice.py` | Full pack from `ROOT/persona.txt` (`#` lines dropped, `utf-8-sig`). Mini today returns the fixed `_PERSONA_MINI_DEFAULT` and does not read the file. Bound once at import as `PERSONA_FULL` / `PERSONA_MINI`. |
| `PERSONA` | `shared/vector-ai/persona.py` | Re-export of `PERSONA_FULL`. Chat only, via `prepare_messages`. |
| `AMBIENT_SYSTEM` | `shared/vector-ai/soul/eye.py` ~42 | `PERSONA_MINI +` PRESENCE/SCENE protocol. |
| `_GREETING_SYSTEM` | `shared/vector-ai/routes/greeting.py` ~30 | `PERSONA_MINI +` greeting task. User turn may append `format_soul_for_prompt(..., detail="lite")`. |
| `_SENSOR_SYSTEM` | `shared/vector-ai/routes/sensor.py` ~21 | `PERSONA_MINI +` one-sentence task. No soul strip. |
| `_build_memory_section` | `shared/vector-ai/prompt_assembly.py` ~42 | Identity + top-k facts. Stable system prefix. |
| `_WORK_AWAITING_EMIT` | `shared/vector-ai/prompt_assembly.py` ~19 and ~368 | Situational teach string appended in `_build_context_note` only, on the latest user turn. **Copy this placement.** |
| `maybe_apply_afternoon_from_user_text` | `shared/vector-ai/chat_flow.py` ~287 | User-text backstop called before the LLM. **Copy this shape** for an explicit remember phrase. |
| `parse_soul_growth_tags` / `apply_soul_growth_actions` / `clean_response` | `shared/vector-ai/response_cleanup.py` ~57, ~144, ~165, ~250 | `{{soulDream}}` schedules `maybe_schedule_dream_job(..., reason="chat_tag", force=False)`. `{{soulGoals\|\|…}}` calls `soul.set_goals` immediately (pad/truncate to 3). Tags stripped before SSE. |
| `run_dream_job` / `_apply_dream_payload` | `shared/vector-ai/soul/growth.py` | `SUMMARY_MODEL`, `llm_view(detail="full")`. Applies dream + 3 goals. `tomorrow_try` folded into the dream sentence. `growth_note` not stored. |
| `DREAM_SYSTEM` | `shared/vector-ai/soul/growth_prompts.py` 26–39 | JSON keys: `last_night_i_dreamed_about`, `three_goals_i_currently_hold`, `tomorrow_try`, `growth_note`. Hard rule: do not rewrite persona or constitution. |
| `set_goals` / `set_dreamed_about` | `shared/vector-ai/soul/soul.py` | Caps: 3 × 200 chars, dream 500 chars. |
| `serialize_soul` / `load_into` | `shared/vector-ai/soul/persist.py` | `soul_format` 2. Missing keys are simply not applied (`data.get`). Unknown keys are ignored. |
| `SoulPatchRequest` | `shared/vector-ai/routes/soul.py` ~13 | PATCH accepts only `three_goals_i_currently_hold` and `last_night_i_dreamed_about`. |
| `format_soul_for_prompt` / `llm_view` | `shared/vector-ai/soul/view.py` | Long keys. Dream injected as `dream[:200]`. No identity key today. |
| `RUNTIME_GUIDANCE` | `shared/vector-ai/runtime_guidance.py` | Already tells the model Cam and Camm are one human. Does not merge SQLite rows. |

### Tests that lock the current (wrong for this desk) contract

- `shared/vector-ai/test_prompt_voice.py`: `len(PERSONA_MINI) <= 400`; mini has no `{{`, `Work Day`, or `CALIBRATION`; `_AMBIENT_SYSTEM` / `_GREETING_SYSTEM` / `_SENSOR_SYSTEM` start with `PERSONA_MINI`; `PERSONA_FULL` is not inside `_AMBIENT_SYSTEM` unless equal to mini.
- `shared/vector-ai/test_soul_growth.py` `test_soul_growth_line_absent_from_memory_section`: `"SOUL GROWTH"`, `"{{soulDream}}"`, and `"{{soulGoals||"` are absent from `_build_memory_section` both when `SOUL` is None and when it is live. **Keep that absence.** Do not “fix” it by injecting the old paragraph.
- `test_parse_and_strip_soul_growth_tags`: tags stripped, goals applied. Keep.

### Production snapshot (measured 2026-10-28, read-only)

| Fact | Evidence |
|------|----------|
| Runtime character file is **724 characters**, one safe paragraph, no `{{`, no `Work Day`, no `CALIBRATION`. It names **Bandit** and **Sprocket** and asks for very few words. | `VectorIntelligence-Production_Snapshot/persona.txt`. Log: `[persona] loaded persona.txt (724 chars)` on 2026-09-28 and 2026-10-07. |
| A 400-character sentence cut of that file ends at char **324** and drops the personality sentences and Sprocket. | Measured on the live body. Last `.` at or under 400 is the eye-animation sentence. |
| Chat requests are prefixed with that Bandit text (**432** log hits of `YOU ARE BANDIT`). Ambient/greeting/sensor requests are prefixed with the stock mini (**2241** hits): `You are Vector, a small desk robot. Dry, deadpan, briefly witty — one or two short sentences, never sycophantic.` | `vector-ai.log` system `content` lines. Snapshot `prompt_voice.py` still returns `_PERSONA_MINI_DEFAULT` and never reads `persona.txt`. |
| A richer Bandit bible sits unused in `persona.txt.backup` (**3539** body chars). Paragraphs through the constraint list are “safe” (**2669** chars) and then `User:` calibration blocks begin. Those blocks must never enter ambient. | `persona.txt.backup` |
| `soul.json` is format 2. No identity key. Dream is 321 chars of desk-object surrealism. Three goals are object quests (`Check coaster position…`, plush stitch pattern, dangling treads). Last dream job **2026-10-27 23:02:45**. The on-disk log ring is 64/64 `dreamed:` lines. | `soul.json` |
| In `vector-ai.log`, **32/32** applied dream jobs are `ambient_sleep_gap`. **Zero** `chat_tag`. **Zero** `http_dream`. **Zero** `[memory] +remember #` lines. `[memory]` hits are `+observation`. | log scan |
| SQLite `memories`: **14 rows**. Faces with rows: shared/`NULL` ×10, `Cam` face 1 ×2, `Cam` face 2 ×1, `Camm` face 5 ×1. `face_meta` also has `allie` (2 interactions). Interaction counts are high (Cam 178, second Cam 105, Camm 25) against almost no durable facts. | `memory.db` |
| The chat system body already contains a Wire-Pod `SilentTags` / `The shape of a turn` block that teaches `{{remember\|\|}}`, `{{soulDream}}`, and `{{soulGoals\|\|`. **866** log lines contain `soulDream`. **`SOUL GROWTH` count is 0.** That block is not in this git repo (not in `shared/config`, not in `runtime_guidance.py`). It arrives as chipper’s system message. The model does not emit the tags. | `vector-ai.log` request bodies. `rg` of the dev repo finds the tags only in the parser, tests, and docs. |

### Decisions locked by that evidence

| # | Decision |
|---|----------|
| D1 | One-shots take a **derived** mini from `persona.txt`. They never take `PERSONA_FULL` verbatim. |
| D2 | Mini cap is **800 characters**, whole paragraphs only. 400 is too small for the live 724-character file. 800 includes that whole file. It does not include the backup’s calibration or the long constraint essay. |
| D3 | Drop any paragraph that contains `{{`, `CALIBRATION`, `Work Day`, or a line whose strip starts with `User:`. |
| D4 | A raw comment line `# @voice` (seen **before** comment-strip) selects the next paragraph as the only mini source. Same safety filters and the 800 cap. |
| D5 | No `# @voice` and no safe paragraph → keep `_PERSONA_MINI_DEFAULT`. |
| D6 | Do **not** copy the production Bandit prose into the repo `persona.txt`. The repo template stays the stock character plus the Work Day paragraph. Header comments in the repo template and `_PERSONA_HEADER` learn the new rule. The running desk picks it up when this code is deployed beside the existing runtime `persona.txt`. |
| D7 | Do **not** put the old `SOUL GROWTH` paragraph back into `_build_memory_section`. Production already sends a longer teach block from chipper, and it produced no remembers and no chat-tag dreams. |
| D8 | Teach soul/remember only as a **short situational block on the latest user turn**, same slot as `_WORK_AWAITING_EMIT`. |
| D9 | Add a **user-text backstop** for an explicit “remember that …” phrase, same call site shape as `maybe_apply_afternoon_from_user_text`. Do not NLP the whole utterance. |
| D10 | `{{soulDream}}` is for “dream on this day / consolidate”, not for “what did you dream?”. Asking what he dreamed must be answered from `last_night_i_dreamed_about` already in the soul strip. A new dream job would replace that text. |
| D11 | Living identity is a new string `who_i_am_becoming`, cap **240** chars. Persist it. Show it in `llm_view` and `format_soul_for_prompt` (lite and full). Dream JSON may set it. **Empty or missing means keep the previous value.** |
| D12 | Dream prompt may patch goals, dream, and that one line. It still must not rewrite `persona.txt`, constitution, or invent a new character. The line is his stance toward this human (and Sprocket only if the snapshot supports Sprocket). It is not another plush/colander quest. The measured goals and dream show that failure mode. |
| D13 | Keep `soul_format` **2**. Old `soul.json` files load with the new field defaulting to `""`. |
| D14 | Do not change `DREAM_DEBOUNCE_S` (`4 * 3600` in code; several comments and `docs/FAQ.md` still say 20h). Out of scope. |
| D15 | Do not merge face ids 1, 2, and 5. `RUNTIME_GUIDANCE` already explains the alias. Out of scope. |
| D16 | Joke idle, morning weather, and Work Day canned lines do not import `PERSONA_MINI`. Leave them. |

### Anti-patterns

- Do not prepend `PERSONA_FULL` onto ambient, greeting, or sensor.
- Do not auto-slice the full file at 400 characters. That cuts Sprocket off the live persona.
- Do not feed `CALIBRATION` or `User:` blocks into one-shots. The backup file is the proof.
- Do not add a second long SilentTags essay to the system prefix.
- Do not schedule a dream because the human asked what last night’s dream was.
- Do not let an empty identity field in dream JSON wipe a good line.
- Do not write `persona.txt` from the dream job.
- Do not edit `VectorIntelligence-Production_Snapshot/`.
- Do not bump `soul_format` to 3.
- Do not put tag grammar into `persona.txt`.

---

## Phase 1 — One-shot voice from `persona.txt`

### What to implement

Copy the loader rules in `load_persona_full` (`prompt_voice.py` 49–78): `ROOT / "persona.txt"`, `utf-8-sig`, restart still required because `PERSONA_MINI = load_persona_mini()` runs at import.

1. Add `_PERSONA_MINI_MAX = 800` and `_UNSAFE_PARA_TOKENS = ("{{", "CALIBRATION", "Work Day")`.

2. Add helpers next to the loader (same file):

   - `_paragraph_is_unsafe(para: str) -> bool` — true if any token above is in the paragraph, or any line’s `strip()` starts with `User:`.
   - `_voice_paragraph_from_raw(raw: str) -> str | None` — if a full line matches `^#\s*@voice\s*$`, return the next paragraph of non-comment lines (blank line ends it). Else `None`.
   - `_safe_paragraphs(raw: str) -> list[str]` — split the comment-stripped body on `\n\n`, drop unsafe paragraphs.
   - `_cap_paragraphs(paras: list[str], limit: int) -> str` — append whole paragraphs while the joined length stays `<= limit`. Join with `\n\n`. Never cut mid-paragraph.
   - `derive_persona_mini(raw: str) -> str` — `@voice` paragraph if present, safe, and within the cap after `_cap_paragraphs([that])`; else `_cap_paragraphs(_safe_paragraphs(raw))`; else `""`.

3. Change `load_persona_mini()` so it reads the same `ROOT / "persona.txt"` as the full loader. On `OSError` or empty derivation, return `_PERSONA_MINI_DEFAULT`. Do not write a file from the mini loader (the full loader already creates the template).

4. Update the module docstring and `_PERSONA_HEADER`, and the matching header in repo `shared/vector-ai/persona.txt`, so they say:

   - Chat uses the whole file except `#` comments.
   - Glances, greetings, and pickup use the paragraph under `# @voice` when that comment exists; otherwise every safe paragraph that fits in 800 characters.
   - Paragraphs that contain command tags, `Work Day`, `CALIBRATION`, or `User:` lines stay on the chat pack.
   - Restart vector-ai after an edit. The file that matters is the one next to the running `service.py` (`~/vector-pod/vector-ai/persona.txt` when installed).

5. Leave `AMBIENT_SYSTEM`, `_GREETING_SYSTEM`, and `_SENSOR_SYSTEM` as `PERSONA_MINI +` task text. Do not paste `PERSONA_FULL` there.

### Documentation references

- Loader to copy: `shared/vector-ai/prompt_voice.py` 41–87
- Prefix call sites to leave structurally unchanged: `soul/eye.py` ~42, `routes/greeting.py` ~30, `routes/sensor.py` ~21
- Tests to update, not delete: `shared/vector-ai/test_prompt_voice.py` 11–94
- Why mini exists: `AGENTS.md` “Persona packs (full vs mini)”
- Measured lengths: Phase 0 table (724 live, 324 at the 400-cut, 2669 safe backup prefix)

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_prompt_voice.py -q` green
- [ ] New test: monkeypatch `prompt_voice.ROOT` to a tmp dir whose `persona.txt` is one safe paragraph of at least 700 characters containing `BANDIT` and `SPROCKET`. `load_persona_mini()` returns that whole paragraph. A 400-cap implementation fails this test.
- [ ] New test: file with a voice paragraph plus a second paragraph containing `{{workAfternoon||yes}}` and `Work Day`. Mini contains neither `{{` nor `Work Day`. Full loader still returns both paragraphs.
- [ ] New test: file with a `User:` calibration paragraph and the word `CALIBRATION`. Mini contains neither.
- [ ] New test: `# @voice` paragraph is used even when an earlier safe paragraph exists.
- [ ] New test: no safe text → `_PERSONA_MINI_DEFAULT`.
- [ ] `len(PERSONA_MINI) <= 800` replaces the old `<= 400` assert.
- [ ] Existing startswith asserts for ambient, greeting, and sensor still pass.
- [ ] Repo `persona.txt` body is still the stock character plus the Work Day paragraph. It does not contain the production all-caps Bandit text.

### Anti-pattern guards

- Do not raise the cap by hard-slicing a paragraph at character 800. Drop the paragraph that does not fit.
- Do not read `persona.txt.backup`. It is not a second config file.
- Do not deploy by editing the snapshot. Code lands in `shared/vector-ai/prompt_voice.py`.

### Commit

One commit for this phase after the tests pass. Message about deriving the one-shot voice from `persona.txt` so glances match the runtime character.

---

## Phase 2 — Situational remember / soul tags, plus an explicit remember backstop

### What to implement

The parsers already work (`response_cleanup.py`). This phase only changes **when the model is told to emit them**, and stores an explicit remember phrase even if the model forgets the tag.

1. Leave `_build_memory_section` without `SOUL GROWTH`, `{{soulDream}}`, and `{{soulGoals||`. `test_soul_growth_line_absent_from_memory_section` stays.

2. Add a constant next to `_WORK_AWAITING_EMIT` in `prompt_assembly.py`, and append it inside `_build_context_note` only when the latest user text matches one of the triggers below. Copy the work-day pattern: the string rides on the **user turn**, not the system prefix.

   Triggers (case-insensitive, any one):

   | User text | Line tells him to |
   |-----------|-------------------|
   | contains `remember that` or `remember this` | emit `{{remember\|\|<fact>}}` once, or `{{remember-shared\|\|<fact>}}` if he says it is household |
   | contains `dream` and one of `consolidate`, `sleep on`, `dream about today`, `dream on` | emit `{{soulDream}}` once |
   | contains `dream` and one of `what did`, `last night`, `what'd you` | answer from the dream line already in desk continuity. **Do not** emit `{{soulDream}}` |
   | contains `goals` and one of `set your`, `your three`, `new goals` | emit `{{soulGoals\|\|one\|\|two\|\|three}}` once |

   If no trigger matches, append nothing. If several match, the line says: one tag only, remember wins over dream, dream-question emits no tag.

   Wording stays under ~80 words. No `SOUL GROWTH` heading (that string is the old absent-test key; a new heading avoids a fake pass). Use `SILENT TAG (do not speak it):`.

3. Add `maybe_apply_remember_from_user_text(text: str) -> None` beside `maybe_apply_afternoon_from_user_text` in `chat_flow.py`. Call it from `generate()` at that same site, before the LLM.

   Accept only:

   - `remember that <fact>`
   - `remember this: <fact>`
   - `remember this <fact>`

   Fact is the remainder of the utterance, stripped, length 8–200. Reuse `_remember_owner_face()` and `MEMORY.remember(...)` from `response_cleanup.py` so ownership matches tag remembers (single enrolled face → personal; stranger or several enrolled faces → shared). Skip exact duplicates (`remember` already returns none). Do not catch “remember my face” here; that phrase is an enrollment instruction already handled in the memory section.

4. Update `docs/FAQ.md` section “Chat tag `{{soulDream}}` — and prompt instructions” so it describes D8–D10 instead of “none are injected” / the removed `SOUL GROWTH` paragraph. Leave the 20h-vs-4h debounce sentences in the sections above it unchanged.

5. In `AGENTS.md`, in the LLM-tags bullet, add `{{soulDream}}` and `{{soulGoals\|\|…\|\|…\|\|…}}` to the stripped-tag list, and one sentence: they are taught on the user turn when the utterance asks, and an explicit “remember that …” is stored even without a tag.

### Documentation references

- Placement to copy: `_WORK_AWAITING_EMIT` in `shared/vector-ai/prompt_assembly.py` ~19–22 and the append in `_build_context_note` ~368–370
- Call site to copy: `maybe_apply_afternoon_from_user_text` in `shared/vector-ai/chat_flow.py` ~287–290
- Owner rules to call, not re-implement: `response_cleanup._remember_owner_face` and the `remember` / `remember_shared` branch in `extract_memory_commands`
- Absence test that must still pass: `shared/vector-ai/test_soul_growth.py` ~197–225
- Strip test that must still pass: same file ~170–194
- Old paragraph, do not restore it: `docs/plans/2026-07-29-soul-v2-persist-llm-view.md` 400–407
- Why a restore would be a no-op on this desk: Phase 0 log counts (866 taught, 0 applied)

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_soul_growth.py shared/vector-ai/test_prompt_assembly.py shared/vector-ai/test_service_modules.py -q` green
- [ ] `test_soul_growth_line_absent_from_memory_section` still asserts the three strings are absent
- [ ] New test: user text `Please remember that I take the late train` → context note contains `{{remember||` and `SILENT TAG`. User text `What's the weather?` → context note does not.
- [ ] New test: `What did you dream last night?` → context note does **not** contain `{{soulDream}}`. `Dream on today and consolidate.` → it does.
- [ ] New test: `remember that I take the late train` calls `MEMORY.remember` with that fact and does not require an assistant tag. `remember my face` does not insert a fact row. A 3-character capture is rejected.
- [ ] `rg "SOUL GROWTH" shared/vector-ai` stays limited to the absence test and historical docs, not `prompt_assembly.py`.

### Anti-pattern guards

- Do not teach these tags inside `PERSONA_MINI` or ambient/greeting/sensor prompts. Ambient speech goes through `_strip_for_speech`, which deletes `{{…}}` with no side effects.
- Do not treat `maybe_schedule_dream_job` returning true as “a dream ran”. Debounce can still skip. The backstop does not call the dream job.
- Do not emit both `{{soulGoals}}` and `{{soulDream}}` from the teach line. A dream job replaces all three goals (`_apply_dream_payload`).

### Commit

One commit after tests pass. Message about a situational silent-tag hint and a remember-that backstop.

---

## Phase 3 — Living identity line on the dream job

### What to implement

There is no identity field today (`soul.py` 85–88, `persist.py` payload, `llm_view`, PATCH). Design reserved it: `docs/superpowers/specs/2026-07-29-soul-melting-pot-design.md` GrowBot mapping (“optional short living string (later dream)”) and non-goal line “schema reserved”.

1. In `soul.py`, add `_WHO_MAX_LEN = 240`, `_who_i_am_becoming: str = ""`, a `who_i_am_becoming` property, and `set_who_i_am_becoming(text: str, *, force_save: bool = True) -> str` copied from `set_dreamed_about` (strip, slice to 240, same save behavior).

2. In `persist.py` `serialize_soul`, always write `"who_i_am_becoming"` (same as dream: write even when empty). In `load_into`, if the key is present, `set` via `_safe_str(..., 240)` onto `_who_i_am_becoming`. If the key is absent, leave the default. Do not change `SOUL_FORMAT`.

3. In `view.py`:

   - `llm_view` adds `who_i_am_becoming_on_this_desk` on the base object (so lite, full, and ops all have it). Value is the raw string.
   - `format_soul_for_prompt` adds, when non-empty, `- who I am becoming on this desk: {text[:200]}` next to the dream line. Lite greetings then hear it. Empty stays omitted.

4. In `growth_prompts.py` `DREAM_SYSTEM`:

   - Replace “Patch ONLY goals and dream fields” with: patch only goals, the dream, and `who_i_am_becoming_on_this_desk`. Do not rewrite persona, constitution, or invent a new character.
   - Add the key to the JSON schema. Instruct: one or two sentences, ≤240 characters, his stance toward the human on this desk. Use Sprocket only if the snapshot already shows Sprocket. Do not turn the line into a quest about a plush, colander, coaster, or other desk object. If nothing new happened, return the current line unchanged. An empty string means “leave it unchanged”.

5. In `_apply_dream_payload` (`growth.py` ~78): read `who_i_am_becoming_on_this_desk`. If the stripped value is non-empty, `set_who_i_am_becoming(..., force_save=False)` **before** the existing `save_now()`. If it is missing or blank, do not call the setter. Goals and dream keep today’s replace behavior. Extra keys (`persona`, `constitution`) stay ignored.

6. `run_reflect_goals_job` uses the same apply path. Empty-means-keep is what stops a goals-only run from erasing identity.

7. `SoulPatchRequest` gains optional `who_i_am_becoming_on_this_desk: Optional[str]`. PATCH still 400s when every field is absent. Apply through the setter, then the existing `save_now()`, and return `llm_view(detail="ops")`.

8. `docs/ENDPOINTS.md` PATCH bullet: add the new field. `docs/FAQ.md` dream answer: the job also updates the living-identity line when the model returns one.

### Documentation references

- Setter to copy: `set_dreamed_about` in `shared/vector-ai/soul/soul.py` 150–160
- Disk shape to extend: `serialize_soul` / `load_into` in `shared/vector-ai/soul/persist.py` 124–142 and 287–289
- Prompt block to edit in place: `shared/vector-ai/soul/growth_prompts.py` 26–39
- Apply order to extend: `_apply_dream_payload` in `shared/vector-ai/soul/growth.py` 78–124
- View lines to copy: dream lines in `format_soul_for_prompt` `shared/vector-ai/soul/view.py` 424–435
- PATCH shape to copy: `shared/vector-ai/routes/soul.py` 13–23 and the handler ~52–86
- Reservation text: melting-pot design GrowBot table (identity = later dream) and v2 plan out-of-scope line “Full GrowBot identity sentence patch”
- Why the prompt must reject object quests: Phase 0 goals and the 2026-10-27 dream

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_soul_growth.py shared/vector-ai/test_soul_persist.py shared/vector-ai/test_soul_view.py shared/vector-ai/test_service_modules.py -q` green
- [ ] Persist round-trip stores a 240-char-capped line. A payload without the key loads as `""` and does not crash. `soul_format` stays 2.
- [ ] Apply: non-empty identity replaces; `""` and a missing key keep the previous line; goals still replace; a `persona` key does not create a file and does not change `persona.txt`.
- [ ] `llm_view` includes `who_i_am_becoming_on_this_desk`. `format_soul_for_prompt` includes the prose line only when non-empty, for `detail="lite"` and `detail="full"`.
- [ ] `DREAM_SYSTEM` contains `who_i_am_becoming_on_this_desk` and `Do not rewrite persona`.
- [ ] Dream user message is still `llm_view(..., detail="full")` (`growth.py` `run_dream_job`). Grep the test that asserts `FULL SOUL SNAPSHOT` and keep it.
- [ ] PATCH with only the new field returns 200 and the ops view. PATCH `{}` stays 400.

### Anti-pattern guards

- Do not store `tomorrow_try` or `growth_note` as their own columns. They already have behavior.
- Do not inject unix timestamps into `llm_view`.
- Do not seed the line by copying `persona.txt` at startup. The first successful dream fills it. Until then the prompt omits the line.
- Do not run the dream job on `detail="lite"`.

### Commit

One commit after tests pass. Message about a durable who-I-am-becoming line that the dream job can update without rewriting persona.

---

## Phase 4 — Verification

Run from the repo root:

```bash
python3 -m pytest -q
```

Then:

```bash
rg -n "PERSONA_FULL" shared/vector-ai/soul/eye.py shared/vector-ai/routes/greeting.py shared/vector-ai/routes/sensor.py
rg -n "SOUL GROWTH" shared/vector-ai/prompt_assembly.py shared/vector-ai/runtime_guidance.py
rg -n "who_i_am_becoming" shared/vector-ai/soul shared/vector-ai/routes/soul.py
```

Expected:

- The three one-shot files still mention `PERSONA_MINI`, not a prepend of the full file.
- `prompt_assembly.py` has no `SOUL GROWTH` heading.
- `who_i_am_becoming` appears in soul state, persist, view, growth apply, and PATCH.

Confirm the working tree has no writes under `VectorIntelligence-Production_Snapshot/`.

Do not claim the live robot changed. This repo is source. The running brain uses whatever tree the supervisor starts. After deploy, restart vector-ai and check:

- `vector-ai.log` contains `[persona] loaded persona.txt (724 chars)` and a new mini log line whose text contains `BANDIT` and `SPROCKET`.
- A fresh ambient system prompt in the log starts with Bandit, not `You are Vector, a small desk robot`.
- `GET /v1/soul` includes `who_i_am_becoming_on_this_desk` (empty until the next dream, or until a PATCH).

### Anti-pattern sweep

- [ ] No new `shared/patches/*.py`
- [ ] No `DREAM_DEBOUNCE_S` edit
- [ ] No face-id merge
- [ ] No production `persona.txt` copied into the repo
- [ ] No dream apply path that writes `persona.txt`

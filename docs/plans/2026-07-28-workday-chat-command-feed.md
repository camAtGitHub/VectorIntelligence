# Work Day chat command feed (half-day yes / brain tags without chipper rebuild)

**Date:** 2026-07-28  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Goal:** Make half-day report-in reliable (“Yes, I’m here to work”) by (1) strengthening how the LLM is taught to emit brain tags and (2) adding a deterministic user-text backstop that applies Work Day commands from spoken/chat user text — all in vector-ai, **no chipper rebuild**.  
**Execute with:** `/do` (or sequential phase agents) after review.

---

## Phase 0 — Documentation discovery (complete)

### Problem (evidence-backed)

| Fact | Source |
|------|--------|
| Work tags are **parsed from assistant text only** and applied in `_apply_work_commands` | `response_cleanup.py` L148–184 |
| Stream path: each sentence → `clean_response` | `chat_flow.py` ~L291 |
| `on_afternoon_yes(date, now=)` only from `LATE_CHECK` or `NO_SHOW`; `on_afternoon_no` only `LATE_CHECK` | `behaviors/workday.py` L182–229 |
| `day_strip` for late: `"Work day: asked if user is working this afternoon (awaiting answer)."` — status only, no “emit tag” grammar | `continuity.py` L238–239 |
| `_SAVE_LINE` teaches memory tags in the system prefix; **no equivalent WORK line** | `prompt_assembly.py` L17–21, L120 |
| `persona.txt` L16 already has WORK emit prose (secondary teaching) | `persona.txt` |
| **No user-utterance yes/no parser** for workday | grep + discovery |
| Vision has the copy-ready **user-text backstop** pattern | `vision.py` + `chat_flow.py` L244–254 |
| quietMode is taught in Wire-Pod `openai_prompt`; work tags are **not** in that “complete set” | `shared/config/wirepod-apiConfig.json` |
| Unknown chipper tags are dropped; brain tags must be stripped in Python | `kgsim_cmds.go` `CmdParamToAction`; AGENTS.md |

**Root cause of flaky half-day yes:** product depends on the model emitting `{{workAfternoon||yes}}`. Teaching is thin (persona only, easy to miss vs openai_prompt command list; day_strip never says “emit tag”). There is no deterministic apply from the user’s spoken “yes”.

### Allowed APIs (use only these)

| API | Location | Use for |
|-----|----------|---------|
| `parse_work_commands(text) -> (cleaned, actions)` | `behaviors/workday.py` | Parse model tags |
| `WorkDayBehavior.on_afternoon_yes(local_date, now=0.0)` | same | Arm `late_working` |
| `WorkDayBehavior.on_afternoon_no(local_date)` | same | Decline |
| `WorkDayBehavior.on_pause` / `on_resume` | same | Pause path (already wired) |
| `pause_until_ts(local_dt, until_hhmm, tz)` | same | Pause times |
| `clean_response` / `_apply_work_commands` | `response_cleanup.py` | Existing model-tag feed |
| `prepare_messages` / `_build_context_note` / `_build_memory_section` | `prompt_assembly.py` | Teach tags |
| `ContinuityStore.day_strip(date)` / `load_workday` | `continuity.py` | Mode-aware prompt + backstop gate |
| `deps.BEHAVIOR_RUNTIME`, `deps._workday_cfg`, `deps._continuity` | `deps.py` | Access runtime (never import `service`) |
| `_extract_last_user_text(messages)` | `prompt_assembly.py` | User speech for backstop |
| `is_vision_intent` pattern | `vision.py` | **Copy** structure for workday affirm/decline regex |

### Anti-patterns (do not)

- Add `workAfternoon` / work tags to chipper `ValidLLMCommands` (forces rebuild; wrong layer).
- Put FSM logic in fat `routes/` or invent new POST speak endpoints outside the arbiter.
- Tear down `WORKING` via afternoon `no` (mode-guard already forbids).
- Rely only on persona.txt (header says no command grammar; openai_prompt may dominate).
- Skip strip-before-speech for any new brain tag family.
- Gate chipper night hours for workday policy.
- Require chipper rebuild for this feature.

### Architectural target

```text
User speech (STT) ──► /v1/chat/completions
                         │
                         ├─► [NEW] user-text backstop if mode late_check/no_show
                         │         → on_afternoon_yes/no (deterministic)
                         │
                         ├─► prepare_messages: WORK silent-tag line + sharper day_strip
                         │
                         └─► LLM stream → clean_response (existing model tags)
                                   → strip brain tags; pass body tags to chipper
```

Brain commands remain **vector-ai only**. Chipper stays mouth/body.

---

## Phase 1 — Strengthen LLM teaching (vector-ai prompt only)

### What to implement

1. **Add a WORK silent-tag line** (copy the shape of `_SAVE_LINE` in `prompt_assembly.py` L17–21).

   Suggested constant (name/style match neighbors):

   ```python
   _WORK_LINE = (
       "WORK DAY (silent tags — never speak them): "
       "if the user confirms they are working this afternoon or reporting in for half a day, "
       "include {{workAfternoon||yes}} once; if they decline afternoon work, "
       "{{workAfternoon||no}}; break until a clock time → {{workPause||until=HH:MM}} (24h); "
       "back to work → {{workResume}}."
   )
   ```

2. **Append `_WORK_LINE` only when Work Day is live** — same gate as day_strip inject (`_workday_cfg.enabled` and `BEHAVIOR_RUNTIME.workday is not None`). Prefer appending inside `_build_memory_section` **or** a small helper called from `prepare_messages` so the line sits in the **stable system prefix** next to `_SAVE_LINE` (not only in the volatile context note). Copy the gate from `_build_context_note` L224–228.

3. **Sharpen `day_strip` for `LATE_CHECK`** (and optionally open `NO_SHOW`) so the per-turn context is an instruction, not only status. Copy current strings from `continuity.py` L234–239 and extend, e.g.:

   - `LATE_CHECK`: keep status, then:  
     `If they affirm work / half-day, emit {{workAfternoon||yes}} once; if not, {{workAfternoon||no}}.`
   - Or append that sentence in `_build_context_note` when strip contains `awaiting answer` so `day_strip` stays a short ops string — **prefer inject in prompt_assembly** to avoid changing ops `GET /v1/behaviors/workday` semantics.  
     **Decision for implementer:** do **not** change the ops-facing `day_strip` string if tests assert exact text; add the emit instruction only in `_build_context_note` when `rec.mode == LATE_CHECK` (load via `_continuity.load_workday` or parse strip). Safest: in `_build_context_note`, after `strip = day_strip(...)`, if `"awaiting answer" in strip`, also append the emit-tag sentence.

4. **Leave `persona.txt` L16 as-is** (redundant teaching is fine). Do **not** expand command grammar further in persona (AGENTS.md / file header).

5. **Do not** edit `wirepod-apiConfig.json` openai_prompt for this phase (optional later; needs apply-wirepod-config + chipper **process** restart, not rebuild).

### Documentation references

- `_SAVE_LINE` pattern: `prompt_assembly.py` L17–21, L120  
- day_strip inject: `prompt_assembly.py` L224–246  
- Tag spellings: `docs/FSM-workday-companion.md` L166–174  
- FSM chat integration: `docs/FSM-implementation.md` §E  

### Verification checklist

- [ ] With workday **disabled**, prepared system prompt has **no** `_WORK_LINE` / no `workAfternoon` teach line from this feature.
- [ ] With workday **enabled** + stub runtime, system or memory section contains `{{workAfternoon||yes}}` instruction.
- [ ] When mode would be late_check, context note includes emit instruction (test with patched continuity / record).
- [ ] `python3 -m pytest shared/vector-ai/test_prompt_assembly.py -q` green.

### Anti-pattern guards

- Do not invent new tag names; use existing `workAfternoon` / `workPause` / `workResume`.
- Do not put WORK line into `PERSONA_MINI` (ambient/greeting must not get work tags).

---

## Phase 2 — Deterministic user-text backstop (copy vision pattern)

### What to implement

Make half-day yes **not** depend on the model. Copy the **vision backstop** idea: inspect **user** text before/around the LLM call; apply state in Python.

1. **New pure helpers** (prefer new small module or bottom of `behaviors/workday.py` to keep chat_flow thin):

   ```python
   def classify_afternoon_reply(user_text: str) -> Optional[str]:
       """Return 'yes' | 'no' | None from user utterance. Pure. No I/O."""
   ```

   Regex guidance (keep tight to avoid false arms):

   - **yes:** `\b(yes|yeah|yep|yup|affirmative)\b`, or phrases like `working (this )?afternoon`, `i('?m| am) (here )?(to )?work`, `half[- ]?day`, `report(ing)? in`, `i am working`, `i'll work`
   - **no:** `\b(no|nope|nah)\b`, `not working`, `day off`, `off (sick|today)`, `not today`
   - If both match, prefer **no** only when clear decline; otherwise require exclusive match — document in tests.
   - Empty / too long free-form → `None` (let LLM + tags handle nuance).

2. **Apply hook** (side effects, uses deps):

   ```python
   def maybe_apply_afternoon_from_user_text(user_text: str, *, now: float | None = None) -> Optional[str]:
       """If workday enabled and mode allows, apply yes/no from user text.
       Returns 'yes'|'no'|None for logging/tests."""
   ```

   Logic (copy mode guards from `on_afternoon_yes` / `on_afternoon_no`):

   - If `BEHAVIOR_RUNTIME` / `.workday` is None or not enabled → `None`.
   - `local_dt = datetime.now(_workday_cfg.tz)`; `date_s = ...`; load record.
   - Classify user text.
   - If `yes` and mode in (`LATE_CHECK`, `NO_SHOW`): `on_afternoon_yes(date_s, now=...)`.
   - If `no` and mode == `LATE_CHECK`: `on_afternoon_no(date_s)`.
   - Else ignore (log verbose).
   - **Do not** invent modes or bypass guards.

3. **Wire into `chat_flow.generate`** after `last_user_text` is known and **before** the LLM stream (near vision backstop L244). Order:

   1. Vision force-getImage (unchanged).
   2. `maybe_apply_afternoon_from_user_text(last_user_text)`.
   3. `prepare_messages` / stream (model can still emit tags; `on_afternoon_yes` is idempotent enough if already `late_working` — currently yes is ignored outside LATE_CHECK/NO_SHOW, so double-apply after arm is a no-op. Good).

4. Log: `[workday] user-text afternoon YES/NO for {date}` (mirror `[workday] afternoon YES` from tags).

5. **LLM still answers** conversationally — backstop does **not** short-circuit the reply (unlike vision’s early return). User still hears a normal acknowledgment.

### Documentation references

- Vision backstop: `vision.py` L1–49, `chat_flow.py` L244–254  
- Mode guards: `workday.py` L182–229  
- Apply-from-tags: `response_cleanup.py` L148–184  
- Design: structured tags preferred; NL is backstop — `docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md` §4.4  

### Verification checklist

- [ ] Unit tests for `classify_afternoon_reply`: yes/no/none cases, STT-ish short replies (`"yes"`, `"yeah i'm working"`, `"no day off"`).
- [ ] Unit test: mode `LATE_CHECK` + user `"yes"` → mode `LATE_WORKING` without any model tag.
- [ ] Unit test: mode `WORKING` + user `"yes"` → no mode change.
- [ ] Unit test: mode `LATE_CHECK` + user `"no"` → `NO_SHOW` + `late_check_done`.
- [ ] `test_behaviors.py` + new tests green.

### Anti-pattern guards

- Do not force a synthetic assistant tag through the stream (unnecessary; call hooks directly).
- Do not skip the LLM reply.
- Do not arm from arbitrary chat when not in late/no_show modes.
- Do not rebuild chipper.

---

## Phase 3 — Integration tests for the existing model-tag feed

### What to implement

Close the test gap discovery found: parse + FSM tested separately; **clean_response → on_afternoon_*** not covered with a live workday behavior.

1. In `test_service_modules.py` (copy `_patch_deps` + quietMode test L328–339):

   - Construct real `WorkDayBehavior` + `ContinuityStore` (tmp_path), assign `deps.BEHAVIOR_RUNTIME.workday = b`, enable `_workday_cfg`.
   - Seed record: mode `LATE_CHECK`, `late_check_asked_at=now`, date = today in cfg tz.
   - `out = clean_response("Sure thing. {{workAfternoon||yes}}")`
   - Assert: tag stripped; mode is `LATE_WORKING`; speech has no `{{`.

2. Same for `{{workAfternoon||no}}` → `NO_SHOW`.

3. In `test_prompt_assembly.py`: workday-enabled inject test (enable cfg + minimal runtime + continuity strip non-empty or WORK line present).

### Documentation references

- `test_clean_response_quiet_mode_tag` pattern  
- `test_behaviors.test_late_check_and_yes` for FSM expectations  
- `test_parse_work_commands` for tag spellings  

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_service_modules.py shared/vector-ai/test_prompt_assembly.py shared/vector-ai/test_behaviors.py -q` all green.

### Anti-pattern guards

- Do not hit live OpenRouter in tests.
- Do not import `service` in tests (use deps patching).

---

## Phase 4 — Docs (companion + AGENTS one-liner)

### What to implement

1. **`docs/FSM-workday-companion.md`** — troubleshooting table add rows:

   | Symptom | What to check |
   |---------|----------------|
   | Said “yes” but day never armed | Chat must hit vector-ai; look for `[workday] user-text afternoon YES` or `[workday] afternoon YES` in logs; mode must be `late_check` or `no_show` (`curl …/v1/behaviors/workday`) |
   | Model never emits tags | After this change, user-text backstop should still arm; teaching is in system WORK line when enabled |

2. Clarify in chat controls section: tags still preferred for pause/resume; **afternoon yes/no also accepted from natural short affirmations** when a late question is open.

3. Optional one line in `AGENTS.md` LLM tags row: user-text backstop for afternoon yes/no when mode allows (brain only).

### Verification checklist

- [ ] Companion docs mention both tag path and user-text path.
- [ ] No claim that chipper rebuild is required for this.

---

## Phase 5 — Verification (agent-completable)

### What to run

```bash
cd VectorIntelligence
python3 -m pytest shared/vector-ai/test_behaviors.py \
  shared/vector-ai/test_service_modules.py \
  shared/vector-ai/test_prompt_assembly.py -q
```

### Grep anti-pattern checks

```bash
# Must NOT add work tags to chipper ValidLLMCommands
rg -n "workAfternoon|workPause|workResume" wire-pod/chipper/pkg/wirepod/ttr/kgsim_cmds.go || true
# Expect no new matches in ValidLLMCommands

# Brain path still strips work tags
rg -n "parse_work_commands|_apply_work_commands|maybe_apply_afternoon|classify_afternoon" \
  VectorIntelligence/shared/vector-ai/
```

### Functional checklist (code-level)

- [ ] Model tag path still works via `clean_response`.
- [ ] User-text path arms without tags.
- [ ] Mode guards preserved.
- [ ] Tags never left in cleaned speech.
- [ ] Chipper Go untouched for this feature.

---

## Human / environment steps (agent cannot complete)

These remain **your** final steps after code lands and tests pass:

| # | Step | Why agent cannot finish |
|---|------|-------------------------|
| 1 | **Restart vector-ai** (stop-vector / start-vector, or supervisor restart) so new Python + persona load | Needs live process control on the robot host |
| 2 | Confirm `pod.conf`: `WORKDAY_ENABLED=1`, `BEHAVIORS_ENABLED` includes `workday`, correct `WORKDAY_TZ` | Runtime path (`~/vector-pod` or `%USERPROFILE%\vector-pod`) may differ from repo |
| 3 | Confirm runtime **persona** is the repo copy or still has the WORK line if customized | User-edited install persona may have dropped L16 |
| 4 | **Live late path:** miss morning window (or force `no_show` via clock), appear at desk with enrolled face, hear late question | Needs robot + face enrollment + real time windows |
| 5 | **Hey Vector**, say clearly: *“Yes, I’m here to work”* / *“Yes, working this afternoon”* | Real STT; agent cannot speak to the robot |
| 6 | Check: `curl -s http://127.0.0.1:8090/v1/behaviors/workday \| jq .mode` → expect `late_working` | Port may differ (`AI_PORT`) |
| 7 | Check logs for `[workday] user-text afternoon YES` and/or `[workday] afternoon YES` | Log path is install-specific |
| 8 | Optional: pause path still via chat tags / natural language depending on model | Pause has no user-text backstop in this plan (tags + teaching only) unless you extend Phase 2 later |
| 9 | If teaching still weak for **pause/resume only**, optionally re-apply Wire-Pod config so openai_prompt stays in sync — **process restart, not rebuild** | Out of Phase 1–2 scope; human ops |

### Out of scope (explicit)

- Chipper rebuild / new Go commands.
- Intent JSON half-day phrases.
- Calendar / Slack integration.
- Changing morning auto-arm (face-based) behavior.
- Full natural-language pause (“break until 2”) without tags (can be a follow-up backstop).

---

## Suggested implementation order

| Order | Phase | Risk | Completeness for “Yes I’m working” |
|-------|-------|------|-------------------------------------|
| 1 | Phase 2 (user-text backstop) | Low–medium | **Highest reliability** |
| 2 | Phase 1 (prompt teach) | Low | Better tags for pause + model path |
| 3 | Phase 3 (tests) | Low | Locks both paths |
| 4 | Phase 4 (docs) | None | Operator clarity |
| 5 | Phase 5 + human table | — | Live proof |

Phases 1–2 can be one PR; tests must cover both before claiming done.

---

## Success criteria

1. With mode `late_check`, user message `"yes I'm here to work"` arms `late_working` **without** any `{{workAfternoon||yes}}` in the model output (unit-tested).  
2. Model-emitted `{{workAfternoon||yes}}` still arms and is stripped from speech (unit-tested).  
3. No chipper source changes for this feature.  
4. Operator can verify with curl + one live robot exchange (human steps above).

---

## Files likely touched

| File | Change |
|------|--------|
| `shared/vector-ai/behaviors/workday.py` | `classify_afternoon_reply` (+ maybe apply helper) |
| `shared/vector-ai/prompt_assembly.py` | `_WORK_LINE` + gate; LATE_CHECK emit hint in context |
| `shared/vector-ai/chat_flow.py` | Call user-text backstop |
| `shared/vector-ai/response_cleanup.py` | No change unless helper shared (optional) |
| `shared/vector-ai/test_behaviors.py` | classify + mode apply tests |
| `shared/vector-ai/test_service_modules.py` | clean_response + live workday apply |
| `shared/vector-ai/test_prompt_assembly.py` | WORK line inject when enabled |
| `docs/FSM-workday-companion.md` | Troubleshooting |
| `AGENTS.md` | Optional one-liner |

**Not touched:** `wire-pod/chipper/**` (for this plan).

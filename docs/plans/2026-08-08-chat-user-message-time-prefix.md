# Chat user-message time + gap prefixes (cache-safe)

**Date:** 2026-08-08  
**Repo:** VectorIntelligence (`shared/vector-ai/`)  
**Goal:** Give the LLM human-relevant **wall-clock** and **inter-turn gap** tags as a **prefix on the latest USER message** in chat/conversation assembly — never on the system prompt (prompt-cache / stable-prefix design).  
**Execute with:** `/do` (or sequential phase agents) after review.  
**Out of scope:** chipper rebuild; Wire-Pod `Remember` schema changes; ambient/greeting/sensor one-shots; rewriting historical user turns in stored history; changing soul relative-time dialect for desk clocks.

---

## Phase 0 — Documentation discovery (complete)

### Problem (evidence-backed)

| Fact | Source |
|------|--------|
| Volatile time already rides on the **latest user** turn, not system | `prompt_assembly.py` `_build_context_note` docstring L228–235; `prepare_messages` L415–478 |
| Explicit cache rule: dynamic context **off** system so cached prefix is not invalidated every turn | same docstring; `docs/superpowers/plans/2026-07-26-prompt-packs-memory-desk-locations.md` anti-pattern |
| Context note already injects long wall clock: `"Current time is { %A %B %d, %Y, %I:%M %p } (morning\|…)"` | `prompt_assembly.py` L244–247 |
| **No** per-turn timestamp on stored chat messages | `chat_session.py` `{role, content}` only; Wire-Pod `RememberedChat` Role+Content only (`kgsim.go`) |
| **No** compact `"Sat 8 Aug, 9:14pm"` formatter; closest is long American form / `_wall_clock_safe` | `prompt_assembly.py`; `soul/view.py` L124–130 |
| **No** inter-turn gap-bucket table matching the product rules | discovery greps |
| `generate()` calls `soul.note_user_voice(now)` **before** `prepare_messages` | `chat_flow.py` L222–292 |
| Therefore `last_user_voice_at` is already **this turn** when assembly runs — unusable as “previous message time” unless snapshotted first | same |
| Session inject also may `note_user_voice` before `generate` | `routes/chat.py` ~L107–122 |
| Pure time helpers + unit-test pattern exist | `soul/view.py` + `test_soul_view.py`; private `_relative_time` in `prompt_assembly.py` |
| Storage stays clean today: context is **assembly-only**; Wire-Pod Remember / session store keep raw speech | `chat_flow` / `kgsim.go` Remember; session `append_message` |

### Product decisions (binding)

| # | Decision |
|---|----------|
| **D1** | Apply tags only as a **prefix on the latest user message** in `prepare_messages` (ephemeral prepared list). Do **not** mutate `chat_session` store content or chipper Remembered text. |
| **D2** | **Never** put these tags on the system message (`PERSONA` + Wire-Pod + memory section stays stable). |
| **D3** | `last_message_time` = best **prior-turn** unix clock, snapshotted **before** this turn’s `note_user_voice`. Prefer `max(soul.last_user_voice_at, soul.last_spoke_at, face_meta.last_convo_at)` via existing `_speech_ts_from_soul_and_prior` *before* voice is noted, or an explicit `prior_turn_ts` argument into `prepare_messages`. |
| **D4** | `first_message_in_thread` = **no prior non-system turns** in the inbound `messages` list (only the current user, optionally after system). If history is empty of user/assistant, treat as first → **wall clock only**. |
| **D5** | When not first but `prior_turn_ts <= 0` (restart, no clocks): fall back to **wall clock only** (same as first for tagging). Do not invent fake deltas. |
| **D6** | Tag selection (product rules): |
| | `if first` (or D5 fallback) → wall only |
| | `else if now.date() != last.date()` → wall + delta (if delta tag non-empty) |
| | `else` → delta only (if non-empty) |
| **D7** | Delta buckets (exact phrases; wrap each non-empty tag in `[…]` as below). `Δ = now - last_message_time` (seconds). |
| **D8** | Wall clock display string: **`Sat 8 Aug, 9:14pm`** — short weekday (`%a`), day **without** leading zero, short month (`%b`), 12h hour without leading zero, minutes, **lowercase** `am`/`pm`, comma after day-month group. Bracket form: **`[Time: Sat 8 Aug, 9:14pm]`**. |
| **D9** | Prefix placement: lines **before** the user’s spoken text, then a blank line optional or single newline; **then** existing `\n\n{context_note}` still appends after content for the latest user (do not fold these tags into the context-note prose blob). |
| **D10** | Overlap with context-note `"Current time is Saturday…"` is **accepted for v1** (context note also carries observations / soul strip / work gates). Do not remove context-note wall clock in this plan unless a follow-up explicitly consolidates. |
| **D11** | No chipper changes. Both voice (`/v1/chat/completions` + Remembered history) and session inject share `generate` → `prepare_messages`. |
| **D12** | Pure formatters take injectable `datetime` / `seconds` (tests freeze clocks; no wall clock in asserts of “now”). |

### Delta bucket table (D7 — implement exactly)

| Condition | Tag body (inside `[…]`) |
|-----------|-------------------------|
| `Δ < 5m` | *(no delta tag)* |
| `Δ < 30m` | `{N} minutes since last message` — `N = max(1, round(Δ/60))` |
| `Δ < 45m` | `Gap since previous message: half an hour ago` |
| `Δ < 60m` | `Gap since previous message: 45 minutes ago` |
| `Δ < 90m` | `Gap since previous message: an hour ago` |
| `Δ < 2h` | `Gap since previous message: 90 minutes ago` |
| `Δ < 12h` | `Gap since previous message: {N} hours ago` — `N = round(Δ/3600)`, clamp/display **2..11** |
| `Δ < 24h` | `Gap since previous message: Half a day ago` |
| `Δ < 7d` | `Gap since previous message: {N} days ago` — `N = floor(Δ/86400)`, **1..6** |
| `Δ < 14d` | `Gap since previous message: A week ago` |
| else | `Gap since previous message: {N} weeks ago` — `N = floor(Δ / (7*86400))`, min 2 after 14d |

**Rendered lines (examples):**

```text
[Time: Sat 8 Aug, 9:14pm]

hello Vector
```

```text
[12 minutes since last message]

what was I saying?
```

```text
[Time: Sun 9 Aug, 8:02am]
[Gap since previous message: Half a day ago]

morning
```

### Architectural target

```text
User speech / session text
        │
        ▼
generate()
  1. prior_ts = speech clocks BEFORE note_user_voice   ← NEW snapshot
  2. note_user_voice(now)                              ← existing
  3. prepare_messages(messages, face, *, now_dt, prior_turn_ts)
        │
        ├─ system: PERSONA + wirepod + memory     (UNCHANGED — cache-stable)
        │
        └─ latest user content:
             [Time: …]?            ← NEW prefix lines (rules D6–D8)
             [Gap …]? 
             {raw user text}
             \n\n[Context for you, Vector - …]   ← existing context_note
```

### Allowed APIs (use only these)

| API | Location | Use for |
|-----|----------|---------|
| `prepare_messages(messages, face)` | `prompt_assembly.py` | Extend signature with optional `now_dt`, `prior_turn_ts` (kwargs; default `datetime.now()` / derived) |
| `_build_context_note` | same | Leave as-is (D10); do not move time tags into system |
| `_speech_ts_from_soul_and_prior` | same | Pattern for combining soul + face_meta clocks — **call before** voice note, or pass snapshot from `generate` |
| `Soul.note_user_voice` / `last_user_voice_at` / `last_spoke_at` | `soul/soul.py` | Prior-turn source (snapshot only) |
| `MEMORY.touch_face` → prior `last_convo_at` | `memory.py` | Optional max into prior_ts (same as speech helper) |
| `generate` | `chat_flow.py` | Snapshot prior_ts → pass into `prepare_messages` |
| Pure test style | `test_soul_view.py`, `test_prompt_assembly.py` | Copy: fixed `datetime`, `SimpleNamespace` messages, `_patch_deps` |
| `datetime`, stdlib only for formatters | — | No new deps |

### Anti-patterns (do not)

- Put wall clock / gap tags on **system** content (breaks cache design).
- Persist stamped text into `chat_session.append_message` or depend on chipper `Remember` storing prefixes (double-prefix / frozen wrong Δ on replay).
- Use `last_user_voice_at` **after** `note_user_voice` without a prior snapshot (Δ always ~0).
- Invent OpenAI message fields (`timestamp` on Pydantic `Message`) unless a later plan owns storage — v1 needs none.
- Reuse soul `"It has been N since Y"` dialect for these tags — product table is different; keep soul helpers for soul strip only.
- Change Wire-Pod Go / patches (archived patches must not grow).
- Sleep real wall clock in tests; assert live `datetime.now()` strings.
- Strip or rewrite older history user turns in the prepared list (v1 = latest only).

---

## Phase 1 — Pure formatters + unit tests

### What to implement

1. **Add a small pure module** `shared/vector-ai/chat_time.py` (preferred over bloating soul/view — different phrase table and no soul import cycle risk). Export:

   ```python
   def format_wall_clock_human(dt: datetime) -> str:
       """Return 'Sat 8 Aug, 9:14pm' (no brackets)."""

   def format_gap_tag(delta_seconds: float) -> str | None:
       """Return bracket-ready body per D7 table, or None if Δ < 5 minutes."""

   def build_user_time_prefix(
       *,
       now_dt: datetime,
       prior_turn_ts: float | None,
       is_first_message: bool,
   ) -> str:
       """Return prefix string (0–2 lines ending with \\n if non-empty), no user body."""
   ```

2. **`format_wall_clock_human` implementation notes (copy portable day/hour style from `_wall_clock_safe`):**
   - `weekday = dt.strftime("%a")`  # Sat  
   - `day = str(dt.day)`  
   - `month = dt.strftime("%b")`  # Aug  
   - Hour: `h = dt.hour % 12 or 12`; minutes `dt.strftime("%M")`; `ampm = "am" if dt.hour < 12 else "pm"`  
   - Return `f"{weekday} {day} {month}, {h}:{minutes}{ampm}"`

3. **`format_gap_tag`:** implement D7 table with explicit thresholds in seconds (`5*60`, `30*60`, …). Document thresholds in a module comment next to the function (copy comment style from `soul/view.format_duration_units`).

4. **`build_user_time_prefix`:** encode D4–D6:
   - Resolve `last_dt` from `prior_turn_ts` when `> 0`.
   - `is_first_message or last_dt is None` → only `[Time: {wall}]\n` (or `\n\n` before body handled by caller).
   - Else compute `Δ`, `gap = format_gap_tag(Δ)`.
   - If `now_dt.date() != last_dt.date()`: include Time line; include Gap line if `gap`.
   - Else: Gap line only if `gap` (same calendar day, Δ&lt;5m → empty prefix).
   - Join lines with `\n`; return `""` if nothing.

5. **Tests:** new `shared/vector-ai/test_chat_time.py` — pure only, no deps/MEMORY.
   - Parametrize wall clock: fixed `datetime(2026, 8, 8, 21, 14)` → `"Sat 8 Aug, 9:14pm"`; morning `9:05` → `"9:05am"` (no leading zero on hour).
   - Parametrize gap buckets at boundaries: 4m59 → None; 5m → minutes; 29m; 30m half hour; 44m; 45m; 59m; 60m hour; 89m; 90m → 90 minutes; 2h–11h hours; 12h half day; 1–6 days; 7–13d week; 14d+ weeks.
   - `build_user_time_prefix`: first message → Time only; same day short gap → empty; same day 12m → minutes line only; cross-midnight → Time + Gap.

### Documentation references

- Portable day-of-month without leading zero: `soul/view.py` `_wall_clock_safe` L124–130  
- Pure duration test style: `test_soul_view.py` L31–95  
- Product table: Phase 0 D7–D8 above  

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_chat_time.py -q` green  
- [ ] No imports of `service`, `deps`, or `soul` from `chat_time.py`  
- [ ] `rg "It has been" shared/vector-ai/chat_time.py` empty (do not reuse soul dialect)  

### Anti-pattern guards

- Do not call `datetime.now()` inside formatters (caller passes `now_dt`).  
- Do not put brackets inside `format_wall_clock_human` / `format_gap_tag` inconsistently — either both return bare bodies and `build_user_time_prefix` wraps, or document one place that wraps (`[Time: …]` vs `[gap body]`).

---

## Phase 2 — Prior-turn snapshot in `generate` + `prepare_messages` wiring

### What to implement

1. **In `chat_flow.generate`**, **before** `soul.note_user_voice(now_voice)`:

   ```python
   prior_turn_ts = 0.0
   soul = getattr(deps, "SOUL", None)
   # Prefer reading clocks without mutating:
   # - soul.last_user_voice_at / last_spoke_at if soul
   # - else process_state._LAST_USER_VOICE_TS if present
   # Face last_convo can be merged later inside prepare via touch_face prior
   ```

   Then call `note_user_voice` as today.

2. **Extend `prepare_messages`:**

   ```python
   def prepare_messages(
       messages: list,
       face: Optional[dict],
       *,
       now_dt: datetime | None = None,
       prior_turn_ts: float | None = None,
   ) -> list:
   ```

   - `now_dt = now_dt or datetime.now()`  
   - If `prior_turn_ts is None`, compute via `_speech_ts_from_soul_and_prior(prior_meta, now_ts)` **after** `touch_face` — but **warn in comment**: when called from `generate` after `note_user_voice`, this is wrong unless `generate` always passes an explicit snapshot. **Require `generate` to pass `prior_turn_ts`.**  
   - Session/tests that call `prepare_messages` directly without kwargs: treat missing prior as `0.0` → first-message style wall clock only when no history, or empty prior clocks.

3. **`is_first_message` helper** (private in `prompt_assembly` or `chat_time`):

   ```python
   def _is_first_user_thread(messages: list) -> bool:
       n = 0
       for m in messages:
           if getattr(m, "role", None) == "system":
               continue
           if not getattr(m, "content", None):
               continue
           n += 1
           if n > 1:
               return False
       return True  # zero or one non-system turn (the current user)
   ```

4. **In the latest-user branch of the turn loop** (today L462–478), build:

   ```python
   prefix = build_user_time_prefix(
       now_dt=now_dt,
       prior_turn_ts=prior_turn_ts or 0.0,
       is_first_message=_is_first_user_thread(messages),
   )
   # string content:
   body = m.content
   if prefix:
       body = f"{prefix}\n{body}" if not body.startswith(prefix) else body
   content = f"{body}\n\n{context_note}" if is_last_user else ...
   ```

   Multimodal last user: prepend a text part `{"type":"text","text": prefix}` **before** existing parts, then append context_note part as today.

5. **`routes/chat.py` session path:** if it calls `note_user_voice` before `generate`, either remove the duplicate note (prefer single note inside `generate`) or snapshot prior in the route the same way. **Prefer single owner:** only `generate` notes voice (grep and drop redundant route note if safe — check tests in `test_chat_session.py`).

6. **Do not** change `append_message` storage shape.

### Documentation references

- `chat_flow.generate` L220–292 (order of voice note vs prepare)  
- `prepare_messages` L415–484  
- Cache-safe latest-user append L463–478  
- `_speech_ts_from_soul_and_prior` L203–225  
- Session route: `routes/chat.py` L99–129  

### Verification checklist

- [ ] With monkeypatched clocks: second turn ~12 minutes later → latest user content starts with `[12 minutes since last message]` (or rounded N)  
- [ ] First turn of empty history → starts with `[Time: …]` matching frozen `now_dt`  
- [ ] System message content does **not** contain `[Time:` or `minutes since last message`  
- [ ] `python3 -m pytest shared/vector-ai/test_prompt_assembly.py shared/vector-ai/test_chat_session.py -q` green after test updates  
- [ ] Debug log `PREPARED messages` still redacts images; prefixes may appear in redacted text (ok)  

### Anti-pattern guards

- Do not update `prior_turn_ts` after `note_user_voice` without keeping a local `prior_*` variable.  
- Do not write prefixes into session history or assistant transcript summary inputs in a way that re-feeds stamps (summary uses raw `messages` from request — good if storage stays clean).  
- Do not add `timestamp` to Pydantic `Message` in this phase.

---

## Phase 3 — Integration tests for `prepare_messages` contracts

### What to implement

1. **Extend `test_prompt_assembly.py`** (copy `_patch_deps` / `SimpleNamespace` patterns):

   | Test | Assert |
   |------|--------|
   | `test_user_time_prefix_first_message_wall_only` | one user msg; `prior_turn_ts=0`; content starts with `[Time:`; no `Gap since` / `minutes since` |
   | `test_user_time_prefix_same_day_gap_only` | two-turn history; prior same calendar day, Δ=12*60; starts with `[12 minutes since last message]`; no `[Time:` |
   | `test_user_time_prefix_cross_day_wall_and_gap` | prior previous calendar day; both Time and Gap lines |
   | `test_user_time_prefix_under_five_minutes_omits_gap` | same day Δ=120s → no time prefix lines (empty); context note may still have “Current time is” |
   | `test_user_time_prefix_not_on_system` | system content free of `[Time:` and gap phrases from this feature |
   | `test_user_time_prefix_not_on_prior_user_turns` | multi-turn: only **last** user has prefix; earlier user content unchanged plain text |

2. Pass `now_dt=` and `prior_turn_ts=` explicitly into `prepare_messages` in tests (do not rely on SOUL clocks unless testing generate path).

3. **Optional thin `generate` test** (in `test_chat_session.py` or prompt tests): monkeypatch `prepare_messages` to capture kwargs and assert `prior_turn_ts` is the value from before voice note — only if easy; else unit-test the snapshot block by extracting a tiny helper `snapshot_prior_chat_ts(soul) -> float`.

4. **Suggested helper** (keeps generate readable):

   ```python
   # prompt_assembly.py or chat_time.py
   def snapshot_prior_chat_ts(soul, process_voice_ts: float = 0.0) -> float:
       ...
   ```

### Documentation references

- Context-on-user tests: `test_prompt_assembly.py` L309–357, L494–501  
- Session multi-turn plain history: `test_chat_session.py` L110–118  
- Phase 0 D1, D6, D9  

### Verification checklist

- [ ] `python3 -m pytest shared/vector-ai/test_chat_time.py shared/vector-ai/test_prompt_assembly.py shared/vector-ai/test_chat_session.py -q`  
- [ ] `rg "\\[Time:" shared/vector-ai --glob '*.py'` only hits `chat_time.py`, `prompt_assembly.py`, tests  
- [ ] Existing soul / mood / workday prompt tests still green  

### Anti-pattern guards

- Do not assert exact full context-note strings in new tests (brittle); assert **prefix** and **system isolation** only.  
- Do not freeze real time with `time.sleep`.

---

## Phase 4 — Final verification

### What to implement

1. Run full unit suite from VectorIntelligence root:

   ```bash
   python3 -m pytest -q
   # or at least:
   python3 -m pytest shared/vector-ai/test_chat_time.py \
     shared/vector-ai/test_prompt_assembly.py \
     shared/vector-ai/test_chat_session.py \
     shared/vector-ai/test_soul_view.py -q
   ```

2. Anti-pattern greps:

   ```bash
   # Time tags must not be assigned into system content builders
   rg "build_user_time_prefix|format_gap_tag|\\[Time:" shared/vector-ai/prompt_assembly.py
   # Ensure PERSONA / memory section path does not call chat_time
   rg "chat_time" shared/vector-ai --glob '*.py'
   ```

3. Manual noticeability (human / runtime — agent cannot finish):
   - Restart vector-ai; first chat → model sees `[Time: …]` on user turn (debug log PREPARED).  
   - Rapid follow-up (&lt;5 min) → no gap/time prefix lines on user (context note still has current time).  
   - Wait ~15 minutes → `{N} minutes since last message`.  
   - After midnight → wall + gap.

### Documentation references

- AGENTS.md testing table L524–545  
- Cache rule: `prompt_assembly.py` L228–235  
- This plan Phase 0 decisions  

### Verification checklist

- [ ] Full `pytest -q` green (or documented failures unrelated)  
- [ ] System prepared message has no new time-prefix strings  
- [ ] Latest user prepared message follows D6/D7  
- [ ] No chipper / patch files touched  
- [ ] Working tree commit after implementation (AGENTS.md commit journal rule)  

### Anti-pattern guards

- Do not claim “done” without running the pytest commands above.  
- Do not document new env knobs unless a follow-up makes prefixes optional (`CHAT_TIME_PREFIX=0`) — **not required for v1**.

---

## Success criteria (noticeability)

- In normal multi-turn desk chat, the model can tell **same-day quick back-and-forth** (no tag / minutes) from **“you wandered off for hours/days”** (hour/day/week buckets) without reading unix timestamps.  
- First utterance of a thread carries an obvious **human wall clock**.  
- Prompt-cache-stable system prefix behavior is unchanged (persona + commands + memories only).

---

## Follow-ups (explicitly not this plan)

| Idea | Why deferred |
|------|----------------|
| Stamp **every** historical user turn in the prepared list | Needs durable per-message timestamps in chipper Remember and/or session store |
| Remove duplicate long “Current time is …” from context note | Product/UX consolidation; avoid scope creep |
| Optional env disable for prefixes | Not requested |
| Align soul `_relative_time` buckets with this table | Different audiences (desk continuity vs inter-turn gap) |

---

## Implementation order summary

| Phase | Deliverable | Primary files |
|-------|-------------|-----------------|
| 0 | Discovery + decisions (this doc) | `docs/plans/2026-08-08-chat-user-message-time-prefix.md` |
| 1 | Pure formatters + tests | `chat_time.py`, `test_chat_time.py` |
| 2 | Snapshot + prepare_messages prefix | `chat_flow.py`, `prompt_assembly.py`, maybe `routes/chat.py` |
| 3 | Integration contracts | `test_prompt_assembly.py`, optional session tests |
| 4 | Full verify + greps | — |

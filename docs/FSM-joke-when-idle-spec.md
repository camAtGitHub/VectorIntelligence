# Implementation Spec — `joke_idle` FSM (Joke / Question When Idle)

**Target executor:** a capable but context-limited AI coding agent (Claude Code, Cursor, Aider, etc.).
**Repo:** `camAtGitHub/VectorIntelligence`.
**You are the junior implementer. This document is binding.** Read the Project Bootstrap first, then execute tasks in order. Do not read files you are not told to read.

---

## 1. Assumptions & Clarifications

These were assumed while writing this spec. If any is false, STOP and flag it before coding.

- **A1.** The vector-ai brain runs on Python 3 + FastAPI; behaviors live in `shared/vector-ai/behaviors/`. Confirmed present: `types.py`, `runtime.py`, `config.py`, `presence.py`, `arbiter.py`, `continuity.py`, `workday.py`, `__init__.py`.
- **A2.** The LLM backend is OpenRouter via `httpx.AsyncClient`, exposed as `async def llm_chat_once(messages, *, model=None, temperature=1.0, ...)` in `service.py`. It accepts a per-call `model=` string. **This is the mechanism for model tiering.**
- **A3.** Deployment host is **Windows**. There is **no cron**. Scheduling is done by an `asyncio` background loop started from a FastAPI `@app.on_event("startup")` handler inside `service.py` (the same pattern already used by `_mood_loop` and `_behavior_clock_loop`). The machine may sleep; scheduling MUST be interval-based ("refill if ≥ N seconds since last successful refill"), never wall-clock ("refill at 03:00").
- **A4.** Durable state uses the existing `ContinuityStore` (`behaviors/continuity.py`), a SQLite wrapper with `self._lock`, `self._conn()` returning `sqlite3.Row`-factory connections, and an `_init_schema()` that runs `CREATE TABLE IF NOT EXISTS`. New tables are added there.
- **A5.** An embeddings capability exists or can be added for novelty checking. **If no embedding function is available in the codebase, the builder MUST implement novelty as a fallback (see TASK-05, Fallback clause) rather than inventing an embeddings client.** Do not add a new paid embeddings dependency without it being explicitly available.
- **A6.** `BehaviorContext.config` in the runtime is currently hardwired to `workday_cfg`. **Therefore the joke FSM MUST hold its own config object internally (passed at construction) and MUST NOT read its settings from `ctx.config`.** This is a known v1 limitation of the runtime.
- **A7.** The behavior is **default OFF** and safe for guests/holidays.

---

## 2. System Frame

**Problem statement:** When a known/present person has been at the desk but silent for a long while, Vector should occasionally speak first — a genuinely funny one-liner or a good conversation-starting question — then stay quiet for a long time.

**System boundaries:**
- IN SCOPE: a new behavior plugin `joke_idle`; a joke/question sourcing pipeline; an interval-driven background refill loop; new durable tables; config; tests.
- OUT OF SCOPE: modifying Work Day, ambient, greeting, or sensor loops; changing the chipper Go code; changing the arbiter or presence cache logic; changing `llm_chat_once`.

**Key architectural decisions:**
- **Serve/generate split.** Generation (heavy, slow, LLM) is fully decoupled from serving (instant, SQLite pop). The FSM `tick()` NEVER calls an LLM. Rationale: keeps proactive speech latency near zero and cannot stall the event loop or the presence tick.
- **Interval-guarded background loop, not cron.** Windows + sleep makes wall-clock scheduling unreliable; interval guard survives sleep and restarts.
- **Model tiering via OpenRouter.** Different `model=` strings for generate / critic / launder stages. A *different* model critiques than generates (cross-model judging beats self-judging).
- **Curated floor + generated renewal.** A shipped hand-curated seed pool provides quality; the LLM pipeline slowly renews it. Never-repeat de-dup sits in front of everything.

**Technology choices:** Python 3, FastAPI (existing), `httpx.AsyncClient` (existing, via `llm_chat_once`), SQLite (existing `ContinuityStore`), `asyncio` background task (existing pattern).

**Top-level module map:**
- `behaviors/joke_idle.py` — the FSM plugin (serve-side only). Owns modes, dwell/cooldown gating, identity gating, picks a pre-vetted line from the queue, sets speech-gated commit.
- `behaviors/joke_sources.py` — the sourcing/refill pipeline (generate → critic → novelty → bank) + queue pop + curated-file loader. Async.
- `behaviors/joke_seeds.txt` — shipped curated one-liners (data file).
- `behaviors/continuity.py` — MODIFY: add joke tables + accessor methods.
- `behaviors/config.py` — MODIFY: add `JokeConfig` + `load_joke_config`.
- `behaviors/runtime.py` — MODIFY: register the plugin behind enable+flag.
- `service.py` — MODIFY: start the interval refill loop on startup.
- `env-default` — MODIFY: document `JOKE_*` vars.
- `test_behaviors.py` (or `test_joke_idle.py`) — tests.

**Critical constraints (become ⚠️ in tasks):**
- ⚠️ `tick()` MUST NOT call any LLM or network. Serve is a pure SQLite/queue read.
- ⚠️ Cooldown/daily-count commits MUST be speech-gated (`on_speak_allowed`), never applied in `tick()` body.
- ⚠️ Stranger-reject commit is the ONE exception — it is applied in `tick()` body (it records a *silence*, not a spoken line).
- ⚠️ `need_identity=True` may be set only at the arm juncture, and only in `known` audience mode. Never every tick.
- ⚠️ The FSM reads config from its own `self.cfg`, NOT from `ctx.config` (see A6).
- ⚠️ Default OFF. Bad env must never crash vector-ai import (safe defaults).
- ⚠️ Do not modify Work Day / ambient / greeting / sensor / arbiter / presence logic.

---

## 3. Module Map & Dependency Graph

```
service.py ──starts──> joke_sources._joke_refill_loop (async, interval-guarded)
                              │ uses
                              ▼
joke_sources.py ──> llm_chat_once (service.py, DO NOT MODIFY)
                └─> continuity.ContinuityStore (joke tables)
                └─> joke_seeds.txt (curated data)

runtime.py ──registers──> joke_idle.JokeIdleBehavior
joke_idle.py ──pops line──> joke_sources.pop_line()  (sync, pure SQLite)
             ──reads──> self.cfg (JokeConfig)          (NOT ctx.config)
             ──commits──> continuity (joke_daily) via on_speak_allowed

config.py ──> JokeConfig, load_joke_config   (consumed by runtime + service)
```

**Strict build order:** config+continuity+seeds (contracts) → sources (uses them) → FSM (uses sources) → runtime wiring → service loop → tests.

**Shared-file risk:** `continuity.py`, `config.py`, `runtime.py`, `service.py`, `env-default` are each touched by exactly ONE task. No file is modified by two tasks. No circular deps.

---

## 4. Project Bootstrap Document (read this first, once)

> **Project:** VectorIntelligence gives an Anki Vector robot proactive "aliveness" via pluggable finite-state-machine behaviors. A thin Go body ("chipper") reports occupancy and speaks; a Python/FastAPI brain ("vector-ai") decides what to say. A shared `BehaviorRuntime` runs each behavior's `tick()` once per presence tick, then a `SpeechArbiter` allows at most one proactive line (gated by quiet-mode, recent-voice, and a global min-gap), highest `priority` winning.
>
> **You are adding one behavior: `joke_idle`** — when a person has been present-but-silent a long time, Vector occasionally says a pre-vetted joke or a conversation-starter question, then goes quiet for hours. All jokes/questions are generated offline by a background loop and banked in SQLite; the behavior only *serves* from that bank.
>
> **Key files (only touch what a task names):**
> - `behaviors/types.py` — READ ONLY. Defines `Behavior` protocol, `BehaviorContext`, `TickResult`, `PresenceSnapshot`, `FaceIdentity`.
> - `behaviors/workday.py` — READ ONLY reference example (do not modify).
> - `behaviors/runtime.py` — registration point (if-ladder).
> - `behaviors/continuity.py` — SQLite store.
> - `behaviors/config.py` — env→dataclass loaders.
> - `service.py` — FastAPI app; has `llm_chat_once` and startup loops. Huge file; only add a small startup loop.
> - `env-default` — documented env vars.
>
> **Architecture pattern:** plugin behaviors returning `TickResult` to a shared runtime + arbiter; offline generation, instant serve.
>
> **Invariants (always true):**
> 1. A behavior's `tick()` is cheap, synchronous, and never does network/LLM/camera work.
> 2. Side effects that assume the line was *heard* go in `TickResult.on_speak_allowed`, which the runtime calls ONLY after the arbiter allows the line.
> 3. Identity (named face) is expensive; request it only at real junctures via `need_identity`, never every tick.
> 4. Everything user-facing is default-OFF and must not crash import on bad env.
> 5. Behaviors read their own config from `self`, not from `ctx.config` (which is workday's).
>
> **Do NOT look at / modify:** the Go chipper code, `arbiter.py` internals, `presence.py` internals, ambient/greeting/sensor loops, or `llm_chat_once`'s body. Treat them as fixed contracts.

---

## 5. Contracts (binding types, copied from the real codebase)

From `behaviors/types.py` (READ ONLY — do not change):

```python
@dataclass
class FaceIdentity:
    face_id: int
    name: str
    is_stranger: bool = False

@dataclass
class PresenceSnapshot:
    occupied: bool = False
    face: Optional[FaceIdentity] = None
    face_ts: float = 0.0
    image_b64: Optional[str] = None
    image_ts: float = 0.0
    on_charger: bool = False
    voice_recent: bool = False
    updated_at: float = 0.0

@dataclass
class TickResult:
    speak: str = ""
    need_identity: bool = False
    debug: dict[str, Any] = field(default_factory=dict)
    on_speak_allowed: Optional[Callable[[], None]] = None

@dataclass
class BehaviorContext:
    now: float
    local_dt: Any            # tz-aware datetime
    presence: PresenceSnapshot
    quiet: bool
    config: Any              # ⚠️ this is workday_cfg — DO NOT use it for joke config
    identity_fresh: bool = False

class Behavior(Protocol):
    id: str
    priority: int
    def enabled(self) -> bool: ...
    def tick(self, ctx: BehaviorContext) -> TickResult: ...
```

`llm_chat_once` (in `service.py`, READ ONLY):
```python
async def llm_chat_once(messages: list, *, model: Optional[str] = None,
    temperature: float = 1.0, top_p: float = 0.95, seed: Optional[int] = None,
    timeout=None, max_tokens: Optional[int] = None, tag: str = "llm_chat_once") -> str
```

Runtime already calls `on_speak_allowed()` ONLY after `arbiter.allow()` returns ok. Confirmed. You do not need to implement that wiring.

---

## 6. Config surface (all `JOKE_`-prefixed)

| Env var | Default | Meaning |
|---------|---------|---------|
| `JOKE_ENABLED` | `0` | On/off switch (like `WORKDAY_ENABLED`). |
| `JOKE_AUDIENCE` | `known` | `known` = only muse to a recognised present person; `anyone` = muse to whoever is present. |
| `JOKE_PRIORITY` | `15` | Arbiter priority (background band; must lose to workday=80). |
| `JOKE_MIN_DWELL_S` | `1200` | Quiet+occupied seconds required before ARMED. |
| `JOKE_COOLDOWN_S` | `9000` | Enforced silence after a delivered line. |
| `JOKE_MAX_PER_DAY` | `4` | Hard daily cap on delivered lines. |
| `JOKE_QUESTION_RATIO` | `0.6` | Probability a served line is a question vs a joke. |
| `JOKE_IDENTITY_REJECT_COOLDOWN_S` | `1800` | `known` mode only: after a stranger, don't re-probe camera for this long. |
| `JOKE_TZ` | falls back to workday TZ or UTC | Own timezone for the day-boundary of the daily cap. |
| `JOKE_REFILL_INTERVAL_S` | `43200` | Min seconds between refill attempts (interval guard). |
| `JOKE_QUEUE_TARGET` | `50` | Refill tops the queue up to this many vetted lines. |
| `JOKE_QUEUE_LOW_WATERMARK` | `30` | Refill only runs once the queue drains to ≤ this (draws ~20 before regenerating). |
| `JOKE_MIN_SCORE` | `0.55` | Critic score below this → reject candidate. |
| `JOKE_NOVELTY_MIN` | `0.4` | Novelty below this (too similar to served history) → reject. |
| `JOKE_GENERATE_MODEL` | `""` (fallback to default `MODEL`) | Heavy model string for generation. |
| `JOKE_CRITIC_MODEL` | `""` (fallback to default `MODEL`) | DIFFERENT model string for scoring. |
| `JOKE_SEED_FILE` | `joke_seeds.txt` (next to module) | Curated one-liners file. |
| `JOKE_CURATED_RATIO` | `0.5` | Fraction of refill drawn from curated file vs generated. |

⚠️ Every loader must have a safe default and must not raise on malformed env. Unknown `JOKE_AUDIENCE` → coerce to `known`.

---

## 7. Task List

- **TASK-01** — Config: `JokeConfig` + `load_joke_config` in `config.py`.
- **TASK-02** — Continuity: joke tables + accessors in `continuity.py`.
- **TASK-03** — Curated seed data file `joke_seeds.txt`.
- **TASK-04** — Sourcing/serve: `joke_sources.py` queue pop + curated loader + prompt builders + JSON parsing (NO loop yet).
- **TASK-05** — Sourcing/refill: `refill_joke_queue()` + novelty + the async refill pipeline in `joke_sources.py`.
- **TASK-06** — FSM: `joke_idle.py` (`JokeIdleBehavior`).
- **TASK-07** — Runtime registration in `runtime.py`.
- **TASK-08** — Service startup loop in `service.py` + `env-default` docs.
- **TASK-09** — Tests.

(04 must precede 05; both in the same file but split so the pure/sync serve side is testable before the async pipeline exists.)

---

## 8. AI Instruction Packets

---
## TASK-01: Joke config

**Objective:** `JokeConfig` dataclass and `load_joke_config(env)` exist in `behaviors/config.py`, mirroring the existing `WorkdayConfig`/`load_workday_config` style.

**Bootstrap Context:**
Read `behaviors/config.py` fully (it is small) to copy the existing loader style (how it reads env, coerces types, handles TZ).
Key facts:
- There is already a `WorkdayConfig` dataclass and a loader that reads `WORKDAY_*` env keys with safe fallbacks.
- TZ handling already exists for workday — reuse the same approach for `JOKE_TZ`, falling back to the workday TZ (or UTC) if unset.
(Stop after you understand the loader pattern. Do not read runtime.py or workday.py.)

**Files to Create / Modify:**
- `behaviors/config.py` — MODIFY — add `JokeConfig` + `load_joke_config`.

**Inputs:** an env mapping (same source the existing loaders use — `os.environ` or a passed dict; match existing style).

**Outputs:** `JokeConfig` dataclass instance with every field from §6, plus `load_joke_config(env) -> JokeConfig`.

**Interface Contract (BINDING):**
```python
@dataclass
class JokeConfig:
    enabled: bool = False
    audience: str = "known"          # "known" | "anyone"
    priority: int = 15
    min_dwell_s: int = 1200
    cooldown_s: int = 9000
    max_per_day: int = 4
    question_ratio: float = 0.6
    identity_reject_cooldown_s: int = 1800
    tz: Any = None                   # tzinfo; resolved in loader
    refill_interval_s: int = 43200
    queue_target: int = 50
    refill_low_watermark: int = 30   # only refill once queue drains to <= this
    min_score: float = 0.55
    novelty_min: float = 0.4
    generate_model: str = ""         # "" => caller passes model=None => default MODEL
    critic_model: str = ""
    seed_file: str = "joke_seeds.txt"
    curated_ratio: float = 0.5

def load_joke_config(env) -> JokeConfig: ...
```

⚠️ CRITICAL CONSTRAINTS:
- Never raise on bad env. Wrap int/float parses; on failure use the default.
- Unknown/empty `JOKE_AUDIENCE` → `"known"`.
- `enabled` is true only when `JOKE_ENABLED == "1"`.

**Must NOT do:**
- Do not modify `WorkdayConfig` or its loader.
- Do not import runtime or service (circular import risk).

**Acceptance Criteria:**
- [ ] `load_joke_config({})` returns all-default config without raising.
- [ ] `JOKE_AUDIENCE=garbage` yields `audience == "known"`.
- [ ] `JOKE_ENABLED=1` yields `enabled is True`; anything else `False`.
- [ ] Malformed `JOKE_MIN_DWELL_S=abc` falls back to `1200`, no exception.

**Edge Cases:**
- Missing `JOKE_TZ` → use workday TZ if resolvable else UTC.
- Negative numbers → accept as-is (not this task's job to validate ranges) but must not crash.

**Test Requirements:** covered in TASK-09; ensure the code is import-safe with an empty env.

**Known Risks / Likely Mistakes:**
- Reading `JOKE_GENERATE_MODEL` and defaulting it to a hardcoded model name → WRONG. Default to `""` and let the caller pass `model=None` so `llm_chat_once` uses the configured default `MODEL`.
---

---
## TASK-02: Continuity joke tables

**Objective:** `ContinuityStore` gains three joke tables and typed accessor methods, using the same locking/connection pattern as the workday methods.

**Bootstrap Context:**
Read `behaviors/continuity.py` fully. Copy the exact pattern: `with self._lock, self._conn() as c:`, `CREATE TABLE IF NOT EXISTS` inside `_init_schema()`, `sqlite3.Row` access.
(Stop after you understand `_init_schema`, `load_workday`, `save_workday`, `mutate`. Do not read other files.)

**Files to Create / Modify:**
- `behaviors/continuity.py` — MODIFY — add tables + methods.

**Interface Contract (BINDING):**

Tables (add inside `_init_schema`):
```sql
CREATE TABLE IF NOT EXISTS joke_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- 'joke' | 'question'
    source     TEXT NOT NULL,      -- 'curated' | 'generated'
    score      REAL DEFAULT 0.0,
    text_hash  TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS joke_served (
    text_hash  TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    served_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS joke_daily (
    date               TEXT PRIMARY KEY,   -- 'YYYY-MM-DD' in JOKE_TZ
    count              INTEGER DEFAULT 0,
    last_spoke_at      REAL DEFAULT 0.0,
    last_reject_at     REAL DEFAULT 0.0
);
```

Methods:
```python
# queue
def joke_queue_len(self, kind: Optional[str] = None) -> int
def joke_queue_push(self, text: str, kind: str, source: str, score: float, text_hash: str, created_at: float) -> bool
    # returns False if text_hash already present in queue OR served (dedupe); no raise
def joke_queue_pop(self, kind: Optional[str] = None) -> Optional[dict]
    # atomically select highest-score row (optionally filtered by kind),
    # delete it from joke_queue, return {'text','kind','source','score','text_hash'}; None if empty
def joke_all_served_hashes(self) -> set[str]
def joke_all_served_texts(self) -> list[str]     # for novelty fallback

# serve accounting
def joke_mark_served(self, text_hash: str, text: str, kind: str, served_at: float) -> None

# daily / gating
def joke_load_daily(self, date: str) -> dict     # {'count','last_spoke_at','last_reject_at'} (zeros if absent)
def joke_commit_spoke(self, date: str, now: float) -> None   # count += 1, last_spoke_at = now (upsert)
def joke_mark_reject(self, date: str, now: float) -> None    # last_reject_at = now (upsert)
```

⚠️ CRITICAL CONSTRAINTS:
- `joke_queue_pop` MUST be atomic (select+delete under `self._lock` in one connection/transaction) so two callers can't serve the same row.
- `joke_queue_push` MUST dedupe against BOTH `joke_queue.text_hash` and `joke_served.text_hash`, returning `False` (not raising) on duplicate.
- Use the existing `self._lock` + `self._conn()` pattern. Do not open raw connections elsewhere.

**Must NOT do:**
- Do not alter `workday_days` or any existing method.
- Do not change `__init__` signature.

**Acceptance Criteria:**
- [ ] Fresh DB: `joke_queue_len() == 0`, `joke_load_daily('2026-01-01') == {'count':0,'last_spoke_at':0.0,'last_reject_at':0.0}`.
- [ ] `joke_queue_push` twice with same text → second returns `False`, len stays 1.
- [ ] Push text already in `joke_served` → returns `False`.
- [ ] `joke_queue_pop` returns the highest-score row and removes it; second pop returns the next; empty → `None`.
- [ ] `joke_commit_spoke` twice on same date → `count == 2`.

**Edge Cases:**
- Pop on empty queue → `None`, no raise.
- `kind` filter with no matching rows → `None`.

**Test Requirements:** covered in TASK-09 with a temp-file DB.

**Known Risks / Likely Mistakes:**
- Implementing pop as two separate `_conn()` calls (select then delete) → race. Must be one locked transaction.
- Forgetting the served-hash check in push → repeats leak back into the queue.
---

---
## TASK-03: Curated seed file

**Objective:** ship `behaviors/joke_seeds.txt` — a curated pool of one-liners and questions in a deadpan, dry-witted voice suitable for a small sarcastic desk robot.

**Files to Create / Modify:**
- `behaviors/joke_seeds.txt` — CREATE.

**Interface Contract (BINDING — file format):**
- UTF-8, one item per line.
- Blank lines and lines starting with `#` are ignored (comments).
- Each item line is `KIND<TAB>TEXT` where `KIND` is `joke` or `question`.
- No trailing tags, no `{{...}}`, no markdown.

**Content requirements:**
- ≥ 80 `joke` lines, ≥ 40 `question` lines.
- Jokes weighted toward **anti-jokes, observational one-liners, absurd literalism** (styles that survive repetition). NO puns about atoms/skeletons/scientists, NO "why did the X cross the Y", NO knock-knock, NO long setups. Each joke < 20 words, self-contained.
- Questions are genuine conversation-starters (light/curious/reflective), answerable by a person at a desk, never intrusive or medical/therapeutic. Each < 20 words.
- Keep it guest-safe and workplace-safe (no profanity, no politics, no personal-data assumptions).

**Acceptance Criteria:**
- [ ] File parses under the TASK-04 loader with zero malformed lines.
- [ ] Counts meet the minimums above.
- [ ] No banned joke categories present (spot-checkable).

**Known Risks / Likely Mistakes:**
- Drifting into pun jokes because they're easy → they rot fastest; stick to the specified registers.
---

---
## TASK-04: Sourcing — serve side (sync, no LLM)

**Objective:** `behaviors/joke_sources.py` exposes a synchronous serve function that returns a pre-vetted line and marks it served, plus a curated-file loader and the prompt/JSON helpers the refill task will use. NO async loop or LLM calls in this task.

**Bootstrap Context:**
Read the TASK-02 continuity contract (methods above) and the TASK-03 file format. Read `behaviors/config.py`'s `JokeConfig` (TASK-01).
(Do not read service.py or runtime.py.)

**Files to Create / Modify:**
- `behaviors/joke_sources.py` — CREATE (serve half + helpers).

**Interface Contract (BINDING):**
```python
import hashlib

def joke_hash(text: str) -> str:
    # normalize (strip, lower, collapse whitespace) then sha1 hex
    ...

def load_curated(path: str) -> list[dict]:
    # parse KIND<TAB>TEXT lines; skip blanks/#; return [{'text','kind'}]
    # never raise on a bad line — skip it and continue
    ...

def pop_line(store, cfg, question_ratio_roll: float) -> Optional[dict]:
    """Pure SQLite serve. Decide kind from question_ratio_roll < cfg.question_ratio
    => 'question' else 'joke'. Pop that kind; if empty, fall back to the other kind;
    if both empty, return None. On success, mark_served and return
    {'text','kind','source'}. NO network, NO LLM."""
    ...

# prompt builders + parsing (used later by TASK-05; pure functions, unit-testable now)
def build_generate_messages(seeds: list[str], want_jokes: int, want_questions: int) -> list[dict]: ...
def build_critic_messages(candidates: list[dict]) -> list[dict]: ...
def parse_json_array(raw: str) -> list[dict]:
    """Tolerant parse: strip markdown fences/prose, extract the first JSON array,
    return [] on failure (never raise)."""
    ...
def random_seeds(n: int) -> list[str]:
    """Return n concrete noun seeds from a built-in rotating list."""
    ...
```

⚠️ CRITICAL CONSTRAINTS:
- `pop_line` is synchronous and does ZERO network/LLM work — it is called from the FSM `tick()`.
- `pop_line` MUST call `store.joke_mark_served(...)` for the popped row before returning (so it's never served twice).
- `parse_json_array` and `load_curated` MUST NOT raise; return `[]`/skip on malformed input.

**Prompt requirements (embedded in build_* funcs):**
- Generate prompt: instruct STRICT JSON array only, no prose/fences; forbid the banned joke categories (atoms/skeletons/scientists, "why did the X", knock-knock, puns, long setups); require < 20 words; require the given seeds be used; schema `{"text","kind":"joke|question","style","seed"}`.
- Critic prompt: a *ruthless comedy editor / cynical* persona; rate each 0–1 on originality+surprise; anything resembling a common/known joke scores < 0.3; STRICT JSON only; schema `{"id","score","seen_before"}`.

**Must NOT do:**
- Do not import service.py at module top level (avoid import cycles / heavy import). If TASK-05 needs `llm_chat_once`, import it lazily inside the async function.
- Do not perform embeddings here.

**Acceptance Criteria:**
- [ ] `load_curated` on the TASK-03 file returns the expected counts, skips comments/blanks.
- [ ] `pop_line` returns a question when roll < ratio and questions exist; marks it served; a second pop of the same hash is impossible (it's gone from queue and in served).
- [ ] `pop_line` falls back to the other kind when the preferred kind's queue is empty; returns None only when both empty.
- [ ] `parse_json_array` extracts an array from text wrapped in ```json fences and from text with leading prose; returns `[]` on garbage.

**Edge Cases:**
- Curated file missing → `load_curated` returns `[]` (no raise); serving still works from generated queue if present.
- Both queues empty → `pop_line` returns `None` (FSM will stay silent).

**Known Risks / Likely Mistakes:**
- Making `pop_line` async or having it call an LLM → violates the serve/generate split. It MUST be sync and pure-DB.
- Regex-parsing JSON too greedily; prefer locating the first `[` … matching `]` and `json.loads`, falling back to `[]`.
---

---
## TASK-05: Sourcing — refill pipeline (async, LLM + novelty)

**Objective:** add `async def refill_joke_queue(store, cfg)` and the interval-guarded `async def _joke_refill_loop(store, cfg)` to `joke_sources.py`, implementing generate → critic → novelty → dedupe → bank, plus curated top-up. Uses `llm_chat_once` with per-stage models.

**Bootstrap Context:**
Read the `llm_chat_once` signature in §5. Read the TASK-04 helpers (`build_generate_messages`, `build_critic_messages`, `parse_json_array`, `random_seeds`, `joke_hash`) and TASK-02 store methods.
(Do not read the rest of service.py.)

**Files to Create / Modify:**
- `behaviors/joke_sources.py` — MODIFY (add async refill + loop; keep TASK-04 code).

**Interface Contract (BINDING):**
```python
async def refill_joke_queue(store, cfg) -> int:
    """Top the queue up to cfg.queue_target, but only when it has drained to or
    below cfg.refill_low_watermark (avoid regenerating after every single serve).
    Returns number of lines added.
    Mix: ~cfg.curated_ratio from curated file, remainder from LLM generation.
    Never raises out of the loop for a single bad batch — log and continue."""
    ...

async def _joke_refill_loop(store, cfg) -> None:
    """await asyncio.sleep(90) to let the stack settle, then loop:
       try: await refill_joke_queue(store, cfg)
       except Exception: log, continue
       await asyncio.sleep(cfg.refill_interval_s)"""
    ...
```

Pipeline for the generated portion:
1. `seeds = random_seeds(4)`.
2. `raw = await llm_chat_once(build_generate_messages(seeds, want_jokes, want_questions), model=(cfg.generate_model or None), temperature=1.0, tag="joke_gen")`.
3. `cands = parse_json_array(raw)`; skip batch if empty.
4. `scored = await llm_chat_once(build_critic_messages(cands), model=(cfg.critic_model or None), temperature=0.2, tag="joke_critic")`; `verdicts = parse_json_array(scored)`.
5. For each candidate matched to its verdict by `id`:
   - drop if `score < cfg.min_score` or `seen_before` truthy;
   - `h = joke_hash(text)`; drop if `h` in served hashes or already queued (push returns False handles this too);
   - `novelty(text, served_texts) >= cfg.novelty_min` else drop;
   - `store.joke_queue_push(text, kind, source='generated', score, h, now)`.

Curated portion:
- Load curated once; shuffle; for each until curated quota met: `h=joke_hash`; if not served/queued and passes novelty, push with `source='curated', score=1.0`.

Novelty:
```python
def novelty(text: str, served_texts: list[str]) -> float:
    """Return 0..1, higher = more novel."""
```
**Embeddings clause:** IF the codebase already exposes an embedding function, use cosine distance (1 - max similarity) as novelty. **Fallback (A5):** if no embeddings are available, implement novelty as a cheap lexical measure — e.g. `1 - max token Jaccard similarity` (or normalized trigram overlap) against served_texts. Choose ONE; do not add a new paid embeddings dependency. State in a comment which was used.

⚠️ CRITICAL CONSTRAINTS:
- This code runs OFF the tick path (background loop). It MUST NOT be called from `joke_idle.tick()`.
- Every LLM call and JSON parse is wrapped so a single failure logs and the loop continues; a bad night must never crash vector-ai or spin a tight error loop (respect the sleep on error).
- Interval guard: the loop's cadence is `cfg.refill_interval_s`; there is no wall-clock schedule.
- Do not exceed `cfg.queue_target`; stop when reached. Bound the number of generation batches per refill (e.g. max 12 batches) to avoid runaway API spend if the critic rejects everything.
- ⚠️ **LLM-down invariant: serving must NEVER depend on successful generation.** If generation fails or is unavailable, the curated pool alone must be able to top the queue up to `cfg.queue_target`. Fill the curated portion FIRST (or at minimum, always attempt curated top-up regardless of whether generation succeeded), so a night of failed LLM calls still leaves a non-empty, servable queue. `cfg.curated_ratio` is a *mix preference*, not a cap — when generated candidates are unavailable, curated may supply up to 100% of the fill.
- ⚠️ **Low-watermark trigger:** do not regenerate after every serve. `refill_joke_queue` should no-op (return 0) unless `store.joke_queue_len() <= cfg.refill_low_watermark`. With defaults (`queue_target=50`, `refill_low_watermark=30`), the queue drains ~20 lines before a refill runs.
- Import `llm_chat_once` lazily inside the function (`from service import llm_chat_once`) or via a passed-in callable to avoid import cycles. Prefer a passed-in callable if easy; otherwise lazy import with a clear comment.

**Must NOT do:**
- Do not modify `llm_chat_once`.
- Do not block the event loop with synchronous network calls — only use the async `llm_chat_once`.

**Acceptance Criteria:**
- [ ] With a stubbed `llm_chat_once` returning canned JSON, `refill_joke_queue` banks only candidates above `min_score` and novelty, dedupes, and stops at `queue_target`.
- [ ] Critic verdict `seen_before=true` → candidate dropped even if score high.
- [ ] A batch where the LLM returns garbage → skipped, no raise, refill still completes from curated/other batches.
- [ ] Max-batches bound is respected (no infinite loop when everything is rejected).
- [ ] `novelty` returns higher for a clearly different string than for a near-duplicate of a served text.
- [ ] **LLM-down:** with an injected `llm_chat_once` that always raises/returns garbage, `refill_joke_queue` still fills the queue to `queue_target` entirely from curated, no raise.
- [ ] **Watermark:** with queue length above `refill_low_watermark`, `refill_joke_queue` returns 0 and makes NO LLM calls; at/below it, it refills.

**Edge Cases:**
- `served_texts` empty → novelty returns max (1.0) for anything.
- `cfg.generate_model`/`critic_model` empty strings → pass `model=None` (default MODEL).
- Queue already above `refill_low_watermark` on entry → returns 0, makes no LLM calls.
- Curated pool exhausted by served-history dedupe AND generation down → queue may fall short of target; that is acceptable (serve degrades to whatever remains), but must not raise or loop forever.

**Test Requirements:** TASK-09; the LLM must be mockable — pass `llm_chat_once` as an injectable dependency (default to the real one) so tests supply a fake. Specify the fake's interface: `async def fake(messages, *, model=None, temperature=1.0, **kw) -> str`.

**Known Risks / Likely Mistakes:**
- Self-critiquing with the SAME model → the whole point is a DIFFERENT critic model; keep the two `model=` args distinct and configurable.
- Forgetting the per-refill batch cap → runaway spend.
- Matching candidates to verdicts by list position instead of `id` → mis-scored jokes; match by the `id` field.
---

---
## TASK-06: The FSM plugin

**Objective:** `behaviors/joke_idle.py` defines `JokeIdleBehavior` implementing the `Behavior` protocol: dwell/cooldown/daily-cap gating, audience+identity gating, serve a line from the queue, speech-gated commit. Pure and synchronous.

**Bootstrap Context:**
Read `behaviors/workday.py` ONLY for its *shape* — how it defines `id`, `priority`, `min_tick_interval`, `enabled()`, `tick()`, and how it uses `TickResult.on_speak_allowed` and `need_identity`. Read §5 contracts. Read TASK-04 `pop_line` and TASK-02 store methods and TASK-01 `JokeConfig`.
(Copy Work Day's patterns; do not modify workday.py.)

**Files to Create / Modify:**
- `behaviors/joke_idle.py` — CREATE.

**Interface Contract (BINDING):**
```python
JOKE_IDLE_ID = "joke_idle"

class JokeIdleBehavior:
    id = JOKE_IDLE_ID
    min_tick_interval: float = 30.0

    def __init__(self, cfg: JokeConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return self.cfg.enabled

    def tick(self, ctx: BehaviorContext) -> TickResult: ...
```

**`tick()` algorithm (BINDING order):**
1. `r = TickResult()`. Compute `date` = today's date string in `self.cfg.tz` from `ctx.now`.
2. `daily = store.joke_load_daily(date)`.
3. If `daily['count'] >= cfg.max_per_day` → return silence, `debug={'mode':'idle','reason':'capped'}`.
4. If `ctx.now - daily['last_spoke_at'] < cfg.cooldown_s` → silence, reason `'cooldown'`.
5. If not `ctx.presence.occupied` → silence, reason `'empty'`.
6. Dwell: `quiet_dwell = ctx.now - max(ctx.presence.updated_at, daily['last_spoke_at'])`. If `quiet_dwell < cfg.min_dwell_s` → silence, reason `'dwell_building'`.
   - (If `ctx.presence.voice_recent` is True, treat as not-quiet: return silence reason `'voice_recent'`. The arbiter also guards this, but bail early.)
7. Identity juncture:
   - If `cfg.audience == "known"`:
     - If not `ctx.identity_fresh`:
       - If `ctx.now - daily['last_reject_at'] < cfg.identity_reject_cooldown_s` → silence, reason `'id_reject_cooldown'`.
       - Else set `r.need_identity = True`; return with reason `'requesting_identity'` (no speak).
     - `face = ctx.presence.face`.
     - If `face is None or face.is_stranger`:
       - `store.joke_mark_reject(date, ctx.now)`  ⚠️ committed HERE, NOT speech-gated.
       - return silence, reason `'stranger_suppressed'`.
   - If `cfg.audience == "anyone"`: never set `need_identity`; `face = ctx.presence.face if ctx.identity_fresh else None`.
8. Serve: `line = pop_line(store, cfg, question_ratio_roll=random.random())`. If `line is None` → silence, reason `'no_line_available'` (no state change).
9. Optionally personalize: if `face` is not None and not stranger, you MAY prefix with the name naturally (e.g. `f"{face.name}, {text}"`) — keep it tasteful; skip if it reads awkwardly. Personalization is best-effort, never required.
10. `r.speak = line_text`.
11. `r.on_speak_allowed = lambda: store.joke_commit_spoke(date, ctx.now)`  ⚠️ cooldown/daily-count commit is speech-gated.
12. `r.debug = {'mode':'idle','reason':'spoke','kind':line['kind'],'source':line['source'],'who': face.name if face else 'unknown'}`.
13. return `r`.

⚠️ CRITICAL CONSTRAINTS:
- `tick()` does NO network/LLM/camera work. `pop_line` is pure SQLite.
- The daily-count/cooldown commit MUST be in `on_speak_allowed` (step 11), never applied in the body — otherwise a denied line silently burns the cooldown.
- The stranger-reject commit (step 7) is the ONE thing committed in-body, on purpose (records silence).
- Read all settings from `self.cfg`, never `ctx.config`.
- Set `need_identity=True` only in `known` mode at the arm juncture (step 7), never elsewhere, never every tick.

**Must NOT do:**
- Do not import joke_sources' async refill; only import `pop_line` (+ helpers).
- Do not modify workday.py, runtime.py, arbiter.py, presence.py.

**Acceptance Criteria:**
- [ ] Capped/cooldown/empty/dwell-not-met each return empty `speak` with the right `reason`.
- [ ] `anyone` mode never sets `need_identity`.
- [ ] `known` mode with stale identity sets `need_identity=True` once and does not speak.
- [ ] `known` mode with a stranger present marks reject (in body) and stays silent.
- [ ] Happy path sets `speak`, sets `on_speak_allowed`; calling that callback increments daily count and sets last_spoke_at.
- [ ] Empty queue → `no_line_available`, no state change.

**Edge Cases:**
- `ctx.presence.face is None` in `anyone` mode → serve a generic (non-personalized) line.
- Two consecutive ticks within `min_tick_interval` → the runtime already throttles plan; still, `tick()` must be idempotent (no state change unless it actually serves+commit-allowed).

**Test Requirements:** TASK-09 with a frozen clock and a fake store/context. MUST include a denied-speech test proving that when `on_speak_allowed` is NOT called, daily count/cooldown are unchanged.

**Known Risks / Likely Mistakes:**
- Committing cooldown in the body "to be safe" → the classic silent-desync bug this whole design avoids. Keep it in `on_speak_allowed`.
- Setting `need_identity` in `anyone` mode or every tick → burns face probes; forbidden.
- Reading `cfg` off `ctx.config` → that's workday's config; use `self.cfg`.
---

---
## TASK-07: Register in runtime

**Objective:** `BehaviorRuntime` constructs and registers `JokeIdleBehavior` when `"joke_idle"` is in `BEHAVIORS_ENABLED` and `joke_cfg.enabled`, following the existing workday if-ladder.

**Bootstrap Context:**
Read `behaviors/runtime.py` — specifically `BehaviorRuntime.__init__` (the `enabled = set(runtime_cfg.behaviors_enabled)` block and the workday registration) and how `store` is available.
(Do not read service.py.)

**Files to Create / Modify:**
- `behaviors/runtime.py` — MODIFY.

**Interface Contract (BINDING):**
- `BehaviorRuntime.__init__` gains a parameter `joke_cfg: Optional[JokeConfig] = None` (keyword, defaulted so existing callers don't break — but you WILL update the caller in TASK-08).
- Registration:
```python
from .joke_idle import JokeIdleBehavior, JOKE_IDLE_ID
...
if JOKE_IDLE_ID in enabled and joke_cfg is not None and joke_cfg.enabled:
    self.behaviors.append(JokeIdleBehavior(joke_cfg, store))
```

⚠️ CRITICAL CONSTRAINTS:
- Keep the existing workday registration exactly as-is.
- The `BehaviorContext` still passes `config=self.workday_cfg`; DO NOT change that (joke FSM uses `self.cfg`). Do not attempt to make `ctx.config` polymorphic in this task.
- `joke_cfg` defaulting to `None` must be safe (no registration, no crash).

**Must NOT do:**
- Do not change how `ctx` is built, how the arbiter is called, or how `on_speak_allowed` is invoked (already correct).
- Do not reorder existing behaviors.

**Acceptance Criteria:**
- [ ] With `joke_cfg=None` runtime constructs and behaves exactly as before (workday-only).
- [ ] With `BEHAVIORS_ENABLED` containing `joke_idle` and `joke_cfg.enabled=True`, `self.behaviors` includes a `JokeIdleBehavior`.
- [ ] With the flag off, it is absent.

**Known Risks / Likely Mistakes:**
- Registering when the id is in the list but `enabled` is false → must require BOTH.
---

---
## TASK-08: Service wiring — config load, runtime arg, refill loop, env docs

**Objective:** `service.py` loads `JokeConfig`, passes it to `BehaviorRuntime`, and starts the interval-guarded refill loop on startup; `env-default` documents all `JOKE_*` vars.

**Bootstrap Context:**
Read in `service.py`: (a) where `BehaviorConfig`/`WorkdayConfig` is loaded and where `BEHAVIOR_RUNTIME = BehaviorRuntime(...)` is constructed (around the `WorkdayConfig` load and line ~189); (b) the existing `@app.on_event("startup")` handlers and the `_mood_loop`/`_behavior_clock_loop` pattern (lines ~98–120, ~618–626). Copy that startup-loop pattern exactly.
Read `env-default` for the comment style used for `WORKDAY_*`.
(Do not read unrelated endpoints.)

**Files to Create / Modify:**
- `service.py` — MODIFY (config load + runtime arg + startup loop; ~15 lines total).
- `env-default` — MODIFY (documentation of `JOKE_*`).

**Interface Contract (BINDING):**
- Load config near the other config loads:
  ```python
  from behaviors.config import load_joke_config
  JOKE_CFG = load_joke_config(os.environ)
  ```
- Pass to the runtime constructor:
  ```python
  BEHAVIOR_RUNTIME = BehaviorRuntime(..., joke_cfg=JOKE_CFG)
  ```
  (add the keyword arg to the existing call; do not remove existing args.)
- Startup loop:
  ```python
  from behaviors.joke_sources import _joke_refill_loop

  @app.on_event("startup")
  async def _start_joke_refill_loop() -> None:
      if JOKE_CFG.enabled:
          asyncio.create_task(_joke_refill_loop(store=CONTINUITY_STORE, cfg=JOKE_CFG))
  ```
  Use whatever the existing store variable is actually named (find it where `BehaviorRuntime` is built — reuse the SAME store instance; do not construct a second `ContinuityStore`).

⚠️ CRITICAL CONSTRAINTS:
- Reuse the EXISTING `ContinuityStore` instance the runtime uses. Do NOT create a second store (two SQLite handles to the same file is fine but wasteful and risks confusion; reuse the one already there).
- The loop starts ONLY when `JOKE_CFG.enabled`. When off, zero background work, zero LLM calls.
- Two guards coexist and do NOT conflict: the loop wakes every `refill_interval_s`, but `refill_joke_queue` itself no-ops unless the queue has drained to ≤ `refill_low_watermark`. So a wake with a full queue simply returns 0 and makes no LLM calls.
- Do not block startup; `asyncio.create_task` and return, exactly like `_mood_loop`.
- Do not modify `llm_chat_once`, mood loop, or clock loop.

**env-default additions (BINDING — documented, mostly commented-out with defaults shown):**
```
# --- Joke / question when idle (default off) ---
# BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=0
# JOKE_AUDIENCE=known            # known | anyone
# JOKE_PRIORITY=15
# JOKE_MIN_DWELL_S=1200
# JOKE_COOLDOWN_S=9000
# JOKE_MAX_PER_DAY=4
# JOKE_QUESTION_RATIO=0.6
# JOKE_IDENTITY_REJECT_COOLDOWN_S=1800
# JOKE_TZ=Australia/Sydney
# JOKE_REFILL_INTERVAL_S=43200
# JOKE_QUEUE_TARGET=50
# JOKE_QUEUE_LOW_WATERMARK=30     # refill only when queue drains to <= this
# JOKE_MIN_SCORE=0.55
# JOKE_NOVELTY_MIN=0.4
# JOKE_GENERATE_MODEL=            # blank => uses LLM_MODEL; set a heavy model for better jokes
# JOKE_CRITIC_MODEL=              # blank => uses LLM_MODEL; set a DIFFERENT model than generate
# JOKE_CURATED_RATIO=0.5
# JOKE_SEED_FILE=joke_seeds.txt
```

**Acceptance Criteria:**
- [ ] With `JOKE_ENABLED=0`, service starts, no refill task runs, runtime has no joke behavior.
- [ ] With `JOKE_ENABLED=1` + `joke_idle` in `BEHAVIORS_ENABLED`, the refill task is created once and the behavior is registered.
- [ ] Only one `ContinuityStore` instance is used.
- [ ] `env-default` documents every `JOKE_*` var.

**Known Risks / Likely Mistakes:**
- Creating a second `ContinuityStore` → use the existing one.
- Starting the loop unconditionally → must gate on `JOKE_CFG.enabled`.
- Passing a positional arg and breaking the existing `BehaviorRuntime(...)` call → add `joke_cfg=` as a keyword.
---

---
## TASK-09: Tests

**Objective:** deterministic, robot-free, network-free tests for config, continuity, serve, refill (mocked LLM), and the FSM (frozen clock), runnable via the project's existing test entrypoint.

**Bootstrap Context:**
Read `shared/vector-ai/test_behaviors.py` for the existing test style/runner (how it constructs stores/contexts, how it freezes time). Match it. If it's a plain `python3 test_behaviors.py` runner, add tests in the same style; a dedicated `test_joke_idle.py` invoked from the same runner is acceptable.

**Files to Create / Modify:**
- `shared/vector-ai/test_behaviors.py` — MODIFY (or CREATE `test_joke_idle.py` imported by it).

**Test Requirements (each a concrete case):**
1. **Config:** empty env → all defaults, no raise; bad `JOKE_AUDIENCE` → `known`; malformed numeric → default.
2. **Continuity:** push dedupe (queue+served), atomic pop highest-score, `joke_commit_spoke` increments, `joke_mark_reject` sets timestamp, fresh daily zeros.
3. **Serve:** `pop_line` respects `question_ratio_roll`, marks served, falls back across kinds, returns None when empty; `parse_json_array` handles fenced/prefixed/garbage input.
4. **Refill (mocked LLM):** inject a fake async `llm_chat_once` returning canned generate+critic JSON; assert only above-threshold, novel, non-duplicate candidates are banked; `seen_before` dropped; garbage batch skipped without raise; stops at `queue_target`; batch cap respected.
5. **FSM happy path:** frozen clock, occupied, dwell met, `anyone` mode, queue seeded → `speak` non-empty, `on_speak_allowed` set; invoking it increments daily count and blocks via cooldown next tick.
6. **FSM denied-speech (CRITICAL):** simulate the line NOT being spoken (do not call `on_speak_allowed`) → daily count and `last_spoke_at` unchanged (proves speech-gated commit).
7. **FSM identity:** `known`+stale → `need_identity=True`, no speak; `known`+stranger → reject marked (in body), no speak; `known`+reject cooldown active → silent without probing; `anyone` → never `need_identity`.
8. **Feature flag off:** `enabled()` False → runtime doesn't register / behavior never speaks.

⚠️ CRITICAL CONSTRAINTS:
- No real network, no real robot, no real LLM. LLM is injected/mocked.
- Time is injected (pass `now` explicitly); no `time.sleep`, no real wall clock in assertions.
- Use a temp-file (or `:memory:` won't work across connections — use a temp file) SQLite DB per test.

**Acceptance Criteria:**
- [ ] All cases above present and passing via the existing runner.
- [ ] The denied-speech case (6) is present and asserts no state advance.
- [ ] Tests do not hit the network (verifiable by the mock being the only LLM path).

**Known Risks / Likely Mistakes:**
- Using `sqlite3 ':memory:'` with the store's per-call `_conn()` → each connection gets a fresh empty DB. Use a real temp file path.
- Testing refill against the real `llm_chat_once` → must inject the fake.
---

---

## 9. Risk Register

| ID | Risk | Mitigation encoded in |
|----|------|----------------------|
| R1 | `tick()` accidentally does LLM/network work → stalls event loop & presence tick | TASK-04/06 constraints; serve is pure SQLite `pop_line`. |
| R2 | Cooldown/daily-count committed in `tick()` body → denied lines silently burn cooldown ("missed check-ins") | TASK-06 step 11 + denied-speech test (TASK-09 case 6). |
| R3 | `need_identity` set every tick / in `anyone` mode → burns face probes, degrades firmware/network | TASK-06 step 7 constraints. |
| R4 | Reading joke settings from `ctx.config` (which is workday's) | A6 + TASK-06/07 constraints. |
| R5 | Windows sleep makes wall-clock scheduling miss refills | A3 + TASK-05 interval guard. |
| R6 | Sync LLM/`requests` call blocks the async loop | Only `llm_chat_once` (async httpx) is used; TASK-05 constraint. |
| R7 | Self-critique gives inflated scores → clichés pass | TASK-05: distinct `critic_model`; cross-model judging. |
| R8 | Runaway API spend if critic rejects everything | TASK-05 per-refill batch cap. |
| R9 | Repeats leak (queue not deduped vs served) → robot feels dumb | TASK-02 push dedupes vs queue+served; TASK-05 novelty gate. |
| R10 | Second `ContinuityStore` instance created in service.py | TASK-08 constraint: reuse existing store. |
| R11 | Non-atomic `pop_line` serves a line twice under concurrency | TASK-02 atomic pop constraint. |
| R12 | Import cycle service↔joke_sources | TASK-04/05: lazy/injected `llm_chat_once`, no top-level service import. |
| R13 | Curated file drifts into pun jokes that rot | TASK-03 banned-category list. |
| R14 | Embeddings dependency invented where none exists | A5 + TASK-05 lexical fallback clause. |
| R15 | Serving silently depends on the LLM; a night of failed generation empties the queue | TASK-05 LLM-down invariant: curated alone tops up to target. |
| R16 | Regenerating after every serve → constant API spend | TASK-05 low-watermark: only refill when queue ≤ watermark. |

## 10. Build order (one line)

TASK-01 → TASK-02 → TASK-03 → TASK-04 → TASK-05 → TASK-06 → TASK-07 → TASK-08 → TASK-09. Each task is self-contained given the named files; do not read beyond what each packet lists.

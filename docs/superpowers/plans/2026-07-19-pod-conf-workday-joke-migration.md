# Plan: Migrate WORKDAY + JOKE config from `.env` → `pod.conf`

**Date:** 2026-07-19  
**Goal:** Runtime behavior knobs (`WORKDAY_*`, `JOKE_*`, shared speech/runtime keys) live in **`pod.conf`**. **`vector-ai/.env` stays OpenRouter/LLM only**. Install/setup scripts **must not wipe** user keys already in `pod.conf`.  
**Status:** Ready for implementation (`/do` or subagent-driven).  
**Depends on (done):** generic `load_pod_conf` + child-env forward in `shared/supervisor.py` (commit `45e7f29`); pytest conversion of unit suites + root `pytest.ini`.

---

## Phase 0 — Documentation discovery (consolidated)

### Sources consulted

| Source | What it establishes |
|--------|---------------------|
| `shared/supervisor.py` (~L85–216, ~L840–890) | `load_pod_conf`, `apply_supervisor_pod_conf`, `merge_pod_conf_into_env`, `_vectorai_env`, `_chipper_env` |
| `shared/test_supervisor_pod_conf.py` | Parser/apply/merge unit tests |
| `shared/vector-ai/behaviors/config.py` | `load_runtime_config` / `load_workday_config` / `load_joke_config` env key lists |
| `shared/vector-ai/service.py` L78–80, L187–189 | `load_dotenv` then loaders from `os.environ` |
| `shared/vector-ai/env-default` L38–79 | WORKDAY/JOKE currently documented + defaulted in `.env` template |
| `AGENTS.md` “pod.conf vs .env” | Preferred split: FSM knobs in pod.conf |
| `docs/FSM-workday-companion.md` | User docs still say edit `.env` |
| `docs/FSM-joke-when-idle-spec.md` §6 | Full `JOKE_*` surface |
| `windows/WirePodPaths.ps1` | `Read-PodConf` exists; **no** safe write/merge |
| `windows/install.ps1` ~L521–543 | Reads ports then **`Set-Content` only WEB/AI** → **clobbers** |
| `linux/install.sh` ~L256–269 | Same: `printf 'WEB_PORT…AI_PORT…' > pod.conf` → **clobbers** |
| `windows/setup-companion.ps1` ~L128–137 | Fixed 5-line rewrite → **clobbers** |
| `windows/apply-wirepod-config.ps1` ~L87–102 | Rebuilds whitelist of keys → **clobbers** unknown keys |

### Allowed APIs (copy these; do not invent)

**Python (supervisor) — already present:**

```text
load_pod_conf(path: Path | None = None) -> dict[str, str]
apply_supervisor_pod_conf(conf: dict[str, str]) -> dict
merge_pod_conf_into_env(conf, base=None, *, conf_wins=True) -> dict
POD_CONF  # module-level dict from disk
```

**Python (vector-ai) — already present; no signature changes required for basic migration:**

```text
load_runtime_config(env: Optional[Mapping[str, str]] = None) -> RuntimeConfig
load_workday_config(env: Optional[Mapping[str, str]] = None) -> WorkdayConfig
load_joke_config(env: Optional[Mapping[str, str]] = None) -> JokeConfig
```

Loaders already take an env **mapping**; they do not know about files. Supervisor injects `pod.conf` into the vector-ai child env; `load_dotenv` does **not** override existing keys → **pod.conf wins over `.env` for the same key**.

**PowerShell (Windows) — exists:**

```text
Get-PodConfPath
Read-PodConf   # returns Hashtable of KEY→value; ignores comments
```

**PowerShell — must be ADDED (this plan):**

```text
Update-PodConf -Set @{ KEY = "value"; ... }   # merge upsert; never truncate foreign keys
```

**Linux — must be ADDED:** small shell helper or inline merge that upserts keys without truncating the file.

### Env keys to relocate (authoritative lists)

**Runtime / shared (from `load_runtime_config`):**

| Key | Default |
|-----|---------|
| `BEHAVIORS_ENABLED` | `workday` |
| `FACE_CACHE_MAX_AGE_S` | `120` |
| `IMAGE_CACHE_MAX_AGE_S` | `45` |
| `SPEECH_MIN_GAP_S` | `90` |
| `SPEECH_SUPPRESS_AFTER_VOICE_S` | `120` |

**Work Day (`load_workday_config`):**

| Key | Default |
|-----|---------|
| `WORKDAY_ENABLED` | off |
| `WORKDAY_TZ` (fallback `TZ`) | `UTC` |
| `WORKDAY_START_BEGIN` | `09:00` |
| `WORKDAY_START_END` | `10:30` |
| `WORKDAY_AWAY_WINDOW_BEGIN` | `09:30` |
| `WORKDAY_END` | `18:00` |
| `WORKDAY_POKE_INTERVAL_S` | `5400` |
| `WORKDAY_AWAY_S` | `1800` |
| `WORKDAY_LATE_CHECK_TIMEOUT_S` | `900` |
| `WORKDAY_REID_AFTER_AWAY_S` | `3600` |
| `WORKDAY_PRIORITY` | `80` |
| `WORKDAY_IDENTITY_REJECT_COOLDOWN_S` | `600` |

**Joke idle (`load_joke_config`):**

| Key | Default |
|-----|---------|
| `JOKE_ENABLED` | off |
| `JOKE_AUDIENCE` | `known` |
| `JOKE_PRIORITY` | `15` |
| `JOKE_MIN_DWELL_S` | `1200` |
| `JOKE_COOLDOWN_S` | `9000` |
| `JOKE_MAX_PER_DAY` | `4` |
| `JOKE_QUESTION_RATIO` | `0.6` |
| `JOKE_IDENTITY_REJECT_COOLDOWN_S` | `1800` |
| `JOKE_TZ` | workday TZ / UTC |
| `JOKE_REFILL_INTERVAL_S` | `43200` |
| `JOKE_QUEUE_TARGET` | `50` |
| `JOKE_QUEUE_LOW_WATERMARK` | `30` |
| `JOKE_MIN_SCORE` | `0.55` |
| `JOKE_NOVELTY_MIN` | `0.4` |
| `JOKE_GENERATE_MODEL` | `""` |
| `JOKE_CRITIC_MODEL` | `""` |
| `JOKE_SEED_FILE` | `joke_seeds.txt` |
| `JOKE_CURATED_RATIO` | `0.5` |

**Stay in `.env` only (do not move):**  
`OPENROUTER_API_KEY`, `LLM_*`, `VECTORAI_DEBUG*`, optional `LLM_HTTP_REFERER` / `LLM_APP_TITLE`.

### Anti-patterns (do not do)

1. **Do not** invent a second typed parser for every `WORKDAY_*` / `JOKE_*` key in supervisor — unknown keys already forward.
2. **Do not** change loader signatures or hardcode model names when env is empty (`JOKE_GENERATE_MODEL` default `""`).
3. **Do not** `Set-Content` / `>` rewrite pod.conf with only managed keys — that is the user-data bug.
4. **Do not** put API keys in pod.conf.
5. **Do not** break empty-env defaults: FSMs stay **off** unless explicitly enabled.
6. **Do not** require users to delete old `.env` keys on day one — dual-read is OK; pod.conf wins when both set.

### Confidence / gaps

- **High:** loader key lists, supervisor forward path, install clobber sites (discovery agents agree with Phase 0 tables).
- **Medium:** best comment-preserving write algorithm for PowerShell/bash (line upsert is preferred over full rewrite).
- **Gap:** no existing automated test for install.ps1 / setup-companion merge behavior — Phase 1 must add unit-testable helpers where feasible (Python can host pure merge tests; PS/bash tested by scripted fixtures).

### Extra discovery notes (do not miss when implementing)

1. **Dual enable gates** (`behaviors/runtime.py`): Work Day needs `"workday" in BEHAVIORS_ENABLED` **and** `WORKDAY_ENABLED` truthy. Joke needs `"joke_idle" in BEHAVIORS_ENABLED` **and** `JOKE_ENABLED` truthy. Docs/examples in pod.conf must show both.
2. **Truthiness:** loaders use `_truthy` → `1`/`true`/`yes`/`on` (not only `"1"`). Spec text that says `== "1"` is outdated.
3. **`env-default` incomplete vs loaders:** missing `WORKDAY_PRIORITY`, `WORKDAY_IDENTITY_REJECT_COOLDOWN_S` — include both in `pod.conf-default` when writing the template.
4. **Install contrast:** `.env` is already “copy only if missing”; pod.conf is the unsafe path today. Match that spirit: upsert managed keys, never truncate.

---

## Phase 1 — Safe `pod.conf` merge writers (protect existing users)

**Why first:** Migrating more keys into pod.conf is unsafe until reinstall / setup / apply-config stop destroying hand-edited keys.

### 1A. Windows: `Update-PodConf` in `WirePodPaths.ps1`

**Implement:** Copy the *read* style from `Read-PodConf` (`windows/WirePodPaths.ps1` L17–26). Add:

```powershell
function Update-PodConf {
  param([hashtable]$Set)  # keys to upsert; other file lines preserved
  # 1. If file missing: write only $Set keys (sorted or stable order).
  # 2. If file exists:
  #    - Read all lines (preserve blanks and # comments).
  #    - For each non-comment KEY= line: if KEY in $Set, replace value, mark done.
  #    - Append any remaining $Set keys not seen.
  # 3. Write UTF-8 without BOM if possible (supervisor strips BOM anyway).
  # 4. Never delete keys not in $Set.
}
```

**Replace clobber sites to call Update-PodConf only for managed keys:**

| File | Today | Change to |
|------|--------|-----------|
| `windows/install.ps1` ~L543 | `Set-Content` WEB+AI only | `Update-PodConf -Set @{ WEB_PORT=…; AI_PORT=… }` |
| `windows/setup-companion.ps1` ~L130–136 | 5-line rewrite | Upsert `WEB_PORT`, `AI_PORT`, `EXTERNAL_CHIPPER=1`, `WIREPOD_DIR`, `WIREPOD_DATA_DIR` only |
| `windows/apply-wirepod-config.ps1` ~L87–102 | Rebuild whitelist | Upsert same managed companion keys; **leave** WORKDAY/JOKE/etc. untouched |

**Optional managed-key list** (for docs only; writer must not strip unknowns):  
`WEB_PORT`, `AI_PORT`, `EXTERNAL_CHIPPER`, `WIREPOD_DIR`, `WIREPOD_DATA_DIR`, `USE_LOCAL_OLLAMA`, volume keys.

### 1B. Linux: merge helper in install / shared pattern

**Implement** (inline in `linux/install.sh` or small `linux/update-pod-conf.sh` sourced by install):

- Behavior equivalent to `Update-PodConf`: line-preserving upsert.
- Replace `printf 'WEB_PORT=%s\nAI_PORT=%s\n' … > "$POD_CONF"` (`linux/install.sh` L269).

Preserve existing “read port if flag not set” logic (L261–268) **before** upsert.

### 1C. Tests for merge semantics

Add **Python** pure-function tests (easiest CI): either

- extract a tiny `shared/pod_conf_io.py` with `upsert_pod_conf_text(text, updates) -> str` used by docs/tests and optionally called from nothing yet, **or**
- keep PowerShell/bash only and add a `shared/test_pod_conf_upsert.py` that duplicates the algorithm in Python as the **canonical** merge, and have install scripts shell out to it:

```bash
python3 "$SHARED_DIR/pod_conf_io.py" upsert "$POD_CONF" WEB_PORT=9080 AI_PORT=8090
```

**Recommended (copy pattern):** put merge logic in **`shared/pod_conf_io.py`** next to supervisor:

| Function | Role |
|----------|------|
| `load_pod_conf_text` / reuse supervisor `load_pod_conf` | parse |
| `upsert_pod_conf_file(path, updates: dict[str,str])` | line-preserving write |
| `cli` | `python pod_conf_io.py upsert path KEY=val …` for install scripts |

Supervisor may keep its own `load_pod_conf` (already tested) **or** import from `pod_conf_io` if install layout always copies both — prefer **shared module** only if install copies it to `vector-pod/`. Safer for Phase 1: **duplicate-free** by having install scripts call repo `shared/pod_conf_io.py` during install, and runtime continue using supervisor’s loader.

**Verification checklist — Phase 1**

- [ ] Fixture file with `JOKE_ENABLED=1`, comments, `WEB_PORT=1` → upsert `WEB_PORT=9080` → joke key + comments remain.
- [ ] Missing file → create with only requested keys.
- [ ] `install.ps1` / `install.sh` paths no longer contain bare `Set-Content`/`>` of full pod.conf with only ports (grep).
- [ ] `setup-companion.ps1` re-run does not drop a pre-seeded `WORKDAY_ENABLED=1`.
- [ ] `python3 shared/test_…` green for upsert.

**Anti-pattern guards**

- No “read all → write only known keys” rewrite.
- No deleting keys “not in schema.”
- Do not force-default `WORKDAY_ENABLED=0` into every user’s file on install (only touch keys installer owns).

---

## Phase 2 — Template + optional seed defaults (no forced enable)

### 2A. Add `shared/config/pod.conf-default` (or `pod.conf.example`)

Documented KEY=VALUE template with **commented** FSM blocks (mirror style of `env-default` L38–79, but path = pod.conf). Include:

- Ports / companion / volume (short)
- Work Day block (all keys commented except maybe show `WORKDAY_ENABLED=0` only in comments)
- Joke block (same)
- Runtime/speech/`BEHAVIORS_ENABLED` comments

**Install behavior:**

- If `pod.conf` **missing**: create from ports (+ companion keys if companion setup) via upsert; **do not** dump entire default template unless product wants it (prefer small file + example in repo).
- If `pod.conf` **exists**: never overwrite from template; optional one-time **append missing commented section** is **out of scope** (too magical). Point users at example file in docs.

### 2B. Strip FSM knobs from `shared/vector-ai/env-default`

**Keep only LLM/OpenRouter section** (L1–36-ish).

Replace Work Day / Joke sections with a short pointer:

```text
# Behavior FSMs (Work Day, Joke idle, …): configure in pod.conf next to
# supervisor (vector-pod/pod.conf), NOT here. See shared/config/pod.conf-default
# and AGENTS.md.
```

**Do not** delete keys from already-deployed runtime `.env` files automatically in this phase (Phase 4 optional migration).

**Verification**

- [ ] `env-default` has no `WORKDAY_*` / `JOKE_*` assignments.
- [ ] New install `.env` from template cannot enable Work Day by leftover keys.
- [ ] Defaults still: empty env → FSMs off (`load_*_config({})` tests remain green).

---

## Phase 3 — Docs + AGENTS / companion guides

Update user-facing paths from `.env` → `pod.conf`:

| Doc | Change |
|-----|--------|
| `AGENTS.md` | Already prefers pod.conf; ensure Work Day/Joke tables say **pod.conf path** and note install merge safety |
| `docs/FSM-workday-companion.md` | Replace “edit vector-ai/.env” with runtime `pod.conf`; example block is pod.conf |
| `docs/FSM-joke-when-idle-spec.md` | Config surface §6: “loaded from process env; production source is pod.conf via supervisor” |
| `docs/FSM-implementation.md` | Config guidance: new knobs → pod.conf |
| `README.md` / `NEXT_STEPS.md` | Work Day enable steps use pod.conf |
| `shared/vector-ai/env-default` | Pointer only (Phase 2) |

Example pod.conf snippet (copy into docs):

```conf
# Work Day (default off)
WORKDAY_ENABLED=1
WORKDAY_TZ=Australia/Sydney
# WORKDAY_START_BEGIN=09:00
# …

# Joke idle (default off)
# BEHAVIORS_ENABLED=workday,joke_idle
JOKE_ENABLED=0
# JOKE_AUDIENCE=known
```

**Verification**

- [ ] Grep docs for `WORKDAY_ENABLED` in context of `.env` — should be gone or marked legacy.
- [ ] Grep `edit.*\.env` for workday/joke instructions — should point to pod.conf.

---

## Phase 4 — Optional: one-shot migrate keys from runtime `.env` → `pod.conf`

**When:** Existing users who already set WORKDAY/JOKE in `.env`.

**Implement** small tool or install hook (Windows + Linux), **opt-in or one-time with backup**:

1. If `vector-ai/.env` exists, parse KEY=VALUE for keys in the relocate list (Phase 0).
2. For each key: if **not already present** in pod.conf, upsert into pod.conf (do not override pod.conf).
3. Optionally comment out migrated keys in `.env` (safer than delete) with a banner `# migrated to pod.conf YYYY-MM-DD`.
4. Always copy backup: `pod.conf.bak-migrate-<timestamp>`, `.env.bak-migrate-<timestamp>`.

**Do not run** automatically without user visibility on companion “apply config” paths that already restart services — prefer explicit `migrate-behavior-config.ps1` / `.sh` or a flag on install `--migrate-fsm-env`.

**Verification**

- [ ] Fixture: `.env` has `WORKDAY_ENABLED=1`, pod.conf has only ports → after migrate, pod.conf has workday key; re-run is idempotent.
- [ ] Fixture: pod.conf already `WORKDAY_ENABLED=0`, `.env` has `=1` → pod.conf **unchanged** (pod wins / no overwrite).
- [ ] LLM keys never copied to pod.conf.

---

## Phase 5 — Tests & regression (pytest)

**Test runner (Debian: `python3-pytest`; optional `python3-pytest-asyncio`):**

Repo root has `pytest.ini` (`pythonpath` = `shared` + `shared/vector-ai`,
`asyncio_default_fixture_loop_scope = function` to silence system
pytest-asyncio deprecation). All `check()` runners converted to
`assert cond, name`; supervisor suites are real `test_*` functions.

```bash
cd VectorIntelligence
python3 -m pytest shared/test_supervisor_pod_conf.py shared/test_supervisor_wedge.py -q
python3 -m pytest shared/vector-ai/test_behaviors.py shared/vector-ai/test_joke_idle.py -q
# full shared suite:
python3 -m pytest -q
```

**Already present:** `test_merged_env_feeds_behavior_loaders` in
`shared/test_supervisor_pod_conf.py` — merge → `load_*_config` enabled flags.

**Phase 1 upsert tests** must also be pytest `test_*` functions (no module-level
`check()` scripts).

**Verification checklist — final**

- [ ] `python3 -m pytest -q` green from repo root (collects >0 tests).
- [ ] Upsert never drops foreign keys (Phase 1 tests).
- [ ] Grep install scripts: no full-file rewrite of pod.conf with fixed key list only.
- [ ] `env-default` LLM-only.
- [ ] Docs say pod.conf for FSM knobs.
- [ ] Manual smoke (optional): put `JOKE_ENABLED=1` + `BEHAVIORS_ENABLED=workday,joke_idle` in pod.conf only; start stack; vector-ai logs show joke/workday config on; `.env` has no JOKE keys.

---

## Implementation order (for executors)

| Order | Phase | Deliverable |
|------:|-------|-------------|
| 1 | Phase 1 | Safe upsert + fix install/setup/apply clobber |
| 2 | Phase 2 | `pod.conf-default` example + strip `env-default` FSM blocks |
| 3 | Phase 3 | Docs / AGENTS / NEXT_STEPS / companion guides |
| 4 | Phase 4 | Optional migrate tool (can ship after 1–3) |
| 5 | Phase 5 | Cross-suite verification |

**Effort guess:** Phase 1 is the critical path (~half the risk). Phases 2–3 are straightforward. Phase 4 is nice-to-have for upgrades.

---

## Out of scope

- Changing FSM logic, priorities, or defaults values.
- Moving OpenRouter keys to pod.conf.
- Auto-enabling Work Day/Joke on upgrade.
- Rewriting chipper Go to read pod.conf (chipper only needs volume/port env already injected).
- Graphite/PR stacking process (executor’s choice).

---

## Handoff notes for `/do` or implementers

1. **Copy** merge semantics from Phase 1 spec; **copy** key lists from Phase 0 tables; **copy** comment style from `env-default` when writing `pod.conf-default`.
2. Prefer one shared `upsert` implementation over three divergent PS/bash/Python merges.
3. After Phase 1, a user can already put `JOKE_*` / `WORKDAY_*` in pod.conf **today** (supervisor forward exists). Remaining work is safety + docs + template hygiene.
4. Commit message style in this repo: short imperative subject (see `45e7f29`).

# Speech Volume: Desired Jdoc + Hold Modes — Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans. Checkbox tasks (`- [ ]`). One TASK per subagent where files collide.
>
> **Scope:** Redesign speech-volume ducking so (1) **desired speech volume** is durable in wire-pod jdocs and only changes on user intent, (2) before speech we ensure `master_volume == desired`, after hang we set `desired - VOLUME_DROP`, (3) longer holds for multi-turn / listen / chat without mid-listen chirps (blackjack, LLM turns, optional `newVoiceRequest`).
>
> **Out of scope:** Firmware changes, new robot `ROBOT_SETTINGS` proto fields, bit-packing speak+drop into one int, redesigning robotsession, ambient behavior FSMs (except wiring utterance hold where they already call `SayText`).

**Goal:** After install + build, volume no longer creeps down across chipper restarts; idle duck still works; multi-turn games and chat do not get killed by mid-listen `VolumeAdjustment` animations.

**Architecture decision (binding):**

| Concern | Decision |
|---------|----------|
| **Source of truth for “how loud should speech be?”** | Pod jdoc `wirepod.SpeechVolume` → `{"desired_speech_volume": N}` under thing `vic:<ESN>` |
| **Live actuator** | Robot `master_volume` via HTTPS `POST /v1/update_settings` (unchanged path) |
| **Idle level** | `clamp(desired - VECTOR_VOLUME_DROP)`; drop stays env/config only |
| **When jdoc updates** | User-facing volume only: web UI `/api-sdk/volume`, and (where practical) volume intents after match; **never** on duck/raise |
| **When live writes happen** | Transition only: if live ≠ target, write; already at target → no HTTP / no chirp |
| **Hold policy** | Separate from desired: `utterance` / `turn` / `session` modes that only move `loudUntil` forward |
| **Not on robot ROBOT_SETTINGS** | Custom keys inside `vic.RobotSettings` get overwritten by pinger / pull; firmware schema is closed |

**Binding discovery date:** 2026-07-21.

**User-confirmed model:**

```text
jdoc desired  = user speech preference (store; rare writes)
before speech = if master != desired → set desired
speak         = hold at desired per mode
after hold    = set master = desired - VOLUME_DROP
```

---

## How to execute

1. Order: TASK-01 → TASK-02 → TASK-03 → TASK-04 → TASK-05 → TASK-06 → TASK-07.
2. Do not invent APIs; only Phase 0 Allowed APIs (extend only as listed in this plan).
3. After each task: run that task’s verification.
4. Path roots:
   - Chipper: `wire-pod/chipper/`
   - Patch generator: `VectorIntelligence/shared/patches/add-speech-volume-bump.py`
   - Docs: `VectorIntelligence/AGENTS.md`, `shared/config/pod.conf-default`
   - Tests: prefer `wire-pod/chipper/...` Go tests; supervisor volume tests stay Python

### Subagent template

```
You are implementing TASK-NN of the speech-volume plan.

READ:
- VectorIntelligence/docs/superpowers/plans/2026-07-21-speech-volume-desired-jdoc.md
  (Phase 0 Allowed APIs, TASK-NN packet, anti-patterns)

WORKDIR: as in TASK-NN

DO: Interface Contract only. Copy patterns from listed sources.
MUST NOT: write custom keys into vic.RobotSettings; adopt idle as desired;
  WriteJdocs without AddJdoc for new docs; GetJdoc with bare ESN (always "vic:"+esn);
  invent end-of-speech callbacks; change robotsession long stream whitelist.

VERIFY: TASK-NN Acceptance Criteria.
REPORT: sources, diffs, commands, confidence, gaps.
```

---

## Phase 0 — Documentation Discovery (completed; binding)

### Sources consulted

| Source | Finding |
|--------|---------|
| `ttr/speech_volume.go` | In-RAM `speakLevel`/`lastWritten`; `SpeechVolumeBump` = hang only; `SpeechVolumeHoldFor` = estimate+hang; HTTPS set/get `master_volume`; no disk |
| `preqs/intent_graph.go:21` | `go SpeechVolumeBump` concurrent with STT |
| `ttr/kgsim_cmds.go:406` | Live `SpeechVolumeHoldFor` in `DoSayText` |
| `ttr/bcontrol.go:14–38` | Live `SayText(esn)` has **no** hold; dead `sayText` still has HoldFor |
| `vars/vars.go` | `GetJdoc`/`AddJdoc`/`WriteJdocs`; thing `vic:ESN`; names like `vic.RobotSettings`, `vic.AppTokens` |
| `servers/token/token.go` | Precedent for **pod-owned** non-firmware jdoc (`vic.AppTokens`) |
| `sdkapp/server.go` + `urlreqs.go` | UI volume → `update_settings` `master_volume` |
| `sdkapp/jdocspinger.go` | Overwrites `vic.RobotSettings` from robot pull only |
| `intentparam.go` | Blackjack remap → `IntentPass`; volume intents pass through to firmware |
| `kgsim.go` / `kgsim_cmds.go` | LLM chunk speak; `newVoiceRequest` → `AppIntent("knowledge_question")` |
| `add-speech-volume-bump.py` | Generates `speech_volume.go` + patches three call sites |
| `temp/blackjack.log` | Restore mid-listen → VolumeAdjustment chirp → `endBlackjack` |
| `AGENTS.md` L263–315 | Env names/defaults and hang sizing notes |

### Allowed APIs (only these)

#### Existing volume API (extend in place; keep names for call sites)

```go
// ttr/speech_volume.go
func EstimateSpeechDuration(text string) time.Duration
func SpeechVolumeBump(esn string)                    // pre-arm: utterance hang only
func SpeechVolumeHoldFor(esn string, d time.Duration) // utterance: d + VolumeHangTime
```

#### New volume API (add in same file)

```go
// Persist / load desired (jdoc). No-op if VolumeDrop <= 0.
func SpeechVolumeSetDesired(esn string, level int)  // clamp 0–5, write jdoc, update RAM
func SpeechVolumeDesired(esn string) int            // load-through cache; unknown → -1 or documented sentinel

// Hold modes (all only move loudUntil forward; ensure desired before write)
func SpeechVolumeEnterTurn(esn string, d time.Duration)     // one reply cycle
func SpeechVolumeEnterSession(esn string, d time.Duration)  // multi-turn / listen window
func SpeechVolumeLeaveSession(esn string)                   // demote to utterance; schedule short hang then idle
```

Implementation may keep a single internal `speechVolumeHold(esn, d)` and a `mode` enum in `volumeState`. Public names above are the contract for call sites.

#### Jdocs (copy AppTokens pattern)

```go
// vars/vars.go — already exists
func GetJdoc(thing, jdocname string) (AJdoc, bool)
func AddJdoc(thing, name string, jdoc AJdoc) uint64  // persists via WriteJdocs inside
```

**Canonical keys:**

```text
thing: "vic:" + esn          // NEVER bare ESN
name:  "wirepod.SpeechVolume"
JsonDoc: `{"desired_speech_volume":4}`
```

#### Live volume I/O (keep; do not replace with gRPC UpdateSettings for master_volume)

```go
// already in speech_volume.go
getMasterVolume(esn) (int, error)   // pull_jdocs ROBOT_SETTINGS
setMasterVolume(esn, level) error   // POST /v1/update_settings
```

#### Estimate / env (unchanged semantics)

| Env | Default | Role |
|-----|---------|------|
| `VECTOR_VOLUME_DROP` | `2` | Idle = desired − drop; `0` disables all volume writes |
| `VECTOR_VOLUME_HANG_MS` | `2500` | Margin after estimate / bare bump |
| `VECTOR_VOLUME_MS_PER_WORD` | `400` | `words * ms`, floor 1500ms |

New optional env (this plan):

| Env | Default | Role |
|-----|---------|------|
| `VECTOR_VOLUME_TURN_MS` | `15000` | Default turn hold if caller omits / blackjack refresh |
| `VECTOR_VOLUME_SESSION_MS` | `45000` | Default session hold (blackjack hand / listen window) |

Supervisor may inject these later in TASK-06; Go can `envInt` with defaults without supervisor until then.

#### Intent / speak call sites (wire only; do not reimplement games)

| Site | File:line (discovery) | Action in later tasks |
|------|----------------------|------------------------|
| IntentGraph pre-arm | `preqs/intent_graph.go:21` | Keep bump (utterance pre-arm) |
| LLM `DoSayText` | `ttr/kgsim_cmds.go:406` | Keep HoldFor(estimate) |
| Live `SayText(esn)` | `ttr/bcontrol.go:14` | Add HoldFor(estimate) |
| Blackjack remap | `ttr/intentparam.go` (~472–477, ~690–695) | EnterSession after match |
| hit/stand pass-through | same file / match path | Refresh session hold |
| Volume UI | `sdkapp/server.go` volume case | `SpeechVolumeSetDesired` |
| `DoNewRequest` | `kgsim_cmds.go` ~718 | EnterSession/turn for listen |

### Anti-patterns (MUST NOT)

1. **Do not** store desired inside `vic.RobotSettings` JSON (pinger overwrites; firmware schema).
2. **Do not** adopt `master_volume == desired - DROP` as a new desired (creep root cause).
3. **Do not** `GetJdoc(esn, …)` with bare ESN — always `"vic:"+esn`.
4. **Do not** write `master_volume` when already at target (chirp spam / blackjack death).
5. **Do not** assume end-of-speech gRPC callback exists — holds stay estimate + mode deadlines.
6. **Do not** bit-pack speak + drop; drop is env.
7. **Do not** only update the patch generator without updating live `speech_volume.go` (tree already has applied Go; both must match).
8. **Do not** hold `volumeMu` (map lock) across network I/O — only per-robot `st.mu` (existing rule).

### Confidence + gaps from discovery

| Item | Confidence |
|------|------------|
| Desired-in-jdoc + raise/duck model | High (user-confirmed) |
| AppTokens-style persistence | High |
| Blackjack only gets 2.5s bump today | High (code + log) |
| Exact firmware game length for session | Low — use env default + hit/stand refresh |
| Voice volume intents updating jdoc without race | Med — firmware applies async; UI path is solid first |

---

## TASK-01 — Desired speech volume: jdoc load/save + no-creep adopt rules

**WORKDIR:** `wire-pod/chipper/pkg/wirepod/ttr/speech_volume.go`  
**Also update embedded source in:** `VectorIntelligence/shared/patches/add-speech-volume-bump.py` (keep generator in sync)

### What to implement

1. Rename mental model in comments: `speakLevel` → **desired** (field may stay `speakLevel` or become `desired`; pick one and use consistently).
2. Add jdoc helpers:
   - `loadDesiredFromJdoc(esn) (int, bool)`
   - `saveDesiredToJdoc(esn, level int)`
   - Thing/name/JsonDoc as in Phase 0.
3. On first use for an ESN (`volumeStateFor` or first `speechVolumeHold`):
   - If jdoc hit → seed desired from jdoc.
   - If jdoc miss → read live `master_volume` **once**, treat as desired, **persist immediately**, then proceed.
4. **Adopt rules (binding):**
   - On idle→loud transition, read live `cur`.
   - If `cur == lastWritten` → no adopt.
   - If desired known and `cur == clamp(desired - VolumeDrop)` → **do not adopt** (our idle).
   - If desired known and `cur == desired` → no adopt (already at speak).
   - If `cur` is something else → human change: `desired = cur`, `saveDesiredToJdoc`, then raise to desired if needed.
5. **Mute:** desired `<= 0` → never raise (keep current).
6. **Writes:** only `setMasterVolume` when target ≠ last known live / lastWritten (transition only).
7. Export `SpeechVolumeSetDesired` / `SpeechVolumeDesired` for UI/intents.

### Documentation references

- Copy jdoc write pattern from `servers/token/token.go` (~AppTokens Get/mutate/AddJdoc).
- Keep lock discipline from current `speechVolumeHold` / `restoreOne` comments.
- Plan Phase 0 anti-patterns 1–4.

### Verification checklist

- [ ] `go test` / compile package: `cd wire-pod/chipper && go test ./pkg/wirepod/ttr/ -count=1` (or at least `go build ./pkg/wirepod/ttr/`).
- [ ] Unit tests (new file `speech_volume_test.go`) covering pure logic where possible:
  - idle value `clamp(desired - drop)` for pairs (4,2)→2, (1,2)→0, (0,2)→0.
  - adopt: cur==idle does **not** lower desired.
  - adopt: cur=5 while lastWritten=idle raises desired to 5.
  - Prefer table tests; mock HTTP if needed by extracting clamp/adopt pure funcs.
- [ ] Grep: no `GetJdoc` with bare esn in new code.
- [ ] Grep: jdoc name `wirepod.SpeechVolume` appears.

### Anti-pattern guards

- Do not call `WriteJdocs` without going through `AddJdoc` (AddJdoc already writes).
- Do not re-seed desired from live on every bump after jdoc exists.

---

## TASK-02 — Runtime loop: ensure desired before speech; duck to desired−drop

**WORKDIR:** `wire-pod/chipper/pkg/wirepod/ttr/speech_volume.go` (+ patch generator sync)

### What to implement

Rewrite hold/restore to match the agreed loop:

```text
Enter hold (any mode):
  load desired (jdoc/RAM)
  if desired <= 0: return
  extend loudUntil
  if already loud: return
  // optional adopt human change per TASK-01
  if live != desired: setMasterVolume(desired); lastWritten=desired
  loud=true

restoreOne when deadline passed and mode allows duck:
  idle = clamp(desired - VolumeDrop)
  if live/lastWritten != idle: setMasterVolume(idle); lastWritten=idle
  loud=false
```

Keep:

- `EstimateSpeechDuration` (ms/word + 1.5s floor).
- `SpeechVolumeBump` = hold `VolumeHangTime` only (pre-arm).
- `SpeechVolumeHoldFor` = `d + VolumeHangTime`.
- 500ms restore ticker.

Log lines should say **desired** / **idle** clearly for field debug (`[volume] esn raised to desired=4 (idle=2)`).

### Documentation references

- Current `speechVolumeHold` / `restoreOne` structure (lock, forward-only deadline).
- AGENTS.md hold sizing notes (estimate is not end-of-speech).

### Verification checklist

- [ ] With `VECTOR_VOLUME_DROP=0`, zero HTTP volume writes (existing disable).
- [ ] Unit test: restore target equals `desired - drop` using stored desired, not re-read as new desired.
- [ ] No path sets desired from restore.

### Anti-pattern guards

- Restore must not call `saveDesiredToJdoc`.
- Do not pump: second HoldFor while loud only extends deadline.

---

## TASK-03 — Hold modes: turn + session API

**WORKDIR:** `speech_volume.go` (+ patch generator)

### What to implement

```go
type volumeMode int
const (
  modeUtterance volumeMode = iota
  modeTurn
  modeSession
)

// volumeState gains: mode volumeMode

func SpeechVolumeEnterTurn(esn string, d time.Duration)
func SpeechVolumeEnterSession(esn string, d time.Duration)
func SpeechVolumeLeaveSession(esn string)
```

Rules:

| Mode | Duck when |
|------|-----------|
| `utterance` | `now > loudUntil` |
| `turn` | `now > loudUntil` (same restore; typically longer d) |
| `session` | only after `LeaveSession` **or** `loudUntil` expiry if you use max session deadline — **prefer:** session sets long `loudUntil` and mode=session; restore allowed when deadline passes OR LeaveSession forces mode=utterance + short hang |

Binding choice for this plan:

- **Session** = `modeSession` + `loudUntil = max(loudUntil, now+d)` with large default `VECTOR_VOLUME_SESSION_MS`.
- **LeaveSession** = set `modeUtterance`, set `loudUntil = now + VolumeHangTime` (then normal restore to idle).
- **EnterTurn/EnterSession** always ensure desired (same as hold entry).
- Mode rank: entering session while in turn upgrades; LeaveSession demotes.

Defaults if `d <= 0`: use env `VECTOR_VOLUME_TURN_MS` / `VECTOR_VOLUME_SESSION_MS`.

### Documentation references

- Plan intro “Hold modes” table.
- Existing forward-only `loudUntil` logic.

### Verification checklist

- [ ] Test: EnterSession(45s) → restoreOne before deadline does nothing; after deadline ducks to idle.
- [ ] Test: LeaveSession schedules hang then idle.
- [ ] Test: HoldFor while in session only extends deadline, does not demote mode.

### Anti-pattern guards

- Do not invent a second restore loop.
- Do not write volume on mode change if already at desired.

---

## TASK-04 — Wire call sites: speak paths + blackjack + volume UI

**WORKDIR:** multiple chipper files (see list). Prefer **direct Go edits** in tree; update patch generator anchors in TASK-05.

### What to implement

#### 4a. Utterance coverage (fix post-robotsession drift)

| File | Change |
|------|--------|
| `ttr/bcontrol.go` `SayText(esn, text)` | Before `Session.Say`: `SpeechVolumeHoldFor(esn, EstimateSpeechDuration(text))` |
| Keep `DoSayText` HoldFor | Already correct |

Optional (P2, same task if cheap): `ambient.go` speak path HoldFor; `kgsim.go` `KGSim` error path HoldFor.

#### 4b. Blackjack / multi-turn firmware games (P0)

After successful blackjack intent remap (both opus + prehistoric paths in `intentparam.go`):

```go
SpeechVolumeEnterSession(botSerial, 0) // 0 → VECTOR_VOLUME_SESSION_MS default
```

When matching / passing `intent_blackjack_hit` or `intent_blackjack_stand`:

```go
SpeechVolumeEnterSession(botSerial, 0) // refresh session window
```

No need to LeaveSession on unknown end — session deadline expiry ducks; refresh on each hit/stand keeps a long hand alive.

Also consider other `intent_play_specific_extend` games if they listen after TTS (fistbump is short — session optional; **blackjack is mandatory**).

#### 4c. LLM turn cohesion (P1)

At start of `StreamingKGSim` speak loop (first chunk) **or** keep per-chunk HoldFor (already stitches replies). Minimum for this task: per-chunk HoldFor remains; add `SpeechVolumeEnterTurn(esn, EstimateSpeechDuration(fullSoFar) or fixed TURN_MS)` once at speak-loop start if easy without large refactor.

If `DoNewRequest` remains reachable:

```go
// before AppIntent knowledge_question
SpeechVolumeEnterSession(esn, 0)
```

#### 4d. Desired updates on user volume

| Path | Change |
|------|--------|
| `sdkapp/server.go` case `/api-sdk/volume` | After successful set (or immediately with parsed N): `ttr.SpeechVolumeSetDesired(esn, N)` |

Volume intents (P1): after matching `intent_imperative_volumelevel_extend` with known `VOLUME_N`, call `SpeechVolumeSetDesired(esn, N)`. Volume up/down without absolute level: skip jdoc (firmware relative); next bump adopt rules may still catch if live ∉ {desired, idle}.

### Documentation references

- Discovery tables in Phase 0.
- `intentparam.go` blackjack remap blocks.
- `sdkapp/server.go` volume case.

### Verification checklist

- [ ] `go build` chipper (or `go test ./pkg/wirepod/...` as feasible).
- [ ] Grep `SpeechVolumeEnterSession` near blackjack.
- [ ] Grep `SpeechVolumeSetDesired` near `/api-sdk/volume`.
- [ ] Grep `SpeechVolumeHoldFor` in `SayText` (bcontrol live path).
- [ ] Manual / log: play blackjack → no `anim_volume_stage_*` during first ~session ms of listening (device test if available).

### Anti-pattern guards

- Do not EnterSession on every IntentGraph open (would never idle).
- Do not SetDesired on duck/raise.

---

## TASK-05 — Patch generator + install idempotency

**WORKDIR:** `VectorIntelligence/shared/patches/add-speech-volume-bump.py`

### What to implement

1. Replace embedded `SPEECH_VOLUME_GO` with the full new `speech_volume.go` content (byte-sync with live tree).
2. Update patch anchors if call-site comments/strings changed:
   - `intent_graph.go` bump (unchanged call OK).
   - `kgsim_cmds.go` DoSayText HoldFor.
   - `bcontrol.go`: patch **live** `SayText(esn, text)` not only dead `sayText`.
3. Idempotency: skip if sentinels present (`SpeechVolumeBump`, `wirepod.SpeechVolume`, `SpeechVolumeEnterSession` as appropriate).
4. Docstring at top of patch: desired jdoc model + hold modes + env table including TURN/SESSION.

### Documentation references

- Existing patch structure (SPEECH_VOLUME_GO, anchor replace, install.sh hooks).
- Install: `linux/install.sh` / `windows/install.ps1` already invoke this patch — no new install step unless missing.

### Verification checklist

- [ ] `python3 shared/patches/add-speech-volume-bump.py <wire-pod-dir>` on a tree that already has changes → skip / no-op without corruption.
- [ ] Fresh apply path still inserts bump + HoldFor if missing (test on copy if needed).
- [ ] Embedded Go includes `wirepod.SpeechVolume` and EnterSession.

### Anti-pattern guards

- Do not leave generator shipping old RAM-only creepy latch.

---

## TASK-06 — Docs, pod.conf, supervisor env (optional TURN/SESSION)

**WORKDIR:**

- `VectorIntelligence/AGENTS.md` (speech volume section)
- `VectorIntelligence/shared/config/pod.conf-default`
- `VectorIntelligence/shared/supervisor.py` (+ `test_supervisor_pod_conf.py` if new keys)

### What to implement

1. Rewrite AGENTS.md speech-volume section to the desired-jdoc model:
   - desired store; raise before speech; idle = desired − drop
   - never adopt idle as desired
   - hold modes: utterance / turn / session
   - env table: existing three + `VECTOR_VOLUME_TURN_MS` / `VECTOR_VOLUME_SESSION_MS`
2. Comment keys in `pod.conf-default`.
3. Supervisor: inject TURN/SESSION into chipper env if added (mirror DROP/HANG pattern + VECTOR_ alias win).
4. README one-liner if it still says “latched from human” without jdoc.

### Verification checklist

- [ ] `pytest VectorIntelligence/shared/test_supervisor_pod_conf.py` if supervisor changed.
- [ ] AGENTS.md mentions `wirepod.SpeechVolume` and creep fix.

### Anti-pattern guards

- Do not put volume knobs in vector-ai `.env` / persona.

---

## TASK-07 — Verification (final)

### Automated

- [ ] `cd wire-pod/chipper && go test ./pkg/wirepod/ttr/ -count=1`
- [ ] `cd wire-pod/chipper && go build -o /dev/null .` (or project’s usual chipper build)
- [ ] `pytest VectorIntelligence/shared/test_supervisor_pod_conf.py -q` (if TASK-06 touched supervisor)
- [ ] Grep guards:

```bash
# must exist
rg -n 'wirepod\.SpeechVolume|SpeechVolumeSetDesired|SpeechVolumeEnterSession' wire-pod/chipper/pkg/wirepod/

# must not: adopt idle as speak without guard (spot-check speech_volume.go restore)
rg -n 'saveDesired|desired_speech_volume' wire-pod/chipper/pkg/wirepod/ttr/speech_volume.go

# bare ESN GetJdoc in new code
rg -n 'GetJdoc\([^"]*esn' wire-pod/chipper/pkg/wirepod/ttr/speech_volume.go || true
```

### Behavioral (device / log) — if robot available

| Scenario | Pass criteria |
|----------|----------------|
| Set volume 4 in UI, restart chipper, say hi | Speaks at 4, then idles at 2 (drop=2); after restart still speaks at 4 (no creep) |
| LLM multi-sentence reply | No mid-reply volume-stage chirp |
| Play blackjack, wait for hit/stand prompt | No `VolumeAdjustment` / `anim_volume_stage_*` during listening window (~session ms); game still listening |
| `VECTOR_VOLUME_DROP=0` | No volume jdocs writes from patch |

### Anti-pattern final scan

- [ ] No custom keys written into robot settings JSON via update_settings except `master_volume`.
- [ ] No desired updates in `restoreOne`.
- [ ] Patch generator and live `speech_volume.go` not divergent (diff or shared regenerate).

---

## Suggested task → subagent split

| Task | Collision risk | Notes |
|------|----------------|-------|
| TASK-01 | High with 02/03 | Same file — **sequential** before 02/03 |
| TASK-02 | High | Same file after 01 |
| TASK-03 | High | Same file after 02 |
| TASK-04 | Med | intentparam + bcontrol + sdkapp — can parallelize **after** 01–03 API exists |
| TASK-05 | Med | After Go stable |
| TASK-06 | Low | Docs/supervisor parallel after API names frozen |
| TASK-07 | — | Orchestrator |

**Recommended sequence:** 01 → 02 → 03 → 04 → 05 → 06 → 07.

---

## Rollout / risk notes

1. **First boot after upgrade:** first live `master_volume` becomes desired. If robot is currently ducked (idle), one-time wrong seed is possible. Mitigation: on first seed, if operators know volume was ducked, set volume once via UI after upgrade (SetDesired). Optional harder mitigation (out of scope unless needed): if `lastWritten` file missing and live is low, prefer not ducking until UI set — document the one-time “set volume once” step in AGENTS.md.
2. **Blackjack session length:** default 45s may be short for slow players; hit/stand refresh extends; tune `VECTOR_VOLUME_SESSION_MS`.
3. **Chirps still happen** on real raise/duck transitions — correct; we only eliminate spurious writes and mid-listen restores.
4. **EXTERNAL_CHIPPER / unpatched chipper:** feature only on full install + this patch; DROP=0 remains emergency off switch.

---

## Done definition

- Desired speech volume persists in `wirepod.SpeechVolume` jdoc and does not ratchet down from idle.
- Runtime: ensure desired → speak → duck to desired−DROP with transition-only writes.
- Hold modes exist; blackjack enters/refreshes session; UI sets desired.
- Patch generator, AGENTS.md, and tests reflect the new model.
- Final verification greps + Go tests green.

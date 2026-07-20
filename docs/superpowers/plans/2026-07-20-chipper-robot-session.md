# Chipper RobotSession — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Scope:** Redesign **who owns** the gRPC channel and the two long streams (`EventStream`, `BehaviorControl`) inside **wire-pod chipper**, so short-lived work rides a session instead of dialing Vector like a stateless HTTP API. Do **not** redesign gRPC itself, recompile `vic-gateway`, or rewrite vector-ai FSMs.
>
> **Out of scope:** firmware/gateway changes, Qualcomm kernel, Python SDK, vector-ai speech arbiter redesign (it stays the speech gate; RobotSession is the **robot transport** mutex).

**Goal:** One durable `RobotSession` per ESN in chipper: single `*vector.Vector` channel, one long-lived secondary `EventStream` for `robot_state` (and optional stim), serialized `BehaviorControl` leases with cancel-based teardown, and all existing call sites (kgsim, sayText, sensor, ambient, behavior-tick, face-probe) either share that session or are deleted as redundant dials.

**Architecture:** New package `chipper/pkg/wirepod/robotsession` (name fixed below) holding a process-wide registry. Background loops and voice paths obtain a session by ESN, use `WithControl` / `Unary` / shared state fan-out, and never call `vector.New` themselves (except session internals + install-time SDK patch path).

**Tech stack:** Go 1.x as already used by chipper; `github.com/fforchino/vector-go-sdk` (pin in `chipper/go.mod`); VI-patched `Close()` via `add-sdk-close.py`; unit tests as pure-Go table tests in the new package (no existing chipper test harness — introduce `go test` for this package only).

**Binding discovery date:** 2026-07-20. Phase 0 below is authoritative for Allowed APIs.

---

## How to execute this plan with subagents

### Orchestrator rules

1. **One TASK = one subagent (or one sequential worker).** Tasks have explicit file ownership; do not merge tasks that share files.
2. **Strict build order:** TASK-01 → TASK-02 → TASK-03 → TASK-04 → TASK-05 → TASK-06 → TASK-07 → TASK-08 → TASK-09 → TASK-10.
3. **Subagent brief must include:** the full TASK packet; Project Bootstrap; Phase 0 Allowed APIs; that task’s anti-patterns.
4. **Subagent reporting contract:** sources consulted, signatures/paths changed, verification commands + output, confidence + gaps.
5. **Do not invent APIs.** Only symbols in Phase 0 Allowed APIs or types defined in this plan’s contracts.
6. **Path roots:**
   - Chipper: `wire-pod/chipper/`
   - Patches that *inject* files today: `VectorIntelligence/shared/patches/`
   - This plan: `VectorIntelligence/docs/superpowers/plans/2026-07-20-chipper-robot-session.md`
7. After each TASK: run that task’s acceptance checks. Do not proceed on red.
8. Prefer implementing **core package + tests first** in tree; migrate call sites second; update patches last so full-install stays consistent.

### Subagent prompt template (copy per task)

```
You are implementing TASK-NN of the Chipper RobotSession plan.

READ FIRST (only these):
- VectorIntelligence/docs/superpowers/plans/2026-07-20-chipper-robot-session.md
  — Project Bootstrap, Phase 0 Allowed APIs, Contracts, full TASK-NN packet.
- Bootstrap Context files listed in TASK-NN only.

WORKDIR: wire-pod/chipper/ (unless TASK says otherwise)

DO:
- Implement exactly the Interface Contract in TASK-NN.
- Use only Allowed APIs + types defined in this plan.
- Match existing chipper style (logger, vars.BotInfo, error handling).

MUST NOT:
- Call vector.New outside robotsession package (after migration tasks).
- Open continuous robot_observed_face streams.
- Use context.Background() for BehaviorControl without a parent cancel/timeout.
- Invent Close/EventStream/BehaviorControl signatures that differ from Phase 0.
- Touch vector-ai Python FSMs or supervisor wedge logic (except optional log line notes).

VERIFY: Acceptance Criteria for TASK-NN.

REPORT: sources, diffs summary, verification output, confidence, gaps.
```

### Shared-file ownership

| File / area | Owner task |
|-------------|------------|
| `chipper/pkg/wirepod/robotsession/*.go` (core) | TASK-01, TASK-02, TASK-03 |
| `robotsession/*_test.go` | TASK-04 |
| `chipper/pkg/wirepod/ttr/bcontrol.go` | TASK-05 |
| `chipper/pkg/wirepod/ttr/kgsim.go` + `kgsim_interrupt.go` | TASK-06 |
| Injected loops via patches → prefer editing installed sources **and** patch generators | TASK-07 |
| `chipper/pkg/wirepod/sdkapp/robot.go` (+ bcassume/server only if needed) | TASK-08 |
| `vars.GetRobot` / scripting | TASK-09 |
| `VectorIntelligence/shared/patches/*` + install docs | TASK-10 |
| Final verification greps/tests | TASK-11 (final phase) |

---

## Phase 0 — Documentation Discovery (completed; binding for implementers)

### Sources consulted

| Source | What was read |
|--------|----------------|
| `wire-pod/chipper/go.mod` | SDK pin `github.com/fforchino/vector-go-sdk v0.0.0-20231108155304-62168f3595d6` |
| Upstream SDK at pin (GitHub `pkg/vector/vector.go`, `options.go`, `behaviorcontrol.go`) | `New`, options, **no** upstream `Close` |
| `VectorIntelligence/shared/patches/add-sdk-close.py` | Patched `Close()` keeping `grpcConn` |
| `vector-cloud/gateway/message_handler.go` | EventStream primary/`connection_id`, 1s keepalive, BC auto-ControlRelease on stream death |
| `vector-cloud/.../behavior.pb.go`, `shared.pb.go` | Priorities, EventRequest fields |
| `chipper/pkg/wirepod/ttr/{kgsim,bcontrol,kgsim_interrupt}.go` | Voice + speak + interrupt streams |
| `chipper/pkg/wirepod/sdkapp/{robot,bcassume,server}.go` | Existing pool without Close |
| `VectorIntelligence/shared/patches/{fix-connection-leak,fix-saytext-stream-leak,add-sensor-reactions,add-ambient-loop,add-behavior-tick,add-face-probe}.py` | Best/worst client patterns |
| `VectorIntelligence/AGENTS.md`, `docs/FSM-implementation.md` | Always Close; no continuous face stream |
| `VectorIntelligence/shared/supervisor.py` | Wedge detector is process bounce, not pooling |

### Allowed APIs (only these for robot I/O)

#### SDK construction (session internals only after migration)

```go
// package github.com/fforchino/vector-go-sdk/pkg/vector
func New(opts ...Option) (*Vector, error)
func WithTarget(s string) Option   // host:port e.g. "192.168.x.x:443"
func WithToken(s string) Option    // GUID / bearer
func WithSerialNo(s string) Option // stored; not used for dial

// VI-patched only (must be applied on install — add-sdk-close.py):
func (v *Vector) Close() error
```

**Anti-pattern:** assuming upstream `Close()` without the patch. Session package must document dependency on patched SDK.

#### Conn RPCs (via `robot.Conn` = `vectorpb.ExternalInterfaceClient`)

```go
EventStream(ctx context.Context, in *vectorpb.EventRequest, opts ...grpc.CallOption) (vectorpb.ExternalInterface_EventStreamClient, error)
BehaviorControl(ctx context.Context, opts ...grpc.CallOption) (vectorpb.ExternalInterface_BehaviorControlClient, error)
// Plus existing unaries already used: BatteryState, SayText, PlayAnimation, etc. — only those already called in chipper today.
```

#### EventRequest (allowed fields)

```go
&vectorpb.EventRequest{
  ListType: &vectorpb.EventRequest_WhiteList{
    WhiteList: &vectorpb.FilterList{List: []string{"robot_state" /* + optional "stimulation_info", "wake_word" */}},
  },
  ConnectionId: "<stable non-empty id>", // session owns primary claim policy
}
```

**Allowed whitelist event names (gateway OrigName):** only those already used in tree: `robot_state`, `wake_word`, `stimulation_info`, and short probes only: `robot_observed_face`.

**Forbidden:** long-lived whitelist of `robot_observed_face` (AGENTS.md / FSM docs).

#### BehaviorControl messages (allowed)

```go
// Priorities (behavior.pb.go):
vectorpb.ControlRequest_OVERRIDE_BEHAVIORS // 10 — speak / LLM
vectorpb.ControlRequest_DEFAULT            // 20 — sdkapp UI default
// RESERVE_CONTROL (30) — do not use unless explicitly required later

// Request oneofs:
BehaviorControlRequest_ControlRequest{ControlRequest: &ControlRequest{Priority: ...}}
BehaviorControlRequest_ControlRelease{ControlRelease: &ControlRelease{}}

// Response: GetControlGrantedResponse() non-nil means grant
// KeepAlive responses: ignore
// ControlLost: treat as lease lost
```

#### Gateway semantics (do not reimplement; design around them)

| Fact | Implication for RobotSession |
|------|------------------------------|
| Single global primary `connection_id` on robot | Session uses **one stable** `ConnectionId` per ESN (e.g. `wirepod-<esn>`); never empty string for the long EventStream |
| Empty ConnectionId can become primary | Short probes must **not** open EventStream with empty id if a primary exists, or must use secondary-safe ids / avoid EventStream |
| EventStream keepalive every 1s | Long stream is healthy when Recv works; client cancel → `transport is closing` is normal |
| BehaviorControl: defer ControlRelease on stream death | **Cancel context** to tear down; ControlRelease alone is insufficient (see fix-saytext-stream-leak) |
| Server does not expose max-conn budget | Client must not leak channels; one channel per ESN is the budget fix |

#### Bot lookup (existing)

```go
// vars.BotInfo.Robots[] — ESN, IPAddress, GUID
// Target format used everywhere: robot.IPAddress + ":443"
```

### Anti-patterns (global)

| Do NOT | Why |
|--------|-----|
| `vector.New` per voice query / per ambient tick / per face probe (after migration) | Exhausts robot connection budget → wifi icon |
| `BehaviorControl(context.Background())` without timeout/cancel | Stream leak on long-lived channel |
| Busy-wait `select { default: continue }` around BC | CPU spin (pre-patch sayText) |
| Continuous `robot_observed_face` EventStream | Overloads firmware (AGENTS.md) |
| sdkapp-style pool without `Close()` | Leaks on removeRobot |
| Open second primary with competing ConnectionId | Gateway secondary / BLE mismatch noise |
| Invent `robot.Dial`, connection pools of N>1 per ESN, or REST to replace gRPC | Out of scope / non-existent APIs |
| Count vector-ai HTTP timeouts as gateway wedge | supervisor wedge pattern is separate |

### Current call-site map (migration targets)

| Site | Today | Target after plan |
|------|-------|-------------------|
| `ttr/kgsim.go` StreamingKGSim / KGSim | `vector.New` per query; BC + interrupt EventStream | Session.Unary health; Session.WithControl; shared state for interrupt |
| `ttr/bcontrol.go` sayText / BControl | Own BC stream | Session.WithControl / Session.Say |
| `ttr/kgsim_interrupt.go` | Own EventStream | Subscribe to session state fan-out |
| sensor loop (patch) | Own New + long EventStream | Session long EventStream only; react via Session.Say |
| ambient / greeting / face probe (patches) | Short New + optional face EventStream | Session.Unary + optional Session.ProbeFace (secondary/short, non-primary) |
| behavior-tick (patch) | Short New | Session |
| sdkapp `robots[]` | Parallel pool | Either wrap Session or become a thin HTTP façade over Session |
| `vars.GetRobot` / Lua | Naked New | Session handle or short lease API |
| intentparam New | Leak | Session.Say |

---

## Project Bootstrap

```text
Repo: vectorStuff
Chipper: wire-pod/chipper
Package to create: chipper/pkg/wirepod/robotsession
SDK: github.com/fforchino/vector-go-sdk (patched Close on install)
Bot config: chipper/pkg/vars BotInfo
Logger: chipper/pkg/logger (or existing ttr log style)
Install patches: VectorIntelligence/shared/patches + linux/install.sh
Plan: VectorIntelligence/docs/superpowers/plans/2026-07-20-chipper-robot-session.md
```

**Prerequisite (install path):** `add-sdk-close.py` remains mandatory. RobotSession calls `Close()`; without the patch, build must fail loudly or stub document — do not silently no-op forever.

---

## Contracts (types to implement — do not invent alternate names)

### Package `robotsession`

```go
package robotsession

// Registry is process-wide. Safe for concurrent use.
type Registry struct { /* private: map[string]*Session, mu sync.Mutex */ }

func NewRegistry() *Registry
func (r *Registry) Get(ctx context.Context, esn string) (*Session, error) // creates/connects if needed
func (r *Registry) Drop(esn string) // Close session; remove from map
func (r *Registry) DropAll()       // for chipper shutdown / wedge bounce prep

// Session is one robot: one channel, optional long EventStream, control mutex.
type Session struct {
    ESN    string
    Target string // ip:443
    // private: *vector.Vector, controlMu, stream cancel, state subscribers, lastError, ...
}

// EnsureConnected dials if needed; BatteryState with short timeout as health.
func (s *Session) EnsureConnected(ctx context.Context) error

// Close tears down EventStream cancel + vector.Close().
func (s *Session) Close() error

// StartStateStream opens long EventStream with whitelist robot_state (+ optional extras).
// ConnectionId MUST be non-empty stable: "wirepod-" + esn (or similar).
// Not primary-hostile: one stream per session; reconnect with backoff.
func (s *Session) StartStateStream(ctx context.Context) error

// SubscribeState returns a channel of latest robot_state (or filtered events).
// Buffer small (e.g. 1–8); drop oldest on overflow. Caller must cancel via context.
func (s *Session) SubscribeState(ctx context.Context) <-chan *vectorpb.Event

// Snapshot helpers used by sensor/ambient without raw Recv loops:
func (s *Session) LastRobotState() *vectorpb.RobotState // may be nil
func (s *Session) OnCharger() bool
func (s *Session) CalmPower() bool

// ControlLease options
type ControlOptions struct {
    Priority vectorpb.ControlRequest_Priority // default OVERRIDE_BEHAVIORS for speak
    Timeout  time.Duration                    // default 30s
}

// WithControl serializes BehaviorControl: one lease at a time per Session.
// Opens BC under ctx∩timeout, waits grant, runs fn, always cancel+release.
func (s *Session) WithControl(ctx context.Context, opt ControlOptions, fn func(ctx context.Context, v *vector.Vector) error) error

// Say is WithControl + SayText (UseVectorVoice true, DurationScalar 1.0) — replace sayText.
func (s *Session) Say(ctx context.Context, text string) error

// Unary runs fn with *vector.Vector without taking control (BatteryState, settings HTTP still external).
func (s *Session) Unary(ctx context.Context, fn func(ctx context.Context, v *vector.Vector) error) error

// ProbeFace: short, bounded EventStream whitelist robot_observed_face OR reuse existing
// face-probe logic — MUST use context timeout ≤6s, MUST NOT claim primary with empty id.
// Prefer secondary: ConnectionId = "wirepod-face-" + esn while state stream holds primary.
func (s *Session) ProbeFace(ctx context.Context, timeout time.Duration) (faceID int32, name string, sawAny bool, err error)
```

### Global wiring

```go
// Package-level or initwirepod-held:
var Default *Registry // set at chipper start

// startserver / init: robotsession.Default = robotsession.NewRegistry()
// on shutdown: Default.DropAll()
// optional: StartStateStream for each BotInfo robot at startup (sensor path)
```

### Control sharing rules

1. **Only one** `WithControl` at a time per ESN (`controlMu`).
2. Waiters: either queue with ctx cancel or return `ErrControlBusy` — pick **queue with ctx** for speak fairness (voice > ambient) by using caller ctx deadlines; document that ambient must use short timeouts.
3. **Do not** hold OVERRIDE across LLM network waits — acquire control only around robot speech/animation RPCs (kgsim today holds longer; improve by narrowing hold to Speak segments where easy, but TASK-06 minimum is session-shared channel + cancelable BC).
4. Sensor reactions call `Session.Say`, never open BC themselves.

### ConnectionId policy

| Stream | ConnectionId | Primary? |
|--------|--------------|----------|
| Long state EventStream | `wirepod-<esn>` | Yes (intended) |
| Face probe EventStream | `wirepod-face-<esn>` | No (secondary while state holds primary) |
| sdkapp stim stream | Prefer SubscribeState fan-out or secondary id `wirepod-ui-<esn>` | Must not steal primary if state stream owns it |

### Reconnect / errors

- On EventStream Recv error: cancel stream, log, backoff (start 2s, cap 30s), re-`StartStateStream` if session still open.
- On `EnsureConnected` failure: return error; do not spin without backoff.
- `Drop` / `Close`: cancel all subscriber ctxs, stop reconnect loop, `vector.Close()`.

---

## Architecture diagram (target)

```text
                    ┌─────────────────────────────────────┐
  chipper process   │  robotsession.Registry              │
                    │   esn → *Session                    │
                    └─────────────────────────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        Session(ESN-A)          Session(ESN-B)              ...
         │ one *vector.Vector
         │ one EventStream (robot_state) ConnectionId=wirepod-A
         │ controlMu → BehaviorControl leases
         │
    ┌────┴────┬────────────┬──────────────┬────────────┐
    ▼         ▼            ▼              ▼            ▼
  sensor   ambient      kgsim/say     face probe    sdkapp/HTTP
  (sub)    Unary/Say    WithControl   ProbeFace     façade
```

---

## Implementation phases / tasks

### TASK-01 — Scaffold package + Registry + Session connect/close

**Files to create:**
- `wire-pod/chipper/pkg/wirepod/robotsession/registry.go`
- `wire-pod/chipper/pkg/wirepod/robotsession/session.go`
- `wire-pod/chipper/pkg/wirepod/robotsession/doc.go` (package comment: requires patched SDK Close)

**Interface contract:**
- `NewRegistry`, `Get`, `Drop`, `DropAll`
- `Get` resolves ESN via `vars.BotInfo` (copy pattern from `sdkapp/robot.go` L44–61 and `add-sensor-reactions` target `IP+":443"`)
- `EnsureConnected`: `vector.New(WithTarget, WithToken, WithSerialNo)`; health `BatteryState` with **≤5s** timeout (copy timeout idea from `fix-connection-leak.py` / ambient 3–5s)
- `Close` / `Drop`: call `vector.Close()` if non-nil
- Thread-safe map

**Bootstrap context:** `sdkapp/robot.go` (lookup only), `vars` BotInfo, `add-sdk-close.py` Close contract.

**Acceptance:**
- [ ] `go build ./pkg/wirepod/robotsession/`
- [ ] `Get` twice same ESN returns same `*Session` pointer
- [ ] `Drop` then `Get` creates new session
- [ ] No `EventStream` / `BehaviorControl` yet

**Anti-patterns:** dialing in `Get` without storing Vector; forgetting mutex; using empty Target.

---

### TASK-02 — Control lease: `WithControl` + `Say` + `Unary`

**Files to modify:** `session.go` (+ `control.go` if preferred)

**Interface contract:**
- Implement `WithControl`, `Say`, `Unary` per Contracts
- Copy **acquire → grant → work → release + cancel** from `fix-saytext-stream-leak.py` REPLACE_FUNC and/or `add-ambient-loop.py` `ambientReact` (sync BC under timeout ctx)
- Default timeout 30s; priority default `OVERRIDE_BEHAVIORS`
- `controlMu` held for entire lease
- On grant wait: loop Recv until `GetControlGrantedResponse() != nil` or ctx done
- Always `cancel()` in defer before unlock

**Bootstrap context:**
- `VectorIntelligence/shared/patches/fix-saytext-stream-leak.py` (REPLACE_FUNC)
- `VectorIntelligence/shared/patches/add-ambient-loop.py` ambientReact BC block
- Gateway note: cancel tears stream; defer ControlRelease on robot

**Acceptance:**
- [ ] `go build ./pkg/wirepod/robotsession/`
- [ ] Unit test with fake/mock: if no robot, test mutex + timeout path with interface injection **or** test pure helper that builds ControlRequest messages (see TASK-04)
- [ ] Grep: no `context.Background()` without `WithTimeout`/`WithCancel` in control path

**Anti-patterns:** busy-wait `default: continue`; fire-and-forget goroutine without waiting for grant; calling `Close()` on session from inside `WithControl`.

---

### TASK-03 — Long EventStream + SubscribeState + ProbeFace

**Files:** `session.go` / `stream.go`

**Interface contract:**
- `StartStateStream`: whitelist `[]string{"robot_state"}` only for the long stream; `ConnectionId: "wirepod-" + strings.ToLower(esn)`
- Reconnect loop with backoff; exit when session closed
- `SubscribeState` fan-out (mutex + subscriber list)
- Cache last `RobotState` for `LastRobotState` / `OnCharger` / `CalmPower` (bit flags same as sensor patch: `ROBOT_STATUS_IS_ON_CHARGER`, `ROBOT_STATUS_CALM_POWER_MODE`)
- `ProbeFace`: timeout default 6s; whitelist `robot_observed_face`; ConnectionId `wirepod-face-`+esn; never leave stream open after return

**Bootstrap context:**
- `add-sensor-reactions.py` stream whitelist + flag bits
- `add-face-probe.py` / ambient `probeForKnownFace` 6s window
- Gateway primary: state stream holds `wirepod-<esn>`

**Acceptance:**
- [ ] `go build ./pkg/wirepod/robotsession/`
- [ ] ConnectionId non-empty on both streams (grep source)
- [ ] Face stream uses context timeout
- [ ] No continuous face subscription

**Anti-patterns:** empty ConnectionId on long stream; face stream without timeout; blocking SubscribeState without ctx.

---

### TASK-04 — Unit tests for robotsession

**Files to create:** `registry_test.go`, `control_test.go` (and fakes as needed)

**Approach (no robot required):**
- Extract pure helpers if needed (`buildControlRequest`, `connectionIDFor`, charger bit parse) and table-test them
- For Registry map behavior: use a test double interface **only if** introduced in TASK-01 as `connector` interface; **do not invent RPC mocks for full gRPC** unless already natural
- Prefer: test ConnectionId strings, flag parsing from synthetic `RobotState`, control options defaults, DropAll clears map

**Bootstrap context:** no chipper Go tests exist — follow standard `testing` package; Python wedge tests are style inspiration only (`test_supervisor_wedge.py`).

**Acceptance:**
- [ ] `go test ./pkg/wirepod/robotsession/ -count=1`
- [ ] Tests pass offline

**Anti-patterns:** tests that require a live Vector; flaky sleep-based timing without `context` deadlines.

---

### TASK-05 — Migrate `ttr/bcontrol.go` to Session

**Files:** `wire-pod/chipper/pkg/wirepod/ttr/bcontrol.go`

**What to implement:**
- `sayText(robot *vector.Vector, text string)` becomes thin wrapper **or** replace call sites with `sayTextESN(esn, text)` / `sayTextSession(s *robotsession.Session, text)`
- Preferred: `func SayText(esn, text string)` using `robotsession.Default.Get` + `Say`
- Keep `BControl` only if kgsim still needs start/stop chans; otherwise implement kgsim on `WithControl` in TASK-06 and delete busy-wait BControl

**Copy from:** session `Say` (TASK-02), not old Background BC.

**Acceptance:**
- [ ] `go build ./pkg/wirepod/ttr/`
- [ ] Grep `bcontrol.go`: no `BehaviorControl(context.Background())` without timeout
- [ ] Grep: no busy-wait `default: continue` in say path

**Anti-patterns:** leaving dual implementations that still open BC on raw robot without cancel.

---

### TASK-06 — Migrate kgsim + interrupt to Session

**Files:**
- `wire-pod/chipper/pkg/wirepod/ttr/kgsim.go`
- `wire-pod/chipper/pkg/wirepod/ttr/kgsim_interrupt.go`

**What to implement:**
- Replace `vector.New` in StreamingKGSim / KGSim with `robotsession.Default.Get(ctx, esn)`
- Remove `defer robot.Close()` that closes shared session (anti-pattern after session) — use session lifecycle, not per-query Close
- Interrupt: subscribe to `SubscribeState` / wake_word via session fan-out **or** temporary extra whitelist on state stream including `wake_word` for duration of speak only (document choice). Prefer: state stream whitelist `robot_state`+`wake_word` always if interrupt needs it (low volume vs face)
- Control: `WithControl` around speak/animation segments; if full stream hold is still required for interrupt coordination, hold lease for speak duration under one cancelable ctx

**Bootstrap context:** `fix-connection-leak.py` sites (but invert Close semantics: session-owned), `kgsim_interrupt.go` whitelist.

**Acceptance:**
- [ ] `go build ./pkg/wirepod/ttr/`
- [ ] Grep `kgsim.go`: no `vector.New` (except comments)
- [ ] Grep `kgsim.go`: no `robot.Close()` on session vector after shared Get
- [ ] Interrupt does not open its own `vector.New`

**Anti-patterns:** Dropping session after each query; opening parallel EventStream for interrupt.

---

### TASK-07 — Migrate sensor / ambient / behavior-tick / face-probe

**Important:** These files are **patch-injected**. Implement by:

1. Editing the **patch generators** under `VectorIntelligence/shared/patches/` so full install stays correct, **and**
2. If a live patched chipper tree exists on the machine, edit the generated `.go` files to match.

**Targets:**
| Patch | Change |
|-------|--------|
| `add-sensor-reactions.py` | No `vector.New` loop; `Get` session; `StartStateStream` once; react via `Session.Say`; on fatal session error `Drop` + backoff |
| `add-ambient-loop.py` | `Session.Unary` / `ProbeFace` / `Say` instead of New/Close per cycle |
| `add-behavior-tick.py` | Same |
| `add-face-probe.py` | `Session.ProbeFace` |
| `fix-connection-leak.py` | Become no-op or comment “superseded by robotsession” once kgsim migrated |
| `fix-saytext-stream-leak.py` | Superseded by Session.Say — keep as safety if bcontrol still has old path during rollout |

**Acceptance:**
- [ ] Patch dry-run / re-apply does not reintroduce `vector.New` in sensor loop body
- [ ] Sensor still never continuous-face
- [ ] Ambient face probe ≤6s timeout preserved

**Anti-patterns:** double EventStream (sensor patch stream **and** session stream).

---

### TASK-08 — sdkapp façade over Session (or Close-safe pool)

**Files:** `chipper/pkg/wirepod/sdkapp/robot.go` (and minimal server/bcassume hooks)

**What to implement (choose A, document in code comment):**

**Option A (preferred):** `newRobot` uses `robotsession.Default.Get`; store `ESN` + session pointer; `removeRobot` calls `Registry.Drop` only if sdkapp is sole user — **simpler:** sdkapp does **not** Drop shared session; only cancels UI-specific streams; session remains for ttr loops.

**Option B:** sdkapp keeps own Vector but **must** call `Close()` on remove (copy Close from patch) — interim only if A is too invasive.

**Minimum for this plan:** Option A for EventStream stim: prefer reading session stim or secondary stream; `assumeBehaviorControl` uses `Session.WithControl` with DEFAULT/OVERRIDE from UI, long-hold via ctx cancelled when `BcAssumption` false (replace busy-wait with `select` on ctx.Done + flag).

**Acceptance:**
- [ ] `go build ./pkg/wirepod/sdkapp/`
- [ ] `removeRobot` never leaves orphan UI EventStream without cancel
- [ ] Document session sharing vs Drop policy in `robot.go` comment

**Anti-patterns:** second long primary EventStream with empty ConnectionId; removeRobot without cancelling BC.

---

### TASK-09 — `vars.GetRobot` / scripting / intentparam

**Files:**
- `chipper/pkg/vars/vars.go` (`GetRobot`)
- `chipper/pkg/scripting/*` as needed
- `chipper/pkg/wirepod/ttr/intentparam.go` if it still `vector.New`s

**What to implement:**
- `GetRobot` returns session-backed `*vector.Vector` for Lua **or** new API `GetSession(esn)` and update Lua bindings to use say via session
- Minimum: any `vector.New` in intentparam → `Session.Say`

**Acceptance:**
- [ ] Grep chipper for `vector.New` — only allowed under `robotsession/` (and maybe one deprecated wrapper)
- [ ] `go build ./...` from chipper module root

**Anti-patterns:** Lua keeping Vector after Drop; documenting Close for callers of shared Vector.

---

### TASK-10 — Wire startup/shutdown + patch/install docs

**Files:**
- `chipper/pkg/initwirepod/startserver.go` (or wherever wire-pod prints “started successfully” — sensor patch hooks here)
- `VectorIntelligence/linux/install.sh` / AGENTS.md short note
- Patch `add-sensor-reactions` start hook: call `StartStateStream` for all bots instead of independent loops where possible

**What to implement:**
- On chipper start: `robotsession.Default = NewRegistry()`
- Optionally warm-start sessions + state streams for each `BotInfo` robot
- On SIGTERM/shutdown path: `DropAll()`
- AGENTS.md: “Robot I/O goes through robotsession; do not vector.New in new code”

**Acceptance:**
- [ ] Grep start path constructs Registry
- [ ] AGENTS.md mentions robotsession
- [ ] install still applies `add-sdk-close.py` before build

**Anti-patterns:** starting state streams before BotInfo loaded; DropAll race with in-flight WithControl (WithControl should fail ctx).

---

### TASK-11 — Final verification phase

**Checks (all required):**

```bash
cd wire-pod/chipper
go test ./pkg/wirepod/robotsession/ -count=1
go build -o /tmp/chipper-build .
# Inventory: New only in robotsession
rg -n 'vector\.New\(' --glob '*.go' pkg/ | rg -v robotsession | rg -v '_test\.go' || true
# Expect: no hits (or only deprecated wrappers marked DO NOT USE)
rg -n 'BehaviorControl\(\s*context\.Background' --glob '*.go' pkg/ || true
# Expect: no hits
rg -n 'robot_observed_face' --glob '*.go' pkg/wirepod/ttr/
# Expect: only ProbeFace / short timeout paths
rg -n 'defer robot\.Close\(\)' --glob '*.go' pkg/wirepod/ttr/
# Expect: none that close shared session after Get (or none at all)
```

**Manual / integration (if robot available):**
- [ ] Chipper start → one TLS session to robot; sensor reacts without new dials per event
- [ ] Voice query → speak works; gateway log shows BehaviorControl cancel, not leak growth
- [ ] Face probe does not log primary `''` if state stream already primary
- [ ] Idle overnight: no wifi-icon wedge from connection budget

**Rollback note:** If session bugs block voice, temporary env `WIREPOD_ROBOTSESSION=0` is **optional** — only add if implementers need a kill switch; not required by contracts. Prefer fix-forward.

---

## Risk register

| Risk | Mitigation |
|------|------------|
| Patched Close missing → panic/nil | doc.go + install order; test build with third_party SDK |
| Control hold blocks ambient | ambient timeouts; voice uses longer ctx |
| Primary connection fights sdkapp | single ConnectionId policy; sdkapp secondary |
| Migrating patches vs unpatched tree | TASK-07 edits patch sources; CI/install applies |
| kgsim long control hold still heavy | session still wins on channel count; later narrow hold |
| No live robot in CI | pure unit tests offline; manual checklist |

---

## Success criteria (plan done)

1. **One gRPC channel per ESN** in steady state (sensor + voice + ambient share it).
2. **One long EventStream** for state (reconnect-owned by session).
3. **All BehaviorControl** via `WithControl` / `Say` with cancel+timeout.
4. **No** per-query `vector.New` in ttr voice path.
5. **No** continuous face EventStream.
6. `go test ./pkg/wirepod/robotsession` green; chipper builds.
7. Patches/install path does not reintroduce leaks.

---

## Appendix A — Copy-ready patterns (anchors)

| Pattern | Copy from |
|---------|-----------|
| ESN → IP:443 + GUID | `sdkapp/robot.go` L44–70 |
| Cancelable say BC | `fix-saytext-stream-leak.py` REPLACE_FUNC |
| Sync ambient BC | `add-ambient-loop.py` ambientReact |
| State flags on charger/calm | `add-sensor-reactions.py` status bit masks |
| Face 6s window | `add-face-probe.py` / ambient probeForKnownFace |
| Battery timeout health | `fix-connection-leak.py` 5s / ambient 3s |
| Gateway primary + BC release | `vector-cloud/gateway/message_handler.go` EventStream + BehaviorControlRequestHandler |
| Do not pool without Close | sdkapp `removeRobot` is **anti-pattern** for lifecycle |

## Appendix B — Explicit non-goals

- Changing `vic-gateway` or DAS logging
- Multiplexing multiple ESNs on one TCP connection
- Replacing supervisor wedge bounce
- Moving speech arbitration into Go (stays vector-ai `SpeechArbiter`)
- Upstream PR to fforchino (optional follow-up; local patch remains source of Close)

---

## Document control

| Field | Value |
|-------|--------|
| Created | 2026-07-20 |
| Status | Ready for `/do` or subagent-driven-development |
| Phase 0 | Complete (3 discovery agents + orchestrator synthesis) |
| Companion design | This plan is self-contained; optional later `docs/superpowers/specs/2026-07-20-chipper-robot-session-design.md` if reviewers want a narrative-only copy |

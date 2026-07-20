# Port Interrupt Patches onto Session-Based Interrupter — Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans. Checkbox tasks (`- [ ]`). One TASK per subagent where files collide.
>
> **Scope:** Restore four VI behaviors that the old Python patches applied to pre-robotsession `kgsim_interrupt.go`, now that interrupt uses `robotsession.Session` + `SubscribeState`.
>
> **Out of scope:** Redesigning robotsession, continuous `robot_observed_face` on the long EventStream, new gRPC channels, vector-ai FSMs.

**Goal:** After install + build, voice replies again support: (1) 5s wake-word grace, (2) back-button interrupt, (3) getImage wake-word mute window, (4) on-demand face for memory — without re-breaking install on anchor miss.

**Architecture decision (binding):**

| Feature | Where it lives after port |
|---------|---------------------------|
| Wake-word grace (5s) | **`kgsim_interrupt.go` main loop** — logic only |
| Back-button interrupt | **Same** — `RobotStatus_IS_BUTTON_PRESSED` on `robot_state` |
| Mute wake-word during getImage | **Setter already in `DoGetImage`**; **check in interrupt** on `WakeWordMutedUntil` |
| On-demand face | **Already covered** by `ObserveFaceBriefly` / `ProbeFace` at voice start — **do not** re-add face to long state stream |

Patches under `VectorIntelligence/shared/patches/` are updated so install is **idempotent**: if the Go tree already has the sentinels, print skip; if applied to a session-based interrupter missing logic, inject or no-op safely. Prefer **implement in Go first**, then make patches detect that state.

**Binding discovery date:** 2026-07-20.

---

## How to execute

1. Order: TASK-01 → TASK-02 → TASK-03 → TASK-04 → TASK-05 → TASK-06.
2. Do not invent APIs; only Phase 0 Allowed APIs.
3. After each task: run that task’s verification.
4. Path roots:
   - Chipper: `wire-pod/chipper/`
   - Patches: `VectorIntelligence/shared/patches/`
   - Install: `VectorIntelligence/windows/install.ps1`, `linux/install.sh`

### Subagent template

```
You are implementing TASK-NN of the interrupt-patch port plan.

READ:
- VectorIntelligence/docs/superpowers/plans/2026-07-20-port-interrupt-patches-robotsession.md
  (Phase 0 Allowed APIs, TASK-NN packet, anti-patterns)

WORKDIR: as in TASK-NN

DO: Interface Contract only. Copy behavior from listed patch sources / current Go.
MUST NOT: vector.New; long EventStream whitelist robot_observed_face; empty ConnectionId; busy-wait BC.

VERIFY: TASK-NN Acceptance Criteria.
REPORT: sources, diffs, commands, confidence, gaps.
```

---

## Phase 0 — Documentation Discovery (completed; binding)

### Sources consulted

| Source | Finding |
|--------|---------|
| `shared/patches/wake-word-grace-period.py` | 5s grace; sentinel `wakeWordGrace`; only wake-word gated |
| `shared/patches/add-button-interrupt.py` | `IS_BUTTON_PRESSED` (16); no grace; sentinel `source: back button` |
| `shared/patches/wake-word-mute-during-getimage.py` | `WakeWordMutedUntil` set 6s in `DoGetImage`; interrupt must check; sentinel `WakeWordMutedUntil` |
| `shared/patches/add-ondemand-face.py` | Old: add face to **per-speak** interrupt stream; calls `notifyFaceSeen` |
| `ttr/kgsim_interrupt.go` | Session-based; touch + immediate wake_word only; **no** grace/button/mute check |
| `ttr/kgsim_cmds.go` | `WakeWordMutedUntil` **already set** in `DoGetImage`; interrupt never reads it |
| `ttr/face_probe.go` | `ObserveFaceBriefly(esn)` → `ProbeFace` 6s; `notifyFaceSeen` |
| `robotsession/stream.go` | Long whitelist: `robot_state`, `wake_word` only; face forbidden on long stream |
| `install.ps1` / `install.sh` | Order: grace → button → mute → ondemand-face |

### Allowed APIs (only these)

#### Interrupt entry (current — do not change signature)

```go
// ttr/kgsim_interrupt.go
func InterruptKGSimWhenTouchedOrWaked(sess *robotsession.Session, stop chan bool, stopStop chan bool) bool
```

#### Session fan-out (already available)

```go
sess.StartStateStream(ctx)           // robot_state + wake_word
sess.SubscribeState(watchCtx)        // <-chan *vectorpb.Event
sess.LastRobotState()                // *vectorpb.RobotState
// Event oneofs on fan-out:
//   *vectorpb.Event_RobotState → GetRobotState()
//   *vectorpb.Event_WakeWord   → GetWakeWord()
```

#### RobotState fields / status bits

```go
rs.TouchData.GetRawTouchValue()  // existing touch interrupt (+50, 5 samples)
// Back button (from add-button-interrupt.py + messages.pb.go):
vectorpb.RobotStatus_ROBOT_STATUS_IS_BUTTON_PRESSED  // value 16
(rs.Status & uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_BUTTON_PRESSED)) != 0
```

#### Package mute var (same package `wirepod_ttr`)

```go
// kgsim_cmds.go — already present after prior mute patch half:
var WakeWordMutedUntil time.Time
// DoGetImage sets: WakeWordMutedUntil = time.Now().Add(6 * time.Second)
```

#### Face (do not re-open long stream)

```go
// face_probe.go
func ObserveFaceBriefly(esn string)
// robotsession
func (s *Session) ProbeFace(ctx context.Context, timeout time.Duration) (faceID int32, name string, sawAny bool, err error)
// sensor_reactions.go
func notifyFaceSeen(faceID int32, name string)  // or whatever exact signature exists — verify before call
```

### Behavioral contracts to restore

| Feature | Contract |
|---------|----------|
| Grace | From interrupter start, **5 seconds**: ignore `Event_WakeWord` (log once-style ok). After 5s: wake-word interrupts. Touch/button **not** grace-gated. |
| Button | Any `robot_state` with button bit set → interrupt immediately (no grace). Log contains `source: back button`. |
| Mute | On `Event_WakeWord`, if `time.Now().Before(WakeWordMutedUntil)` → ignore (log getImage mute). Touch/button not mute-gated. `DoGetImage` still sets ~6s window. |
| Face | Voice path already runs `ObserveFaceBriefly` at request start. **No** `robot_observed_face` on long session stream. On-demand patch becomes no-op documenting supersession. |

### Anti-patterns

| Do NOT | Why |
|--------|-----|
| Re-apply old Python anchors to session interrupter | Anchors dead; exit 1 kills install |
| `vector.New` in interrupt | Connection budget / plan |
| Whitelist `robot_observed_face` on `StartStateStream` | AGENTS / stream.go / firmware load |
| Grace or mute gating touch/button | Old product intent |
| Busy-wait / spin selects | CPU |
| Change `InterruptKGSimWhenTouchedOrWaked` channel protocol without updating `kgsim.go` | Breaks speak cancel |

### Suggested interrupt loop shape (copy into TASK-01)

```go
startTime := time.Now()
const wakeWordGrace = 5 * time.Second
// ... existing baseline touch seed ...
for {
  select {
  case <-watchCtx.Done():
    return false
  case ev, ok := <-evCh:
    if !ok { return false }
    switch ev.GetEvent().(type) { // use same switch style as current file
    case *vectorpb.Event_RobotState:
      rs := ev.GetRobotState()
      // existing touch debounce on TouchData
      if rs != nil && (rs.Status&uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_BUTTON_PRESSED)) != 0 {
        // log source: back button; stopResponse = true
      }
    case *vectorpb.Event_WakeWord:
      if time.Since(startTime) < wakeWordGrace {
        // log Ignoring wake-word during grace period; continue
      }
      if time.Now().Before(WakeWordMutedUntil) {
        // log Ignoring wake-word during getImage mute window; continue
      }
      // stopResponse = true (wake-word)
    }
    // existing valsAboveValue touch threshold → stopResponse
    // existing stop <- true + return true
  }
}
```

Match **actual** switch style in current `kgsim_interrupt.go` (EventType oneof), do not invent a different pattern.

---

## Project Bootstrap

```text
Chipper interrupt: wire-pod/chipper/pkg/wirepod/ttr/kgsim_interrupt.go
Chipper cmds:      wire-pod/chipper/pkg/wirepod/ttr/kgsim_cmds.go
Face:              wire-pod/chipper/pkg/wirepod/ttr/face_probe.go
Session stream:    wire-pod/chipper/pkg/wirepod/robotsession/stream.go
Patches:           VectorIntelligence/shared/patches/
Install:           VectorIntelligence/windows/install.ps1 , linux/install.sh
Plan:              VectorIntelligence/docs/superpowers/plans/2026-07-20-port-interrupt-patches-robotsession.md
```

---

## Tasks

### TASK-01 — Implement grace + button + mute check in `kgsim_interrupt.go`

**Files:** `wire-pod/chipper/pkg/wirepod/ttr/kgsim_interrupt.go` only

**Implement:**
1. `startTime` + `const wakeWordGrace = 5 * time.Second` at start of main watch phase (after baseline touch seed is OK).
2. On wake_word: grace check then `WakeWordMutedUntil` check then interrupt.
3. On robot_state: keep existing touch logic; add button bit interrupt + log `source: back button`.
4. Sentinels present as substrings for patch detection: `wakeWordGrace`, `source: back button`, and a reference to `WakeWordMutedUntil` in this file.

**Copy behavior from:**
- `shared/patches/wake-word-grace-period.py` NEW_BLOCK intent
- `shared/patches/add-button-interrupt.py` button check
- `shared/patches/wake-word-mute-during-getimage.py` mute check after grace
- Current file’s touch debounce + channel protocol

**Verify:**
- [ ] `go build ./pkg/wirepod/ttr/` (or CGO-free package build)
- [ ] Grep: `wakeWordGrace`, `source: back button`, `WakeWordMutedUntil` in `kgsim_interrupt.go`
- [ ] Grep: no `vector.New`, no `robot_observed_face` in interrupt file

**Anti-patterns:** changing function signature; gating touch by grace.

---

### TASK-02 — Confirm getImage mute setter (cmds)

**Files:** `wire-pod/chipper/pkg/wirepod/ttr/kgsim_cmds.go` (edit only if missing)

**Implement:**
- If `WakeWordMutedUntil` and `DoGetImage` 6s set already exist → no code change; document in report.
- If missing → re-apply mute **setter only** from `wake-word-mute-during-getimage.py` patch_cmds half.

**Verify:**
- [ ] Grep `WakeWordMutedUntil` and `DoGetImage` mute set in `kgsim_cmds.go`
- [ ] Build ttr package

**Anti-patterns:** changing photo UX (countdown) in this task.

---

### TASK-03 — Face: document supersession; no long-stream face

**Files:**
- Optionally short comment in `kgsim_interrupt.go` package or function comment
- `shared/patches/add-ondemand-face.py` (rewrite for TASK-04 may overlap — this task only verifies face path)

**Implement:**
- Confirm `ObserveFaceBriefly` still invoked from `intent_graph` (or equivalent voice entry).
- Confirm `robotsession` long stream still excludes `robot_observed_face`.
- **No** face subscription in interrupt.

**Verify:**
- [ ] Grep `ObserveFaceBriefly` call site under chipper
- [ ] Grep `StartStateStream` / whitelist in `stream.go`: no `robot_observed_face`
- [ ] Report: face feature = voice-start probe, not interrupt stream

**Anti-patterns:** adding face to session long whitelist.

---

### TASK-04 — Rewrite the four patch scripts for session tree + install safety

**Files (all under `VectorIntelligence/shared/patches/`):**
1. `wake-word-grace-period.py`
2. `add-button-interrupt.py`
3. `wake-word-mute-during-getimage.py`
4. `add-ondemand-face.py`

**Policy for each:**

| Patch | New behavior |
|-------|----------------|
| grace | If `wakeWordGrace` already in `kgsim_interrupt.go` → print already patched, exit 0. Else inject session-style grace (match post-TASK-01 source as optional second path only if implementer can safely anchor). **Minimum: exit 0 when present; exit 0 with clear “requires robotsession tree already ported” if absent** — prefer **not** failing install. |
| button | Same with sentinel `source: back button` |
| mute | cmds: keep existing idempotent `WakeWordMutedUntil` inject; interrupt: if `WakeWordMutedUntil` referenced in interrupt → skip; never require old grace-shaped OLD block |
| ondemand-face | If `face_probe.go` exists or `ObserveFaceBriefly` exists → **no-op success** with message superseded by face_probe/ProbeFace. Do **not** try to add face to interrupt whitelist. |

**Critical:** All four must **exit 0** on the current robotsession tree so `install.ps1` `Patch` does not Fail.

**Verify:**
```bash
# From a clean simulation against current tree:
python3 wake-word-grace-period.py $WP/chipper/pkg/wirepod/ttr/kgsim_interrupt.go  # exit 0
python3 add-button-interrupt.py $WP/.../kgsim_interrupt.go  # exit 0
python3 wake-word-mute-during-getimage.py $WP  # exit 0
python3 add-ondemand-face.py $WP/.../kgsim_interrupt.go  # exit 0
```

**Anti-patterns:** exit 1 on “already robotsession”; reintroducing dead anchors as hard Fail.

---

### TASK-05 — Install script notes (optional soft messaging)

**Files:** `windows/install.ps1`, `linux/install.sh` (comment-only preferred)

**Implement:**
- One-line comments above the four patches: “session interrupter: patches must exit 0 / idempotent after robotsession port.”
- Do **not** remove the four Patch calls if TASK-04 makes them safe no-ops.

**Verify:**
- [ ] Install scripts still invoke all four in order grace → button → mute → face
- [ ] No hard-coded skip that drops mute cmds path

---

### TASK-06 — Final verification

```bash
export PATH="${PATH}:/home/cam-test/.local/go/bin"  # if needed
cd wire-pod/chipper
go build ./pkg/wirepod/ttr/ ./pkg/wirepod/robotsession/

# Logic present
grep -n 'wakeWordGrace\|source: back button\|WakeWordMutedUntil' \
  pkg/wirepod/ttr/kgsim_interrupt.go pkg/wirepod/ttr/kgsim_cmds.go

# Face not on long stream
grep -n 'robot_observed_face' pkg/wirepod/robotsession/stream.go
# expect comments only / ProbeFace only, not state whitelist List

# Patches dry-run exit 0
python3 ../../VectorIntelligence/shared/patches/wake-word-grace-period.py pkg/wirepod/ttr/kgsim_interrupt.go
# ... all four ...
```

**Manual (if robot available):**
- [ ] Start speak; “Hey Vector” in first 2s does not cancel reply
- [ ] After 5s+, wake-word cancels
- [ ] Back button cancels immediately
- [ ] getImage / shutter does not self-cancel via wake-word for ~6s
- [ ] Face memory / enrollment still works via voice-start probe

**Success criteria:**
1. Install no longer dies on the four patches.
2. Grace + button + mute-check live in interrupt.
3. Face remains probe-based, not long-stream firehose.
4. ttr + robotsession packages build.

---

## Risk register

| Risk | Mitigation |
|------|------------|
| Button level-trigger re-fires | Interrupter returns on first interrupt (current design) |
| Mute var race | Same package; simple time.Time write; acceptable |
| Fresh stock wire-pod without robotsession | Patches exit 0 with message; robotsession plan is prerequisite |
| Dual face paths (probe + old) | On-demand patch no-ops; only probe |

---

## Document control

| Field | Value |
|-------|--------|
| Created | 2026-07-20 |
| Status | Ready for `/do` |
| Depends on | robotsession package + session-based `kgsim_interrupt.go` (already landed) |
| Plan path | `VectorIntelligence/docs/superpowers/plans/2026-07-20-port-interrupt-patches-robotsession.md` |

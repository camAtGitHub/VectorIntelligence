#!/usr/bin/env python3
"""Add multi-behavior presence tick loop to Wire-Pod (Work Day Mode).

Creates `behavior_tick.go` with a thin loop that:
  1. Estimates occupancy cheaply (recent face sighting sticky window)
  2. POSTs /v1/behaviors/tick with occupied (+ face only when probing)
  3. Speaks if vector-ai returns a non-empty speak line
  4. If need_identity, runs a short face probe and ticks again with face

Uses robotsession: ProbeFace for occupancy/identity glances, ambientReact
(Session.WithControl) for speech. No per-tick vector.New / Close.

Does NOT replace ambient, greeting, or sensor loops — runs alongside them.

Named-face probes run only when vector-ai sets need_identity (junctures),
not every tick. Optional sparse occupancy probe every 5 minutes.

Patches startserver.go to launch StartBehaviorTickLoop() once at boot.

Idempotent.
"""
import re
import sys
from pathlib import Path

BEHAVIOR_GO_FILENAME = "behavior_tick.go"
SENTINEL_STARTSERVER = "StartBehaviorTickLoop"

BEHAVIOR_GO = r'''package wirepod_ttr

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/kercre123/wire-pod/chipper/pkg/vars"
	"github.com/kercre123/wire-pod/chipper/pkg/wirepod/robotsession"
)

// Behavior tick loop: thin presence reporter for vector-ai's multi-behavior
// runtime (Work Day Mode first). Occupancy is approximate — sticky on recent
// face sightings — and named-face ID runs only when vector-ai asks via
// need_identity (morning start, late arm, optional long-absence re-ID).
//
// Face probes use Session.ProbeFace (≤ behaviorFaceProbeWindow, secondary
// ConnectionId). Speech uses ambientReact → Session.WithControl.
// Ambient / greeting / sensor loops are unchanged and continue in parallel.

const (
	behaviorTickInterval = 60 * time.Second
	// Short sticky after a positive face sighting. Empty probes clear occupancy
	// immediately so away scolds track WORKDAY_AWAY_S (~30m), not sticky+away.
	// (A 15m sticky made effective away ~45m.)
	behaviorOccupancySticky  = 2 * time.Minute
	// Sparse occupancy glance when we think we might still be occupied or need
	// to confirm empty (not a named-ID stream every tick).
	behaviorSparseProbeEvery = 2 * time.Minute
	behaviorFaceProbeWindow  = 4 * time.Second
)

// lastSeenAnyFace is the unix time any face event was observed (greeting,
// sensor face notify, or a behavior probe). Used as a cheap occupancy proxy.
// Zero means "unknown / empty" after a failed probe clears it.
var lastSeenAnyFace atomic.Int64

// lastBehaviorNeedIdentity is sticky: after a need_identity response we probe
// on the next cycle before the normal tick.
var lastBehaviorNeedIdentity atomic.Bool

// lastSparseOccupancyProbe unix time of last optional short face glance for
// occupancy (not full ID requirement — any face counts).
var lastSparseOccupancyProbe atomic.Int64

// NoteAnyFaceSeen records that someone was at the desk zone (any face).
// Safe to call from greeting / sensor paths; behavior tick also updates it.
func NoteAnyFaceSeen() {
	lastSeenAnyFace.Store(time.Now().Unix())
}

// NoteDeskEmpty clears sticky occupancy after a probe saw no face.
func NoteDeskEmpty() {
	lastSeenAnyFace.Store(0)
}

// StartBehaviorTickLoop launches the presence tick loop per enrolled robot.
// Call once at chipper startup (alongside ambient / sensor).
func StartBehaviorTickLoop() {
	time.Sleep(45 * time.Second) // let ambient/sensor settle first
	for _, bot := range vars.BotInfo.Robots {
		go runBehaviorTickLoop(bot.Esn)
	}
}

func runBehaviorTickLoop(esn string) {
	fmt.Printf("[behavior-tick] starting for %s (robotsession)\n", esn)
	failStreak := 0
	for {
		sleep := behaviorTickInterval
		if failStreak > 0 {
			shift := failStreak
			if shift > 3 {
				shift = 3
			}
			sleep = behaviorTickInterval * time.Duration(1<<shift)
		}
		time.Sleep(sleep)

		// Skip only mid-conversation to avoid hammering the robot during chat.
		// Do NOT gate on ambientInNightHours: vector-ai owns work windows via
		// WORKDAY_TZ (host local midnight ≠ user TZ can miss morning arm).
		if recentlyConversed() {
			continue
		}

		ok := behaviorTickOnce(esn)
		if ok {
			failStreak = 0
		} else {
			failStreak++
		}
	}
}

type behaviorTickResponse struct {
	Speak        string `json:"speak"`
	NeedIdentity bool   `json:"need_identity"`
	Error        string `json:"error,omitempty"`
}

func askVectorAIBehaviorTick(occupied bool, face map[string]interface{}, onCharger, voiceRecent bool) behaviorTickResponse {
	payload := map[string]interface{}{
		"occupied":     occupied,
		"on_charger":   onCharger,
		"voice_recent": voiceRecent,
	}
	if face != nil {
		payload["face"] = face
	}
	body, _ := json.Marshal(payload)
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(vectorAIBase+"/v1/behaviors/tick", "application/json", bytes.NewReader(body))
	if err != nil {
		fmt.Printf("[behavior-tick] vector-ai call failed: %v\n", err)
		return behaviorTickResponse{}
	}
	defer resp.Body.Close()
	var result behaviorTickResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		fmt.Printf("[behavior-tick] vector-ai bad json: %v\n", err)
		return behaviorTickResponse{}
	}
	if result.Error != "" {
		fmt.Printf("[behavior-tick] vector-ai error: %s\n", result.Error)
	}
	return result
}

func behaviorOccupiedNow() bool {
	last := lastSeenAnyFace.Load()
	if last == 0 {
		return false
	}
	return time.Now().Unix()-last < int64(behaviorOccupancySticky/time.Second)
}

// behaviorProbeAnyFace uses Session.ProbeFace; any face (named or not)
// counts for occupancy. Named faces are returned for identity junctures.
func behaviorProbeAnyFace(esn string) (faceID int32, name string, sawAny bool) {
	if robotsession.Default == nil {
		return 0, "", false
	}
	ctx, cancel := context.WithTimeout(context.Background(), behaviorFaceProbeWindow+2*time.Second)
	defer cancel()
	sess, err := robotsession.Default.Get(ctx, esn)
	if err != nil {
		fmt.Printf("[behavior-tick] session get failed for %s: %v\n", esn, err)
		return 0, "", false
	}
	fid, fname, saw, err := sess.ProbeFace(ctx, behaviorFaceProbeWindow)
	if err != nil {
		fmt.Printf("[behavior-tick] ProbeFace failed for %s: %v\n", esn, err)
		return 0, "", false
	}
	if saw {
		NoteAnyFaceSeen()
	}
	return fid, fname, saw
}

func behaviorTickOnce(esn string) bool {
	if robotsession.Default == nil {
		fmt.Printf("[behavior-tick] robotsession.Default nil for %s\n", esn)
		return false
	}

	// Light health: ensure session exists (BatteryState inside Get).
	getCtx, getCancel := context.WithTimeout(context.Background(), 10*time.Second)
	sess, err := robotsession.Default.Get(getCtx, esn)
	getCancel()
	if err != nil {
		fmt.Printf("[behavior-tick] session get failed for %s: %v\n", esn, err)
		return false
	}
	_ = sess.StartStateStream(context.Background())

	needID := lastBehaviorNeedIdentity.Load()
	var facePayload map[string]interface{}

	// Probe when identity is needed, or periodically to confirm occupied/empty.
	// Empty probe clears sticky occupancy so away timers match real desk leave.
	nowUnix := time.Now().Unix()
	sparseDue := nowUnix-lastSparseOccupancyProbe.Load() >= int64(behaviorSparseProbeEvery/time.Second)
	probed := false
	if needID || sparseDue {
		lastSparseOccupancyProbe.Store(nowUnix)
		probed = true
		fid, fname, saw := behaviorProbeAnyFace(esn)
		if saw {
			if fname != "" && fid > 0 {
				facePayload = map[string]interface{}{
					"face_id":     fid,
					"name":        fname,
					"is_stranger": false,
				}
			} else {
				// Stranger / unnamed (Vector often uses negative face_id).
				facePayload = map[string]interface{}{
					"face_id":     fid,
					"name":        fname,
					"is_stranger": true,
				}
			}
		} else {
			NoteDeskEmpty()
		}
		if needID {
			lastBehaviorNeedIdentity.Store(false)
		}
	}

	occupied := behaviorOccupiedNow()
	_ = probed
	onCharger := sess.OnCharger()
	if sess.LastRobotState() == nil {
		onCharger = IsOnCharger()
	}
	voiceRecent := recentlyConversed()

	result := askVectorAIBehaviorTick(occupied, facePayload, onCharger, voiceRecent)
	if result.NeedIdentity {
		lastBehaviorNeedIdentity.Store(true)
	}

	line := result.Speak
	if line == "" {
		return true
	}
	// Trust vector-ai: it already applied quiet / voice suppress / min-gap and
	// committed speech-gated side effects (poke/away/late_check). Dropping the
	// line here after a post-HTTP recentlyConversed check would leave timers
	// advanced without speech — always deliver non-empty speak.
	fmt.Printf("[behavior-tick] speak: %q\n", line)
	// Keep ambient/greeting clear of our proactive line.
	MarkVoiceActivity()
	lastAmbientReaction.Store(time.Now().Unix())
	ambientReact(esn, line, false)
	return true
}
'''


def patch_startserver(path: Path) -> bool:
    """Insert `go ttr.StartBehaviorTickLoop()` after ambient start if present."""
    src = path.read_text(encoding="utf-8")
    if SENTINEL_STARTSERVER in src:
        print(f"[behavior-tick] {path.name} already patched.")
        return False

    # Prefer inserting after ambient; fall back to after startup banner.
    ambient_line = "go ttr.StartAmbientLoop()\n"
    insert = "\tgo ttr.StartBehaviorTickLoop()\n"
    if ambient_line in src:
        src = src.replace(ambient_line, ambient_line + insert, 1)
    else:
        anchor = 'fmt.Println("\\033[33m\\033[1mwire-pod started successfully!\\033[0m")\n'
        if anchor not in src:
            print(f"[behavior-tick] startup banner not found in {path}", file=sys.stderr)
            sys.exit(1)
        src = src.replace(anchor, anchor + "\n" + insert, 1)

    if 'ttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"' not in src:
        src = re.sub(
            r'(import \(\n)',
            r'\1\tttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"\n',
            src,
            count=1,
        )

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[behavior-tick] {path.name} patched.")
    return True


def write_behavior_go(ttr_dir: Path) -> bool:
    target = ttr_dir / BEHAVIOR_GO_FILENAME
    if target.exists() and target.read_text(encoding="utf-8") == BEHAVIOR_GO:
        print(f"[behavior-tick] {BEHAVIOR_GO_FILENAME} already in place.")
        return False
    target.write_text(BEHAVIOR_GO, encoding="utf-8", newline="\n")
    print(f"[behavior-tick] wrote {target}")
    return True


def patch_ambient_face_hook(ttr_dir: Path) -> bool:
    """If ambient.go exists, call NoteAnyFaceSeen when a known face is probed.

    Improves occupancy sticky window without per-tick ID from the behavior loop.
    """
    path = ttr_dir / "ambient.go"
    if not path.exists():
        print("[behavior-tick] ambient.go not found - skip face occupancy hook.")
        return False
    src = path.read_text(encoding="utf-8")
    if "NoteAnyFaceSeen()" in src:
        print("[behavior-tick] ambient.go already hooks NoteAnyFaceSeen.")
        return False
    # robotsession ProbeFace path in probeForKnownFace
    old = """\tfaceID, name, _, err := sess.ProbeFace(ctx, 6*time.Second)
\tif err != nil {
\t\tfmt.Printf("[greeting] ProbeFace failed for %s: %v\\n", esn, err)
\t\treturn 0, ""
\t}
\tif faceID > 0 && name != "" {
\t\treturn faceID, name
\t}
\treturn 0, ""
}"""
    new = """\tfaceID, name, _, err := sess.ProbeFace(ctx, 6*time.Second)
\tif err != nil {
\t\tfmt.Printf("[greeting] ProbeFace failed for %s: %v\\n", esn, err)
\t\treturn 0, ""
\t}
\tif faceID > 0 && name != "" {
\t\tNoteAnyFaceSeen()
\t\treturn faceID, name
\t}
\treturn 0, ""
}"""
    if old not in src:
        # Fallback: older raw EventStream probe body
        old_legacy = """\t\t\tif rof.GetFaceId() > 0 && rof.GetName() != "" {
\t\t\t\treturn rof.GetFaceId(), rof.GetName()
\t\t\t}"""
        new_legacy = """\t\t\tif rof.GetFaceId() > 0 && rof.GetName() != "" {
\t\t\t\tNoteAnyFaceSeen()
\t\t\t\treturn rof.GetFaceId(), rof.GetName()
\t\t\t}"""
        if old_legacy not in src:
            print("[behavior-tick] probeForKnownFace pattern not found - skip hook.")
            return False
        path.write_text(src.replace(old_legacy, new_legacy, 1), encoding="utf-8", newline="\n")
        print("[behavior-tick] ambient.go hooked NoteAnyFaceSeen on known face (legacy).")
        return True
    path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="\n")
    print("[behavior-tick] ambient.go hooked NoteAnyFaceSeen on known face.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    startserver = wirepod / "chipper" / "pkg" / "initwirepod" / "startserver.go"
    if not ttr_dir.exists() or not startserver.exists():
        print(f"[behavior-tick] target dirs not found under {wirepod}", file=sys.stderr)
        sys.exit(1)
    write_behavior_go(ttr_dir)
    patch_startserver(startserver)
    patch_ambient_face_hook(ttr_dir)

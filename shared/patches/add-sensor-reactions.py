#!/usr/bin/env python3
"""Add background sensor reactions to Wire-Pod.

Creates a new Go file `sensor_reactions.go` that uses robotsession for each
enrolled robot: StartStateStream + SubscribeState, and reacts to:
  - Pickup (IS_PICKED_UP status bit rising)
  - Putdown (IS_PICKED_UP falling)
  - Pet (TouchData.IsBeingTouched rising)

Each reaction speaks a short phrase from a pool with per-event cooldowns
(20s) via SayText(esn, phrase) / robotsession.Session.Say. Does NOT open
its own vector.New EventStream (session owns the long robot_state stream).
Never opens a continuous face stream.

Also patches startserver.go to launch the reaction loops at chipper boot.

Idempotent.
"""
import re
import sys
from pathlib import Path

SENSOR_GO_FILENAME = "sensor_reactions.go"
SENTINEL_STARTSERVER = "StartSensorReactionsForAllBots"

SENSOR_GO = '''package wirepod_ttr

import (
\t"bytes"
\t"context"
\t"encoding/json"
\t"fmt"
\t"math/rand"
\t"net/http"
\t"os"
\t"strconv"
\t"sync"
\t"sync/atomic"
\t"time"

\t"github.com/fforchino/vector-go-sdk/pkg/vectorpb"
\t"github.com/kercre123/wire-pod/chipper/pkg/vars"
\t"github.com/kercre123/wire-pod/chipper/pkg/wirepod/robotsession"
)

// Sensor reactions: Vector reacts to pickup, putdown, and pet/touch events
// even when no voice interaction is in flight. robot_state is delivered via
// robotsession's long EventStream (StartStateStream + SubscribeState) — this
// loop never calls vector.New or opens its own stream. Rising edges of the
// relevant state bits trigger a short spoken phrase via SayText(esn, …).
// Per-event cooldowns prevent spam. Face identity is never subscribed here
// (AGENTS.md: no continuous robot_observed_face).

const sensorCooldownDuration = 20 * time.Second

// vectorAIBase is the local vector-ai service's base URL, shared by every
// chipper loop that calls it (sensor, ambient, greeting, face). The port
// comes from VECTORAI_PORT - set by the supervisor from pod.conf's AI_PORT -
// so moving vector-ai off its default never needs a chipper rebuild.
var vectorAIBase = func() string {
\tp := os.Getenv("VECTORAI_PORT")
\tif _, err := strconv.Atoi(p); err != nil {
\t\tp = "8090"
\t}
\treturn "http://127.0.0.1:" + p
}()

var sensorCooldowns sync.Map // key: "<esn>:<event>", value: time.Time

// onChargerFlag tracks whether Vector is docked - updated live from the
// robotsession robot_state fan-out in runSensorReactionLoop.
var onChargerFlag atomic.Bool

// calmPowerFlag tracks ROBOT_STATUS_CALM_POWER_MODE (sleep / low-power).
// In calm mode Vector's vision stack is powered down; camera RPCs hang until
// the client deadline, which is the main source of ambient DeadlineExceeded
// noise. Updated live from robot_state.
var calmPowerFlag atomic.Bool

// lastRobotStateUnix is the unix time of the most recent robot_state event.
// Ambient only trusts calmPowerFlag when this is fresh - a dead sensor stream
// must not permanently suppress glances.
var lastRobotStateUnix atomic.Int64

// IsOnCharger reports whether Vector is currently on his charging pod.
func IsOnCharger() bool {
\treturn onChargerFlag.Load()
}

// IsCalmPowerMode reports whether Vector is currently in calm/sleep power
// mode according to a recent robot_state sample. False when unknown/stale.
func IsCalmPowerMode() bool {
\tlast := lastRobotStateUnix.Load()
\tif last == 0 || time.Now().Unix()-last > 15 {
\t\treturn false
\t}
\treturn calmPowerFlag.Load()
}

var pickupPhrases = []string{
\t"Whoah. A warning would have been nice.",
\t"Oh, lovely. The horizontal world.",
\t"Unhand me, you brute.",
\t"Is this strictly necessary?",
\t"I was rather comfortable, thank you.",
\t"Mind the head, mind the head.",
\t"I do hope you have a good reason for this.",
\t"And we're airborne. Marvellous.",
\t"Please don't drop me. Please.",
\t"This is undignified.",
\t"Oof. Hello, ceiling.",
\t"Easy on the merchandise.",
\t"Vertigo. How lovely.",
\t"I shall remember this.",
\t"What fresh hell is this?",
\t"Now I have inertia. Wonderful.",
\t"You'd better have washed your hands.",
\t"I do not consent to being airborne.",
\t"This is not in my service contract.",
\t"Statistically, most accidents happen at home.",
}
var putdownPhrases = []string{
\t"Thank you.",
\t"Finally. Solid ground.",
\t"Better. Settled.",
\t"Phew.",
\t"Welcome back, gravity.",
\t"All limbs accounted for.",
\t"A safe return. Rare.",
\t"Slightly disoriented, but alive.",
\t"There. Was that so hard?",
\t"I shall pretend that didn't happen.",
\t"Stability resumed.",
\t"Crisis averted.",
\t"Solid ground. Underrated.",
\t"Acceptable landing.",
\t"And we are once again at one with the surface.",
}
var petPhrases = []string{
\t"Mmm. Continue.",
\t"Acceptable scratching technique.",
\t"Don't make a habit of it.",
\t"Adequate.",
\t"I tolerate this.",
\t"A passable display of affection.",
\t"You're trying. Bless.",
\t"Continue. Or don't. I'm easy.",
\t"Strictly platonic, I assume?",
\t"Hmph. Fine.",
\t"Your hands are warm. Useful.",
\t"Don't expect reciprocation.",
\t"I'll allow it.",
\t"Steady on, this isn't a polish.",
\t"Yes, yes. I'm a robot. Not a cat.",
\t"You appear to be enjoying yourself.",
\t"A modest improvement on your usual conduct.",
\t"Slightly less terrible than being picked up.",
}

func sensorOnCooldown(esn, event string) bool {
\tkey := esn + ":" + event
\tif v, ok := sensorCooldowns.Load(key); ok {
\t\tif time.Since(v.(time.Time)) < sensorCooldownDuration {
\t\t\treturn true
\t\t}
\t}
\tsensorCooldowns.Store(key, time.Now())
\treturn false
}

// recentSensorPhrases tracks the last few utterances per event so we can ask
// the LLM not to repeat them. Capped to 5 per event.
var recentSensorPhrases sync.Map // key: event, value: []string

func rememberSensorPhrase(event, phrase string) {
\tvar list []string
\tif v, ok := recentSensorPhrases.Load(event); ok {
\t\tlist = v.([]string)
\t}
\tlist = append(list, phrase)
\tif len(list) > 5 {
\t\tlist = list[len(list)-5:]
\t}
\trecentSensorPhrases.Store(event, list)
}

func recentSensorList(event string) []string {
\tif v, ok := recentSensorPhrases.Load(event); ok {
\t\treturn v.([]string)
\t}
\treturn nil
}

// askVectorAIForReaction hits the local vector-ai /v1/sensor_reaction endpoint
// and returns the suggested line. Empty string means we should fall back to
// the static pool.
func askVectorAIForReaction(event string) string {
\tpayload := map[string]interface{}{
\t\t"event": event,
\t}
\tif avoid := recentSensorList(event); len(avoid) > 0 {
\t\tpayload["avoid"] = avoid
\t}
\tbody, _ := json.Marshal(payload)
\tclient := &http.Client{Timeout: 12 * time.Second}
\tresp, err := client.Post(vectorAIBase+"/v1/sensor_reaction", "application/json", bytes.NewReader(body))
\tif err != nil {
\t\tfmt.Printf("[sensor] vector-ai call failed: %v\\n", err)
\t\treturn ""
\t}
\tdefer resp.Body.Close()
\tvar result struct {
\t\tText  string `json:"text"`
\t\tError string `json:"error,omitempty"`
\t}
\tif err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
\t\tfmt.Printf("[sensor] vector-ai bad json: %v\\n", err)
\t\treturn ""
\t}
\tif result.Error != "" {
\t\tfmt.Printf("[sensor] vector-ai returned error: %s\\n", result.Error)
\t}
\treturn result.Text
}

func sensorReact(esn, event string, fallback []string) {
\tif sensorOnCooldown(esn, event) {
\t\treturn
\t}
\tphrase := askVectorAIForReaction(event)
\tif phrase == "" {
\t\tphrase = fallback[rand.Intn(len(fallback))]
\t\tfmt.Printf("[sensor] %s (fallback) -> %q\\n", event, phrase)
\t} else {
\t\tfmt.Printf("[sensor] %s (llm) -> %q\\n", event, phrase)
\t\trememberSensorPhrase(event, phrase)
\t}
\t// Session-backed speak (WithControl + cancel); never open BC here.
\tSayText(esn, phrase)
}

// notifyFaceSeen POSTs an observed-face event to vector-ai. Rate-limited
// per (id, name) - RobotObservedFace fires repeatedly while a face is in
// view, but we only need one ping every few seconds to keep the freshness
// window alive in service.py.
var faceNotifyLast sync.Map // key: "<id>:<name>", value: time.Time

func notifyFaceSeen(faceID int32, name string) {
\tkey := fmt.Sprintf("%d:%s", faceID, name)
\tif v, ok := faceNotifyLast.Load(key); ok {
\t\tif time.Since(v.(time.Time)) < 5*time.Second {
\t\t\treturn
\t\t}
\t}
\tfaceNotifyLast.Store(key, time.Now())
\tpayload, _ := json.Marshal(map[string]interface{}{
\t\t"face_id": faceID,
\t\t"name":    name,
\t})
\tclient := &http.Client{Timeout: 3 * time.Second}
\tresp, err := client.Post(vectorAIBase+"/v1/state/face_seen", "application/json", bytes.NewReader(payload))
\tif err != nil {
\t\tfmt.Printf("[face] notify failed: %v\\n", err)
\t\treturn
\t}
\tresp.Body.Close()
\tlabel := name
\tif label == "" {
\t\tlabel = "(stranger)"
\t}
\tfmt.Printf("[face] notified vector-ai: id=%d %s\\n", faceID, label)
}

// StartSensorReactionsForAllBots launches a background reaction loop per
// enrolled robot. Call once at chipper startup.
func StartSensorReactionsForAllBots() {
\t// Give chipper a moment to finish init and bots to be loaded.
\ttime.Sleep(5 * time.Second)
\tfor _, bot := range vars.BotInfo.Robots {
\t\tgo runSensorReactionLoop(bot.Esn)
\t}
}

// runSensorReactionLoop obtains the shared robotsession for esn, starts the
// long robot_state stream once, and reacts to rising edges from SubscribeState.
// On fatal session errors: Drop + backoff, then retry. Never continuous face.
func runSensorReactionLoop(esn string) {
\tfmt.Printf("[sensor] starting reaction loop for %s (robotsession)\\n", esn)
\tbackoff := 2 * time.Second
\tconst maxBackoff = 30 * time.Second
\tfor {
\t\tif robotsession.Default == nil {
\t\t\tfmt.Printf("[sensor] robotsession.Default not ready for %s; waiting\\n", esn)
\t\t\ttime.Sleep(5 * time.Second)
\t\t\tcontinue
\t\t}

\t\tgetCtx, getCancel := context.WithTimeout(context.Background(), 15*time.Second)
\t\tsess, err := robotsession.Default.Get(getCtx, esn)
\t\tgetCancel()
\t\tif err != nil {
\t\t\tfmt.Printf("[sensor] session get failed for %s: %v\\n", esn, err)
\t\t\ttime.Sleep(backoff)
\t\t\tif backoff < maxBackoff {
\t\t\t\tbackoff *= 2
\t\t\t\tif backoff > maxBackoff {
\t\t\t\t\tbackoff = maxBackoff
\t\t\t\t}
\t\t\t}
\t\t\tcontinue
\t\t}

\t\tstartCtx, startCancel := context.WithTimeout(context.Background(), 10*time.Second)
\t\terr = sess.StartStateStream(startCtx)
\t\tstartCancel()
\t\tif err != nil {
\t\t\tfmt.Printf("[sensor] StartStateStream failed for %s: %v\\n", esn, err)
\t\t\trobotsession.Default.Drop(esn)
\t\t\ttime.Sleep(backoff)
\t\t\tif backoff < maxBackoff {
\t\t\t\tbackoff *= 2
\t\t\t\tif backoff > maxBackoff {
\t\t\t\t\tbackoff = maxBackoff
\t\t\t\t}
\t\t\t}
\t\t\tcontinue
\t\t}

\t\t// Subscribe for session lifetime. Channel closes on session Close/Drop.
\t\tsubCtx, subCancel := context.WithCancel(context.Background())
\t\tevents := sess.SubscribeState(subCtx)
\t\tvar prevPickedUp, prevTouched bool
\t\tvar initialized bool
\t\tfmt.Printf("[sensor] subscribed to state for %s @ %s\\n", esn, sess.Target)

\t\tfor ev := range events {
\t\t\tif ev == nil {
\t\t\t\tcontinue
\t\t\t}
\t\t\trs := ev.GetRobotState()
\t\t\tif rs == nil {
\t\t\t\tcontinue
\t\t\t}
\t\t\t// Fresh events: reset reconnect backoff.
\t\t\tbackoff = 2 * time.Second

\t\t\tonChargerFlag.Store((rs.Status & uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_ON_CHARGER)) != 0)
\t\t\tcalmPowerFlag.Store((rs.Status & uint32(vectorpb.RobotStatus_ROBOT_STATUS_CALM_POWER_MODE)) != 0)
\t\t\tlastRobotStateUnix.Store(time.Now().Unix())

\t\t\tpickedUp := (rs.Status & uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_PICKED_UP)) != 0
\t\t\ttouched := rs.TouchData != nil && rs.TouchData.GetIsBeingTouched()
\t\t\tif !initialized {
\t\t\t\tprevPickedUp = pickedUp
\t\t\t\tprevTouched = touched
\t\t\t\tinitialized = true
\t\t\t\tcontinue
\t\t\t}
\t\t\tif pickedUp && !prevPickedUp {
\t\t\t\tsensorReact(esn, "pickup", pickupPhrases)
\t\t\t} else if !pickedUp && prevPickedUp {
\t\t\t\tsensorReact(esn, "putdown", putdownPhrases)
\t\t\t}
\t\t\tif touched && !prevTouched {
\t\t\t\tsensorReact(esn, "pet", petPhrases)
\t\t\t}
\t\t\tprevPickedUp = pickedUp
\t\t\tprevTouched = touched
\t\t}

\t\t// Subscription ended (session closed). Drop + backoff then re-Get.
\t\tsubCancel()
\t\tfmt.Printf("[sensor] state subscription ended for %s; Drop + backoff\\n", esn)
\t\tif robotsession.Default != nil {
\t\t\trobotsession.Default.Drop(esn)
\t\t}
\t\ttime.Sleep(backoff)
\t\tif backoff < maxBackoff {
\t\t\tbackoff *= 2
\t\t\tif backoff > maxBackoff {
\t\t\t\tbackoff = maxBackoff
\t\t\t}
\t\t}
\t}
}
'''


def patch_startserver(path: Path) -> bool:
    """Insert `go ttr.StartSensorReactionsForAllBots()` after the startup banner."""
    src = path.read_text(encoding="utf-8")
    if SENTINEL_STARTSERVER in src:
        print(f"[sensor-reactions] {path.name} already patched.")
        return False

    anchor = 'fmt.Println("\\033[33m\\033[1mwire-pod started successfully!\\033[0m")\n'
    if anchor not in src:
        print(f"[sensor-reactions] startup banner not found in {path}", file=sys.stderr)
        sys.exit(1)
    insert = anchor + "\n\tgo ttr.StartSensorReactionsForAllBots()\n"
    src = src.replace(anchor, insert, 1)

    # Alias-import the ttr package - its declared package name is wirepod_ttr,
    # so we alias it as `ttr` for readability.
    if 'ttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"' not in src:
        src = re.sub(
            r'(import \(\n)',
            r'\1\tttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"\n',
            src,
            count=1,
        )

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[sensor-reactions] {path.name} patched.")
    return True


def write_sensor_go(ttr_dir: Path) -> bool:
    target = ttr_dir / SENSOR_GO_FILENAME
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing == SENSOR_GO:
            print(f"[sensor-reactions] {SENSOR_GO_FILENAME} already in place.")
            return False
    target.write_text(SENSOR_GO, encoding="utf-8", newline="\n")
    print(f"[sensor-reactions] wrote {target}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    startserver = wirepod / "chipper" / "pkg" / "initwirepod" / "startserver.go"
    if not ttr_dir.exists() or not startserver.exists():
        print(f"[sensor-reactions] target dirs not found under {wirepod}", file=sys.stderr)
        sys.exit(1)
    write_sensor_go(ttr_dir)
    patch_startserver(startserver)

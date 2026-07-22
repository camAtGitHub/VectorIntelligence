#!/usr/bin/env python3
"""Add ambient awareness to Wire-Pod.

Creates a new Go file `ambient.go` with a background loop that, when Vector is
idle (awake, off the charger, not mid-conversation, not in night hours),
periodically takes a SILENT camera frame and asks vector-ai's /v1/ambient
endpoint whether anything is genuinely new.

Uses robotsession for all robot I/O:
  - ambientObserveOnce → Session.Unary (CaptureSingleImage)
  - ambientReact → Session.WithControl (investigate + SayText)
  - probeForKnownFace / greeting → Session.ProbeFace + WithControl
Never per-cycle vector.New / Close.

Also exposes MarkVoiceActivity(), which the face probe calls at the start of
every voice request so the ambient loop knows to stay out of the way.

Patches startserver.go to launch the loop at chipper boot.

Idempotent.
"""
import re
import sys
from pathlib import Path

AMBIENT_GO_FILENAME = "ambient.go"
SENTINEL_STARTSERVER = "StartAmbientLoop"

AMBIENT_GO = '''package wirepod_ttr

import (
\t"bytes"
\t"context"
\t"encoding/base64"
\t"encoding/json"
\t"fmt"
\t"net/http"
\t"sync/atomic"
\t"time"

\t"github.com/fforchino/vector-go-sdk/pkg/vector"
\t"github.com/fforchino/vector-go-sdk/pkg/vectorpb"
\t"github.com/kercre123/wire-pod/chipper/pkg/vars"
\t"github.com/kercre123/wire-pod/chipper/pkg/wirepod/robotsession"
)

// Ambient awareness: whenever Vector is awake and free - not mid-conversation
// and not in his night-hours sleep window - he periodically takes a silent
// camera frame and lets the multimodal model decide whether anything is
// genuinely new. He does this whether or not he is docked: being on the
// charger no longer blocks observation, only being asleep does. Almost always
// nothing happens. On real novelty he speaks a short line and vector-ai
// stores the observation so he can discuss it later.
//
// Restraint is the whole point: a desk barely changes, so the model is told
// its default answer is "nothing", a multi-minute interval keeps glances
// rare, and a post-reaction cooldown stops him remarking again straight away.
//
// All robot I/O goes through robotsession (shared channel per ESN).

const (
\t// How often Vector glances around when idle. He only SPEAKS on genuine
\t// novelty, so this is the latency to NOTICE a change, not how often he
\t// talks. Longer = calmer and less GPU churn (each glance is a vision call
\t// that keeps the model warm in VRAM).
\tambientInterval = 3 * time.Minute

\t// After Vector reacts to something, stay silent at least this long before
\t// reacting to anything again.
\tambientReactCooldown = 15 * time.Minute

\t// Skip a glance if a voice interaction happened within this window.
\tambientVoiceCooldown = 2 * time.Minute

\t// No ambient activity during these hours (24h clock) - Vector's presumed
\t// sleep window, and the sole "asleep" gate now that docking no longer
\t// blocks observation. The resulting overnight gap in /v1/ambient calls is
\t// also what vector-ai uses to expire quiet mode (a sleep cycle has passed).
\t// Tune these to match when Vector actually sleeps.
\tambientNightStart = 23 // 11pm
\tambientNightEnd   = 7  // 7am

\t// How often, when idle, Vector briefly probes for a known face to greet.
\t// Each probe is a short secondary face EventStream via Session.ProbeFace
\t// (≤6s) — never continuous.
\tgreetingInterval = 2 * time.Minute
)

// lastVoiceActivity is the unix time of the most recent voice interaction,
// set by MarkVoiceActivity (called from the face probe at the start of every
// voice request) so the ambient loop can stay out of the way.
var lastVoiceActivity atomic.Int64

// lastAmbientReaction is the unix time Vector last spoke an ambient reaction.
var lastAmbientReaction atomic.Int64

// MarkVoiceActivity records that a voice interaction is happening right now.
func MarkVoiceActivity() {
\tlastVoiceActivity.Store(time.Now().Unix())
}

func recentlyConversed() bool {
\tlast := lastVoiceActivity.Load()
\treturn last != 0 && time.Now().Unix()-last < int64(ambientVoiceCooldown/time.Second)
}

func ambientInNightHours() bool {
\th := time.Now().Hour()
\tif ambientNightStart > ambientNightEnd {
\t\treturn h >= ambientNightStart || h < ambientNightEnd
\t}
\treturn h >= ambientNightStart && h < ambientNightEnd
}

func ambientReactionOnCooldown() bool {
\tlast := lastAmbientReaction.Load()
\treturn last != 0 && time.Now().Unix()-last < int64(ambientReactCooldown/time.Second)
}

// StartAmbientLoop launches the ambient observation and proactive-greeting
// loops per enrolled robot. Call once at chipper startup.
func StartAmbientLoop() {
\ttime.Sleep(30 * time.Second) // let chipper finish init and bots load
\tfor _, bot := range vars.BotInfo.Robots {
\t\tgo runAmbientLoop(bot.Esn)
\t\tgo runGreetingLoop(bot.Esn)
\t}
}

func runAmbientLoop(esn string) {
\tfmt.Printf("[ambient] starting ambient loop for %s (robotsession)\\n", esn)
\tfailStreak := 0
\tfor {
\t\t// Failure backoff: when glances keep failing (robot asleep, link down,
\t\t// gateway wedged) poke him less often. 3m -> 6m -> 12m -> 24m, reset on
\t\t// the first success.
\t\tsleep := ambientInterval
\t\tif failStreak > 0 {
\t\t\tshift := failStreak
\t\t\tif shift > 3 {
\t\t\t\tshift = 3
\t\t\t}
\t\t\tsleep = ambientInterval * time.Duration(1<<shift)
\t\t}
\t\ttime.Sleep(sleep)
\t\t// Idle gate: glance around whenever Vector is awake and free. Being
\t\t// docked is fine - only night hours (asleep) and an in-flight
\t\t// conversation hold him back.
\t\tif recentlyConversed() {
\t\t\tcontinue
\t\t}
\t\tif ambientInNightHours() {
\t\t\tcontinue
\t\t}
\t\tif ambientReactionOnCooldown() {
\t\t\tcontinue
\t\t}
\t\tif ambientObserveOnce(esn) {
\t\t\tfailStreak = 0
\t\t} else {
\t\t\tfailStreak++
\t\t}
\t}
}

// askVectorAIAmbient sends a camera frame to vector-ai and returns the line
// Vector should speak. Empty string means "nothing worth saying" - the
// overwhelmingly common case.
func askVectorAIAmbient(jpeg []byte) string {
\tpayload, _ := json.Marshal(map[string]string{
\t\t"image": base64.StdEncoding.EncodeToString(jpeg),
\t})
\tclient := &http.Client{Timeout: 35 * time.Second}
\tresp, err := client.Post(vectorAIBase+"/v1/ambient", "application/json", bytes.NewReader(payload))
\tif err != nil {
\t\tfmt.Printf("[ambient] vector-ai call failed: %v\\n", err)
\t\treturn ""
\t}
\tdefer resp.Body.Close()
\tvar result struct {
\t\tText  string `json:"text"`
\t\tQuiet bool   `json:"quiet"`
\t\tError string `json:"error,omitempty"`
\t}
\tif err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
\t\tfmt.Printf("[ambient] vector-ai bad json: %v\\n", err)
\t\treturn ""
\t}
\tif result.Error != "" {
\t\tfmt.Printf("[ambient] vector-ai returned error: %s\\n", result.Error)
\t}
\treturn result.Text
}

// ambientObserveOnce takes one silent photo via Session.Unary, asks vector-ai
// whether anything is new, and speaks the reaction if there is one. Returns
// false when the robot couldn't be reached or photographed, so the caller can
// back off. Returns true (no backoff) when we deliberately skip - e.g. Vector
// is in calm/sleep power mode where camera RPCs hang to deadline.
func ambientObserveOnce(esn string) bool {
\t// Calm/sleep: vision stack is powered down. Sensor loop tracks this bit
\t// live; only trust it when robot_state is fresh.
\tif IsCalmPowerMode() {
\t\treturn true
\t}
\tif robotsession.Default == nil {
\t\tfmt.Printf("[ambient] robotsession.Default nil for %s\\n", esn)
\t\treturn false
\t}

\tctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
\tdefer cancel()
\tsess, err := robotsession.Default.Get(ctx, esn)
\tif err != nil {
\t\tfmt.Printf("[ambient] session get failed for %s: %v\\n", esn, err)
\t\treturn false
\t}
\t// Ensure state stream is up so IsOnCharger / calm flags stay fresh.
\t_ = sess.StartStateStream(ctx)

\tvar jpeg []byte
\terr = sess.Unary(ctx, func(ctx context.Context, robot *vector.Vector) error {
\t\t// CaptureSingleImage on Vector's gateway: enables streaming, waits for
\t\t// one full JPEG, then disables. One RPC, one dedicated budget.
\t\timgCtx, imgCancel := context.WithTimeout(ctx, 30*time.Second)
\t\tdefer imgCancel()
\t\timg, capErr := robot.Conn.CaptureSingleImage(imgCtx, &vectorpb.CaptureSingleImageRequest{
\t\t\tEnableHighResolution: false,
\t\t})
\t\tif capErr != nil || img == nil || len(img.Data) == 0 {
\t\t\tif capErr == nil {
\t\t\t\tcapErr = fmt.Errorf("empty image")
\t\t\t}
\t\t\treturn capErr
\t\t}
\t\tjpeg = img.Data
\t\treturn nil
\t})
\tif err != nil {
\t\tfmt.Printf("[ambient] image capture failed for %s: %v\\n", esn, err)
\t\treturn false
\t}

\tline := askVectorAIAmbient(jpeg)
\tif line == "" {
\t\treturn true // nothing novel - the overwhelmingly common case
\t}

\t// Re-check: a conversation may have started while we were thinking. The
\t// observation is already stored by vector-ai, so Vector can still mention
\t// it later - we just don't speak over the user now.
\tif recentlyConversed() {
\t\tfmt.Printf("[ambient] suppressing reaction (conversation in progress): %q\\n", line)
\t\treturn true
\t}

\tfmt.Printf("[ambient] reaction: %q\\n", line)
\tlastAmbientReaction.Store(time.Now().Unix())
\t// Off the charger, Vector physically investigates before commenting;
\t// docked, he simply speaks (we never drive him off his pod).
\tonCharger := sess.OnCharger()
\tif sess.LastRobotState() == nil {
\t\tonCharger = IsOnCharger()
\t}
\tambientReact(esn, line, !onCharger)
\treturn true
}

// ambientReact makes Vector react via Session.WithControl: optionally a brief
// investigative move, then SayText, all under one cancelable lease.
func ambientReact(esn, text string, investigate bool) {
\tif robotsession.Default == nil {
\t\tfmt.Printf("[ambient] robotsession.Default nil; cannot speak\\n")
\t\treturn
\t}
\tctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
\tdefer cancel()
\tsess, err := robotsession.Default.Get(ctx, esn)
\tif err != nil {
\t\tfmt.Printf("[ambient] session get failed: %v\\n", err)
\t\treturn
\t}
\terr = sess.WithControl(ctx, robotsession.ControlOptions{Timeout: 40 * time.Second},
\t\tfunc(ctx context.Context, robot *vector.Vector) error {
\t\t\tif investigate {
\t\t\t\tambientInvestigateMove(ctx, robot)
\t\t\t}
\t\t\t// Raise to desired speech volume for this phrase (same as SayText(esn)).
\t\t\t// After investigate so motion does not burn the hold window.
\t\t\tSpeechVolumeHoldFor(esn, EstimateSpeechDuration(text))
\t\t\t_, sayErr := robot.Conn.SayText(ctx, &vectorpb.SayTextRequest{
\t\t\t\tText:           text,
\t\t\t\tUseVectorVoice: true,
\t\t\t\tDurationScalar: 1.0,
\t\t\t})
\t\t\tif sayErr != nil {
\t\t\t\treturn sayErr
\t\t\t}
\t\t\t// Brief pause so audio tail is not cut by control release.
\t\t\tselect {
\t\t\tcase <-ctx.Done():
\t\t\tcase <-time.After(500 * time.Millisecond):
\t\t\t}
\t\t\treturn nil
\t\t})
\tif err != nil {
\t\tfmt.Printf("[ambient] react failed: %v\\n", err)
\t\treturn
\t}
\t// Count ambient speech as voice activity so workday/greeting suppress
\t// windows and other loops stay clear (avoids ambient→workday double-speak).
\tMarkVoiceActivity()
}

// ambientInvestigateMove is a brief, modest "I noticed something" beat - a
// curious tilt of the head and a short approach toward what Vector saw. It is
// deliberately small, not navigation. Behavior control must already be held,
// and Vector must be off the charger.
func ambientInvestigateMove(ctx context.Context, robot *vector.Vector) {
\trobot.Conn.SetHeadAngle(ctx, &vectorpb.SetHeadAngleRequest{
\t\tAngleRad:          0.30,
\t\tMaxSpeedRadPerSec: 2.0,
\t\tAccelRadPerSec2:   10.0,
\t\tDurationSec:       0.4,
\t})
\trobot.Conn.DriveStraight(ctx, &vectorpb.DriveStraightRequest{
\t\tSpeedMmps:           50,
\t\tDistMm:              35,
\t\tShouldPlayAnimation: true,
\t})
}

// runGreetingLoop periodically, when Vector is idle, briefly probes for a known
// face via Session.ProbeFace. If someone he knows has come into view, vector-ai
// decides whether a greeting is warranted; if so, Vector says it unprompted.
func runGreetingLoop(esn string) {
\tfmt.Printf("[greeting] starting proactive-greeting loop for %s\\n", esn)
\tfailStreak := 0
\tfor {
\t\t// Same failure backoff as the ambient loop: 2m -> 4m -> 8m -> 16m,
\t\t// reset on the first successful connect.
\t\tsleep := greetingInterval
\t\tif failStreak > 0 {
\t\t\tshift := failStreak
\t\t\tif shift > 3 {
\t\t\t\tshift = 3
\t\t\t}
\t\t\tsleep = greetingInterval * time.Duration(1<<shift)
\t\t}
\t\ttime.Sleep(sleep)
\t\tif recentlyConversed() || ambientInNightHours() {
\t\t\tcontinue
\t\t}
\t\tfaceID, name := probeForKnownFace(esn)
\t\tif faceID <= 0 {
\t\t\t// Probe completed (possibly empty) — count as success for backoff
\t\t\t// only when robotsession is ready; nil registry keeps failing.
\t\t\tif robotsession.Default == nil {
\t\t\t\tfailStreak++
\t\t\t} else {
\t\t\t\tfailStreak = 0
\t\t\t}
\t\t\tcontinue
\t\t}
\t\tfailStreak = 0
\t\tline := askVectorAIGreeting(faceID, name)
\t\tif line == "" || recentlyConversed() {
\t\t\tcontinue
\t\t}
\t\tfmt.Printf("[greeting] %s -> %q\\n", name, line)
\t\tMarkVoiceActivity() // a greeting counts as an interaction - keep loops clear
\t\tambientReact(esn, line, false)
\t}
}

// probeForKnownFace uses Session.ProbeFace (≤6s secondary face stream) and
// returns the first enrolled (named) face, or (0, "") if none.
func probeForKnownFace(esn string) (int32, string) {
\tif robotsession.Default == nil {
\t\treturn 0, ""
\t}
\tctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
\tdefer cancel()
\tsess, err := robotsession.Default.Get(ctx, esn)
\tif err != nil {
\t\tfmt.Printf("[greeting] session get failed for %s: %v\\n", esn, err)
\t\treturn 0, ""
\t}
\tfaceID, name, _, err := sess.ProbeFace(ctx, 6*time.Second)
\tif err != nil {
\t\tfmt.Printf("[greeting] ProbeFace failed for %s: %v\\n", esn, err)
\t\treturn 0, ""
\t}
\tif faceID > 0 && name != "" {
\t\treturn faceID, name
\t}
\treturn 0, ""
}

// askVectorAIGreeting asks vector-ai whether to greet this person and for the
// line. Empty string means "do not greet" (e.g. they were seen recently).
func askVectorAIGreeting(faceID int32, name string) string {
\tpayload, _ := json.Marshal(map[string]interface{}{
\t\t"face_id": faceID,
\t\t"name":    name,
\t})
\tclient := &http.Client{Timeout: 20 * time.Second}
\tresp, err := client.Post(vectorAIBase+"/v1/proactive_greeting", "application/json", bytes.NewReader(payload))
\tif err != nil {
\t\tfmt.Printf("[greeting] vector-ai call failed: %v\\n", err)
\t\treturn ""
\t}
\tdefer resp.Body.Close()
\tvar result struct {
\t\tText  string `json:"text"`
\t\tError string `json:"error,omitempty"`
\t}
\tif err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
\t\tfmt.Printf("[greeting] vector-ai bad json: %v\\n", err)
\t\treturn ""
\t}
\treturn result.Text
}
'''


def patch_startserver(path: Path) -> bool:
    """Insert `go ttr.StartAmbientLoop()` after the startup banner."""
    src = path.read_text(encoding="utf-8")
    if SENTINEL_STARTSERVER in src:
        print(f"[ambient-loop] {path.name} already patched.")
        return False

    anchor = 'fmt.Println("\\033[33m\\033[1mwire-pod started successfully!\\033[0m")\n'
    if anchor not in src:
        print(f"[ambient-loop] startup banner not found in {path}", file=sys.stderr)
        sys.exit(1)
    insert = anchor + "\n\tgo ttr.StartAmbientLoop()\n"
    src = src.replace(anchor, insert, 1)

    # Alias-import the ttr package if not already imported (add-sensor-reactions
    # also adds this - whichever patch runs first wins, both are idempotent).
    if 'ttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"' not in src:
        src = re.sub(
            r'(import \(\n)',
            r'\1\tttr "github.com/kercre123/wire-pod/chipper/pkg/wirepod/ttr"\n',
            src,
            count=1,
        )

    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[ambient-loop] {path.name} patched.")
    return True


def write_ambient_go(ttr_dir: Path) -> bool:
    target = ttr_dir / AMBIENT_GO_FILENAME
    if target.exists() and target.read_text(encoding="utf-8") == AMBIENT_GO:
        print(f"[ambient-loop] {AMBIENT_GO_FILENAME} already in place.")
        return False
    target.write_text(AMBIENT_GO, encoding="utf-8", newline="\n")
    print(f"[ambient-loop] wrote {target}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    startserver = wirepod / "chipper" / "pkg" / "initwirepod" / "startserver.go"
    if not ttr_dir.exists() or not startserver.exists():
        print(f"[ambient-loop] target dirs not found under {wirepod}", file=sys.stderr)
        sys.exit(1)
    write_ambient_go(ttr_dir)
    patch_startserver(startserver)

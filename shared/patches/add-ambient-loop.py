#!/usr/bin/env python3
"""Add ambient awareness to Wire-Pod.

Creates a new Go file `ambient.go` with a background loop that, when Vector is
idle (awake, off the charger, not mid-conversation, not in night hours),
periodically takes a SILENT camera frame and asks vector-ai's /v1/ambient
endpoint whether anything is genuinely new.

The whole design is built around restraint: a desk barely changes, so the
model's default answer is "nothing", glances are minutes apart, and a
post-reaction cooldown stops Vector from remarking again straight away. Only on
genuine novelty does he speak a short line — and vector-ai stores the new thing
as a visual observation so he can talk about it later when asked.

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
)

// Ambient awareness: whenever Vector is awake and free — not mid-conversation
// and not in his night-hours sleep window — he periodically takes a silent
// camera frame and lets the multimodal model decide whether anything is
// genuinely new. He does this whether or not he is docked: being on the
// charger no longer blocks observation, only being asleep does. Almost always
// nothing happens. On real novelty he speaks a short line and vector-ai
// stores the observation so he can discuss it later.
//
// Restraint is the whole point: a desk barely changes, so the model is told
// its default answer is "nothing", a multi-minute interval keeps glances
// rare, and a post-reaction cooldown stops him remarking again straight away.

const (
\t// How often Vector glances around when idle. He only SPEAKS on genuine
\t// novelty, so this is the latency to NOTICE a change, not how often he
\t// talks. Longer = calmer and less GPU churn (each glance is a vision call
\t// that keeps the model warm in VRAM).
\tambientInterval = 5 * time.Minute

\t// After Vector reacts to something, stay silent at least this long before
\t// reacting to anything again.
\tambientReactCooldown = 15 * time.Minute

\t// Skip a glance if a voice interaction happened within this window.
\tambientVoiceCooldown = 2 * time.Minute

\t// No ambient activity during these hours (24h clock) — Vector's presumed
\t// sleep window, and the sole "asleep" gate now that docking no longer
\t// blocks observation. The resulting overnight gap in /v1/ambient calls is
\t// also what vector-ai uses to expire quiet mode (a sleep cycle has passed).
\t// Tune these to match when Vector actually sleeps.
\tambientNightStart = 23 // 11pm
\tambientNightEnd   = 7  // 7am
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

// StartAmbientLoop launches the ambient observation loop per enrolled robot.
// Call once at chipper startup.
func StartAmbientLoop() {
\ttime.Sleep(30 * time.Second) // let chipper finish init and bots load
\tfor _, bot := range vars.BotInfo.Robots {
\t\tgo runAmbientLoop(bot.Esn, bot.GUID, bot.IPAddress+":443")
\t}
}

func runAmbientLoop(esn, guid, target string) {
\tfmt.Printf("[ambient] starting ambient loop for %s @ %s\\n", esn, target)
\tfor {
\t\ttime.Sleep(ambientInterval)
\t\t// Idle gate: glance around whenever Vector is awake and free. Being
\t\t// docked is fine — only night hours (asleep) and an in-flight
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
\t\tambientObserveOnce(esn, guid, target)
\t}
}

// askVectorAIAmbient sends a camera frame to vector-ai and returns the line
// Vector should speak. Empty string means "nothing worth saying" — the
// overwhelmingly common case.
func askVectorAIAmbient(jpeg []byte) string {
\tpayload, _ := json.Marshal(map[string]string{
\t\t"image": base64.StdEncoding.EncodeToString(jpeg),
\t})
\tclient := &http.Client{Timeout: 35 * time.Second}
\tresp, err := client.Post("http://127.0.0.1:8000/v1/ambient", "application/json", bytes.NewReader(payload))
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

// ambientObserveOnce takes one silent photo, asks vector-ai whether anything
// is new, and speaks the reaction if there is one.
func ambientObserveOnce(esn, guid, target string) {
\trobot, err := vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))
\tif err != nil {
\t\tfmt.Printf("[ambient] connect failed for %s: %v\\n", esn, err)
\t\treturn
\t}
\t// Release the gRPC connection when done — otherwise every cycle leaks one.
\tdefer robot.Close()

\t// Silent single-frame capture — no shutter animation, no viewfinder.
\tctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
\tdefer cancel()
\timg, err := robot.Conn.CaptureSingleImage(ctx, &vectorpb.CaptureSingleImageRequest{
\t\tEnableHighResolution: false,
\t})
\tif err != nil || img == nil || len(img.Data) == 0 {
\t\tfmt.Printf("[ambient] image capture failed for %s: %v\\n", esn, err)
\t\treturn
\t}

\tline := askVectorAIAmbient(img.Data)
\tif line == "" {
\t\treturn // nothing novel — the overwhelmingly common case
\t}

\t// Re-check: a conversation may have started while we were thinking. The
\t// observation is already stored by vector-ai, so Vector can still mention
\t// it later — we just don't speak over the user now.
\tif recentlyConversed() {
\t\tfmt.Printf("[ambient] suppressing reaction (conversation in progress): %q\\n", line)
\t\treturn
\t}

\tfmt.Printf("[ambient] reaction: %q\\n", line)
\tlastAmbientReaction.Store(time.Now().Unix())
\tsayText(robot, line)
\t// sayText is fire-and-forget (it spawns goroutines to acquire behavior
\t// control and speak). Hold the connection open until the line has been
\t// spoken, otherwise the deferred Close cuts it off mid-sentence.
\ttime.Sleep(14 * time.Second)
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
    # also adds this — whichever patch runs first wins, both are idempotent).
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

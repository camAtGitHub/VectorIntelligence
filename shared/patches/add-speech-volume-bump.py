#!/usr/bin/env python3
"""Keep Vector quiet at idle and bump the volume up only while he speaks.

Vector's master_volume is a single global level, so setting it low enough
that his idle noise (charger chirps, wake-word grunts, animation stings)
isn't shouting across the desk also makes his actual replies too quiet to
hear. This patch decouples the two.

The speaking level is NOT configured here - it is whatever a human last set
by any route: the web UI volume radios, "Vector, volume 4", or the official
app. The existing volume control keeps its normal meaning, and this patch
only decides how far Vector ducks BETWEEN utterances: idle sits VolumeDrop
presets below the speaking level, clamped so it can never go below Mute.

  master_volume presets: 0=Mute 1=Low 2=Medium Low 3=Medium 4=Medium High
                         5=High

  speaking level 4 -> idles at 2      speaking level 1 -> idles at 0 (Mute)
  speaking level 3 -> idles at 1      speaking level 0 -> untouched (muted
                                        by a human; never raised)

Because the idle level is one we wrote ourselves, we can't simply read
master_volume back and call it the human's preference - once we've ducked to
1, a pull returns 1 either way. So we latch: the patch remembers the value it
last wrote, and on the next utterance re-reads master_volume. If it differs
from what we wrote, a human moved it, and that becomes the new speaking
level. That catches all three input routes without hooking any of them.

The bump is issued at the START of a voice request, concurrently with
speech-to-text - the same trick add-face-probe.py uses. The read and write
overlap the user still talking, so the level is already up by the time Vector
opens his mouth and it costs no perceptible latency.

Restoring is deliberately debounced rather than per-utterance. An LLM reply
is split into sentence chunks and each chunk is a separate DoSayText call, so
a naive bump/restore pair per call would pump the volume audibly between
sentences and write jdocs a dozen times per answer. Instead every utterance
pushes a shared "hold" deadline forward, and a single watchdog drops the
level once the robot has been quiet for VolumeHangTime.

Note that gRPC SayText is unary: it returns once the robot has accepted the
request, NOT when the utterance finishes. There is no end-of-speech callback
on any path, so the hold is always sized from an estimate of how long the
text will take to speak, never from "speech has stopped".

Tunable without a rebuild:
  VECTOR_VOLUME_DROP        (default 2)     presets to duck below speaking level
  VECTOR_VOLUME_HANG_MS     (default 2500)  margin held after the estimate ends
  VECTOR_VOLUME_MS_PER_WORD (default 400)   TTS rate used to size the estimate
Setting VECTOR_VOLUME_DROP=0 disables the patch at runtime: no reads, no
writes, volume left exactly where the human put it.

If Vector ducks partway through long replies, the estimate is running short:
raise VECTOR_VOLUME_MS_PER_WORD (or VECTOR_VOLUME_HANG_MS) until he doesn't.

Creates chipper/pkg/wirepod/ttr/speech_volume.go and patches intent_graph.go,
kgsim_cmds.go and bcontrol.go. Idempotent, and a drop-in replacement for the
absolute-level version of this patch (identical call sites; re-running simply
rewrites speech_volume.go).
"""
import sys
from pathlib import Path

SPEECH_VOLUME_FILENAME = "speech_volume.go"

SENTINEL = "SpeechVolumeBump"

SPEECH_VOLUME_GO = '''package wirepod_ttr

import (
\t"bytes"
\t"crypto/tls"
\t"encoding/json"
\t"fmt"
\t"io"
\t"net/http"
\t"os"
\t"strconv"
\t"strings"
\t"sync"
\t"time"

\t"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

// master_volume is the firmware preset index that the web UI's volume radios
// write: 0=Mute 1=Low 2=Medium Low 3=Medium 4=Medium High 5=High.
const (
\tvolumeMin = 0
\tvolumeMax = 5

\t// unknownVolume marks "we have never observed this robot's level". It must
\t// not collide with a real preset, and in particular must not be 0 - Mute is
\t// a legitimate value a human can choose.
\tunknownVolume = -1
)

var (
\t// VolumeDrop is how many presets below the speaking level Vector idles at.
\t// The speaking level itself is whatever a human last set (web UI, voice
\t// command, official app) - this patch never invents it. Set to 0 to disable
\t// the whole mechanism at runtime.
\tVolumeDrop = envInt("VECTOR_VOLUME_DROP", 2)

\t// VolumeHangTime is the margin held on top of the estimated speech length
\t// before dropping back to idle. It absorbs both the error in that estimate
\t// and the gap between consecutive sentence chunks of one LLM reply - if it
\t// is too short, the level pumps mid-answer.
\tVolumeHangTime = time.Duration(envInt("VECTOR_VOLUME_HANG_MS", 2500)) * time.Millisecond

\t// VolumeMsPerWord is the assumed TTS rate used to size the hold. Vector
\t// speaks slowly and deliberately; erring long only costs a little idle
\t// delay, whereas erring short ducks him mid-sentence.
\tVolumeMsPerWord = envInt("VECTOR_VOLUME_MS_PER_WORD", 400)
)

func envInt(key string, def int) int {
\tif v := strings.TrimSpace(os.Getenv(key)); v != "" {
\t\tif n, err := strconv.Atoi(v); err == nil {
\t\t\treturn n
\t\t}
\t}
\treturn def
}

// clampVolume keeps a preset inside the firmware's range. This is what stops
// a low speaking level from producing a negative idle level: speak 1 with a
// drop of 2 lands at Mute, not at -1.
func clampVolume(v int) int {
\tif v < volumeMin {
\t\treturn volumeMin
\t}
\tif v > volumeMax {
\t\treturn volumeMax
\t}
\treturn v
}

type volumeState struct {
\t// mu serializes ALL work for one robot, network calls included. Holding a
\t// lock across I/O is usually a smell; here it is the entire point. The bump
\t// path reads master_volume back to spot human changes, so if a restore
\t// write could land between that read and the decision it drives, the patch
\t// mistakes its own idle level for a human's choice, adopts it as the new
\t// speaking level, and ratchets the volume down a preset per reply until it
\t// bottoms out. Serializing per robot makes that window not exist.
\tmu sync.Mutex

\tloud      bool
\tloudUntil time.Time

\t// speakLevel is the level a human last asked for, as far as we can tell.
\tspeakLevel int
\t// lastWritten is the level THIS patch last wrote. The difference between
\t// the two is the whole trick: if a fresh read doesn't match lastWritten,
\t// something other than us moved the volume, and that's a human.
\tlastWritten int
}

var (
\t// volumeMu guards the map itself ONLY - never held across I/O. Per-robot
\t// work is serialized by volumeState.mu instead, so one slow robot can't
\t// block another.
\tvolumeMu     sync.Mutex
\tvolumeStates = map[string]*volumeState{}
\tvolumeWatch  sync.Once

\t// One pooled client - these are frequent, short, same-host requests.
\tvolumeHTTP = &http.Client{
\t\tTransport: &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}},
\t\tTimeout:   5 * time.Second,
\t}
)

// EstimateSpeechDuration guesses how long Vector will take to say text.
//
// Every speech path needs this, because none of them can tell us when the
// utterance actually ended: gRPC SayText is unary and returns as soon as the
// robot accepts the request, and bcontrol's sayText is fire-and-forget on top
// of that. So the hold is sized from the text rather than from an
// end-of-speech signal that doesn't exist.
func EstimateSpeechDuration(text string) time.Duration {
\twords := len(strings.Fields(text))
\td := time.Duration(words*VolumeMsPerWord) * time.Millisecond
\tif d < 1500*time.Millisecond {
\t\td = 1500 * time.Millisecond
\t}
\treturn d
}

// SpeechVolumeBump raises the robot to its speaking level if it isn't already
// there and holds it for VolumeHangTime. Cheap and safe to call on every
// utterance: once loud, further calls only extend the deadline and issue no
// HTTP at all, which is what stops the level pumping between the sentence
// chunks of one reply.
func SpeechVolumeBump(esn string) {
\tspeechVolumeHold(esn, VolumeHangTime)
}

// SpeechVolumeHoldFor is SpeechVolumeBump with an explicit minimum hold on top
// of the hang time - for callers that know roughly how long the utterance will
// run but don't block until it finishes.
func SpeechVolumeHoldFor(esn string, d time.Duration) {
\tspeechVolumeHold(esn, d+VolumeHangTime)
}

// volumeStateFor returns the state for a robot, creating it on first sight.
// Takes only the map lock, briefly - callers then take st.mu themselves.
func volumeStateFor(esn string) *volumeState {
\tvolumeMu.Lock()
\tdefer volumeMu.Unlock()
\tst, ok := volumeStates[esn]
\tif !ok {
\t\tst = &volumeState{speakLevel: unknownVolume, lastWritten: unknownVolume}
\t\tvolumeStates[esn] = st
\t}
\treturn st
}

func speechVolumeHold(esn string, d time.Duration) {
\tif esn == "" || VolumeDrop <= 0 {
\t\treturn
\t}
\tvolumeWatch.Do(func() { go volumeRestoreLoop() })
\tst := volumeStateFor(esn)

\t// Held across the read and the write below, so a restore for this robot
\t// cannot interleave and be mistaken for a human moving the volume.
\tst.mu.Lock()
\tdefer st.mu.Unlock()

\t// The deadline only ever moves forward - a short bump arriving after a long
\t// hold must not cut the long one short.
\tif until := time.Now().Add(d); until.After(st.loudUntil) {
\t\tst.loudUntil = until
\t}
\tif st.loud {
\t\t// Already loud: deadline extended, nothing else to do. This is the
\t\t// common path (every chunk after the first) and it touches no network.
\t\treturn
\t}

\t// Transitioning idle -> loud. Resync the speaking level: if the robot's
\t// current level isn't the one we last wrote, something else moved it, and
\t// the only something else is a human (web UI, voice command, app).
\tcur, err := getMasterVolume(esn)
\tswitch {
\tcase err == nil && cur != st.lastWritten:
\t\tst.speakLevel = cur
\tcase err != nil && st.speakLevel == unknownVolume:
\t\t// Never observed this robot and can't read it now - don't guess a
\t\t// level. Stays not-loud, so the next utterance retries.
\t\tfmt.Printf("[volume] %s: cannot read level, skipping bump: %v\\n", esn, err)
\t\treturn
\t}

\tif st.speakLevel <= volumeMin {
\t\t// A human muted him. Respect that absolutely: never raise, never write.
\t\t// Stays not-loud so we re-read next time and notice an unmute.
\t\treturn
\t}
\tif err := setMasterVolume(esn, st.speakLevel); err != nil {
\t\t// Stays not-loud so the next utterance retries, rather than believing
\t\t// we're loud while actually sitting at the idle level.
\t\treturn
\t}
\t// Only now is it true. Both facts are recorded under the same lock that
\t// covered the write, so no reader can observe one without the other.
\tst.lastWritten = st.speakLevel
\tst.loud = true
\tfmt.Printf("[volume] %s raised to %d to speak (idles at %d)\\n",
\t\tesn, st.speakLevel, clampVolume(st.speakLevel-VolumeDrop))
}

// volumeRestoreLoop drops each robot back to its idle level once it has been
// quiet for long enough. One watchdog covers every robot.
func volumeRestoreLoop() {
\tticker := time.NewTicker(500 * time.Millisecond)
\tdefer ticker.Stop()
\tfor range ticker.C {
\t\t// Snapshot under the map lock, then work per robot - never hold the map
\t\t// lock across a network call.
\t\tvolumeMu.Lock()
\t\tsnapshot := make(map[string]*volumeState, len(volumeStates))
\t\tfor esn, st := range volumeStates {
\t\t\tsnapshot[esn] = st
\t\t}
\t\tvolumeMu.Unlock()
\t\tfor esn, st := range snapshot {
\t\t\trestoreOne(esn, st)
\t\t}
\t}
}

// restoreOne drops a single robot to idle if its hold has lapsed. Takes the
// same per-robot lock the bump path uses, so the two can never interleave -
// which is what stops a bump reading a half-applied restore and mistaking it
// for a human changing the volume.
func restoreOne(esn string, st *volumeState) {
\tst.mu.Lock()
\tdefer st.mu.Unlock()
\tif !st.loud || !time.Now().After(st.loudUntil) {
\t\treturn
\t}
\tif st.speakLevel == unknownVolume {
\t\tst.loud = false
\t\treturn
\t}
\tidle := clampVolume(st.speakLevel - VolumeDrop)
\tif err := setMasterVolume(esn, idle); err != nil {
\t\t// Stay loud and retry on the next tick.
\t\treturn
\t}
\tst.lastWritten = idle
\tst.loud = false
\tfmt.Printf("[volume] %s dropped to %d (idle)\\n", esn, idle)
}

// robotEndpoint resolves an ESN to the robot's gateway host and token.
func robotEndpoint(esn string) (string, string, error) {
\tfor _, bot := range vars.BotInfo.Robots {
\t\tif strings.EqualFold(strings.TrimSpace(bot.Esn), strings.TrimSpace(esn)) {
\t\t\treturn bot.IPAddress + ":443", bot.GUID, nil
\t\t}
\t}
\treturn "", "", fmt.Errorf("no robot with esn %s", esn)
}

// setMasterVolume writes master_volume through the robot's HTTPS settings
// endpoint - the same path the web UI's volume radios use. gRPC UpdateSettings
// doesn't carry master_volume, so this is the available route.
func setMasterVolume(esn string, level int) error {
\ttarget, token, err := robotEndpoint(esn)
\tif err != nil {
\t\treturn err
\t}
\tlevel = clampVolume(level)
\tbody := []byte(fmt.Sprintf(`{"update_settings": true, "settings": {"master_volume": %d} }`, level))
\treq, err := http.NewRequest("POST", "https://"+target+"/v1/update_settings", bytes.NewBuffer(body))
\tif err != nil {
\t\treturn err
\t}
\treq.Header.Set("Authorization", "Bearer "+token)
\treq.Header.Set("Content-Type", "application/json")
\tresp, err := volumeHTTP.Do(req)
\tif err != nil {
\t\tfmt.Printf("[volume] set %d failed for %s: %v\\n", level, esn, err)
\t\treturn err
\t}
\tdefer resp.Body.Close()
\tio.Copy(io.Discard, resp.Body)
\treturn nil
}

// getMasterVolume reads the robot's current master_volume preset.
//
// The robot will sometimes answer a ROBOT_SETTINGS pull with a different jdoc
// entirely - wire-pod's own get_sdk_settings retries for exactly this reason.
// Rather than sniffing for the wrong document, we simply require the reply to
// carry a master_volume key and retry if it doesn't.
func getMasterVolume(esn string) (int, error) {
\ttarget, token, err := robotEndpoint(esn)
\tif err != nil {
\t\treturn unknownVolume, err
\t}
\tvar lastErr error
\tfor attempt := 0; attempt < 4; attempt++ {
\t\tif attempt > 0 {
\t\t\ttime.Sleep(400 * time.Millisecond)
\t\t}
\t\tdoc, err := pullRobotSettings(target, token)
\t\tif err != nil {
\t\t\tlastErr = err
\t\t\tcontinue
\t\t}
\t\t// Pointer, not int: 0 is Mute, a real choice, and must be
\t\t// distinguishable from "the key wasn't there".
\t\tvar settings struct {
\t\t\tMasterVolume *int `json:"master_volume"`
\t\t}
\t\tif err := json.Unmarshal([]byte(doc), &settings); err != nil {
\t\t\tlastErr = err
\t\t\tcontinue
\t\t}
\t\tif settings.MasterVolume == nil {
\t\t\tlastErr = fmt.Errorf("jdoc carried no master_volume")
\t\t\tcontinue
\t\t}
\t\treturn clampVolume(*settings.MasterVolume), nil
\t}
\treturn unknownVolume, lastErr
}

// pullRobotSettings fetches the ROBOT_SETTINGS jdoc over the robot's HTTPS
// gateway and returns the settings JSON it carries.
func pullRobotSettings(target, token string) (string, error) {
\treq, err := http.NewRequest("POST", "https://"+target+"/v1/pull_jdocs",
\t\tbytes.NewBuffer([]byte(`{"jdoc_types": ["ROBOT_SETTINGS"]}`)))
\tif err != nil {
\t\treturn "", err
\t}
\treq.Header.Set("Authorization", "Bearer "+token)
\treq.Header.Set("Content-Type", "application/json")
\tresp, err := volumeHTTP.Do(req)
\tif err != nil {
\t\treturn "", err
\t}
\tdefer resp.Body.Close()
\traw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
\tif err != nil {
\t\treturn "", err
\t}
\tif resp.StatusCode != http.StatusOK {
\t\treturn "", fmt.Errorf("pull_jdocs returned %s", resp.Status)
\t}

\t// grpc-gateway emits either the proto's orig_name or its camelCase JSON
\t// name depending on the marshaler it was built with, so accept both rather
\t// than betting on one.
\tvar top map[string]json.RawMessage
\tif err := json.Unmarshal(raw, &top); err != nil {
\t\treturn "", err
\t}
\tnamed, ok := pickKey(top, "named_jdocs", "namedJdocs")
\tif !ok {
\t\treturn "", fmt.Errorf("pull_jdocs reply had no named_jdocs")
\t}
\tvar jdocs []map[string]json.RawMessage
\tif err := json.Unmarshal(named, &jdocs); err != nil {
\t\treturn "", err
\t}
\tif len(jdocs) == 0 {
\t\treturn "", fmt.Errorf("pull_jdocs reply was empty")
\t}
\tdocRaw, ok := pickKey(jdocs[0], "doc")
\tif !ok {
\t\treturn "", fmt.Errorf("named_jdoc had no doc")
\t}
\tvar doc map[string]json.RawMessage
\tif err := json.Unmarshal(docRaw, &doc); err != nil {
\t\treturn "", err
\t}
\tjsonDocRaw, ok := pickKey(doc, "json_doc", "jsonDoc")
\tif !ok {
\t\treturn "", fmt.Errorf("doc had no json_doc")
\t}
\tvar jsonDoc string
\tif err := json.Unmarshal(jsonDocRaw, &jsonDoc); err != nil {
\t\treturn "", err
\t}
\treturn jsonDoc, nil
}

func pickKey(m map[string]json.RawMessage, keys ...string) (json.RawMessage, bool) {
\tfor _, k := range keys {
\t\tif v, ok := m[k]; ok {
\t\t\treturn v, true
\t\t}
\t}
\treturn nil, false
}
'''

# ---------------------------------------------------------------- intent_graph
# Anchor on the single ReqToSpeechRequest line so this applies cleanly whether
# or not add-face-probe.py has already inserted its own goroutine after it.
ANCHOR_IG = "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
REPLACE_IG = (
    "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
    "\t// Raise the speaking volume now, concurrently with speech-to-text, so\n"
    "\t// the level is already up before Vector replies. The read/write overlaps\n"
    "\t// the user still talking, so it costs no perceptible latency.\n"
    "\tgo ttr.SpeechVolumeBump(speechReq.Device)\n"
)

# ----------------------------------------------------------------- kgsim_cmds
ANCHOR_SAYTEXT = "func DoSayText(input string, robot *vector.Vector) error {\n"
REPLACE_SAYTEXT = (
    "func DoSayText(input string, robot *vector.Vector) error {\n"
    "\t// Hold the level for as long as this text should take to speak.\n"
    "\t// gRPC SayText below is unary - it returns once the robot accepts the\n"
    "\t// request, not when the utterance ends - so there is nothing to re-arm\n"
    "\t// on and the hold has to be sized from the text. Placed above the\n"
    "\t// OpenAI-TTS branch so it covers that early return too.\n"
    "\tSpeechVolumeHoldFor(robot.Cfg.SerialNo, EstimateSpeechDuration(input))\n"
)

# An earlier cut of this patch wrongly assumed gRPC SayText blocked until the
# utterance finished and leaned on a deferred re-arm. It doesn't and it didn't:
# the defer fired the instant the robot acked, so the hold started at speech
# START and any reply longer than the hang time ducked mid-sentence. Rewrite
# that form in place - the sentinel check below would otherwise see the call
# site as "already patched" and silently leave the bug in.
MIGRATE_SAYTEXT_FROM = (
    "\t// Raise before the utterance, and re-arm the hold as it ends so the\n"
    "\t// drop back to idle lands after Vector stops talking rather than\n"
    "\t// mid-sentence on a long reply. SayText blocks until the utterance\n"
    "\t// finishes, so the deferred call is the one that matters; the defer\n"
    "\t// also covers the OpenAI-TTS early return below.\n"
    "\tSpeechVolumeBump(robot.Cfg.SerialNo)\n"
    "\tdefer SpeechVolumeBump(robot.Cfg.SerialNo)\n"
)
MIGRATE_SAYTEXT_TO = REPLACE_SAYTEXT[len(ANCHOR_SAYTEXT):]

# ------------------------------------------------------------------- bcontrol
ANCHOR_BCONTROL = "func sayText(robot *vector.Vector, text string) {\n"
REPLACE_BCONTROL = (
    "func sayText(robot *vector.Vector, text string) {\n"
    "\t// Reaction phrases (sensor reactions, ambient remarks) come through\n"
    "\t// here. This path is fire-and-forget - it returns before the robot has\n"
    "\t// spoken - so there's no completion to re-arm on and we hold for the\n"
    "\t// estimated length of the phrase instead.\n"
    "\tSpeechVolumeHoldFor(robot.Cfg.SerialNo, EstimateSpeechDuration(text))\n"
)


def write_speech_volume(ttr_dir: Path) -> bool:
    target = ttr_dir / SPEECH_VOLUME_FILENAME
    if target.exists() and target.read_text(encoding="utf-8") == SPEECH_VOLUME_GO:
        print(f"[volume] {SPEECH_VOLUME_FILENAME} already in place.")
        return False
    existed = target.exists()
    target.write_text(SPEECH_VOLUME_GO, encoding="utf-8", newline="\n")
    print(f"[volume] {'rewrote' if existed else 'wrote'} {target}")
    return True


def migrate_saytext(path: Path) -> bool:
    """Repair the defer-based DoSayText call site left by an earlier cut."""
    src = path.read_text(encoding="utf-8")
    if MIGRATE_SAYTEXT_FROM not in src:
        return False
    src = src.replace(MIGRATE_SAYTEXT_FROM, MIGRATE_SAYTEXT_TO, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[volume] {path.name}: migrated DoSayText off the broken defer-based hold.")
    return True


def patch_file(path: Path, anchor: str, replacement: str) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src or "SpeechVolumeHoldFor" in src:
        print(f"[volume] {path.name} already patched.")
        return False
    if anchor not in src:
        print(f"[volume] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(anchor, replacement, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[volume] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    intent_graph = wirepod / "chipper" / "pkg" / "wirepod" / "preqs" / "intent_graph.go"
    kgsim_cmds = ttr_dir / "kgsim_cmds.go"
    bcontrol = ttr_dir / "bcontrol.go"
    for p in (ttr_dir, intent_graph, kgsim_cmds, bcontrol):
        if not p.exists():
            print(f"[volume] target path not found: {p}", file=sys.stderr)
            sys.exit(1)
    write_speech_volume(ttr_dir)
    patch_file(intent_graph, ANCHOR_IG, REPLACE_IG)
    migrate_saytext(kgsim_cmds)
    patch_file(kgsim_cmds, ANCHOR_SAYTEXT, REPLACE_SAYTEXT)
    patch_file(bcontrol, ANCHOR_BCONTROL, REPLACE_BCONTROL)

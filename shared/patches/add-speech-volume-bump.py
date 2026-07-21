#!/usr/bin/env python3
"""Speech volume ducking: desired jdoc + hold modes.

Vector's master_volume is a single global level, so setting it low enough
that idle noise (charger chirps, wake-word grunts, animation stings) is quiet
also makes spoken replies too quiet. This patch decouples the two.

Desired speech volume (source of truth)
---------------------------------------
Stored in a pod-owned jdoc, NOT in firmware ROBOT_SETTINGS (pinger overwrites
custom keys there):

  thing:  vic:<ESN>
  name:   wirepod.SpeechVolume
  body:   {"desired_speech_volume": N}   # N = 0..5 master_volume preset

Desired only changes on user intent: web UI /api-sdk/volume, and volume
intents where wired. Duck/raise never writes desired. On first use with no
jdoc, live master_volume is seeded once into desired.

Live actuator (unchanged path)
------------------------------
Robot master_volume via HTTPS POST /v1/update_settings. Transition-only:
if already at target, no HTTP (avoids VolumeAdjustment chirps).

  before speech: if master != desired -> set desired
  speak:         hold at desired per mode
  after hold:    set master = clamp(desired - VOLUME_DROP)

Hold modes
----------
  utterance  SpeechVolumeBump / SpeechVolumeHoldFor
             hold for estimate (+ hang) of one phrase
  turn       SpeechVolumeEnterTurn
             one reply cycle; default VECTOR_VOLUME_TURN_MS
  session    SpeechVolumeEnterSession
             multi-turn / listen (blackjack, games); default SESSION_MS
  leave      SpeechVolumeLeaveSession demotes to utterance + hang

Env (tunable without rebuild)
-----------------------------
  VECTOR_VOLUME_DROP        (default 2)     presets below desired at idle
  VECTOR_VOLUME_HANG_MS     (default 2500)  margin after estimate / leave
  VECTOR_VOLUME_MS_PER_WORD (default 400)   TTS rate for duration estimate
  VECTOR_VOLUME_TURN_MS     (default 15000) default EnterTurn hold
  VECTOR_VOLUME_SESSION_MS  (default 45000) default EnterSession hold

DROP=0 disables the mechanism: no reads, no writes, volume left where set.

master_volume presets: 0=Mute 1=Low 2=Medium Low 3=Medium 4=Medium High 5=High

  desired 4 -> idles at 2 (drop=2); desired 1 -> idles at 0 (Mute)
  desired 0 (muted by human) is never raised

gRPC SayText is unary: it returns once the robot accepts the request, NOT
when the utterance finishes. Holds are sized from text estimates (and turn/
session defaults), never from an end-of-speech callback.

Creates chipper/pkg/wirepod/ttr/speech_volume.go and patches intent_graph.go,
kgsim_cmds.go and bcontrol.go (live SayText(esn) + legacy sayText). Idempotent:
re-run skips call sites that already have sentinels and rewrites
speech_volume.go only when content differs.
"""
import sys
from pathlib import Path

SPEECH_VOLUME_FILENAME = "speech_volume.go"

# Call-site / content sentinels for idempotency checks.
SENTINEL_BUMP = "SpeechVolumeBump"
SENTINEL_HOLD = "SpeechVolumeHoldFor"
SENTINEL_JDOC = "wirepod.SpeechVolume"
SENTINEL_SESSION = "SpeechVolumeEnterSession"
# Specific hold lines so we can patch SayText(esn) even if sayText(robot) already has HoldFor.
SENTINEL_HOLD_ESN = "SpeechVolumeHoldFor(esn, EstimateSpeechDuration(text))"
SENTINEL_HOLD_ROBOT = "SpeechVolumeHoldFor(robot.Cfg.SerialNo, EstimateSpeechDuration"

SPEECH_VOLUME_GO = r'''package wirepod_ttr

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/kercre123/wire-pod/chipper/pkg/vars"
)

// master_volume is the firmware preset index that the web UI's volume radios
// write: 0=Mute 1=Low 2=Medium Low 3=Medium 4=Medium High 5=High.
const (
	volumeMin = 0
	volumeMax = 5

	// unknownVolume marks "we have never observed this robot's level". It must
	// not collide with a real preset, and in particular must not be 0 - Mute is
	// a legitimate value a human can choose.
	unknownVolume = -1

	// Canonical pod-owned jdoc for desired speech volume. Never store this
	// inside vic.RobotSettings — the pinger overwrites firmware schema docs.
	speechVolumeThingPrefix = "vic:"
	speechVolumeJdocName    = "wirepod.SpeechVolume"
)

var (
	// VolumeDrop is how many presets below the speaking level Vector idles at.
	// The speaking level itself is the human-desired level (jdoc + UI/intents).
	// Set to 0 to disable the whole raise/duck mechanism at runtime.
	VolumeDrop = envInt("VECTOR_VOLUME_DROP", 2)

	// VolumeHangTime is the margin held on top of the estimated speech length
	// before dropping back to idle. It absorbs both the error in that estimate
	// and the gap between consecutive sentence chunks of one LLM reply - if it
	// is too short, the level pumps mid-answer.
	VolumeHangTime = time.Duration(envInt("VECTOR_VOLUME_HANG_MS", 2500)) * time.Millisecond

	// VolumeMsPerWord is the assumed TTS rate used to size the hold. Vector
	// speaks slowly and deliberately; erring long only costs a little idle
	// delay, whereas erring short ducks him mid-sentence.
	VolumeMsPerWord = envInt("VECTOR_VOLUME_MS_PER_WORD", 400)

	// VolumeTurnMs is the default hold for SpeechVolumeEnterTurn when d <= 0.
	VolumeTurnMs = envInt("VECTOR_VOLUME_TURN_MS", 15000)

	// VolumeSessionMs is the default hold for SpeechVolumeEnterSession when d <= 0.
	VolumeSessionMs = envInt("VECTOR_VOLUME_SESSION_MS", 45000)
)

func envInt(key string, def int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// clampVolume keeps a preset inside the firmware's range. This is what stops
// a low speaking level from producing a negative idle level: speak 1 with a
// drop of 2 lands at Mute, not at -1.
func clampVolume(v int) int {
	if v < volumeMin {
		return volumeMin
	}
	if v > volumeMax {
		return volumeMax
	}
	return v
}

// idleLevel is the ducked level for a given desired speech volume and drop.
func idleLevel(desired, drop int) int {
	return clampVolume(desired - drop)
}

// volumeMode controls how long a hold lasts and how restore demotes.
type volumeMode int

const (
	modeUtterance volumeMode = iota
	modeTurn
	modeSession
)

// speechVolumeDoc is the JsonDoc payload for wirepod.SpeechVolume.
type speechVolumeDoc struct {
	DesiredSpeechVolume int `json:"desired_speech_volume"`
}

type volumeState struct {
	// mu serializes ALL work for one robot, network calls included. Holding a
	// lock across I/O is usually a smell; here it is the entire point. The bump
	// path reads master_volume back to spot human changes, so if a restore
	// write could land between that read and the decision it drives, the patch
	// mistakes its own idle level for a human's choice, adopts it as the new
	// speaking level, and ratchets the volume down a preset per reply until it
	// bottoms out. Serializing per robot makes that window not exist.
	mu sync.Mutex

	loud      bool
	loudUntil time.Time
	mode      volumeMode

	// desired is the level a human last asked for (source of truth for speech).
	// Persisted in wirepod.SpeechVolume jdoc. Never updated by restore/duck.
	desired int
	// lastWritten is the level THIS patch last wrote. The difference between
	// live master_volume and lastWritten is how we spot a human change.
	lastWritten int
	// desiredLoaded is true after we have tried jdoc (and optional live seed).
	desiredLoaded bool
}

var (
	// volumeMu guards the map itself ONLY - never held across I/O. Per-robot
	// work is serialized by volumeState.mu instead, so one slow robot can't
	// block another.
	volumeMu     sync.Mutex
	volumeStates = map[string]*volumeState{}
	volumeWatch  sync.Once

	// One pooled client - these are frequent, short, same-host requests.
	volumeHTTP = &http.Client{
		Transport: &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}},
		Timeout:   5 * time.Second,
	}
)

// EstimateSpeechDuration guesses how long Vector will take to say text.
//
// Every speech path needs this, because none of them can tell us when the
// utterance actually ended: gRPC SayText is unary and returns as soon as the
// robot accepts the request, and bcontrol's sayText is fire-and-forget on top
// of that. So the hold is sized from the text rather than from an
// end-of-speech signal that doesn't exist.
func EstimateSpeechDuration(text string) time.Duration {
	words := len(strings.Fields(text))
	d := time.Duration(words*VolumeMsPerWord) * time.Millisecond
	if d < 1500*time.Millisecond {
		d = 1500 * time.Millisecond
	}
	return d
}

// SpeechVolumeBump raises the robot to its desired speaking level if it isn't
// already there and holds it for VolumeHangTime. Cheap and safe to call on
// every utterance: once loud, further calls only extend the deadline.
func SpeechVolumeBump(esn string) {
	speechVolumeHold(esn, VolumeHangTime, modeUtterance)
}

// SpeechVolumeHoldFor is SpeechVolumeBump with an explicit minimum hold on top
// of the hang time - for callers that know roughly how long the utterance will
// run but don't block until it finishes.
func SpeechVolumeHoldFor(esn string, d time.Duration) {
	speechVolumeHold(esn, d+VolumeHangTime, modeUtterance)
}

// SpeechVolumeEnterTurn holds desired volume for one reply cycle (or d if > 0).
// d <= 0 uses VECTOR_VOLUME_TURN_MS.
func SpeechVolumeEnterTurn(esn string, d time.Duration) {
	if d <= 0 {
		d = time.Duration(VolumeTurnMs) * time.Millisecond
	}
	speechVolumeHold(esn, d, modeTurn)
}

// SpeechVolumeEnterSession holds desired volume for a multi-turn / listen window.
// d <= 0 uses VECTOR_VOLUME_SESSION_MS.
func SpeechVolumeEnterSession(esn string, d time.Duration) {
	if d <= 0 {
		d = time.Duration(VolumeSessionMs) * time.Millisecond
	}
	speechVolumeHold(esn, d, modeSession)
}

// SpeechVolumeLeaveSession demotes session/turn to utterance and schedules a
// short hang before restore ducks to idle. Unlike holds, this may shorten
// loudUntil so a long session ends promptly after leave.
func SpeechVolumeLeaveSession(esn string) {
	if esn == "" || VolumeDrop <= 0 {
		return
	}
	st := volumeStateFor(esn)
	st.mu.Lock()
	defer st.mu.Unlock()
	st.mode = modeUtterance
	st.loudUntil = time.Now().Add(VolumeHangTime)
}

// SpeechVolumeSetDesired clamps level to 0–5, updates RAM, and persists the
// desired speech volume to the pod jdoc. Does not write live master_volume.
func SpeechVolumeSetDesired(esn string, level int) {
	if esn == "" {
		return
	}
	level = clampVolume(level)
	st := volumeStateFor(esn)
	st.mu.Lock()
	defer st.mu.Unlock()
	st.desired = level
	st.desiredLoaded = true
	saveDesiredToJdoc(esn, level)
}

// SpeechVolumeDesired returns the cached/jdoc desired level, or unknownVolume
// (-1) if it has never been observed.
func SpeechVolumeDesired(esn string) int {
	if esn == "" {
		return unknownVolume
	}
	st := volumeStateFor(esn)
	st.mu.Lock()
	defer st.mu.Unlock()
	ensureDesiredLocked(esn, st)
	return st.desired
}

// volumeStateFor returns the state for a robot, creating it on first sight.
// Takes only the map lock, briefly - callers then take st.mu themselves.
func volumeStateFor(esn string) *volumeState {
	volumeMu.Lock()
	defer volumeMu.Unlock()
	st, ok := volumeStates[esn]
	if !ok {
		st = &volumeState{desired: unknownVolume, lastWritten: unknownVolume}
		volumeStates[esn] = st
	}
	return st
}

// loadDesiredFromJdoc reads wirepod.SpeechVolume for vic:esn.
func loadDesiredFromJdoc(esn string) (int, bool) {
	jdoc, ok := vars.GetJdoc(speechVolumeThingPrefix+esn, speechVolumeJdocName)
	if !ok || strings.TrimSpace(jdoc.JsonDoc) == "" {
		return unknownVolume, false
	}
	var doc speechVolumeDoc
	if err := json.Unmarshal([]byte(jdoc.JsonDoc), &doc); err != nil {
		return unknownVolume, false
	}
	return clampVolume(doc.DesiredSpeechVolume), true
}

// saveDesiredToJdoc persists desired under thing "vic:"+esn. AddJdoc already
// calls WriteJdocs; do not call WriteJdocs again.
func saveDesiredToJdoc(esn string, level int) {
	level = clampVolume(level)
	thing := speechVolumeThingPrefix + esn
	jdoc, exists := vars.GetJdoc(thing, speechVolumeJdocName)
	if !exists {
		jdoc.DocVersion = 1
		jdoc.FmtVersion = 1
		jdoc.ClientMetadata = "wirepod-speech-volume"
	}
	raw, err := json.Marshal(speechVolumeDoc{DesiredSpeechVolume: level})
	if err != nil {
		fmt.Printf("[volume] %s: marshal desired jdoc: %v\n", esn, err)
		return
	}
	jdoc.JsonDoc = string(raw)
	vars.AddJdoc(thing, speechVolumeJdocName, jdoc)
}

// ensureDesiredLocked seeds st.desired from jdoc, or once from live master_volume
// on first use when the jdoc is missing. Caller must hold st.mu.
func ensureDesiredLocked(esn string, st *volumeState) {
	if st.desiredLoaded && st.desired != unknownVolume {
		return
	}
	if level, ok := loadDesiredFromJdoc(esn); ok {
		st.desired = level
		st.desiredLoaded = true
		return
	}
	// First use and no jdoc: read live once, treat as desired, persist.
	cur, err := getMasterVolume(esn)
	if err != nil {
		st.desiredLoaded = true // avoid hammering; next SetDesired/hold may retry via unknown
		return
	}
	st.desired = cur
	st.desiredLoaded = true
	saveDesiredToJdoc(esn, cur)
}

// shouldAdoptDesired decides whether live cur is a human volume change that
// should become the new desired. Pure function for tests and hold path.
//
// Rules (binding):
//   - cur == lastWritten → no adopt (our own write)
//   - desired known and cur == idle(desired, drop) → never adopt (our duck)
//   - desired known and cur == desired → no adopt (already at speak)
//   - otherwise → human change: adopt cur
//
// When desired is unknown, any cur that is not lastWritten is adopted.
func shouldAdoptDesired(cur, lastWritten, desired, drop int) (newDesired int, adopt bool) {
	if cur == lastWritten {
		return desired, false
	}
	if desired != unknownVolume {
		if cur == idleLevel(desired, drop) {
			return desired, false
		}
		if cur == desired {
			return desired, false
		}
	}
	return cur, true
}

// defaultHoldDuration returns d, or the mode's env default when d <= 0.
// Exported for tests via the package-level env ints; pure for turn/session.
func defaultHoldDuration(d time.Duration, mode volumeMode) time.Duration {
	if d > 0 {
		return d
	}
	switch mode {
	case modeTurn:
		return time.Duration(VolumeTurnMs) * time.Millisecond
	case modeSession:
		return time.Duration(VolumeSessionMs) * time.Millisecond
	default:
		return VolumeHangTime
	}
}

// rankMode returns whether next should replace current (session > turn > utterance).
// Entering a higher-or-equal mode upgrades/keeps; LeaveSession demotes explicitly.
func applyMode(st *volumeState, next volumeMode) {
	// Session upgrades everything; turn upgrades utterance; utterance never demotes session/turn.
	if next == modeSession {
		st.mode = modeSession
		return
	}
	if next == modeTurn && st.mode != modeSession {
		st.mode = modeTurn
		return
	}
	if next == modeUtterance && st.mode == modeUtterance {
		// stay utterance
		return
	}
	// HoldFor/Bump while in session or turn: keep elevated mode, only extend deadline.
}

func speechVolumeHold(esn string, d time.Duration, mode volumeMode) {
	if esn == "" || VolumeDrop <= 0 {
		return
	}
	volumeWatch.Do(func() { go volumeRestoreLoop() })
	st := volumeStateFor(esn)

	// Held across the read and the write below, so a restore for this robot
	// cannot interleave and be mistaken for a human moving the volume.
	st.mu.Lock()
	defer st.mu.Unlock()

	// The deadline only ever moves forward - a short bump arriving after a long
	// hold must not cut the long one short.
	if until := time.Now().Add(d); until.After(st.loudUntil) {
		st.loudUntil = until
	}
	applyMode(st, mode)

	if st.loud {
		// Already loud: deadline extended, mode maybe upgraded, no HTTP.
		return
	}

	ensureDesiredLocked(esn, st)

	// Transitioning idle -> loud. Resync desired only on true human changes.
	cur, err := getMasterVolume(esn)
	if err == nil {
		if newDes, adopt := shouldAdoptDesired(cur, st.lastWritten, st.desired, VolumeDrop); adopt {
			st.desired = newDes
			st.desiredLoaded = true
			saveDesiredToJdoc(esn, newDes)
		}
	} else if st.desired == unknownVolume {
		// Never observed this robot and can't read it now - don't guess a
		// level. Stays not-loud, so the next utterance retries.
		fmt.Printf("[volume] %s: cannot read level, skipping hold: %v\n", esn, err)
		return
	}

	if st.desired <= volumeMin {
		// A human muted him. Respect that absolutely: never raise, never write.
		// Stays not-loud so we re-read next time and notice an unmute.
		return
	}

	// Transition-only write: if already at desired, just mark loud.
	if err == nil && cur == st.desired {
		st.lastWritten = st.desired
		st.loud = true
		return
	}
	if st.lastWritten == st.desired && err != nil {
		// Can't confirm live, but we last wrote desired — treat as loud.
		st.loud = true
		return
	}

	if err := setMasterVolume(esn, st.desired); err != nil {
		// Stays not-loud so the next utterance retries, rather than believing
		// we're loud while actually sitting at the idle level.
		return
	}
	st.lastWritten = st.desired
	st.loud = true
	idle := idleLevel(st.desired, VolumeDrop)
	fmt.Printf("[volume] %s raised to desired=%d (idle=%d)\n", esn, st.desired, idle)
}

// volumeRestoreLoop drops each robot back to its idle level once it has been
// quiet for long enough. One watchdog covers every robot.
func volumeRestoreLoop() {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for range ticker.C {
		// Snapshot under the map lock, then work per robot - never hold the map
		// lock across a network call.
		volumeMu.Lock()
		snapshot := make(map[string]*volumeState, len(volumeStates))
		for esn, st := range volumeStates {
			snapshot[esn] = st
		}
		volumeMu.Unlock()
		for esn, st := range snapshot {
			restoreOne(esn, st)
		}
	}
}

// restoreOne drops a single robot to idle if its hold has lapsed. Takes the
// same per-robot lock the bump path uses, so the two can never interleave.
// NEVER saves desired on restore (duck must not become the new speech level).
func restoreOne(esn string, st *volumeState) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if !st.loud || !time.Now().After(st.loudUntil) {
		return
	}
	// Session / turn / utterance all duck when loudUntil expires. LeaveSession
	// only demotes mode and shortens the deadline; restore still uses loudUntil.
	if st.desired == unknownVolume {
		st.loud = false
		return
	}
	idle := idleLevel(st.desired, VolumeDrop)
	// Transition-only: skip HTTP if we already wrote idle.
	if st.lastWritten != idle {
		if err := setMasterVolume(esn, idle); err != nil {
			// Stay loud and retry on the next tick.
			return
		}
		st.lastWritten = idle
	}
	st.loud = false
	if st.mode != modeUtterance {
		st.mode = modeUtterance
	}
	fmt.Printf("[volume] %s dropped to idle=%d\n", esn, idle)
}

// robotEndpoint resolves an ESN to the robot's gateway host and token.
func robotEndpoint(esn string) (string, string, error) {
	for _, bot := range vars.BotInfo.Robots {
		if strings.EqualFold(strings.TrimSpace(bot.Esn), strings.TrimSpace(esn)) {
			return bot.IPAddress + ":443", bot.GUID, nil
		}
	}
	return "", "", fmt.Errorf("no robot with esn %s", esn)
}

// setMasterVolume writes master_volume through the robot's HTTPS settings
// endpoint - the same path the web UI's volume radios use. gRPC UpdateSettings
// doesn't carry master_volume, so this is the available route.
func setMasterVolume(esn string, level int) error {
	target, token, err := robotEndpoint(esn)
	if err != nil {
		return err
	}
	level = clampVolume(level)
	body := []byte(fmt.Sprintf(`{"update_settings": true, "settings": {"master_volume": %d} }`, level))
	req, err := http.NewRequest("POST", "https://"+target+"/v1/update_settings", bytes.NewBuffer(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := volumeHTTP.Do(req)
	if err != nil {
		fmt.Printf("[volume] set %d failed for %s: %v\n", level, esn, err)
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return nil
}

// getMasterVolume reads the robot's current master_volume preset.
//
// The robot will sometimes answer a ROBOT_SETTINGS pull with a different jdoc
// entirely - wire-pod's own get_sdk_settings retries for exactly this reason.
// Rather than sniffing for the wrong document, we simply require the reply to
// carry a master_volume key and retry if it doesn't.
func getMasterVolume(esn string) (int, error) {
	target, token, err := robotEndpoint(esn)
	if err != nil {
		return unknownVolume, err
	}
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if attempt > 0 {
			time.Sleep(400 * time.Millisecond)
		}
		doc, err := pullRobotSettings(target, token)
		if err != nil {
			lastErr = err
			continue
		}
		// Pointer, not int: 0 is Mute, a real choice, and must be
		// distinguishable from "the key wasn't there".
		var settings struct {
			MasterVolume *int `json:"master_volume"`
		}
		if err := json.Unmarshal([]byte(doc), &settings); err != nil {
			lastErr = err
			continue
		}
		if settings.MasterVolume == nil {
			lastErr = fmt.Errorf("jdoc carried no master_volume")
			continue
		}
		return clampVolume(*settings.MasterVolume), nil
	}
	return unknownVolume, lastErr
}

// pullRobotSettings fetches the ROBOT_SETTINGS jdoc over the robot's HTTPS
// gateway and returns the settings JSON it carries.
func pullRobotSettings(target, token string) (string, error) {
	req, err := http.NewRequest("POST", "https://"+target+"/v1/pull_jdocs",
		bytes.NewBuffer([]byte(`{"jdoc_types": ["ROBOT_SETTINGS"]}`)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := volumeHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("pull_jdocs returned %s", resp.Status)
	}

	// grpc-gateway emits either the proto's orig_name or its camelCase JSON
	// name depending on the marshaler it was built with, so accept both rather
	// than betting on one.
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return "", err
	}
	named, ok := pickKey(top, "named_jdocs", "namedJdocs")
	if !ok {
		return "", fmt.Errorf("pull_jdocs reply had no named_jdocs")
	}
	var jdocs []map[string]json.RawMessage
	if err := json.Unmarshal(named, &jdocs); err != nil {
		return "", err
	}
	if len(jdocs) == 0 {
		return "", fmt.Errorf("pull_jdocs reply was empty")
	}
	docRaw, ok := pickKey(jdocs[0], "doc")
	if !ok {
		return "", fmt.Errorf("named_jdoc had no doc")
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(docRaw, &doc); err != nil {
		return "", err
	}
	jsonDocRaw, ok := pickKey(doc, "json_doc", "jsonDoc")
	if !ok {
		return "", fmt.Errorf("doc had no json_doc")
	}
	var jsonDoc string
	if err := json.Unmarshal(jsonDocRaw, &jsonDoc); err != nil {
		return "", err
	}
	return jsonDoc, nil
}

func pickKey(m map[string]json.RawMessage, keys ...string) (json.RawMessage, bool) {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			return v, true
		}
	}
	return nil, false
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
# Live path: SayText(esn, text) via robotsession (preferred).
ANCHOR_SAYTEXT_ESN = (
    "func SayText(esn, text string) {\n"
    "\tif robotsession.Default == nil {\n"
    "\t\tlogger.Println(\"SayText: robotsession.Default is nil; cannot speak on \" + esn)\n"
    "\t\treturn\n"
    "\t}\n"
)
REPLACE_SAYTEXT_ESN = (
    ANCHOR_SAYTEXT_ESN
    + "\t// Raise/hold desired speech volume for the estimated utterance length.\n"
    + "\t// Session.Say is unary and returns when the robot accepts the request, so\n"
    + "\t// we size the hold from the text rather than from end-of-speech.\n"
    + "\tSpeechVolumeHoldFor(esn, EstimateSpeechDuration(text))\n"
)

# Legacy/dead path: sayText(robot, text) still present as a stub on some trees.
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
    # Sanity: never ship the old RAM-only latch without jdoc / session API.
    for needle in (SENTINEL_JDOC, SENTINEL_SESSION, SENTINEL_BUMP):
        if needle not in SPEECH_VOLUME_GO:
            print(
                f"[volume] embedded {SPEECH_VOLUME_FILENAME} missing {needle!r}",
                file=sys.stderr,
            )
            sys.exit(1)
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


def patch_file(path: Path, anchor: str, replacement: str, *, already: str) -> bool:
    """Insert replacement at anchor unless `already` sentinel is present."""
    src = path.read_text(encoding="utf-8")
    if already in src:
        print(f"[volume] {path.name} already patched ({already.split('(')[0]}).")
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
    patch_file(intent_graph, ANCHOR_IG, REPLACE_IG, already=SENTINEL_BUMP)
    migrate_saytext(kgsim_cmds)
    patch_file(kgsim_cmds, ANCHOR_SAYTEXT, REPLACE_SAYTEXT, already=SENTINEL_HOLD_ROBOT)
    # Prefer live SayText(esn); also keep legacy sayText hold if that stub exists.
    bsrc = bcontrol.read_text(encoding="utf-8")
    if "func SayText(esn, text string)" in bsrc:
        patch_file(
            bcontrol,
            ANCHOR_SAYTEXT_ESN,
            REPLACE_SAYTEXT_ESN,
            already=SENTINEL_HOLD_ESN,
        )
    bsrc = bcontrol.read_text(encoding="utf-8")
    if "func sayText(robot *vector.Vector, text string)" in bsrc:
        # Use robot-specific hold string; avoid skipping because SayText(esn) is patched.
        patch_file(
            bcontrol,
            ANCHOR_BCONTROL,
            REPLACE_BCONTROL,
            already="SpeechVolumeHoldFor(robot.Cfg.SerialNo, EstimateSpeechDuration(text))",
        )

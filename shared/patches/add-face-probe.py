#!/usr/bin/env python3
"""Add a concurrent face probe so Vector knows the speaker before he replies.

Faces normally only reach the server through the per-response interrupt
stream, so the face used to scope query N is whoever was seen during
response N-1 - a one-turn lag (a stranger is greeted on their *second*
utterance, a speaker hand-off lands a turn late).

This patch adds ObserveFaceBriefly(), launched as a goroutine at the start
of a voice request - it runs concurrently with speech-to-text, so by the
time the LLM request fires vector-ai already knows the current speaker.
Zero added latency: it overlaps the user speaking.

Uses robotsession.Session.ProbeFace (≤6s secondary face stream) — never
vector.New and never a continuous face subscription.

Creates chipper/pkg/wirepod/ttr/face_probe.go and patches intent_graph.go.
Idempotent.
"""
import sys
from pathlib import Path

FACE_PROBE_FILENAME = "face_probe.go"

FACE_PROBE_GO = '''package wirepod_ttr

import (
\t"context"
\t"fmt"
\t"time"

\t"github.com/kercre123/wire-pod/chipper/pkg/wirepod/robotsession"
)

// ObserveFaceBriefly briefly probes for who Vector is looking at and reports
// to vector-ai (via notifyFaceSeen). Launched as a goroutine at the START of
// a voice request so it runs concurrently with speech-to-text - by the time
// the LLM request fires, vector-ai already knows the current speaker, with no
// one-turn lag and no added latency.
//
// Uses Session.ProbeFace (≤6s, secondary ConnectionId wirepod-face-<esn>).
// Never continuous robot_observed_face; never vector.New.
//
// It also marks voice activity so the ambient awareness loop knows a
// conversation is in progress and stays out of the way.
func ObserveFaceBriefly(esn string) {
\tMarkVoiceActivity()
\tif robotsession.Default == nil {
\t\treturn
\t}
\tctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
\tdefer cancel()
\tsess, err := robotsession.Default.Get(ctx, esn)
\tif err != nil {
\t\tfmt.Printf("[face-probe] session get failed for %s: %v\\n", esn, err)
\t\treturn
\t}
\tfaceID, name, sawAny, err := sess.ProbeFace(ctx, 6*time.Second)
\tif err != nil {
\t\tfmt.Printf("[face-probe] ProbeFace failed for %s: %v\\n", esn, err)
\t\treturn
\t}
\tif sawAny || faceID != 0 || name != "" {
\t\tnotifyFaceSeen(faceID, name)
\t}
}
'''

SENTINEL_INTENT = "ObserveFaceBriefly"

ANCHOR = (
    "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
    "\tvar transcribedText string\n"
)
REPLACEMENT = (
    "\tspeechReq := sr.ReqToSpeechRequest(req)\n"
    "\t// Observe the speaker's face concurrently with speech-to-text so the\n"
    "\t// current person is known before the LLM request fires (no one-turn lag).\n"
    "\tgo ttr.ObserveFaceBriefly(speechReq.Device)\n"
    "\tvar transcribedText string\n"
)


def write_face_probe(ttr_dir: Path) -> bool:
    target = ttr_dir / FACE_PROBE_FILENAME
    if target.exists() and target.read_text(encoding="utf-8") == FACE_PROBE_GO:
        print(f"[face-probe] {FACE_PROBE_FILENAME} already in place.")
        return False
    target.write_text(FACE_PROBE_GO, encoding="utf-8", newline="\n")
    print(f"[face-probe] wrote {target}")
    return True


def patch_intent_graph(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL_INTENT in src:
        print(f"[face-probe] {path.name} already patched.")
        return False
    if ANCHOR not in src:
        print(f"[face-probe] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    src = src.replace(ANCHOR, REPLACEMENT, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[face-probe] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-wire-pod-dir>", file=sys.stderr)
        sys.exit(2)
    wirepod = Path(sys.argv[1])
    ttr_dir = wirepod / "chipper" / "pkg" / "wirepod" / "ttr"
    intent_graph = wirepod / "chipper" / "pkg" / "wirepod" / "preqs" / "intent_graph.go"
    if not ttr_dir.exists() or not intent_graph.exists():
        print(f"[face-probe] target paths not found under {wirepod}", file=sys.stderr)
        sys.exit(1)
    write_face_probe(ttr_dir)
    patch_intent_graph(intent_graph)

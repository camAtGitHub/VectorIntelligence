#!/usr/bin/env python3
"""Add a 5-second wake-word grace period to Wire-Pod's response interrupter.

Without this, false-positive wake words during the silent gap between
"end of user speech" and "start of LLM response" abort the reply before
Vector ever speaks it. Touch interrupts (petting Vector to stop him) are
preserved unchanged.

On the robotsession-based interrupter (kgsim_interrupt.go using
SubscribeState), grace is implemented in-tree (sentinel wakeWordGrace).

Exit policy:
  0 — sentinel present, legacy inject applied, or robotsession markers
      without sentinel (in-tree port expected; WARN)
  1 — file missing, or unrecognized stock shape without injectable anchor
  2 — usage error

Idempotent: detects whether the patch is already applied and skips if so.
"""
import sys
from pathlib import Path

SENTINEL = "wakeWordGrace"

# Legacy pre-robotsession stream.Recv interrupter (stock wire-pod shape).
OLD_BLOCK = """\tif origValueGotten {
\t\tfor {
\t\t\tvar resp *vectorpb.EventResponse
\t\t\tresp, err = strm.Recv()
\t\t\tif err != nil {
\t\t\t\tlogger.Println(\"Event stream error: \" + err.Error())
\t\t\t\treturn false
\t\t\t}
\t\t\tswitch resp.Event.EventType.(type) {
\t\t\tcase *vectorpb.Event_RobotState:
\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {
\t\t\t\t\tvalsAboveValue++
\t\t\t\t} else {
\t\t\t\t\tvalsAboveValue = 0
\t\t\t\t}
\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true
\t\t\tdefault:
\t\t\t}"""

NEW_BLOCK = """\tif origValueGotten {
\t\t// Wake-word grace period: ignore wake-word events for the first few
\t\t// seconds so false positives during the LLM-thinking silence (or
\t\t// Vector's own motor noise) don't kill the response before it starts.
\t\tstartTime := time.Now()
\t\tconst wakeWordGrace = 5 * time.Second
\t\tfor {
\t\t\tvar resp *vectorpb.EventResponse
\t\t\tresp, err = strm.Recv()
\t\t\tif err != nil {
\t\t\t\tlogger.Println(\"Event stream error: \" + err.Error())
\t\t\t\treturn false
\t\t\t}
\t\t\tswitch resp.Event.EventType.(type) {
\t\t\tcase *vectorpb.Event_RobotState:
\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {
\t\t\t\t\tvalsAboveValue++
\t\t\t\t} else {
\t\t\t\t\tvalsAboveValue = 0
\t\t\t\t}
\t\t\tcase *vectorpb.Event_WakeWord:
\t\t\t\tif time.Since(startTime) < wakeWordGrace {
\t\t\t\t\tlogger.Println(\"Ignoring wake-word during grace period\")
\t\t\t\t\tcontinue
\t\t\t\t}
\t\t\t\tlogger.Println(\"Interrupting LLM response (source: wake word)\")
\t\t\t\tstopResponse = true
\t\t\tdefault:
\t\t\t}"""


def is_session_tree(src: str) -> bool:
    return "SubscribeState" in src or "robotsession" in src


def patch_file(path: Path) -> int:
    """Return process exit code (0 success/skip, 1 hard fail)."""
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[wake-word-grace] {path.name} already patched, skipping.")
        return 0
    if OLD_BLOCK in src:
        src = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
        path.write_text(src, encoding="utf-8", newline="\n")
        print(f"[wake-word-grace] {path.name} patched: wake-word grace period added.")
        return 0
    if is_session_tree(src):
        print(
            f"[wake-word-grace] WARN: {path.name}: robotsession interrupter without "
            f"{SENTINEL}; requires in-tree port. Skipping (exit 0)."
        )
        return 0
    print(
        f"[wake-word-grace] ERROR: {path.name}: unrecognized stock interrupter "
        f"without injectable anchor; refusing silent feature drop.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_interrupt.go>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    if not target.is_file():
        print(f"[wake-word-grace] file not found: {target}", file=sys.stderr)
        sys.exit(1)
    sys.exit(patch_file(target))

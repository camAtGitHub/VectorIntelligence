#!/usr/bin/env python3
"""Make Vector's back button interrupt his speech.

Wire-Pod's interrupt loop (kgsim_interrupt.go) already stops a response on
a wake-word event or a touch-sensor spike, but ignores the physical back
button - which registers as the IS_BUTTON_PRESSED status bit in
robot_state, not as a wake-word event. This patch adds a check for that
bit so pressing the button stops him talking immediately.

No grace period for the button: a deliberate physical press can't be a
false trigger from Vector's own opening words (unlike the wake word), so
it interrupts at any point in the response.

On the robotsession-based interrupter, the button check is implemented
in-tree (sentinel "source: back button").

Exit policy:
  0 — sentinel present, legacy inject applied, or robotsession markers
  1 — file missing, or unrecognized stock without injectable anchor
  2 — usage error

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim_interrupt.go.
"""
import sys
from pathlib import Path

SENTINEL = "source: back button"

# Legacy pre-robotsession stream.Recv interrupter.
ANCHOR = (
    "\t\t\tcase *vectorpb.Event_RobotState:\n"
    "\t\t\t\tif resp.Event.GetRobotState().TouchData.GetRawTouchValue() > origTouchValue+50 {\n"
    "\t\t\t\t\tvalsAboveValue++\n"
    "\t\t\t\t} else {\n"
    "\t\t\t\t\tvalsAboveValue = 0\n"
    "\t\t\t\t}\n"
)

REPLACEMENT = (
    "\t\t\tcase *vectorpb.Event_RobotState:\n"
    "\t\t\t\trsEvt := resp.Event.GetRobotState()\n"
    "\t\t\t\tif rsEvt.TouchData.GetRawTouchValue() > origTouchValue+50 {\n"
    "\t\t\t\t\tvalsAboveValue++\n"
    "\t\t\t\t} else {\n"
    "\t\t\t\t\tvalsAboveValue = 0\n"
    "\t\t\t\t}\n"
    "\t\t\t\t// Physical back button - interrupt immediately, no grace period.\n"
    "\t\t\t\tif rsEvt.Status&uint32(vectorpb.RobotStatus_ROBOT_STATUS_IS_BUTTON_PRESSED) != 0 {\n"
    "\t\t\t\t\tlogger.Println(\"Interrupting LLM response (source: back button)\")\n"
    "\t\t\t\t\tstopResponse = true\n"
    "\t\t\t\t}\n"
)


def is_session_tree(src: str) -> bool:
    return "SubscribeState" in src or "robotsession" in src


def patch(path: Path) -> int:
    """Return process exit code (0 success/skip, 1 hard fail)."""
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[button-interrupt] {path.name} already patched.")
        return 0
    if ANCHOR in src:
        src = src.replace(ANCHOR, REPLACEMENT, 1)
        path.write_text(src, encoding="utf-8", newline="\n")
        print(f"[button-interrupt] {path.name} patched.")
        return 0
    if is_session_tree(src):
        print(
            f"[button-interrupt] WARN: {path.name}: robotsession interrupter without "
            f"'{SENTINEL}'; requires in-tree port. Skipping (exit 0)."
        )
        return 0
    print(
        f"[button-interrupt] ERROR: {path.name}: unrecognized stock interrupter "
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
        print(f"[button-interrupt] file not found: {target}", file=sys.stderr)
        sys.exit(1)
    sys.exit(patch(target))

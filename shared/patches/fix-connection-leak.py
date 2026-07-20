#!/usr/bin/env python3
"""Fix the gRPC connection leak in Wire-Pod's LLM response path.

HISTORICAL: Added defer robot.Close() at vector.New leak sites in kgsim.go and
a 5s BatteryState timeout. That approach is superseded by robotsession
(TASK-05/06): StreamingKGSim / KGSim use robotsession.Default.Get and never
vector.New per query, so Close-on-query would tear down the shared session.

This patch is now a no-op when kgsim is session-based. It remains idempotent
and still applies the old Close-based fix only if legacy anchors are present
(unmigrated trees).

Requires the SDK Close() method when the legacy path still applies — see
add-sdk-close.py.

Idempotent. Modifies chipper/pkg/wirepod/ttr/kgsim.go only if needed.
"""
import sys
from pathlib import Path

SENTINEL_LEGACY = "defer robot.Close()"
SENTINEL_SESSION = "robotsession.Default"

# Leak site 1: the per-request robot created in the `if matched` block, plus
# the unbounded BatteryState probe right after it.
ANCHOR_BAT = (
    "\t\trobot, err = vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))\n"
    "\t\tif err != nil {\n"
    "\t\t\treturn err.Error(), err\n"
    "\t\t}\n"
    "\t}\n"
    "\t_, err := robot.Conn.BatteryState(context.Background(), &vectorpb.BatteryStateRequest{})\n"
)
REPLACE_BAT = (
    "\t\trobot, err = vector.New(vector.WithSerialNo(esn), vector.WithToken(guid), vector.WithTarget(target))\n"
    "\t\tif err != nil {\n"
    "\t\t\treturn err.Error(), err\n"
    "\t\t}\n"
    "\t\t// Release the gRPC connection when this request finishes - otherwise\n"
    "\t\t// every voice query leaks a connection and the robot's SDK wedges.\n"
    "\t\tdefer robot.Close()\n"
    "\t}\n"
    "\t// 5s timeout: if the robot's SDK is unresponsive this fails fast instead\n"
    "\t// of hanging forever on context.Background().\n"
    "\tbatCtx, batCancel := context.WithTimeout(context.Background(), 5*time.Second)\n"
    "\t_, err := robot.Conn.BatteryState(batCtx, &vectorpb.BatteryStateRequest{})\n"
    "\tbatCancel()\n"
)

# Leak site 2: the speak-goroutine that acquires behavior control.
ANCHOR_GO = (
    "\tgo func() {\n"
    "\t\tstart := make(chan bool)\n"
    "\t\tstop := make(chan bool)\n"
)
REPLACE_GO = (
    "\tgo func() {\n"
    "\t\t// Release the robot connection when this speak-goroutine finishes.\n"
    "\t\tdefer robot.Close()\n"
    "\t\tstart := make(chan bool)\n"
    "\t\tstop := make(chan bool)\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")

    # Preferred path: kgsim already uses robotsession (TASK-06+).
    if SENTINEL_SESSION in src and "vector.New(" not in src:
        print(f"[connection-leak] {path.name}: superseded by robotsession (no vector.New); no-op.")
        return False

    if SENTINEL_LEGACY in src and ANCHOR_BAT not in src:
        print(f"[connection-leak] {path.name} already patched (legacy Close).")
        return False

    if ANCHOR_BAT not in src or ANCHOR_GO not in src:
        # Migrated or differently structured — do not fail install.
        print(
            f"[connection-leak] {path.name}: legacy anchors not found; "
            "superseded by robotsession or already transformed. no-op.",
        )
        return False

    src = src.replace(ANCHOR_BAT, REPLACE_BAT, 1)
    src = src.replace(ANCHOR_GO, REPLACE_GO, 1)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[connection-leak] {path.name} patched (legacy Close path).")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))

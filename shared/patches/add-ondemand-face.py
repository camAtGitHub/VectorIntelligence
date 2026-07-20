#!/usr/bin/env python3
"""On-demand face detection for Wire-Pod (install-safe no-op on modern trees).

Historically this patch added robot_observed_face to the *per-speak* interrupt
EventStream whitelist and called notifyFaceSeen from the interrupt switch.

That approach is superseded on robotsession builds:
  - Long Session state stream must NOT whitelist robot_observed_face
    (firmware load / AGENTS).
  - Voice-start face uses ObserveFaceBriefly -> Session.ProbeFace (short
    secondary stream in face_probe.go).

This script exits 0 when face_probe / ObserveFaceBriefly is present, or when
the interrupter is session-based. It never injects robot_observed_face onto
the long stream or the session interrupter whitelist.

Exit policy:
  0 — face_probe/ObserveFaceBriefly present, session tree, already has face
      handling, or stock without face_probe (deliberate: do not reintroduce
      firehose-on-interrupt; use add-face-probe.py)
  1 — path missing
  2 — usage error

Idempotent. Safe to leave in install.ps1 / install.sh order.
"""
import sys
from pathlib import Path


def has_face_probe(interrupt_path: Path) -> bool:
    """True if modern face path exists near the interrupter."""
    ttr = interrupt_path.parent
    face_probe = ttr / "face_probe.go"
    if face_probe.is_file():
        text = face_probe.read_text(encoding="utf-8", errors="replace")
        if "ObserveFaceBriefly" in text or "ProbeFace" in text:
            return True
    if interrupt_path.is_file():
        for p in ttr.glob("*.go"):
            try:
                if "ObserveFaceBriefly" in p.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                continue
    return False


def is_session_tree(src: str) -> bool:
    return "SubscribeState" in src or "robotsession" in src


def patch(path: Path) -> int:
    if not path.is_file():
        print(f"[ondemand-face] ERROR: file not found: {path}", file=sys.stderr)
        return 1

    src = path.read_text(encoding="utf-8")

    # Already has face handling in interrupt (legacy) — leave alone; do not write.
    if "Event_RobotObservedFace" in src or "robot_observed_face" in src:
        print(f"[ondemand-face] {path.name} already has face event handling.")
        return 0

    if has_face_probe(path):
        print(
            "[ondemand-face] superseded by face_probe.go / ObserveFaceBriefly "
            "(Session.ProbeFace). No-op; long stream stays robot_state+wake_word only."
        )
        return 0

    if is_session_tree(src):
        print(
            "[ondemand-face] robotsession interrupter: face is ProbeFace-only; "
            "not adding robot_observed_face to interrupt. Skipping."
        )
        return 0

    # Stock pre-robotsession tree without face_probe: do not reintroduce the
    # old firehose-on-interrupt path from install — face_probe patch should
    # land separately. Exit 0 deliberately (anti-pattern to inject face here).
    print(
        "[ondemand-face] no face_probe / ObserveFaceBriefly found; "
        "skipping firehose inject (use add-face-probe.py / in-tree ProbeFace)."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_interrupt.go>", file=sys.stderr)
        sys.exit(2)
    sys.exit(patch(Path(sys.argv[1])))

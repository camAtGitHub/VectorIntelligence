"""Regression: ambient patch keeps SpeechVolumeHoldFor before SayText.

Without this, ambient / greeting / behavior-tick speech stays at idle duck,
and full reinstall overwrites live ambient.go from the embedded source.
"""
from pathlib import Path

PATCH = Path(__file__).resolve().parent / "patches" / "add-ambient-loop.py"


def test_ambient_patch_holds_volume_before_saytext():
    src = PATCH.read_text(encoding="utf-8")
    # Function appears once in the embedded AMBIENT_GO blob.
    marker = "func ambientReact("
    assert marker in src, "add-ambient-loop.py missing ambientReact"
    start = src.index(marker)
    # Next top-level func in the embedded Go (investigate move).
    next_fn = src.find("func ambientInvestigateMove", start + len(marker))
    body = src[start : next_fn if next_fn > 0 else start + 2500]

    hold = "SpeechVolumeHoldFor(esn, EstimateSpeechDuration(text))"
    say = "robot.Conn.SayText"
    assert hold in body, "ambientReact must call SpeechVolumeHoldFor before speak"
    assert say in body, "ambientReact must call robot.Conn.SayText"
    assert body.index(hold) < body.index(say), (
        "SpeechVolumeHoldFor must precede Conn.SayText so ambient is not ducked"
    )
    # Hold after investigate so motion does not burn loudUntil.
    invest = "ambientInvestigateMove"
    if invest in body:
        assert body.index(invest) < body.index(hold), (
            "SpeechVolumeHoldFor must follow ambientInvestigateMove"
        )

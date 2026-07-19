#!/usr/bin/env python3
"""Unit tests for supervisor.py's SDK-wedge detector.

Feeds the detector synthetic chipper.log lines (from issue #8) and asserts it
bounces only on the real wedge signature: N robot-RPC hangs within the window
while Vector is TCP-reachable, with a cooldown between bounces.

Run:
  python3 -m pytest shared/test_supervisor_wedge.py -q
"""
from __future__ import annotations

from supervisor import (
    WEDGE_COOLDOWN,
    WEDGE_PATTERN,
    WEDGE_STRIKES,
    WEDGE_WINDOW,
    WedgeDetector,
)

# Real lines from the issue #8 chipper.log.
WEDGE_LINES = [
    "[ambient] enable camera feed failed for 0050668d: rpc error: code = DeadlineExceeded desc = context deadline exceeded",
    "[ambient] image capture failed for 0050668d: rpc error: code = DeadlineExceeded desc = context deadline exceeded",
    '[greeting] connect failed for 0050668d: rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 10.0.0.163:443: i/o timeout"',
    '[sensor] event stream failed for 0050668d: rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 10.0.0.163:443: i/o timeout"',
]
# Lines that must NEVER count as strikes.
BENIGN_LINES = [
    # vector-ai HTTP timeout - local service slow, nothing to do with the robot
    '[sensor] vector-ai call failed: Post "http://127.0.0.1:8000/v1/sensor_reaction": context deadline exceeded (Client.Timeout exceeded while awaiting headers)',
    # ordinary stream reset - happens on robot sleep/reboot transitions
    "[sensor] recv error for 0050668d: rpc error: code = Unavailable desc = error reading from server: read tcp 1.2.3.4:63788->5.6.7.8:443: wsarecv: An existing connection was forcibly closed by the remote host.",
    "Bot 0050668d Transcribed text: what is 2+2?",
    "[ambient] starting ambient loop for 0050668d @ 1.2.3.4:443",
]


def test_wedge_pattern_matches_rpc_timeouts() -> None:
    for ln in WEDGE_LINES:
        assert WEDGE_PATTERN.search(ln) is not None, ln[:60]


def test_wedge_pattern_ignores_benign_lines() -> None:
    for ln in BENIGN_LINES:
        assert WEDGE_PATTERN.search(ln) is None, ln[:60]


def test_no_strikes_while_link_down() -> None:
    d = WedgeDetector()
    for i in range(5):
        d.feed(WEDGE_LINES[0], now=100.0 + i, link_up=False)
    assert len(d.strikes) == 0
    assert not d.should_bounce(110.0)


def test_bounce_at_strike_threshold() -> None:
    d = WedgeDetector()
    t = 1000.0
    for i in range(WEDGE_STRIKES - 1):
        d.feed(WEDGE_LINES[i % len(WEDGE_LINES)], now=t + i * 60, link_up=True)
    assert not d.should_bounce(t + 300)
    d.feed(WEDGE_LINES[0], now=t + 300, link_up=True)
    assert d.should_bounce(t + 300)
    assert len(d.strikes) == 0


def test_stale_strikes_age_out() -> None:
    d = WedgeDetector()
    d.feed(WEDGE_LINES[0], now=0.0, link_up=True)
    d.feed(WEDGE_LINES[1], now=1.0, link_up=True)
    late = WEDGE_WINDOW + 10.0
    d.feed(WEDGE_LINES[2], now=late, link_up=True)
    assert not d.should_bounce(late)


def test_cooldown_and_no_landmine_after_expiry() -> None:
    d = WedgeDetector()
    t = 5000.0
    for _ in range(WEDGE_STRIKES):
        d.feed(WEDGE_LINES[0], now=t, link_up=True)
    assert d.should_bounce(t)

    for _ in range(WEDGE_STRIKES):
        d.feed(WEDGE_LINES[0], now=t + 60, link_up=True)
    assert not d.should_bounce(t + 60)
    assert len(d.strikes) == 0

    t2 = t + WEDGE_COOLDOWN + 60
    # Critical regression: time alone + stale/cooldown-era strikes must not bounce.
    assert not d.should_bounce(t2)

    for i in range(WEDGE_STRIKES):
        d.feed(WEDGE_LINES[0], now=t2 + i, link_up=True)
    assert d.should_bounce(t2 + WEDGE_STRIKES)

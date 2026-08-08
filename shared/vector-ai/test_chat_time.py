"""Pure unit tests for chat_time formatters (no deps/MEMORY/soul).

Run:
  python3 -m pytest shared/vector-ai/test_chat_time.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from types import SimpleNamespace

from chat_time import (
    build_user_time_prefix,
    format_gap_tag,
    format_wall_clock_human,
    snapshot_prior_chat_ts,
)


# ---------------------------------------------------------------------------
# format_wall_clock_human
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dt, expected",
    [
        (datetime(2026, 8, 8, 21, 14), "Sat 8 Aug, 9:14pm"),
        (datetime(2026, 8, 8, 9, 5), "Sat 8 Aug, 9:05am"),
        (datetime(2026, 8, 8, 0, 0), "Sat 8 Aug, 12:00am"),
        (datetime(2026, 8, 8, 12, 0), "Sat 8 Aug, 12:00pm"),
        (datetime(2026, 8, 9, 8, 2), "Sun 9 Aug, 8:02am"),
        (datetime(2026, 1, 1, 15, 30), "Thu 1 Jan, 3:30pm"),
    ],
)
def test_format_wall_clock_human(dt, expected):
    assert format_wall_clock_human(dt) == expected
    assert "[" not in format_wall_clock_human(dt)


# ---------------------------------------------------------------------------
# format_gap_tag — D7 boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta_seconds, expected",
    [
        # Δ < 5m → None
        (0, None),
        (60, None),
        (4 * 60 + 59, None),
        (-10, None),
        # Δ < 30m → minutes
        (5 * 60, "5 minutes since last message"),
        (5 * 60 + 1, "5 minutes since last message"),
        (12 * 60, "12 minutes since last message"),
        (29 * 60, "29 minutes since last message"),
        (29 * 60 + 59, "30 minutes since last message"),  # round(1799/60)=30
        # Δ < 45m → half an hour
        (30 * 60, "Gap since previous message: half an hour ago"),
        (44 * 60 + 59, "Gap since previous message: half an hour ago"),
        # Δ < 60m → 45 minutes
        (45 * 60, "Gap since previous message: 45 minutes ago"),
        (59 * 60 + 59, "Gap since previous message: 45 minutes ago"),
        # Δ < 90m → an hour
        (60 * 60, "Gap since previous message: an hour ago"),
        (89 * 60 + 59, "Gap since previous message: an hour ago"),
        # Δ < 2h → 90 minutes
        (90 * 60, "Gap since previous message: 90 minutes ago"),
        (2 * 3600 - 1, "Gap since previous message: 90 minutes ago"),
        # Δ < 12h → N hours (2..11)
        (2 * 3600, "Gap since previous message: 2 hours ago"),
        (3 * 3600, "Gap since previous message: 3 hours ago"),
        (11 * 3600, "Gap since previous message: 11 hours ago"),
        (12 * 3600 - 1, "Gap since previous message: 11 hours ago"),  # round ≈ 12 → clamp 11
        # Δ < 24h → Half a day
        (12 * 3600, "Gap since previous message: Half a day ago"),
        (24 * 3600 - 1, "Gap since previous message: Half a day ago"),
        # Δ < 7d → N days (1..6)
        (1 * 86400, "Gap since previous message: 1 days ago"),
        (2 * 86400, "Gap since previous message: 2 days ago"),
        (6 * 86400, "Gap since previous message: 6 days ago"),
        (7 * 86400 - 1, "Gap since previous message: 6 days ago"),
        # Δ < 14d → A week
        (7 * 86400, "Gap since previous message: A week ago"),
        (13 * 86400 + 86399, "Gap since previous message: A week ago"),
        # else → N weeks (min 2 after 14d)
        (14 * 86400, "Gap since previous message: 2 weeks ago"),
        (21 * 86400, "Gap since previous message: 3 weeks ago"),
        (28 * 86400, "Gap since previous message: 4 weeks ago"),
    ],
)
def test_format_gap_tag_buckets(delta_seconds, expected):
    assert format_gap_tag(delta_seconds) == expected
    if expected is not None:
        assert not expected.startswith("[")
        assert not expected.endswith("]")


def test_format_gap_tag_invalid_returns_none():
    assert format_gap_tag("not-a-number") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_user_time_prefix — D4–D6
# ---------------------------------------------------------------------------


def test_build_prefix_first_message_wall_only():
    now = datetime(2026, 8, 8, 21, 14)
    prior = (now - timedelta(hours=3)).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=True,
    )
    assert out == "[Time: Sat 8 Aug, 9:14pm]"
    assert "Gap" not in out
    assert "minutes since" not in out


def test_build_prefix_missing_prior_wall_only():
    now = datetime(2026, 8, 8, 21, 14)
    for prior in (None, 0.0, -1.0):
        out = build_user_time_prefix(
            now_dt=now,
            prior_turn_ts=prior,
            is_first_message=False,
        )
        assert out == "[Time: Sat 8 Aug, 9:14pm]", prior


def test_build_prefix_same_day_short_gap_empty():
    """Same calendar day, Δ < 5m → empty prefix."""
    now = datetime(2026, 8, 8, 21, 14)
    prior = (now - timedelta(minutes=2)).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=False,
    )
    assert out == ""


def test_build_prefix_same_day_12m_gap_only():
    now = datetime(2026, 8, 8, 21, 14)
    prior = (now - timedelta(minutes=12)).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=False,
    )
    assert out == "[12 minutes since last message]"
    assert "Time:" not in out


def test_build_prefix_cross_day_wall_and_gap():
    now = datetime(2026, 8, 9, 8, 2)
    # Previous evening ~ half a day earlier (cross midnight)
    prior = datetime(2026, 8, 8, 20, 0).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=False,
    )
    lines = out.split("\n")
    assert lines[0] == "[Time: Sun 9 Aug, 8:02am]"
    assert len(lines) == 2
    assert lines[1].startswith("[Gap since previous message:")
    # ~12h gap → Half a day
    assert "Half a day ago" in lines[1]


def test_build_prefix_cross_day_wall_only_when_gap_under_five():
    """Cross-day but Δ < 5m (clock skew / near midnight) → Time line only."""
    now = datetime(2026, 8, 9, 0, 2)
    prior = datetime(2026, 8, 8, 23, 59).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=False,
    )
    assert out == "[Time: Sun 9 Aug, 12:02am]"
    assert "Gap" not in out
    assert "minutes since" not in out


def test_build_prefix_no_trailing_blank_line():
    now = datetime(2026, 8, 8, 21, 14)
    prior = (now - timedelta(minutes=12)).timestamp()
    out = build_user_time_prefix(
        now_dt=now,
        prior_turn_ts=prior,
        is_first_message=False,
    )
    assert not out.endswith("\n")
    assert not out.startswith("\n")


# ---------------------------------------------------------------------------
# snapshot_prior_chat_ts
# ---------------------------------------------------------------------------


def test_snapshot_prior_none_soul_and_zero_process():
    assert snapshot_prior_chat_ts(None, 0.0) == 0.0
    assert snapshot_prior_chat_ts(None) == 0.0


def test_snapshot_prior_max_of_soul_and_process():
    soul = SimpleNamespace(last_user_voice_at=100.0, last_spoke_at=50.0)
    assert snapshot_prior_chat_ts(soul, 80.0) == 100.0
    assert snapshot_prior_chat_ts(soul, 150.0) == 150.0
    soul2 = SimpleNamespace(last_user_voice_at=10.0, last_spoke_at=200.0)
    assert snapshot_prior_chat_ts(soul2, 0.0) == 200.0


def test_snapshot_prior_missing_attrs_safe():
    assert snapshot_prior_chat_ts(SimpleNamespace(), 42.0) == 42.0
    assert snapshot_prior_chat_ts(object(), 7.5) == 7.5

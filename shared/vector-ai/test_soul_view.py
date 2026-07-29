"""Phase 2 soul llm_view: long keys + It has been X since Y.

Run:
  python3 -m pytest shared/vector-ai/test_soul_view.py -q
"""
from __future__ import annotations

import re

from behaviors.presence import PresenceCache
from soul import Soul
from soul.view import (
    format_duration_units,
    it_has_been_since,
    llm_view,
    time_since_compact,
    was_ago,
)


_IT_HAS_BEEN = re.compile(
    r"^It has been .+ since .+\.$|"
    r"^It has never been recorded this session\.$|"
    r"^I have not .+|"
    r"^The human has not .+|"
    r"^No continuous desk session is active yet\.$|"
    r"^There has been no contact .+"
)


def test_format_duration_units_breakpoints():
    n, u = format_duration_units(30)
    assert n is None
    assert "minute" in u or "less" in u

    n, u = format_duration_units(60)
    # 60s is still < 90 → less than a minute
    assert n is None

    n, u = format_duration_units(120)
    assert n == 2 and u == "minutes"

    n, u = format_duration_units(60)
    assert n is None  # <90s

    n, u = format_duration_units(3600)
    assert n == 1 and u == "hour"

    n, u = format_duration_units(90 * 60)
    assert n == 2 and "hour" in u

    n, u = format_duration_units(2 * 86400)
    assert n == 2 and u == "days"


def test_it_has_been_since_phrases():
    now = 1_000_000.0
    s = it_has_been_since("I last spoke aloud", now, now - 12 * 60)
    assert s == "It has been 12 minutes since I last spoke aloud."

    s = it_has_been_since("I last spoke aloud", now, 0)
    assert "never" in s.lower() or "not spoken" in s.lower()

    s = it_has_been_since(
        "I last spoke aloud",
        now,
        0,
        never_phrase="I have not spoken aloud yet this session.",
    )
    assert s == "I have not spoken aloud yet this session."

    s = it_has_been_since("that camera glance", now, now - 30)
    assert "less than a minute" in s
    assert "since that camera glance" in s

    # singular
    s = it_has_been_since("event", now, now - 60)
    # 60s < 90 → less than a minute
    assert "less than a minute" in s

    s = it_has_been_since("event", now, now - 120)
    assert s == "It has been 2 minutes since event."


def test_time_since_compact_list_form():
    now = 2_000_000.0
    s = time_since_compact("I said this", now, now - 6 * 60)
    assert s == "Time since I said this: 6 min"
    assert "ago" not in s  # not bare ago form alone


def test_was_ago():
    now = 3_000_000.0
    s = was_ago("Last contact with the human", now, now - 2 * 3600)
    assert s == "Last contact with the human was 2 hours ago."


def test_llm_view_long_keys_and_no_raw_unix(tmp_path):
    now = 4_000_000.0
    soul = Soul(presence=PresenceCache(sticky_s=1800, empty_streak_clear=2))
    soul._persist_path = tmp_path / "soul.json"
    # Keep sticky occupancy warm (within 1800s).
    soul.note_person(now - 10 * 60, source="t")
    soul.note_spoke(now - 12 * 60, source="chat", text="hi")
    soul.note_user_voice(now - 2 * 3600)
    soul.set_mood("quietly content after a sociable morning", now - 28 * 60)
    soul.set_goals(["g1", "g2", "g3"], force_save=False)
    soul.set_dreamed_about("a quiet desk rain", force_save=False)
    soul.note_face(now - 10, face_id=1, name="Cam", is_stranger=False, source="t")
    soul.note_eye_frame(
        now - 4 * 60,
        reason="ambient",
        note="mug moved left",
        presence="person",
    )
    soul._notify_recent.append(
        {"t": now - 6 * 60, "source": "chat", "text": "hello there"}
    )

    view = llm_view(soul, now, detail="full")
    assert view["a_person_is_currently_at_the_desk"] is True
    assert "It has been" in view["time_since_i_last_spoke_aloud"]
    assert "I last spoke aloud" in view["time_since_i_last_spoke_aloud"]
    assert view["three_goals_i_currently_hold"] == ["g1", "g2", "g3"]
    assert view["last_night_i_dreamed_about"] == "a quiet desk rain"
    assert view["my_current_inner_mood_in_one_phrase"]
    assert view["person_i_currently_believe_i_am_looking_at"]["name"] == "Cam"
    assert view["what_i_noted_in_my_most_recent_camera_glance"] == "mug moved left"
    assert "recent_things_i_said_aloud" in view
    assert view["recent_things_i_said_aloud"][0]["text_preview"] == "hello there"
    assert "Time since" in view["recent_things_i_said_aloud"][0]["time_since_i_said_this"]

    # No raw unix epochs in time field values.
    time_keys = [k for k in view if k.startswith("time_since_")]
    for k in time_keys:
        v = view[k]
        assert isinstance(v, str), f"{k} should be str, got {type(v)}"
        assert not re.fullmatch(r"\d+(\.\d+)?", v), f"{k} is digit-only: {v}"
        # epoch-like numbers should not appear as the whole value
        assert not re.search(r"\b1[67]\d{8,}\b", v), f"{k} looks like epoch: {v}"

    # lite omits notify ring
    lite = llm_view(soul, now, detail="lite")
    assert "recent_things_i_said_aloud" not in lite
    assert "three_goals_i_currently_hold" in lite
    assert "time_since_i_last_spoke_aloud" in lite


def test_soul_snapshot_is_ops_llm_view(tmp_path):
    now = 5_000_000.0
    soul = Soul(presence=PresenceCache())
    soul._persist_path = tmp_path / "soul.json"
    soul.note_spoke(now - 60, source="t")
    snap = soul.snapshot(now)
    assert "time_since_i_last_spoke_aloud" in snap
    assert "a_person_is_currently_at_the_desk" in snap
    assert "occupied" not in snap  # short keys gone from export
    assert "last_spoke_at" not in snap
    assert "three_goals_i_currently_hold" in snap
    assert "soft_name_hint_from_presence" in snap  # ops detail


def test_no_bare_minutes_ago_without_event():
    now = 6_000_000.0
    s = it_has_been_since("I last spoke aloud", now, now - 180)
    assert "ago" not in s  # canonical is "It has been X since Y"
    assert s.startswith("It has been")

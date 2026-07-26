#!/usr/bin/env python3
"""Tests for desk_items store + desk_extract JSON parse.

Run:
  python3 -m pytest shared/vector-ai/test_memory_desk_items.py -q
"""
from __future__ import annotations

import time

from desk_extract import parse_desk_items_json
from memory import MemoryStore, _normalize_desk_name_key


def test_normalize_name_key():
    assert _normalize_desk_name_key("  Coffee   Mug ") == "coffee mug"
    assert _normalize_desk_name_key("MUG") == "mug"


def test_upsert_and_list(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    store.upsert_desk_item("mug", "left of monitor")
    store.upsert_desk_item("headphones", "right edge")
    items = store.list_desk_items(present_only=True, limit=24)
    assert len(items) == 2
    by_key = {i["name_key"]: i for i in items}
    assert by_key["mug"]["location"] == "left of monitor"
    assert by_key["headphones"]["location"] == "right edge"
    assert by_key["mug"]["missing_since"] is None


def test_unique_name_key_case_insensitive(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    store.upsert_desk_item("Mug", "left of monitor", now=1000.0)
    store.upsert_desk_item("MUG", "on keyboard", now=2000.0)
    items = store.list_desk_items(present_only=False, limit=24)
    assert len(items) == 1
    assert items[0]["name_key"] == "mug"
    assert items[0]["location"] == "on keyboard"
    assert items[0]["last_seen"] == 2000.0


def test_mark_missing(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    store.upsert_desk_item("rubber duck", "on monitor", now=1000.0)
    store.mark_desk_item_missing("Rubber Duck", now=1500.0)
    present = store.list_desk_items(present_only=True)
    assert present == []
    all_items = store.list_desk_items(present_only=False)
    assert len(all_items) == 1
    assert all_items[0]["missing_since"] == 1500.0
    # Re-sighting clears missing.
    store.upsert_desk_item("rubber duck", "desk edge", now=2000.0)
    present = store.list_desk_items(present_only=True)
    assert len(present) == 1
    assert present[0]["missing_since"] is None
    assert present[0]["location"] == "desk edge"


def test_desk_map_prompt_lines_cap(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    assert store.desk_map_prompt_lines() == ""
    t0 = time.time()
    for i in range(20):
        store.upsert_desk_item(f"item{i}", f"spot {i}", now=t0 + i)
    lines = store.desk_map_prompt_lines(limit=12)
    assert lines
    bullets = [ln for ln in lines.splitlines() if ln.startswith("- ")]
    assert len(bullets) == 12
    # Most recently seen first.
    assert "item19" in bullets[0]
    assert ":" in bullets[0]


def test_empty_name_or_location_noop(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    store.upsert_desk_item("", "somewhere")
    store.upsert_desk_item("thing", "")
    store.upsert_desk_item("   ", "x")
    assert store.list_desk_items(present_only=False) == []


def test_parse_desk_items_json_tolerant():
    fenced = '```json\n[{"name": "mug", "location": "left of monitor"}]\n```'
    assert parse_desk_items_json(fenced) == [("mug", "left of monitor")]

    prefixed = 'Here you go:\n[{"name":"headphones","location":"right edge"}]\nThanks'
    assert parse_desk_items_json(prefixed) == [("headphones", "right edge")]

    assert parse_desk_items_json("not json at all") == []
    assert parse_desk_items_json("") == []
    assert parse_desk_items_json(None) == []  # type: ignore[arg-type]
    assert parse_desk_items_json('{"name": "mug"}') == []

    mixed = '[{"name":"ok","location":"here"}, "skip", {"name":1,"location":"x"}, {"name":"a","location":"b"}]'
    out = parse_desk_items_json(mixed)
    assert ("ok", "here") in out
    assert ("a", "b") in out
    assert len(out) == 2

    # Dedup by lower name.
    dup = '[{"name":"Mug","location":"a"},{"name":"mug","location":"b"}]'
    assert parse_desk_items_json(dup) == [("Mug", "a")]

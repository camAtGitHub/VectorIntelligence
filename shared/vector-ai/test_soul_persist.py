"""Phase 1 soul persist: soul.json load/save, goals/dream round-trip.

Run:
  python3 -m pytest shared/vector-ai/test_soul_persist.py -q
"""
from __future__ import annotations

import json
import time

from behaviors.presence import PresenceCache
from soul import Soul
from soul.persist import SOUL_FORMAT, load_into, save_from, serialize_soul


def test_save_load_clocks_goals_dream(tmp_path):
    path = tmp_path / "soul.json"
    now = 1_700_000_000.0
    soul = Soul(presence=PresenceCache())
    soul._persist_path = path
    soul.last_seen_at = now - 100
    soul.last_spoke_at = now - 50
    soul.last_user_voice_at = now - 30
    soul.set_mood("quietly content", now - 20)
    soul.set_goals(
        ["notice the window light", "learn one desk object", "say hi once"],
        force_save=False,
    )
    soul.set_dreamed_about("cables that whispered riddles", force_save=False)
    soul.save_now()

    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["soul_format"] == SOUL_FORMAT
    assert data["last_spoke_at"] == now - 50
    assert len(data["goals"]) == 3
    assert data["last_night_i_dreamed_about"] == "cables that whispered riddles"
    assert "image_b64" not in path.read_text(encoding="utf-8")

    soul2 = Soul(presence=PresenceCache())
    soul2._persist_path = path
    assert soul2.load_from_disk() is True
    assert soul2.last_spoke_at == now - 50
    assert soul2.last_seen_at == now - 100
    assert soul2.last_user_voice_at == now - 30
    assert soul2.goals[0] == "notice the window light"
    assert len(soul2.goals) == 3
    assert soul2.last_night_i_dreamed_about == "cables that whispered riddles"
    assert soul2._mood_text == "quietly content"


def test_goals_always_length_3(tmp_path):
    path = tmp_path / "soul.json"
    soul = Soul(presence=PresenceCache())
    soul._persist_path = path
    soul.set_goals(["only one"], force_save=True)
    assert soul.goals == ["only one", "", ""]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["goals"] == ["only one", "", ""]

    soul.set_goals(["a", "b", "c", "d", "e"], force_save=True)
    assert soul.goals == ["a", "b", "c"]


def test_dream_max_length_cap(tmp_path):
    path = tmp_path / "s.json"
    soul = Soul(presence=PresenceCache())
    soul._persist_path = path
    long = "x" * 900
    out = soul.set_dreamed_about(long, force_save=True)
    assert len(out) == 500
    soul2 = Soul(presence=PresenceCache())
    assert load_into(soul2, path=path) is True
    assert len(soul2.last_night_i_dreamed_about) == 500


def test_corrupt_file_starts_clean(tmp_path):
    path = tmp_path / "soul.json"
    path.write_text("{not json!!", encoding="utf-8")
    soul = Soul(presence=PresenceCache())
    soul._persist_path = path
    assert soul.load_from_disk() is False
    assert soul.last_spoke_at == 0.0
    assert soul.goals == ["", "", ""]


def test_missing_file_returns_false(tmp_path):
    soul = Soul(presence=PresenceCache())
    assert soul.load_from_disk(path=tmp_path / "nope.json") is False


def test_face_buffer_and_notify_round_trip(tmp_path):
    path = tmp_path / "soul.json"
    now = time.time()
    soul = Soul(presence=PresenceCache())
    soul._persist_path = path
    soul.note_face(now, face_id=1, name="Cam", is_stranger=False, source="t")
    # Direct ring append (notify_speech also fires pushover).
    soul._notify_recent.append({"t": now, "source": "chat", "text": "hello desk"})
    soul.save_now()

    blob = path.read_text(encoding="utf-8")
    assert "image_b64" not in blob
    assert "hello desk" in blob

    soul2 = Soul(presence=PresenceCache())
    assert load_into(soul2, path=path) is True
    assert len(soul2.face.buffer_list()) == 1
    assert soul2.face.buffer_list()[0]["name"] == "Cam"
    assert len(list(soul2._notify_recent)) == 1
    assert list(soul2._notify_recent)[0]["text"] == "hello desk"


def test_atomic_file_exists_after_save(tmp_path):
    path = tmp_path / "soul.json"
    soul = Soul(presence=PresenceCache())
    soul.last_spoke_at = 42.0
    save_from(soul, path=path)
    assert path.is_file()
    # No leftover temp files.
    temps = list(tmp_path.glob("soul.*.tmp"))
    assert temps == []


def test_serialize_has_no_image_fields():
    soul = Soul(presence=PresenceCache())
    soul.note_person(1.0, source="t", image_b64="SHOULD_NOT_PERSIST")
    payload = serialize_soul(soul)
    dumped = json.dumps(payload)
    assert "image_b64" not in dumped
    assert "SHOULD_NOT_PERSIST" not in dumped

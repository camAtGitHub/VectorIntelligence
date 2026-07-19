#!/usr/bin/env python3
"""Tests for line-preserving pod.conf upsert + env→pod behavior migrate.

Run:
  python3 -m pytest shared/test_pod_conf_upsert.py -q
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from pod_conf_io import (
    BEHAVIOR_RELOCATE_KEYS,
    main,
    migrate_behavior_env_files,
    migrate_behavior_env_to_pod,
    parse_env_assignments,
    upsert_pod_conf_file,
    upsert_pod_conf_text,
)


def test_upsert_preserves_joke_and_comments() -> None:
    text = (
        "# stack ports\n"
        "\n"
        "WEB_PORT=1\n"
        "# joke idle\n"
        "JOKE_ENABLED=1\n"
        "WORKDAY_ENABLED=1\n"
    )
    out = upsert_pod_conf_text(text, {"WEB_PORT": "9080"})
    assert "WEB_PORT=9080" in out
    assert "JOKE_ENABLED=1" in out
    assert "WORKDAY_ENABLED=1" in out
    assert "# stack ports" in out
    assert "# joke idle" in out
    assert "WEB_PORT=1" not in out


def test_upsert_appends_missing_keys() -> None:
    text = "WEB_PORT=8080\n"
    out = upsert_pod_conf_text(text, {"WEB_PORT": "8080", "AI_PORT": "8090"})
    assert "WEB_PORT=8080" in out
    assert "AI_PORT=8090" in out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert lines[0].startswith("WEB_PORT=")
    assert any(ln.startswith("AI_PORT=") for ln in lines)


def test_upsert_empty_file_creates_only_requested() -> None:
    out = upsert_pod_conf_text("", {"WEB_PORT": "9080", "AI_PORT": "8090"})
    parsed = parse_env_assignments(out)
    assert parsed == {"WEB_PORT": "9080", "AI_PORT": "8090"}


def test_upsert_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "pod.conf"
    upsert_pod_conf_file(p, {"WEB_PORT": "1", "AI_PORT": "2"})
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "WEB_PORT=1" in text
    assert "AI_PORT=2" in text


def test_upsert_empty_values() -> None:
    out = upsert_pod_conf_text("FOO=bar\n", {"FOO": "", "EMPTY": ""})
    assert "FOO=\n" in out or out.strip().splitlines()[0] == "FOO="
    assert "EMPTY=" in out
    parsed = parse_env_assignments(out)
    assert parsed["FOO"] == ""
    assert parsed["EMPTY"] == ""


def test_upsert_strips_bom_does_not_rewire() -> None:
    text = "\ufeffWEB_PORT=1\nJOKE_ENABLED=1\n"
    out = upsert_pod_conf_text(text, {"WEB_PORT": "9080"})
    assert not out.startswith("\ufeff")
    assert "WEB_PORT=9080" in out
    assert "JOKE_ENABLED=1" in out


def test_upsert_never_deletes_foreign_keys() -> None:
    text = "A=1\nB=2\nC=3\n"
    out = upsert_pod_conf_text(text, {"B": "9"})
    parsed = parse_env_assignments(out)
    assert parsed == {"A": "1", "B": "9", "C": "3"}


def test_upsert_preserves_indent_on_replace() -> None:
    text = "  WEB_PORT = 1\n"
    out = upsert_pod_conf_text(text, {"WEB_PORT": "9080"})
    assert out.startswith("  WEB_PORT=9080")


def test_upsert_no_updates_returns_same_without_bom() -> None:
    text = "WEB_PORT=1\n"
    assert upsert_pod_conf_text(text, {}) == text


def test_upsert_dedupes_duplicate_keys_for_updated_key() -> None:
    """First WEB_PORT replaced; later duplicate dropped (loaders are last-wins)."""
    text = "WEB_PORT=1\nJOKE_ENABLED=1\nWEB_PORT=2\n"
    out = upsert_pod_conf_text(text, {"WEB_PORT": "9080"})
    lines = [ln for ln in out.splitlines() if ln.startswith("WEB_PORT=")]
    assert lines == ["WEB_PORT=9080"]
    assert "JOKE_ENABLED=1" in out
    # last-wins parse would have been 2 before upsert; now single 9080
    assert parse_env_assignments(out)["WEB_PORT"] == "9080"


def test_upsert_multipass_preserves_foreign_keys() -> None:
    text = "WEB_PORT=1\nJOKE_ENABLED=1\nWORKDAY_ENABLED=1\n"
    mid = upsert_pod_conf_text(text, {"WEB_PORT": "9080"})
    out = upsert_pod_conf_text(mid, {"AI_PORT": "8090"})
    parsed = parse_env_assignments(out)
    assert parsed["WEB_PORT"] == "9080"
    assert parsed["AI_PORT"] == "8090"
    assert parsed["JOKE_ENABLED"] == "1"
    assert parsed["WORKDAY_ENABLED"] == "1"


def test_upsert_file_oserror_no_wipe(tmp_path: Path) -> None:
    p = tmp_path / "pod.conf"
    p.write_text("JOKE_ENABLED=1\nWEB_PORT=1\n", encoding="utf-8")
    original = p.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("simulated read failure")

    with mock.patch.object(Path, "read_text", boom):
        with pytest.raises(OSError, match="unreadable"):
            upsert_pod_conf_file(p, {"WEB_PORT": "9080"})
    assert p.read_text(encoding="utf-8") == original


def test_migrate_moves_workday_when_absent_from_pod() -> None:
    env = "OPENROUTER_API_KEY=sk-secret\nWORKDAY_ENABLED=1\nLLM_MODEL=x\n"
    pod = "WEB_PORT=8080\nAI_PORT=8090\n"
    new_pod, new_env, migrated = migrate_behavior_env_to_pod(env, pod)
    assert "WORKDAY_ENABLED" in migrated
    assert parse_env_assignments(new_pod).get("WORKDAY_ENABLED") == "1"
    assert "OPENROUTER_API_KEY=sk-secret" in new_env
    assert "LLM_MODEL=x" in new_env
    # Migrated line commented out
    assert "# WORKDAY_ENABLED=1" in new_env
    assert "migrated to pod.conf" in new_env
    # Secret never landed in pod
    assert "OPENROUTER" not in new_pod
    assert "LLM_MODEL" not in new_pod


def test_migrate_pod_wins_no_overwrite_but_comments_env() -> None:
    env = "WORKDAY_ENABLED=1\n"
    pod = "WORKDAY_ENABLED=0\nWEB_PORT=1\n"
    new_pod, new_env, migrated = migrate_behavior_env_to_pod(env, pod)
    assert migrated == []
    assert parse_env_assignments(new_pod).get("WORKDAY_ENABLED") == "0"
    # Active .env line commented — pod owns the knob
    assert "# WORKDAY_ENABLED=1" in new_env
    assert "owned by pod.conf" in new_env
    # No active assignment left
    assert "WORKDAY_ENABLED" not in parse_env_assignments(new_env)


def test_migrate_idempotent() -> None:
    env = "JOKE_ENABLED=1\nBEHAVIORS_ENABLED=workday,joke_idle\n"
    pod = "WEB_PORT=1\n"
    pod1, env1, m1 = migrate_behavior_env_to_pod(env, pod)
    assert set(m1) == {"JOKE_ENABLED", "BEHAVIORS_ENABLED"}
    pod2, env2, m2 = migrate_behavior_env_to_pod(env1, pod1)
    assert m2 == []
    assert parse_env_assignments(pod2) == parse_env_assignments(pod1)
    # Second pass leaves already-commented env alone (no new banner spam)
    assert env2 == env1 or parse_env_assignments(env2) == parse_env_assignments(env1)


def test_migrate_never_copies_llm_keys() -> None:
    env = (
        "OPENROUTER_API_KEY=sk\n"
        "LLM_BASE_URL=https://x\n"
        "LLM_MODEL=m\n"
        "VECTORAI_DEBUG=1\n"
        "WORKDAY_ENABLED=1\n"
    )
    pod = ""
    new_pod, _new_env, migrated = migrate_behavior_env_to_pod(env, pod)
    assert migrated == ["WORKDAY_ENABLED"]
    parsed = parse_env_assignments(new_pod)
    assert list(parsed.keys()) == ["WORKDAY_ENABLED"]


def test_migrate_files_backup(tmp_path: Path) -> None:
    env_p = tmp_path / ".env"
    pod_p = tmp_path / "pod.conf"
    env_p.write_text("WORKDAY_ENABLED=1\nOPENROUTER_API_KEY=sk\n", encoding="utf-8")
    pod_p.write_text("WEB_PORT=8080\n", encoding="utf-8")
    migrated = migrate_behavior_env_files(env_p, pod_p, backup=True)
    assert "WORKDAY_ENABLED" in migrated
    assert parse_env_assignments(pod_p.read_text(encoding="utf-8")).get(
        "WORKDAY_ENABLED"
    ) == "1"
    bak_env = list(tmp_path.glob(".env.bak-migrate-*"))
    bak_pod = list(tmp_path.glob("pod.conf.bak-migrate-*"))
    assert len(bak_env) == 1
    assert len(bak_pod) == 1
    assert "WORKDAY_ENABLED=1" in bak_env[0].read_text(encoding="utf-8")


def test_migrate_files_pod_owned_env_cleanup_only(tmp_path: Path) -> None:
    """Pod already has key; env still active → comment env, do not change pod value."""
    env_p = tmp_path / ".env"
    pod_p = tmp_path / "pod.conf"
    env_p.write_text("WORKDAY_ENABLED=1\n", encoding="utf-8")
    pod_p.write_text("WORKDAY_ENABLED=0\n", encoding="utf-8")
    migrated = migrate_behavior_env_files(env_p, pod_p, backup=True)
    assert migrated == []
    assert parse_env_assignments(pod_p.read_text(encoding="utf-8"))["WORKDAY_ENABLED"] == "0"
    env_txt = env_p.read_text(encoding="utf-8")
    assert "# WORKDAY_ENABLED=1" in env_txt
    assert "WORKDAY_ENABLED" not in parse_env_assignments(env_txt)


def test_behavior_relocate_keys_full_set() -> None:
    expected = frozenset({
        "BEHAVIORS_ENABLED",
        "FACE_CACHE_MAX_AGE_S",
        "IMAGE_CACHE_MAX_AGE_S",
        "SPEECH_MIN_GAP_S",
        "SPEECH_SUPPRESS_AFTER_VOICE_S",
        "WORKDAY_ENABLED",
        "WORKDAY_TZ",
        "WORKDAY_START_BEGIN",
        "WORKDAY_START_END",
        "WORKDAY_AWAY_WINDOW_BEGIN",
        "WORKDAY_END",
        "WORKDAY_POKE_INTERVAL_S",
        "WORKDAY_AWAY_S",
        "WORKDAY_LATE_CHECK_TIMEOUT_S",
        "WORKDAY_REID_AFTER_AWAY_S",
        "WORKDAY_PRIORITY",
        "WORKDAY_IDENTITY_REJECT_COOLDOWN_S",
        "JOKE_ENABLED",
        "JOKE_AUDIENCE",
        "JOKE_PRIORITY",
        "JOKE_MIN_DWELL_S",
        "JOKE_COOLDOWN_S",
        "JOKE_MAX_PER_DAY",
        "JOKE_QUESTION_RATIO",
        "JOKE_IDENTITY_REJECT_COOLDOWN_S",
        "JOKE_TZ",
        "JOKE_REFILL_INTERVAL_S",
        "JOKE_QUEUE_TARGET",
        "JOKE_QUEUE_LOW_WATERMARK",
        "JOKE_MIN_SCORE",
        "JOKE_NOVELTY_MIN",
        "JOKE_GENERATE_MODEL",
        "JOKE_CRITIC_MODEL",
        "JOKE_SEED_FILE",
        "JOKE_CURATED_RATIO",
    })
    assert BEHAVIOR_RELOCATE_KEYS == expected
    assert "OPENROUTER_API_KEY" not in BEHAVIOR_RELOCATE_KEYS
    assert "LLM_MODEL" not in BEHAVIOR_RELOCATE_KEYS


def test_cli_main_upsert_and_help(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "pod.conf"
    p.write_text("JOKE_ENABLED=1\nWEB_PORT=1\n", encoding="utf-8")
    rc = main(["upsert", str(p), "WEB_PORT=9080", "AI_PORT=8090"])
    assert rc == 0
    parsed = parse_env_assignments(p.read_text(encoding="utf-8"))
    assert parsed["WEB_PORT"] == "9080"
    assert parsed["AI_PORT"] == "8090"
    assert parsed["JOKE_ENABLED"] == "1"

    rc_help = main(["--help"])
    assert rc_help == 0
    err = capsys.readouterr().err
    assert "upsert" in err

    rc_bad = main(["upsert", str(p)])
    assert rc_bad == 2

    rc_unknown = main(["nope"])
    assert rc_unknown == 2


def test_cli_main_migrate_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_p = tmp_path / ".env"
    pod_p = tmp_path / "pod.conf"
    env_p.write_text("WORKDAY_ENABLED=1\nOPENROUTER_API_KEY=sk\n", encoding="utf-8")
    pod_p.write_text("WEB_PORT=8080\n", encoding="utf-8")
    rc = main(["migrate-env", str(env_p), str(pod_p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "migrated:" in out
    assert parse_env_assignments(pod_p.read_text(encoding="utf-8"))["WORKDAY_ENABLED"] == "1"

    rc2 = main(["migrate-env", str(env_p), str(pod_p)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "nothing to migrate" in out2

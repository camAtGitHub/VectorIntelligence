#!/usr/bin/env python3
"""Unit tests for supervisor.py pod.conf loading.

Generic KEY=VALUE parser, typed apply for supervisor-owned keys, and
child-env overlay so FSM knobs can live in pod.conf without .env growth.

Run:
  python3 -m pytest shared/test_supervisor_pod_conf.py -q
  # or from shared/:  python3 -m pytest test_supervisor_pod_conf.py -q
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from supervisor import (
    apply_supervisor_pod_conf,
    load_pod_conf,
    merge_pod_conf_into_env,
)


def _write(text: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".conf", text=True)
    os.close(fd)
    path = Path(name)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def sample_conf(tmp_path: Path) -> dict[str, str]:
    p = tmp_path / "pod.conf"
    p.write_text(
        "# comment\n"
        "\n"
        "WEB_PORT=9080\n"
        "AI_PORT = 8091\n"
        "EXTERNAL_CHIPPER=1\n"
        "WIREPOD_DIR=C:\\\\Program Files\\\\wire-pod\n"
        "JOKE_ENABLED=1\n"
        "EMPTY=\n"
        "NOEQUALS\n"
        "  # not a key\n",
        encoding="utf-8",
    )
    return load_pod_conf(p)


def test_load_parses_ports_and_flags(sample_conf: dict[str, str]) -> None:
    assert sample_conf.get("WEB_PORT") == "9080"
    assert sample_conf.get("AI_PORT") == "8091"
    assert sample_conf.get("EXTERNAL_CHIPPER") == "1"


def test_load_keeps_paths_and_unknown_fsm_keys(sample_conf: dict[str, str]) -> None:
    assert "wire-pod" in sample_conf.get("WIREPOD_DIR", "")
    assert sample_conf.get("JOKE_ENABLED") == "1"


def test_load_empty_values_and_skips(sample_conf: dict[str, str]) -> None:
    assert "EMPTY" in sample_conf and sample_conf["EMPTY"] == ""
    assert "NOEQUALS" not in sample_conf
    assert len(sample_conf) == 6


def test_load_missing_file() -> None:
    assert load_pod_conf(Path("/nonexistent/pod.conf.nope")) == {}


def test_load_strips_utf8_bom(tmp_path: Path) -> None:
    p = tmp_path / "bom.conf"
    p.write_text("\ufeffWEB_PORT=7777\n", encoding="utf-8")
    assert load_pod_conf(p).get("WEB_PORT") == "7777"


def test_apply_typed_supervisor_keys() -> None:
    applied = apply_supervisor_pod_conf({
        "WEB_PORT": "9090",
        "AI_PORT": "not-a-port",
        "VOLUME_DROP": "3",
        "VOLUME_HANG_MS": "3000",
        "USE_LOCAL_OLLAMA": "yes",
        "EXTERNAL_CHIPPER": "true",
        "WIREPOD_DIR": "/opt/wire-pod",
        "JOKE_ENABLED": "1",
    })
    assert applied["WEB_PORT"] == "9090"
    assert applied["AI_PORT"] != "not-a-port"
    assert applied["VOLUME_DROP"] == 3 and isinstance(applied["VOLUME_DROP"], int)
    assert applied["VOLUME_HANG_MS"] == 3000
    assert applied["USE_LOCAL_OLLAMA"] is True
    assert applied["EXTERNAL_CHIPPER"] is True
    assert applied["WIREPOD_DIR"] == "/opt/wire-pod"


def test_apply_vector_volume_aliases_win() -> None:
    applied = apply_supervisor_pod_conf({
        "VOLUME_DROP": "1",
        "VECTOR_VOLUME_DROP": "4",
        "VOLUME_HANG_MS": "1000",
        "VECTOR_VOLUME_HANG_MS": "5000",
        "VECTOR_VOLUME_MS_PER_WORD": "350",
    })
    assert applied["VOLUME_DROP"] == 4
    assert applied["VOLUME_HANG_MS"] == 5000
    assert applied["VECTOR_VOLUME_MS_PER_WORD"] == 350


def test_apply_falsey_bools_and_bad_int() -> None:
    applied = apply_supervisor_pod_conf({
        "USE_LOCAL_OLLAMA": "no",
        "EXTERNAL_CHIPPER": "0",
    })
    assert applied["USE_LOCAL_OLLAMA"] is False
    assert applied["EXTERNAL_CHIPPER"] is False

    applied2 = apply_supervisor_pod_conf({"VOLUME_DROP": "nope"})
    assert isinstance(applied2["VOLUME_DROP"], int)


def test_merge_pod_conf_wins_and_preserves_base() -> None:
    base = {"PATH": "/bin", "JOKE_ENABLED": "0", "KEEP": "yes"}
    merged = merge_pod_conf_into_env(
        {"JOKE_ENABLED": "1", "WORKDAY_ENABLED": "1", "AI_PORT": "8090"},
        base=base,
        conf_wins=True,
    )
    assert merged["JOKE_ENABLED"] == "1"
    assert merged["WORKDAY_ENABLED"] == "1"
    assert merged["KEEP"] == "yes" and merged["PATH"] == "/bin"


def test_merge_conf_wins_false_keeps_nonempty_base() -> None:
    merged = merge_pod_conf_into_env(
        {"JOKE_ENABLED": "1"},
        base={"JOKE_ENABLED": "0"},
        conf_wins=False,
    )
    assert merged["JOKE_ENABLED"] == "0"


def test_merged_env_feeds_behavior_loaders() -> None:
    """pod.conf overlay is enough for load_*_config without .env."""
    from behaviors.config import load_joke_config, load_workday_config, load_runtime_config

    env = merge_pod_conf_into_env(
        {
            "WORKDAY_ENABLED": "1",
            "WORKDAY_TZ": "UTC",
            "JOKE_ENABLED": "1",
            "BEHAVIORS_ENABLED": "workday,joke_idle",
        },
        base={},
    )
    assert load_workday_config(env).enabled is True
    assert load_joke_config(env).enabled is True
    assert "joke_idle" in load_runtime_config(env).behaviors_enabled

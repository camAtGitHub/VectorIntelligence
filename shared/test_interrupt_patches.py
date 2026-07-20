"""Exit-code and safety tests for interrupt-related install patches.

Run from VectorIntelligence root:
  python3 -m pytest shared/test_interrupt_patches.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent / "patches"
# Monorepo wire-pod next to VectorIntelligence when present.
MONOREPO_WP = Path(__file__).resolve().parents[2] / "wire-pod"
INTERRUPT_REL = Path("chipper/pkg/wirepod/ttr/kgsim_interrupt.go")


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCHES / script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _load_patch_module(script: str):
    path = PATCHES / script
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_usage_exit_2():
    for script in (
        "wake-word-grace-period.py",
        "add-button-interrupt.py",
        "wake-word-mute-during-getimage.py",
        "add-ondemand-face.py",
    ):
        r = _run(script)
        assert r.returncode == 2, script


def test_missing_path_exit_1(tmp_path: Path):
    missing = tmp_path / "nope.go"
    for script in (
        "wake-word-grace-period.py",
        "add-button-interrupt.py",
        "add-ondemand-face.py",
    ):
        r = _run(script, str(missing))
        assert r.returncode == 1, script


def test_sentinel_skip_exit_0(tmp_path: Path):
    grace = tmp_path / "kgsim_interrupt.go"
    grace.write_text(
        "// wakeWordGrace\n// source: back button\n// WakeWordMutedUntil\n",
        encoding="utf-8",
    )
    assert _run("wake-word-grace-period.py", str(grace)).returncode == 0
    assert _run("add-button-interrupt.py", str(grace)).returncode == 0

    root = tmp_path / "wp"
    cmds = root / "chipper/pkg/wirepod/ttr/kgsim_cmds.go"
    intr = root / "chipper/pkg/wirepod/ttr/kgsim_interrupt.go"
    cmds.parent.mkdir(parents=True)
    cmds.write_text("// WakeWordMutedUntil\n", encoding="utf-8")
    intr.write_text("// WakeWordMutedUntil\n", encoding="utf-8")
    assert _run("wake-word-mute-during-getimage.py", str(root)).returncode == 0


def test_session_without_sentinel_exit_0(tmp_path: Path):
    """Robotsession markers without sentinel must not Fail install."""
    p = tmp_path / "kgsim_interrupt.go"
    p.write_text(
        'package ttr\n// uses robotsession.Session and SubscribeState\n',
        encoding="utf-8",
    )
    r = _run("wake-word-grace-period.py", str(p))
    assert r.returncode == 0
    assert "WARN" in r.stdout or "robotsession" in r.stdout.lower()

    r = _run("add-button-interrupt.py", str(p))
    assert r.returncode == 0


def test_unknown_stock_without_anchor_exit_1(tmp_path: Path):
    p = tmp_path / "kgsim_interrupt.go"
    p.write_text(
        "package ttr\n// stock-like file without session markers or anchors\n",
        encoding="utf-8",
    )
    assert _run("wake-word-grace-period.py", str(p)).returncode == 1
    assert _run("add-button-interrupt.py", str(p)).returncode == 1


def test_legacy_grace_inject(tmp_path: Path):
    # Use the patch module's exact OLD_BLOCK so whitespace matches.
    grace = _load_patch_module("wake-word-grace-period.py")
    p = tmp_path / "kgsim_interrupt.go"
    p.write_text("package ttr\n" + grace.OLD_BLOCK + "\n", encoding="utf-8")
    r = _run("wake-word-grace-period.py", str(p))
    assert r.returncode == 0, r.stderr + r.stdout
    out = p.read_text(encoding="utf-8")
    assert "wakeWordGrace" in out
    # Idempotent second run.
    assert _run("wake-word-grace-period.py", str(p)).returncode == 0


def test_ondemand_face_noop_no_write(tmp_path: Path):
    ttr = tmp_path / "ttr"
    ttr.mkdir()
    interrupt = ttr / "kgsim_interrupt.go"
    before = 'package ttr\n// robotsession SubscribeState\n'
    interrupt.write_text(before, encoding="utf-8")
    (ttr / "face_probe.go").write_text(
        "package ttr\nfunc ObserveFaceBriefly(esn string) {}\n",
        encoding="utf-8",
    )
    r = _run("add-ondemand-face.py", str(interrupt))
    assert r.returncode == 0
    assert "superseded" in r.stdout or "ProbeFace" in r.stdout or "face_probe" in r.stdout
    after = interrupt.read_text(encoding="utf-8")
    assert after == before
    assert "robot_observed_face" not in after


def test_ondemand_face_session_no_face_inject(tmp_path: Path):
    p = tmp_path / "kgsim_interrupt.go"
    before = "package ttr\n// robotsession only, no face_probe sibling\n"
    p.write_text(before, encoding="utf-8")
    r = _run("add-ondemand-face.py", str(p))
    assert r.returncode == 0
    assert p.read_text(encoding="utf-8") == before
    assert "robot_observed_face" not in p.read_text(encoding="utf-8")


@pytest.mark.skipif(
    not (MONOREPO_WP / INTERRUPT_REL).is_file(),
    reason="monorepo wire-pod tree not adjacent",
)
def test_monorepo_tree_all_four_exit_0():
    intr = MONOREPO_WP / INTERRUPT_REL
    assert _run("wake-word-grace-period.py", str(intr)).returncode == 0
    assert _run("add-button-interrupt.py", str(intr)).returncode == 0
    assert _run("wake-word-mute-during-getimage.py", str(MONOREPO_WP)).returncode == 0
    assert _run("add-ondemand-face.py", str(intr)).returncode == 0
    # Ensure ondemand-face did not inject firehose into monorepo interrupt.
    text = intr.read_text(encoding="utf-8")
    assert "robot_observed_face" not in text

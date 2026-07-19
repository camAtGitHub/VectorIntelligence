#!/usr/bin/env python3
"""Unit tests for supervisor.py pod.conf loading.

Covers the generic KEY=VALUE parser, typed apply for supervisor-owned keys,
and child-env overlay so FSM knobs can live in pod.conf without .env growth.
Run:  python test_supervisor_pod_conf.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from supervisor import (  # noqa: E402
    apply_supervisor_pod_conf,
    load_pod_conf,
    merge_pod_conf_into_env,
)

passed = 0


def check(name: str, cond: bool) -> None:
    global passed
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")
    passed += 1


def _write(text: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".conf", text=True)
    Path(name).write_text(text, encoding="utf-8")
    return Path(name)


# -- load_pod_conf -------------------------------------------------------------

p = _write(
    "# comment\n"
    "\n"
    "WEB_PORT=9080\n"
    "AI_PORT = 8091\n"
    "EXTERNAL_CHIPPER=1\n"
    "WIREPOD_DIR=C:\\\\Program Files\\\\wire-pod\n"
    "JOKE_ENABLED=1\n"
    "EMPTY=\n"
    "NOEQUALS\n"
    "  # not a key\n"
)
conf = load_pod_conf(p)
p.unlink(missing_ok=True)

check("parses WEB_PORT", conf.get("WEB_PORT") == "9080")
check("strips spaces around key/value", conf.get("AI_PORT") == "8091")
check("parses bool-ish as string", conf.get("EXTERNAL_CHIPPER") == "1")
check("keeps path values", "wire-pod" in conf.get("WIREPOD_DIR", ""))
check("keeps unknown FSM keys (JOKE_ENABLED)", conf.get("JOKE_ENABLED") == "1")
check("keeps empty values", "EMPTY" in conf and conf["EMPTY"] == "")
check("skips lines without =", "NOEQUALS" not in conf)
check("skips comments and blanks", len(conf) == 6)

missing = load_pod_conf(Path("/nonexistent/pod.conf.nope"))
check("missing file → {}", missing == {})

bom = _write("\ufeffWEB_PORT=7777\n")
check("strips UTF-8 BOM", load_pod_conf(bom).get("WEB_PORT") == "7777")
bom.unlink(missing_ok=True)

# -- apply_supervisor_pod_conf -------------------------------------------------

applied = apply_supervisor_pod_conf({
    "WEB_PORT": "9090",
    "AI_PORT": "not-a-port",
    "VOLUME_DROP": "3",
    "VOLUME_HANG_MS": "3000",
    "USE_LOCAL_OLLAMA": "yes",
    "EXTERNAL_CHIPPER": "true",
    "WIREPOD_DIR": "/opt/wire-pod",
    "JOKE_ENABLED": "1",  # unknown to apply — must not crash
})
check("WEB_PORT digit accepted", applied["WEB_PORT"] == "9090")
check("AI_PORT non-digit kept default-ish (not applied as junk)",
      applied["AI_PORT"] != "not-a-port")
check("VOLUME_DROP is int", applied["VOLUME_DROP"] == 3 and isinstance(applied["VOLUME_DROP"], int))
check("VOLUME_HANG_MS is int", applied["VOLUME_HANG_MS"] == 3000)
check("USE_LOCAL_OLLAMA truthy", applied["USE_LOCAL_OLLAMA"] is True)
check("EXTERNAL_CHIPPER truthy", applied["EXTERNAL_CHIPPER"] is True)
check("WIREPOD_DIR string", applied["WIREPOD_DIR"] == "/opt/wire-pod")

# VECTOR_VOLUME_* preferred over short aliases
applied2 = apply_supervisor_pod_conf({
    "VOLUME_DROP": "1",
    "VECTOR_VOLUME_DROP": "4",
    "VOLUME_HANG_MS": "1000",
    "VECTOR_VOLUME_HANG_MS": "5000",
    "VECTOR_VOLUME_MS_PER_WORD": "350",
})
check("VECTOR_VOLUME_DROP wins over VOLUME_DROP", applied2["VOLUME_DROP"] == 4)
check("VECTOR_VOLUME_HANG_MS wins over VOLUME_HANG_MS", applied2["VOLUME_HANG_MS"] == 5000)
check("VECTOR_VOLUME_MS_PER_WORD applied", applied2["VECTOR_VOLUME_MS_PER_WORD"] == 350)

applied3 = apply_supervisor_pod_conf({"USE_LOCAL_OLLAMA": "no", "EXTERNAL_CHIPPER": "0"})
check("falsey bools",
      applied3["USE_LOCAL_OLLAMA"] is False and applied3["EXTERNAL_CHIPPER"] is False)

applied4 = apply_supervisor_pod_conf({"VOLUME_DROP": "nope"})
check("bad int falls back to default type int", isinstance(applied4["VOLUME_DROP"], int))

# -- merge_pod_conf_into_env ---------------------------------------------------

base = {"PATH": "/bin", "JOKE_ENABLED": "0", "KEEP": "yes"}
merged = merge_pod_conf_into_env(
    {"JOKE_ENABLED": "1", "WORKDAY_ENABLED": "1", "AI_PORT": "8090"},
    base=base,
    conf_wins=True,
)
check("pod.conf overlays existing key", merged["JOKE_ENABLED"] == "1")
check("pod.conf adds new FSM key", merged["WORKDAY_ENABLED"] == "1")
check("base keys preserved", merged["KEEP"] == "yes" and merged["PATH"] == "/bin")

merged2 = merge_pod_conf_into_env(
    {"JOKE_ENABLED": "1"},
    base={"JOKE_ENABLED": "0"},
    conf_wins=False,
)
check("conf_wins=False keeps non-empty base", merged2["JOKE_ENABLED"] == "0")

print(f"\n{passed} checks passed")

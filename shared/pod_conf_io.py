#!/usr/bin/env python3
"""Safe line-preserving pod.conf read/write helpers.

Install/setup scripts must never truncate pod.conf to a fixed key list —
users keep WORKDAY_*/JOKE_*/etc. hand-edits across reinstall.

CLI:
  python3 pod_conf_io.py upsert PATH KEY=val [KEY=val ...]
  python3 pod_conf_io.py migrate-env ENV_PATH POD_CONF_PATH
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

# KEY=value line: optional indent, identifier key, optional spaces around =.
_KEY_LINE_RE = re.compile(
    r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)

# Behavior / runtime knobs that belong in pod.conf (not OpenRouter secrets).
BEHAVIOR_RELOCATE_KEYS: frozenset[str] = frozenset({
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

# Never copy these from .env into pod.conf.
_NEVER_MIGRATE_PREFIXES: tuple[str, ...] = (
    "OPENROUTER_",
    "LLM_",
    "VECTORAI_DEBUG",
)


def _is_never_migrate(key: str) -> bool:
    k = key.strip()
    if k in ("OPENROUTER_API_KEY", "LLM_API_KEY", "LLM_HTTP_REFERER", "LLM_APP_TITLE"):
        return True
    for p in _NEVER_MIGRATE_PREFIXES:
        if k.startswith(p) or k == p.rstrip("_"):
            return True
    return False


def upsert_pod_conf_text(text: str, updates: Mapping[str, str]) -> str:
    """Return conf text with updates applied; preserve comments/blanks/foreign keys.

    - Strip a leading UTF-8 BOM if present (do not re-emit it).
    - Replace the first KEY= line for each key in *updates* (in place).
    - Drop subsequent active lines for keys in *updates* (dedupe so loaders
      that are last-wins cannot keep a stale trailing value after install).
    - Append keys not yet present, in *updates* insertion order.
    - Never delete keys that are not in *updates*.
    - Empty values are allowed (KEY=).
    """
    if not updates:
        if text.startswith("\ufeff"):
            return text[1:]
        return text

    raw = text
    if raw.startswith("\ufeff"):
        raw = raw[1:]

    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()

    remaining: dict[str, str] = {str(k): str(v) for k, v in updates.items()}
    update_keys = frozenset(remaining)
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        m = _KEY_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        indent, key, _old = m.group(1), m.group(2), m.group(3)
        if key in remaining:
            # First occurrence of an updated key → replace value.
            out.append(f"{indent}{key}={remaining.pop(key)}")
        elif key in update_keys:
            # Later duplicate of a key we already rewrote → drop.
            continue
        else:
            out.append(line)

    for key in updates:
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")

    if not out:
        return ""

    # Always terminate non-empty conf with a newline (POSIX / install tooling).
    body = newline.join(out)
    if not body.endswith(newline):
        body += newline
    return body


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* via temp file + os.replace (same-directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def upsert_pod_conf_file(path: Path | str, updates: Mapping[str, str]) -> None:
    """Upsert keys into a pod.conf file; create parent dirs and file if needed.

    If the path exists but cannot be read, abort without writing (never wipe).
    """
    p = Path(path)
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise OSError(
                f"refusing to upsert {p}: existing file is unreadable ({e})"
            ) from e
    else:
        text = ""
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
    new_text = upsert_pod_conf_text(text, updates)
    _atomic_write_text(p, new_text)


def parse_env_assignments(text: str) -> dict[str, str]:
    """Parse active (non-comment) KEY=VALUE lines from .env or pod.conf text."""
    conf: dict[str, str] = {}
    raw = text
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(2), m.group(3).strip()
        conf[key] = val
    return conf


def migrate_behavior_env_to_pod(
    env_text: str,
    pod_text: str,
    *,
    relocate_keys: Optional[Iterable[str]] = None,
    banner_date: Optional[str] = None,
) -> tuple[str, str, list[str]]:
    """Move behavior knobs from .env text into pod.conf text.

    Rules:
    - Only keys in *relocate_keys* (default BEHAVIOR_RELOCATE_KEYS).
    - Never migrate OpenRouter/LLM/debug keys.
    - If key already present in pod.conf, leave pod value alone (pod wins)
      but still comment the .env line (pod owns the knob).
    - Keys newly written to pod are commented out in .env with a banner.
    - Returns (new_pod_text, new_env_text, list of keys newly upserted into pod).

    Idempotent: second run with same inputs migrates nothing more and leaves
    already-commented env lines alone.
    """
    keys = frozenset(relocate_keys) if relocate_keys is not None else BEHAVIOR_RELOCATE_KEYS
    env_map = parse_env_assignments(env_text)
    pod_map = parse_env_assignments(pod_text)

    to_upsert: dict[str, str] = {}
    migrated: list[str] = []
    # Keys to comment out in .env: newly migrated + already present in pod.
    to_comment: set[str] = set()
    for key in sorted(keys):
        if key not in env_map:
            continue
        if _is_never_migrate(key):
            continue
        if key in pod_map:
            to_comment.add(key)
            continue
        to_upsert[key] = env_map[key]
        migrated.append(key)
        to_comment.add(key)

    new_pod = upsert_pod_conf_text(pod_text, to_upsert) if to_upsert else (
        pod_text[1:] if pod_text.startswith("\ufeff") else pod_text
    )

    if not to_comment:
        return new_pod, env_text, migrated

    date_s = banner_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if migrated:
        banner = f"# migrated to pod.conf {date_s}"
    else:
        banner = f"# owned by pod.conf (not overwritten) {date_s}"

    env_raw = env_text
    if env_raw.startswith("\ufeff"):
        env_raw = env_raw[1:]
    newline = "\r\n" if "\r\n" in env_raw else "\n"
    ended_with_nl = env_raw.endswith("\n") or env_raw.endswith("\r\n") or env_raw == ""
    out_env: list[str] = []
    banner_inserted = False
    for line in env_raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out_env.append(line)
            continue
        m = _KEY_LINE_RE.match(line)
        if m and m.group(2) in to_comment:
            if not banner_inserted:
                out_env.append(banner)
                banner_inserted = True
            out_env.append(f"# {stripped}")
        else:
            out_env.append(line)

    body = newline.join(out_env)
    if ended_with_nl or out_env:
        if body and not body.endswith(newline):
            body += newline
    return new_pod, body, migrated


def migrate_behavior_env_files(
    env_path: Path | str,
    pod_path: Path | str,
    *,
    backup: bool = True,
) -> list[str]:
    """File-level migrate with optional timestamped backups. Returns newly upserted keys.

    Also rewrites .env when only commenting pod-owned relocate keys (no upsert).
    Writes use temp files + os.replace.
    """
    env_p = Path(env_path)
    pod_p = Path(pod_path)
    if not env_p.is_file():
        return []

    env_text = env_p.read_text(encoding="utf-8")
    pod_text = pod_p.read_text(encoding="utf-8") if pod_p.is_file() else ""

    new_pod, new_env, migrated = migrate_behavior_env_to_pod(env_text, pod_text)
    env_changed = new_env != env_text
    # Normalize BOM-only pod change comparison.
    pod_norm = pod_text[1:] if pod_text.startswith("\ufeff") else pod_text
    pod_changed = new_pod != pod_norm
    if not env_changed and not pod_changed:
        return []

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if backup:
        if pod_p.is_file() and pod_changed:
            bak = pod_p.with_name(f"{pod_p.name}.bak-migrate-{stamp}")
            bak.write_text(pod_text, encoding="utf-8")
        if env_changed:
            env_bak = env_p.with_name(f"{env_p.name}.bak-migrate-{stamp}")
            env_bak.write_text(env_text, encoding="utf-8")

    if pod_changed:
        _atomic_write_text(pod_p, new_pod)
    if env_changed:
        _atomic_write_text(env_p, new_env)
    return migrated


def _parse_cli_pairs(pairs: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"expected KEY=value, got {item!r}")
        key, _, val = item.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"empty key in {item!r}")
        out[key] = val
    return out


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        return 0 if args and args[0] in ("-h", "--help") else 2

    cmd = args[0]
    if cmd == "upsert":
        if len(args) < 3:
            print("usage: pod_conf_io.py upsert PATH KEY=val [KEY=val ...]", file=sys.stderr)
            return 2
        path = args[1]
        updates = _parse_cli_pairs(args[2:])
        upsert_pod_conf_file(path, updates)
        return 0

    if cmd == "migrate-env":
        if len(args) < 3:
            print(
                "usage: pod_conf_io.py migrate-env ENV_PATH POD_CONF_PATH",
                file=sys.stderr,
            )
            return 2
        migrated = migrate_behavior_env_files(args[1], args[2], backup=True)
        if migrated:
            print("migrated:", ", ".join(migrated))
        else:
            print("nothing to migrate")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

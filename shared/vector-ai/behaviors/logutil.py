"""Lightweight behavior debug logs for vector-ai.

High-signal events always print (show up in vector-ai.log / journalctl).
Routine per-tick skip reasons only print when VECTORAI_DEBUG / DEBUG /
LOG_LEVEL=debug is on — otherwise presence ticks would flood the log.

Usage:
    from .logutil import blog
    blog("joke_idle", "spoke kind=joke: %r" % text)
    blog("joke_idle", "skip: cooldown", verbose=True)
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

_DEBUG_RAW = (
    os.getenv("VECTORAI_DEBUG")
    or os.getenv("DEBUG")
    or os.getenv("LOG_LEVEL")
    or ""
).strip().lower()
_VERBOSE = _DEBUG_RAW in ("1", "true", "yes", "on", "debug")


def behaviors_verbose() -> bool:
    """True when VECTORAI_DEBUG-style flags request per-tick detail."""
    return _VERBOSE


def blog(tag: str, msg: str, *, verbose: bool = False, data: Any = None) -> None:
    """Print a timestamped `[tag] msg` line.

    Args:
        tag: short subsystem id, e.g. "joke_idle", "joke_sources", "workday", "runtime"
        msg: human-readable event
        verbose: if True, only emit when behaviors_verbose() is on
        data: optional extra payload (repr'd, truncated)
    """
    if verbose and not _VERBOSE:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{tag}] {msg}"
    if data is not None:
        try:
            extra = repr(data)
        except Exception:
            extra = f"<{type(data).__name__}>"
        if len(extra) > 400:
            extra = extra[:400] + "…"
        line = f"{line} | {extra}"
    print(line, flush=True)


def short(text: Optional[str], n: int = 80) -> str:
    """Truncate speech for log lines."""
    s = (text or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"

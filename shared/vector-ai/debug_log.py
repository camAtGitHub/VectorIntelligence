"""Debug flag parsing, payload redaction, and debug() logger for vector-ai."""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from paths import ROOT
from logging_util import print  # noqa: F401 - use timestamped print

# Debug: VECTORAI_DEBUG=1 / DEBUG=1 / LOG_LEVEL=debug
# Logs request/response payloads (images redacted) to stdout and vector-ai-debug.log
_DEBUG_RAW = (
    os.getenv("VECTORAI_DEBUG")
    or os.getenv("DEBUG")
    or os.getenv("LOG_LEVEL")
    or ""
).strip().lower()
DEBUG = _DEBUG_RAW in ("1", "true", "yes", "on", "debug")
try:
    DEBUG_MAX_CHARS = max(200, int(os.getenv("VECTORAI_DEBUG_MAX_CHARS", "4000")))
except ValueError:
    DEBUG_MAX_CHARS = 4000
_DEBUG_LOG_PATH = ROOT / "vector-ai-debug.log"
_DEBUG_LOG_MAX = 10 * 1024 * 1024  # rotate debug log at 10 MB


def _redact_content(content: Any) -> Any:
    """Strip huge base64 image payloads from debug dumps."""
    if isinstance(content, str):
        if content.startswith("data:image") or len(content) > 500 and (
            ";base64," in content[:80] or content[:20].count("/") > 0 and len(content) > 2000
        ):
            if "base64," in content[:200] or content.startswith("data:image"):
                return f"<image data omitted len={len(content)}>"
        if len(content) > DEBUG_MAX_CHARS:
            return content[:DEBUG_MAX_CHARS] + f"... <truncated total={len(content)}>"
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                p = dict(part)
                if p.get("type") == "image_url":
                    url = ""
                    iu = p.get("image_url")
                    if isinstance(iu, dict):
                        url = str(iu.get("url") or "")
                        p["image_url"] = {
                            "url": f"<image data omitted len={len(url)}>"
                        }
                    else:
                        p["image_url"] = f"<image omitted type={type(iu).__name__}>"
                elif "text" in p and isinstance(p["text"], str):
                    p["text"] = _redact_content(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content


def _redact_messages(messages: list) -> list:
    out = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "?")
            content = _redact_content(m.get("content"))
            out.append({"role": role, "content": content})
        else:
            # pydantic Message or similar
            role = getattr(m, "role", "?")
            content = _redact_content(getattr(m, "content", None))
            out.append({"role": role, "content": content})
    return out


def _redact_body(body: dict) -> dict:
    b = dict(body)
    if "messages" in b:
        b["messages"] = _redact_messages(b["messages"])
    return b


def _debug_rotate() -> None:
    try:
        if _DEBUG_LOG_PATH.exists() and _DEBUG_LOG_PATH.stat().st_size > _DEBUG_LOG_MAX:
            old = _DEBUG_LOG_PATH.with_suffix(".log.old")
            old.unlink(missing_ok=True)
            _DEBUG_LOG_PATH.rename(old)
    except OSError:
        pass


def debug(msg: str, data: Any = None) -> None:
    """Write a debug line to stdout and vector-ai-debug.log when DEBUG is on.

    Must never raise: callers (llm_chat_once, sensor_reaction, …) invoke this
    on the hot path after a successful upstream reply. A Windows charmap
    failure here used to surface as sensor_reaction error and drop the line.
    """
    if not DEBUG:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [debug] {msg}"
        if data is not None:
            try:
                payload = json.dumps(data, ensure_ascii=False, default=str, indent=2)
            except Exception:
                payload = repr(data)
            if len(payload) > DEBUG_MAX_CHARS * 4:
                payload = payload[: DEBUG_MAX_CHARS * 4] + f"\n... <truncated>"
            line = f"{line}\n{payload}"
        print(line)
        try:
            _debug_rotate()
            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    except Exception:
        # Last-resort: debug must never abort a request handler.
        pass

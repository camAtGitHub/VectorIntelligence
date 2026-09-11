"""Debug flag parsing, payload redaction, and debug() logger for vector-ai."""
import json
import os
from typing import Any

from logging_util import print  # noqa: F401 - use timestamped print

# Debug: VECTORAI_DEBUG=1 / DEBUG=1 / LOG_LEVEL=debug
# Logs request/response payloads (images redacted) to stdout. Supervisor
# captures that into vector-ai.log — there is no second debug file.
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


def debug(msg: str, data: Any = None) -> None:
    """Write a debug line to stdout when DEBUG is on.

    Supervisor captures stdout into vector-ai.log. There is no second
    debug file.

    Must never raise: callers (llm_chat_once, sensor_reaction, …) invoke this
    on the hot path after a successful upstream reply. A Windows charmap
    failure here used to surface as sensor_reaction error and drop the line.
    """
    if not DEBUG:
        return
    try:
        # logging_util.print adds timestamp + [file:function:line]; skip
        # debug() itself so the line points at the real call site.
        line = f"[debug] {msg}"
        if data is not None:
            try:
                payload = json.dumps(data, ensure_ascii=False, default=str, indent=2)
            except Exception:
                payload = repr(data)
            if len(payload) > DEBUG_MAX_CHARS * 4:
                payload = payload[: DEBUG_MAX_CHARS * 4] + f"\n... <truncated>"
            line = f"{line}\n{payload}"
        print(line)
    except Exception:
        # Last-resort: debug must never abort a request handler.
        pass

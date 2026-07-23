"""Timestamped print wrapper and stdlib logging formatters for vector-ai."""
import logging
import sys
from datetime import datetime

# Force UTF-8 on stdio. On Windows the default is often cp1252/charmap; when the
# LLM returns non-ASCII (Hangul, arrows, …) a plain print() raises
# UnicodeEncodeError and can abort an otherwise successful request path
# (e.g. /v1/sensor_reaction after a good OpenRouter reply).
def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            try:
                stream.reconfigure(line_buffering=True)
            except Exception:
                pass


_configure_stdio()

_orig_print = print
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _safe_write(dest, text: str, *, end: str = "\n") -> None:
    """Write to a text stream without ever raising UnicodeEncodeError."""
    try:
        _orig_print(text, end=end, file=dest, flush=True)
    except UnicodeEncodeError:
        enc = getattr(dest, "encoding", None) or "utf-8"
        safe = text.encode(enc, errors="backslashreplace").decode(enc, errors="replace")
        _orig_print(safe, end=end, file=dest, flush=True)


def print(*args, sep=" ", end="\n", file=None, flush=True):  # noqa: A001 - intentional wrap
    """Prefix lines with YYYY-MM-DD HH:MM:SS for log files.

    Never raises UnicodeEncodeError: non-encodable chars are replaced or
    backslash-escaped so request handlers can keep returning LLM text.
    """
    dest = file if file is not None else sys.stdout
    # Leave non-stdio prints alone (rare).
    if dest not in (sys.stdout, sys.stderr, None):
        try:
            _orig_print(*args, sep=sep, end=end, file=file, flush=flush)
        except UnicodeEncodeError:
            msg = sep.join(str(a) for a in args)
            enc = getattr(dest, "encoding", None) or "utf-8"
            safe = msg.encode(enc, errors="backslashreplace").decode(enc, errors="replace")
            _orig_print(safe, end=end, file=file, flush=flush)
        return
    ts = datetime.now().strftime(_LOG_DATEFMT)
    if not args:
        _safe_write(dest, "", end=end)
        return
    msg = sep.join(str(a) for a in args)
    # Avoid double-prefix if a caller already stamped the line.
    if len(msg) >= 19 and msg[4:5] == "-" and msg[7:8] == "-" and msg[10:11] == " ":
        _safe_write(dest, msg, end=end)
    else:
        _safe_write(dest, f"{ts} {msg}", end=end)


def _apply_log_timestamps() -> None:
    """Stamp stdlib logging (uvicorn, behaviors.*) the same way as print().

    Only reformats existing handlers so we do not double-emit uvicorn lines.
    """
    formatter = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "behaviors", "behaviors.config")
    for name in names:
        lg = logging.getLogger(name) if name else logging.getLogger()
        for h in lg.handlers:
            h.setFormatter(formatter)


class _SkipHealthAccessLog(logging.Filter):
    """Drop uvicorn access lines for GET /health (supervisor polls often)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        # Typical: '127.0.0.1:38615 - "GET /health HTTP/1.1" 200 OK'
        if "/health" in msg:
            return False
        return True

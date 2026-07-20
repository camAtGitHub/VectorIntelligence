"""Timestamped print wrapper and stdlib logging formatters for vector-ai."""
import logging
import sys
from datetime import datetime

# Make print() flush immediately so journalctl / vector-ai.log show lines in real time.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

_orig_print = print
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def print(*args, sep=" ", end="\n", file=None, flush=True):  # noqa: A001 - intentional wrap
    """Prefix lines with YYYY-MM-DD HH:MM:SS for log files."""
    dest = file if file is not None else sys.stdout
    # Leave non-stdio prints alone (rare).
    if dest not in (sys.stdout, sys.stderr, None):
        _orig_print(*args, sep=sep, end=end, file=file, flush=flush)
        return
    ts = datetime.now().strftime(_LOG_DATEFMT)
    if not args:
        _orig_print(end=end, file=dest, flush=True)
        return
    msg = sep.join(str(a) for a in args)
    # Avoid double-prefix if a caller already stamped the line.
    if len(msg) >= 19 and msg[4:5] == "-" and msg[7:8] == "-" and msg[10:11] == " ":
        _orig_print(msg, end=end, file=dest, flush=True)
    else:
        _orig_print(f"{ts} {msg}", end=end, file=dest, flush=True)


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

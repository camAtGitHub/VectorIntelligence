"""Pure wall-clock and inter-turn gap formatters for chat user-message prefixes.

No service/deps/soul imports. Callers inject datetime / seconds (no datetime.now()).
Bracket wrapping lives in build_user_time_prefix; format_* return bare bodies.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def format_wall_clock_human(dt: datetime) -> str:
    """Return 'Sat 8 Aug, 9:14pm' (no brackets)."""
    weekday = dt.strftime("%a")
    day = str(dt.day)  # no leading zero, portable
    month = dt.strftime("%b")
    h = dt.hour % 12 or 12
    minutes = dt.strftime("%M")
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{weekday} {day} {month}, {h}:{minutes}{ampm}"


def format_gap_tag(delta_seconds: float) -> Optional[str]:
    """Return bracket-ready gap body per D7 table, or None if Δ < 5 minutes.

    Thresholds (seconds):
      < 5*60          → None
      < 30*60         → "{N} minutes since last message"  N=max(1, round(Δ/60))
      < 45*60         → "Gap since previous message: half an hour ago"
      < 60*60         → "Gap since previous message: 45 minutes ago"
      < 90*60         → "Gap since previous message: an hour ago"
      < 2*3600        → "Gap since previous message: 90 minutes ago"
      < 12*3600       → "Gap since previous message: {N} hours ago"  N=round(Δ/3600) in 2..11
      < 24*3600       → "Gap since previous message: Half a day ago"
      < 7*86400       → "Gap since previous message: {N} days ago"  N=floor(Δ/86400) in 1..6
      < 14*86400      → "Gap since previous message: A week ago"
      else            → "Gap since previous message: {N} weeks ago"  N=floor(Δ/(7*86400))
    """
    try:
        delta = float(delta_seconds)
    except (TypeError, ValueError):
        return None
    if delta < 5 * 60:
        return None
    if delta < 30 * 60:
        n = max(1, int(round(delta / 60)))
        return f"{n} minutes since last message"
    if delta < 45 * 60:
        return "Gap since previous message: half an hour ago"
    if delta < 60 * 60:
        return "Gap since previous message: 45 minutes ago"
    if delta < 90 * 60:
        return "Gap since previous message: an hour ago"
    if delta < 2 * 3600:
        return "Gap since previous message: 90 minutes ago"
    if delta < 12 * 3600:
        n = int(round(delta / 3600))
        n = max(2, min(11, n))
        return f"Gap since previous message: {n} hours ago"
    if delta < 24 * 3600:
        return "Gap since previous message: Half a day ago"
    if delta < 7 * 86400:
        n = int(delta // 86400)  # floor
        n = max(1, min(6, n))
        return f"Gap since previous message: {n} days ago"
    if delta < 14 * 86400:
        return "Gap since previous message: A week ago"
    n = int(delta // (7 * 86400))  # floor weeks
    n = max(2, n)  # min 2 after 14d
    return f"Gap since previous message: {n} weeks ago"


def build_user_time_prefix(
    *,
    now_dt: datetime,
    prior_turn_ts: Optional[float],
    is_first_message: bool,
) -> str:
    """Return prefix string (0–2 lines joined by \\n), no user body.

    Wraps as ``[Time: …]`` and ``[{gap body}]``. Empty string if nothing to show.
    Rules (D4–D6): first / missing prior → wall only; cross-day → wall + gap;
    same day → gap only (omitted when Δ < 5m).
    """
    last_dt: Optional[datetime] = None
    try:
        ts = float(prior_turn_ts) if prior_turn_ts is not None else 0.0
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 0:
        try:
            last_dt = datetime.fromtimestamp(ts)
        except (TypeError, ValueError, OSError):
            last_dt = None

    if is_first_message or last_dt is None:
        wall = format_wall_clock_human(now_dt)
        return f"[Time: {wall}]"

    delta = (now_dt - last_dt).total_seconds()
    gap = format_gap_tag(delta)
    lines: list[str] = []
    if now_dt.date() != last_dt.date():
        lines.append(f"[Time: {format_wall_clock_human(now_dt)}]")
        if gap:
            lines.append(f"[{gap}]")
    else:
        if gap:
            lines.append(f"[{gap}]")
    return "\n".join(lines)

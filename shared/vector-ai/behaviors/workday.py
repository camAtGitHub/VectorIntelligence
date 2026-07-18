"""Work Day Mode: accountability pokes, late arm, pause — templates only (v1)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import WorkdayConfig, minutes_since_midnight
from .continuity import ContinuityStore, WorkdayRecord
from .types import (
    BehaviorContext,
    FaceIdentity,
    PresenceSnapshot,
    TickResult,
    WorkdayMode,
)

# Chat command tags (stripped from speech by service).
_AFTERNOON_RE = re.compile(
    r"\{\{workAfternoon\|\|(yes|no)\}\}", re.IGNORECASE
)
_PAUSE_RE = re.compile(
    r"\{\{workPause\|\|until=(\d{1,2}:\d{2})\}\}", re.IGNORECASE
)
_RESUME_RE = re.compile(r"\{\{workResume\}\}", re.IGNORECASE)


def parse_work_commands(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Extract workday control tags; return cleaned text and action list.

    Actions:
      ("afternoon", "yes"|"no")
      ("pause", "HH:MM")
      ("resume", "")
    """
    actions: List[Tuple[str, str]] = []
    if not text:
        return text or "", actions

    for m in _AFTERNOON_RE.finditer(text):
        actions.append(("afternoon", m.group(1).strip().lower()))
    text = _AFTERNOON_RE.sub("", text)

    for m in _PAUSE_RE.finditer(text):
        actions.append(("pause", m.group(1).strip()))
    text = _PAUSE_RE.sub("", text)

    if _RESUME_RE.search(text):
        actions.append(("resume", ""))
    text = _RESUME_RE.sub("", text)

    # Collapse leftover whitespace from stripped tags
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), actions


def _in_window(local_dt: datetime, begin: tuple[int, int], end: tuple[int, int]) -> bool:
    """Same-day [begin, end] inclusive of begin, exclusive of end boundary by minutes."""
    now_m = minutes_since_midnight(local_dt.hour, local_dt.minute)
    b = minutes_since_midnight(*begin)
    e = minutes_since_midnight(*end)
    return b <= now_m < e


def _past(local_dt: datetime, hhmm: tuple[int, int]) -> bool:
    now_m = minutes_since_midnight(local_dt.hour, local_dt.minute)
    return now_m >= minutes_since_midnight(*hhmm)


def _local_date(local_dt: datetime) -> str:
    return local_dt.strftime("%Y-%m-%d")


def _identified_primary(face: Optional[FaceIdentity], rec: WorkdayRecord) -> bool:
    """True if face is a named non-stranger suitable to arm the day."""
    if face is None or face.is_stranger:
        return False
    if not (face.name or "").strip():
        return False
    # Once a primary is set, only that face_id can re-arm / re-ID
    if rec.primary_face_id is not None and face.face_id != rec.primary_face_id:
        return False
    return True


class WorkDayBehavior:
    id = "workday"

    def __init__(self, cfg: WorkdayConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # -- chat / command hooks -------------------------------------------------

    def on_afternoon_yes(self, local_date: str, now: float = 0.0) -> None:
        rec = self.store.load_workday(local_date)
        if rec.mode not in (WorkdayMode.LATE_CHECK, WorkdayMode.NO_SHOW):
            # Allow yes even if already late_check timed out back to no_show
            if rec.mode != WorkdayMode.LATE_CHECK and rec.mode != WorkdayMode.NO_SHOW:
                if rec.mode not in (WorkdayMode.LATE_WORKING, WorkdayMode.WORKING):
                    return
        rec.mode = WorkdayMode.LATE_WORKING
        rec.arm_source = "late_yes"
        if rec.started_at <= 0:
            rec.started_at = now or rec.late_check_asked_at or 0.0
        rec.last_poke_at = rec.started_at
        rec.pause_until = 0.0
        rec.paused_from = ""
        self.store.save_workday(rec)

    def on_afternoon_no(self, local_date: str) -> None:
        rec = self.store.load_workday(local_date)
        rec.mode = WorkdayMode.NO_SHOW
        rec.pause_until = 0.0
        rec.paused_from = ""
        self.store.save_workday(rec)

    def on_pause(self, local_date: str, until_ts: float) -> None:
        rec = self.store.load_workday(local_date)
        if rec.mode in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
            rec.paused_from = rec.mode.value
            rec.mode = WorkdayMode.PAUSED
            rec.pause_until = until_ts
            self.store.save_workday(rec)
        elif rec.mode == WorkdayMode.PAUSED:
            rec.pause_until = until_ts
            self.store.save_workday(rec)

    def on_resume(self, local_date: str) -> None:
        rec = self.store.load_workday(local_date)
        if rec.mode != WorkdayMode.PAUSED:
            return
        prev = rec.paused_from or WorkdayMode.WORKING.value
        try:
            rec.mode = WorkdayMode(prev)
        except ValueError:
            rec.mode = WorkdayMode.WORKING
        rec.pause_until = 0.0
        rec.paused_from = ""
        self.store.save_workday(rec)

    # -- speech templates -----------------------------------------------------

    def _line_on_task(self, rec: WorkdayRecord) -> str:
        if rec.absence_count >= 2:
            return (
                "Still here? After those absences earlier, I hope this stretch "
                "is the real work."
            )
        if rec.arm_source == "late_yes":
            return "Afternoon check-in. Still on task, or inventing a new sideline?"
        return "Ninety minutes. Still on the work, or inventing a new sideline?"

    def _line_away(self, rec: WorkdayRecord) -> str:
        if rec.absence_count >= 1:
            return "Gone again. Shouldn't you be working?"
        return "You've been gone a while. Shouldn't you be working?"

    def _line_late_check(self, rec: WorkdayRecord) -> str:
        return (
            "Didn't see you this morning. What happened — working this afternoon?"
        )

    # -- core tick ------------------------------------------------------------

    def tick(self, ctx: BehaviorContext) -> TickResult:
        return self.plan(ctx)

    def plan(self, ctx: BehaviorContext) -> TickResult:
        """Raw desire: speak text / need_identity. Runtime applies arbiter."""
        if not self.cfg.enabled:
            return TickResult(debug={"mode": WorkdayMode.OFF.value, "reason": "disabled"})

        local_dt: datetime = ctx.local_dt
        date = _local_date(local_dt)
        rec = self.store.load_workday(date)
        now = ctx.now
        presence = ctx.presence
        face = presence.face if ctx.identity_fresh else None
        # If identity is fresh, use cache face even when plan doesn't re-set it
        if face is None and ctx.identity_fresh and presence.face is not None:
            face = presence.face

        # Day rollover / end-of-day: after WORK_END, idle for the day
        if rec.mode not in (WorkdayMode.OFF, WorkdayMode.WAITING_MORNING) and _past(
            local_dt, self.cfg.end
        ):
            if rec.mode in (
                WorkdayMode.WORKING,
                WorkdayMode.LATE_WORKING,
                WorkdayMode.PAUSED,
                WorkdayMode.LATE_CHECK,
            ):
                rec.mode = WorkdayMode.OFF
                self.store.save_workday(rec)
                return TickResult(debug={"mode": "off", "reason": "work_end"})

        # Ensure enabled day starts in waiting_morning (not leftover OFF from prior logic)
        if rec.mode == WorkdayMode.OFF and not _past(local_dt, self.cfg.end):
            # Only reset to waiting if we never armed and still in morning window area
            if rec.started_at <= 0 and not _past(local_dt, self.cfg.start_end):
                rec.mode = WorkdayMode.WAITING_MORNING
                self.store.save_workday(rec)

        # Clock: waiting_morning → no_show after start window
        if rec.mode == WorkdayMode.WAITING_MORNING and _past(local_dt, self.cfg.start_end):
            rec.mode = WorkdayMode.NO_SHOW
            self.store.save_workday(rec)

        # Pause expiry
        if rec.mode == WorkdayMode.PAUSED:
            if rec.pause_until > 0 and now >= rec.pause_until:
                self.on_resume(date)
                rec = self.store.load_workday(date)
            else:
                return TickResult(debug={"mode": "paused", "reason": "paused"})

        # Late check timeout → back to no_show
        if rec.mode == WorkdayMode.LATE_CHECK:
            if (
                rec.late_check_asked_at > 0
                and (now - rec.late_check_asked_at) >= self.cfg.late_check_timeout_s
            ):
                rec.mode = WorkdayMode.NO_SHOW
                self.store.save_workday(rec)
                return TickResult(debug={"mode": "no_show", "reason": "late_timeout"})

        result = TickResult(debug={"mode": rec.mode.value})

        # --- Mode-specific transitions / speech ---

        if rec.mode == WorkdayMode.WAITING_MORNING:
            return self._tick_waiting_morning(ctx, rec, face, result)

        if rec.mode == WorkdayMode.NO_SHOW:
            return self._tick_no_show(ctx, rec, face, result)

        if rec.mode == WorkdayMode.LATE_CHECK:
            # Already asked; wait for chat yes/no (or timeout above)
            result.debug["reason"] = "awaiting_afternoon"
            return result

        if rec.mode in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
            return self._tick_armed(ctx, rec, face, result)

        if rec.mode == WorkdayMode.OFF:
            result.debug["reason"] = "off"
            return result

        return result

    def _tick_waiting_morning(
        self,
        ctx: BehaviorContext,
        rec: WorkdayRecord,
        face: Optional[FaceIdentity],
        result: TickResult,
    ) -> TickResult:
        local_dt = ctx.local_dt
        if not _in_window(local_dt, self.cfg.start_begin, self.cfg.start_end):
            result.debug["reason"] = "outside_morning_window"
            return result
        if not ctx.presence.occupied:
            result.debug["reason"] = "empty"
            return result
        # Juncture: need identified primary (not a stranger / guest)
        if not ctx.identity_fresh:
            result.need_identity = True
            result.debug["reason"] = "need_identity_morning"
            return result
        if not _identified_primary(face, rec):
            # Fresh identity but wrong person — do not re-probe every tick
            result.debug["reason"] = "stranger_or_non_primary"
            return result
        # Arm morning work
        rec.mode = WorkdayMode.WORKING
        rec.primary_face_id = face.face_id  # type: ignore[union-attr]
        rec.primary_face_name = face.name  # type: ignore[union-attr]
        rec.started_at = ctx.now
        rec.arm_source = "morning"
        rec.last_poke_at = ctx.now  # poke timer from start
        rec.absence_started_at = 0.0
        rec.away_scold_spoken = False
        self.store.save_workday(rec)
        result.debug["mode"] = WorkdayMode.WORKING.value
        result.debug["reason"] = "morning_start"
        return result

    def _tick_no_show(
        self,
        ctx: BehaviorContext,
        rec: WorkdayRecord,
        face: Optional[FaceIdentity],
        result: TickResult,
    ) -> TickResult:
        # After start window, if occupied + identified → late_check once
        if _past(ctx.local_dt, self.cfg.end):
            result.debug["reason"] = "past_end"
            return result
        if not ctx.presence.occupied:
            result.debug["reason"] = "empty"
            return result
        if not ctx.identity_fresh:
            result.need_identity = True
            result.debug["reason"] = "need_identity_late"
            return result
        if not _identified_primary(face, rec):
            result.debug["reason"] = "stranger_or_non_primary"
            return result
        # Enter late_check and speak once
        rec.mode = WorkdayMode.LATE_CHECK
        rec.primary_face_id = face.face_id  # type: ignore[union-attr]
        rec.primary_face_name = face.name  # type: ignore[union-attr]
        rec.late_check_asked_at = ctx.now
        self.store.save_workday(rec)
        result.speak = self._line_late_check(rec)
        result.debug["mode"] = WorkdayMode.LATE_CHECK.value
        result.debug["reason"] = "late_check"
        return result

    def _tick_armed(
        self,
        ctx: BehaviorContext,
        rec: WorkdayRecord,
        face: Optional[FaceIdentity],
        result: TickResult,
    ) -> TickResult:
        now = ctx.now
        occupied = ctx.presence.occupied
        local_dt = ctx.local_dt

        # Optional re-ID after very long absence
        if rec.reid_pending:
            if not occupied:
                result.debug["reason"] = "reid_wait_occupied"
                return result
            if not ctx.identity_fresh or not _identified_primary(face, rec):
                result.need_identity = True
                result.debug["reason"] = "need_identity_reid"
                return result
            rec.reid_pending = False
            self.store.save_workday(rec)

        # Occupancy → away tracking
        if not occupied:
            if rec.absence_started_at <= 0:
                rec.absence_started_at = now
                rec.away_scold_spoken = False
                self.store.save_workday(rec)
            away_s = now - rec.absence_started_at
            # Long-absence re-ID flag (speak path still uses occupancy)
            if (
                self.cfg.reid_after_away_s > 0
                and away_s >= self.cfg.reid_after_away_s
                and not rec.reid_pending
            ):
                rec.reid_pending = True
                self.store.save_workday(rec)

            in_away_window = (
                _past(local_dt, self.cfg.away_window_begin)
                and not _past(local_dt, self.cfg.end)
            )
            if (
                in_away_window
                and away_s >= self.cfg.away_s
                and not rec.away_scold_spoken
            ):
                rec.away_scold_spoken = True
                rec.absence_count += 1
                rec.total_away_s += away_s
                self.store.save_workday(rec)
                result.speak = self._line_away(rec)
                result.debug["reason"] = "away_scold"
                return result
            result.debug["reason"] = "away"
            result.debug["away_s"] = away_s
            return result

        # Occupied again → clear absence stretch
        if rec.absence_started_at > 0:
            away_s = now - rec.absence_started_at
            if not rec.away_scold_spoken and away_s > 0:
                rec.total_away_s += away_s
            rec.absence_started_at = 0.0
            rec.away_scold_spoken = False
            self.store.save_workday(rec)

        # On-task poke
        if _past(local_dt, self.cfg.end):
            result.debug["reason"] = "past_end"
            return result

        anchor = rec.last_poke_at if rec.last_poke_at > 0 else rec.started_at
        if anchor <= 0:
            anchor = now
            rec.last_poke_at = now
            self.store.save_workday(rec)

        elapsed = now - anchor
        if elapsed >= self.cfg.poke_interval_s:
            rec.last_poke_at = now
            self.store.save_workday(rec)
            result.speak = self._line_on_task(rec)
            result.debug["reason"] = "on_task_poke"
            return result

        result.debug["reason"] = "armed_idle"
        result.debug["poke_in_s"] = self.cfg.poke_interval_s - elapsed
        return result

    def clock_tick(self, now: float, local_dt: datetime) -> None:
        """Clock-only transitions (no presence) — waiting_morning → no_show, EOD."""
        if not self.cfg.enabled:
            return
        date = _local_date(local_dt)
        rec = self.store.load_workday(date)
        dirty = False
        if rec.mode == WorkdayMode.WAITING_MORNING and _past(local_dt, self.cfg.start_end):
            rec.mode = WorkdayMode.NO_SHOW
            dirty = True
        if rec.mode in (
            WorkdayMode.WORKING,
            WorkdayMode.LATE_WORKING,
            WorkdayMode.PAUSED,
            WorkdayMode.LATE_CHECK,
        ) and _past(local_dt, self.cfg.end):
            rec.mode = WorkdayMode.OFF
            dirty = True
        if rec.mode == WorkdayMode.LATE_CHECK and rec.late_check_asked_at > 0:
            if (now - rec.late_check_asked_at) >= self.cfg.late_check_timeout_s:
                rec.mode = WorkdayMode.NO_SHOW
                dirty = True
        if rec.mode == WorkdayMode.PAUSED and rec.pause_until > 0 and now >= rec.pause_until:
            prev = rec.paused_from or WorkdayMode.WORKING.value
            try:
                rec.mode = WorkdayMode(prev)
            except ValueError:
                rec.mode = WorkdayMode.WORKING
            rec.pause_until = 0.0
            rec.paused_from = ""
            dirty = True
        if dirty:
            self.store.save_workday(rec)


def pause_until_ts(local_dt: datetime, until_hhmm: str, tz: ZoneInfo) -> float:
    """Convert HH:MM on the same local day (or next if already past) to epoch."""
    h, m = map(int, until_hhmm.split(":"))
    target = local_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.tzinfo is None and tz is not None:
        target = target.replace(tzinfo=tz)
    if target.timestamp() <= local_dt.timestamp():
        # Already past — treat as next day
        from datetime import timedelta
        target = target + timedelta(days=1)
    return target.timestamp()

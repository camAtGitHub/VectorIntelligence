"""Work Day Mode: accountability pokes, late arm, pause — templates only (v1)."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, tzinfo
from enum import Enum
from typing import List, Optional, Tuple

import deps

from .config import WorkdayConfig, minutes_since_midnight, parse_hhmm
from .continuity import ContinuityStore, WorkdayRecord
from .logutil import blog, short
from .types import (
    BehaviorContext,
    FaceIdentity,
    TickResult,
    WorkdayMode,
)

_TAG = "workday"


class ClockOutcome(str, Enum):
    """Machine keys for plan early-returns after clock transitions."""

    NONE = ""
    WORK_END = "work_end"
    LATE_TIMEOUT = "late_timeout"
    OFF_TO_WAITING = "off_to_waiting"
    MORNING_TO_NO_SHOW = "morning_to_no_show"
    PAUSE_EXPIRED = "pause_expired"

# Strict tags (parsed as actions). Accept || or single | separators.
_AFTERNOON_RE = re.compile(
    r"\{\{workAfternoon\|{1,2}(yes|no)\}\}", re.IGNORECASE
)
_PAUSE_RE = re.compile(
    r"\{\{workPause\|{1,2}until=(\d{1,2}:\d{2})\}\}", re.IGNORECASE
)
_RESUME_RE = re.compile(r"\{\{workResume\}\}", re.IGNORECASE)
# Near-miss / malformed work tags — strip from TTS even if not actioned.
# Allows single | or || and optional payload after pipes.
_WORK_TAG_STRIP_RE = re.compile(
    r"\{\{\s*work[A-Za-z]*\s*(?:\|{1,2}[^}]*)?\}\}", re.IGNORECASE
)


def parse_work_commands(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Extract workday control tags; return cleaned text and action list.

    Actions:
      ("afternoon", "yes"|"no")
      ("pause", "HH:MM")  — only if HH:MM validates
      ("resume", "")
    Malformed {{work…}} tags are always stripped so they never reach TTS.
    """
    actions: List[Tuple[str, str]] = []
    if not text:
        return text or "", actions

    for m in _AFTERNOON_RE.finditer(text):
        actions.append(("afternoon", m.group(1).strip().lower()))
    text = _AFTERNOON_RE.sub("", text)

    for m in _PAUSE_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            h, mi = parse_hhmm(raw)
            actions.append(("pause", f"{h:02d}:{mi:02d}"))
        except ValueError:
            pass  # invalid time — strip tag only
    text = _PAUSE_RE.sub("", text)

    if _RESUME_RE.search(text):
        actions.append(("resume", ""))
    text = _RESUME_RE.sub("", text)

    # Strip any leftover near-miss work tags (|| yes, single |, maybe, etc.)
    text = _WORK_TAG_STRIP_RE.sub("", text)

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), actions


def _in_window(local_dt: datetime, begin: tuple[int, int], end: tuple[int, int]) -> bool:
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
    if face.face_id <= 0:
        return False
    # Once a primary is set, only that face_id can re-arm / re-ID
    if rec.primary_face_id is not None and face.face_id != rec.primary_face_id:
        return False
    return True


class WorkDayBehavior:
    id = "workday"
    # Reserved for a future multi-FSM throttle; runtime does not honor this yet.
    min_tick_interval: float = 30.0

    def __init__(self, cfg: WorkdayConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def status_summary(self, now: float, **_kwargs) -> str:
        """One-line card text for GET /v1/behaviors/state (mode value)."""
        try:
            date_s = datetime.fromtimestamp(now, tz=self.cfg.tz).strftime("%Y-%m-%d")
            rec = self.store.load_workday(date_s)
            return rec.mode.value
        except Exception as e:
            blog(_TAG, f"status_summary failed: {e}")
            return "error"

    def status(self, now: float, **_kwargs) -> dict:
        """Full workday detail for GET /v1/behaviors/workday (ops/debug)."""
        try:
            local_dt = datetime.fromtimestamp(now, tz=self.cfg.tz)
            date_s = local_dt.strftime("%Y-%m-%d")
            rec = self.store.load_workday(date_s)
            strip = self.store.day_strip(date_s)
            mode = rec.mode.value
        except Exception as e:
            blog(_TAG, f"status failed: {e}")
            return {
                "id": self.id,
                "schema_version": 1,
                "workday_enabled": bool(self.cfg.enabled),
                "date": "",
                "mode": "error",
                "day_strip": str(e),
            }

        def _epoch_or_none(v: float) -> Optional[float]:
            return float(v) if v and float(v) > 0 else None

        return {
            "id": self.id,
            "schema_version": 1,
            "workday_enabled": bool(self.cfg.enabled),
            "date": date_s,
            "mode": mode,
            "day_strip": strip,
            "primary_face_id": rec.primary_face_id,
            "primary_face_name": rec.primary_face_name or "",
            "arm_source": rec.arm_source or "",
            "started_at": _epoch_or_none(rec.started_at),
            "last_poke_at": _epoch_or_none(rec.last_poke_at),
            "pause_until": _epoch_or_none(rec.pause_until),
            "paused_from": rec.paused_from or "",
            "absence_count": rec.absence_count,
            "total_away_s": rec.total_away_s,
            "late_check_done": rec.late_check_done,
            "late_check_asked_at": _epoch_or_none(rec.late_check_asked_at),
        }

    # -- chat / command hooks -------------------------------------------------

    def on_afternoon_yes(self, local_date: str, now: float = 0.0) -> None:
        """Arm late work only from LATE_CHECK or NO_SHOW (post-ask / timeout)."""
        with self.store.fsm_lock:
            rec = self.store.load_workday(local_date)
            if rec.mode not in (WorkdayMode.LATE_CHECK, WorkdayMode.NO_SHOW):
                blog(
                    _TAG,
                    f"afternoon YES ignored (mode={rec.mode.value})",
                    verbose=True,
                )
                return
            # NO_SHOW without ever being asked: still allow explicit chat arm
            # only if late_check_done (declined/timeout) or late_check_asked_at
            # — not from pure morning no_show before any late path.
            if rec.mode == WorkdayMode.NO_SHOW and not (
                rec.late_check_done or rec.late_check_asked_at > 0
            ):
                # Allow arming no_show via chat yes as convenience (user declared).
                pass
            prev = rec.mode.value
            rec.mode = WorkdayMode.LATE_WORKING
            rec.arm_source = "late_yes"
            rec.late_check_done = True  # won't re-ask if they pause etc.
            if rec.started_at <= 0:
                rec.started_at = now or rec.late_check_asked_at or 0.0
            rec.last_poke_at = rec.started_at
            rec.pause_until = 0.0
            rec.paused_from = ""
            self.store.save_workday(rec)
            blog(_TAG, f"afternoon YES: {prev} → late_working date={local_date}")

    def on_afternoon_no(self, local_date: str) -> None:
        """Only valid during LATE_CHECK — refuse to tear down an armed day."""
        with self.store.fsm_lock:
            rec = self.store.load_workday(local_date)
            if rec.mode != WorkdayMode.LATE_CHECK:
                blog(
                    _TAG,
                    f"afternoon NO ignored (mode={rec.mode.value})",
                    verbose=True,
                )
                return
            rec.mode = WorkdayMode.NO_SHOW
            rec.late_check_done = True
            rec.pause_until = 0.0
            rec.paused_from = ""
            self.store.save_workday(rec)
            blog(_TAG, f"afternoon NO: late_check → no_show date={local_date}")

    def on_pause(self, local_date: str, until_ts: float) -> None:
        with self.store.fsm_lock:
            rec = self.store.load_workday(local_date)
            if rec.mode in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
                rec.paused_from = rec.mode.value
                rec.mode = WorkdayMode.PAUSED
                rec.pause_until = until_ts
                self.store.save_workday(rec)
                blog(_TAG, f"pause until_ts={until_ts:.0f} from {rec.paused_from}")
            elif rec.mode == WorkdayMode.PAUSED:
                rec.pause_until = until_ts
                self.store.save_workday(rec)
                blog(_TAG, f"pause extended until_ts={until_ts:.0f}")

    def on_resume(self, local_date: str) -> None:
        with self.store.fsm_lock:
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
            blog(_TAG, f"resume → {rec.mode.value}")

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
        with self.store.fsm_lock:
            return self.plan(ctx)

    def _apply_clock_transitions(
        self, rec: WorkdayRecord, now: float, local_dt: datetime
    ) -> tuple[WorkdayRecord, bool, ClockOutcome, str]:
        """Shared clock-driven mode transitions for plan + clock_tick.

        Returns (rec, dirty, outcome, note). Mutates ``rec`` in place when dirty.
        ``outcome`` is a machine key for plan early-returns; ``note`` is log-only.
        Does not save; caller persists when dirty.
        """
        note = ""
        dirty = False
        outcome = ClockOutcome.NONE
        # End-of-day for armed / paused / late_check
        if rec.mode in (
            WorkdayMode.WORKING,
            WorkdayMode.LATE_WORKING,
            WorkdayMode.PAUSED,
            WorkdayMode.LATE_CHECK,
        ) and _past(local_dt, self.cfg.end):
            rec.mode = WorkdayMode.OFF
            dirty = True
            outcome = ClockOutcome.WORK_END
            note = "work end → off"
            return rec, dirty, outcome, note

        if rec.mode == WorkdayMode.OFF and not _past(local_dt, self.cfg.end):
            if rec.started_at <= 0 and not _past(local_dt, self.cfg.start_end):
                rec.mode = WorkdayMode.WAITING_MORNING
                dirty = True
                outcome = ClockOutcome.OFF_TO_WAITING
                note = "off → waiting_morning"

        if rec.mode == WorkdayMode.WAITING_MORNING and _past(local_dt, self.cfg.start_end):
            rec.mode = WorkdayMode.NO_SHOW
            dirty = True
            outcome = ClockOutcome.MORNING_TO_NO_SHOW
            note = "morning window ended → no_show"

        if rec.mode == WorkdayMode.PAUSED:
            if rec.pause_until > 0 and now >= rec.pause_until:
                prev_mode = rec.paused_from or WorkdayMode.WORKING.value
                try:
                    rec.mode = WorkdayMode(prev_mode)
                except ValueError:
                    rec.mode = WorkdayMode.WORKING
                rec.pause_until = 0.0
                rec.paused_from = ""
                dirty = True
                outcome = ClockOutcome.PAUSE_EXPIRED
                note = f"pause expired → {rec.mode.value}"

        if rec.mode == WorkdayMode.LATE_CHECK and rec.late_check_asked_at > 0:
            if (now - rec.late_check_asked_at) >= self.cfg.late_check_timeout_s:
                rec.mode = WorkdayMode.NO_SHOW
                rec.late_check_done = True
                dirty = True
                outcome = ClockOutcome.LATE_TIMEOUT
                note = "late_check timeout → no_show"

        return rec, dirty, outcome, note

    def plan(self, ctx: BehaviorContext) -> TickResult:
        """Raw desire: speak text / need_identity.

        Speech-gated mutations are deferred via TickResult.on_speak_allowed so
        the runtime arbiter can deny without advancing timers or entering
        late_check without speaking.
        """
        if not self.cfg.enabled:
            return TickResult(debug={"mode": WorkdayMode.OFF.value, "reason": "disabled"})

        local_dt: datetime = ctx.local_dt
        date = _local_date(local_dt)
        rec = self.store.load_workday(date)
        now = ctx.now
        presence = ctx.presence
        face = presence.face if ctx.identity_fresh else None

        rec, dirty, outcome, note = self._apply_clock_transitions(rec, now, local_dt)
        if dirty:
            self.store.save_workday(rec)
            if outcome == ClockOutcome.WORK_END:
                return TickResult(debug={"mode": "off", "reason": "work_end"})
            if outcome == ClockOutcome.LATE_TIMEOUT:
                return TickResult(debug={"mode": "no_show", "reason": "late_timeout"})

        if rec.mode == WorkdayMode.PAUSED:
            # Still paused (pause not expired, or just-expired already resumed).
            if rec.pause_until > 0 and now < rec.pause_until:
                return TickResult(debug={"mode": "paused", "reason": "paused"})
            # If pause_until is 0 we already resumed via clock transitions.
            if rec.mode == WorkdayMode.PAUSED:
                return TickResult(debug={"mode": "paused", "reason": "paused"})

        result = TickResult(debug={"mode": rec.mode.value})

        if rec.mode == WorkdayMode.WAITING_MORNING:
            return self._tick_waiting_morning(ctx, rec, face, result)

        if rec.mode == WorkdayMode.NO_SHOW:
            return self._tick_no_show(ctx, rec, face, result)

        if rec.mode == WorkdayMode.LATE_CHECK:
            result.debug["reason"] = "awaiting_afternoon"
            return result

        if rec.mode in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
            return self._tick_armed(ctx, rec, face, result)

        if rec.mode == WorkdayMode.OFF:
            result.debug["reason"] = "off"
            return result

        return result

    def _identity_on_cooldown(self, rec: WorkdayRecord, now: float) -> bool:
        return rec.identity_reject_until > 0 and now < rec.identity_reject_until

    def _mark_identity_reject(self, rec: WorkdayRecord, now: float) -> None:
        rec.identity_reject_until = now + max(0, int(self.cfg.identity_reject_cooldown_s))
        self.store.save_workday(rec)

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
        if not ctx.identity_fresh:
            if self._identity_on_cooldown(rec, ctx.now):
                result.debug["reason"] = "identity_reject_cooldown"
                return result
            result.need_identity = True
            result.debug["reason"] = "need_identity_morning"
            return result
        if not _identified_primary(face, rec):
            self._mark_identity_reject(rec, ctx.now)
            result.debug["reason"] = "stranger_or_non_primary"
            return result
        # Morning arm is not speech-gated — commit immediately.
        rec.mode = WorkdayMode.WORKING
        rec.primary_face_id = face.face_id  # type: ignore[union-attr]
        rec.primary_face_name = face.name  # type: ignore[union-attr]
        rec.started_at = ctx.now
        rec.arm_source = "morning"
        rec.last_poke_at = ctx.now
        rec.absence_started_at = 0.0
        rec.away_scold_spoken = False
        rec.identity_reject_until = 0.0
        self.store.save_workday(rec)
        result.debug["mode"] = WorkdayMode.WORKING.value
        result.debug["reason"] = "morning_start"
        blog(
            _TAG,
            f"morning arm → working primary={face.name!r} id={face.face_id}",
        )
        return result

    def _tick_no_show(
        self,
        ctx: BehaviorContext,
        rec: WorkdayRecord,
        face: Optional[FaceIdentity],
        result: TickResult,
    ) -> TickResult:
        if _past(ctx.local_dt, self.cfg.end):
            result.debug["reason"] = "past_end"
            return result
        # Spec: after no/timeout stay silent rest of day — no re-ask.
        if rec.late_check_done:
            result.debug["reason"] = "late_check_done"
            return result
        if not ctx.presence.occupied:
            result.debug["reason"] = "empty"
            return result
        if not ctx.identity_fresh:
            if self._identity_on_cooldown(rec, ctx.now):
                result.debug["reason"] = "identity_reject_cooldown"
                return result
            result.need_identity = True
            result.debug["reason"] = "need_identity_late"
            return result
        if not _identified_primary(face, rec):
            self._mark_identity_reject(rec, ctx.now)
            result.debug["reason"] = "stranger_or_non_primary"
            return result

        # Speech-gated: enter LATE_CHECK only if arbiter allows the question.
        face_id = face.face_id  # type: ignore[union-attr]
        face_name = face.name  # type: ignore[union-attr]
        line = self._line_late_check(rec)
        date = rec.date
        asked_at = ctx.now
        store = self.store

        def _commit_late() -> None:
            with store.fsm_lock:
                r = store.load_workday(date)
                if r.mode != WorkdayMode.NO_SHOW or r.late_check_done:
                    return
                r.mode = WorkdayMode.LATE_CHECK
                r.primary_face_id = face_id
                r.primary_face_name = face_name
                r.late_check_asked_at = asked_at
                r.identity_reject_until = 0.0
                store.save_workday(r)

        result.speak = line
        result.on_speak_allowed = _commit_late
        result.debug["mode"] = WorkdayMode.LATE_CHECK.value
        result.debug["reason"] = "late_check"
        result.debug["pending_commit"] = "late_check"
        blog(
            _TAG,
            f"want late_check ask to {face_name!r}: {short(line)!r} "
            f"— waiting on speech arbiter",
        )
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
        date = rec.date
        store = self.store

        if rec.reid_pending:
            if not occupied:
                result.debug["reason"] = "reid_wait_occupied"
                return result
            if not ctx.identity_fresh:
                if self._identity_on_cooldown(rec, now):
                    result.debug["reason"] = "identity_reject_cooldown"
                    return result
                result.need_identity = True
                result.debug["reason"] = "need_identity_reid"
                return result
            if not _identified_primary(face, rec):
                self._mark_identity_reject(rec, now)
                result.debug["reason"] = "stranger_or_non_primary_reid"
                return result
            rec.reid_pending = False
            rec.identity_reject_until = 0.0
            self.store.save_workday(rec)

        # Occupancy → away tracking (non-speech; always commit)
        if not occupied:
            if rec.absence_started_at <= 0:
                rec.absence_started_at = now
                rec.away_scold_spoken = False
                self.store.save_workday(rec)
            away_s = now - rec.absence_started_at
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
                line = self._line_away(rec)
                absence_count = rec.absence_count + 1
                total_away = rec.total_away_s + away_s

                def _commit_away() -> None:
                    with store.fsm_lock:
                        r = store.load_workday(date)
                        if r.mode not in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
                            return
                        if r.away_scold_spoken:
                            return
                        r.away_scold_spoken = True
                        r.absence_count = max(r.absence_count, absence_count)
                        r.total_away_s = max(r.total_away_s, total_away)
                        store.save_workday(r)

                result.speak = line
                result.on_speak_allowed = _commit_away
                result.debug["reason"] = "away_scold"
                result.debug["pending_commit"] = "away_scold"
                blog(
                    _TAG,
                    f"want away_scold (away={away_s:.0f}s): {short(line)!r} "
                    f"— waiting on speech arbiter",
                )
                return result
            result.debug["reason"] = "away"
            result.debug["away_s"] = away_s
            return result

        # Occupied again → clear absence stretch (non-speech)
        if rec.absence_started_at > 0:
            away_s = now - rec.absence_started_at
            if not rec.away_scold_spoken and away_s > 0:
                rec.total_away_s += away_s
            rec.absence_started_at = 0.0
            rec.away_scold_spoken = False
            self.store.save_workday(rec)

        if _past(local_dt, self.cfg.end):
            result.debug["reason"] = "past_end"
            return result

        anchor = rec.last_poke_at if rec.last_poke_at > 0 else rec.started_at
        if anchor <= 0:
            # Bootstrap anchor only — not a speech event
            rec.last_poke_at = now
            self.store.save_workday(rec)
            anchor = now

        elapsed = now - anchor
        if elapsed >= self.cfg.poke_interval_s:
            line = self._line_on_task(rec)
            poke_at = now

            def _commit_poke() -> None:
                with store.fsm_lock:
                    r = store.load_workday(date)
                    if r.mode not in (WorkdayMode.WORKING, WorkdayMode.LATE_WORKING):
                        return
                    r.last_poke_at = poke_at
                    store.save_workday(r)

            result.speak = line
            result.on_speak_allowed = _commit_poke
            result.debug["reason"] = "on_task_poke"
            result.debug["pending_commit"] = "on_task_poke"
            blog(
                _TAG,
                f"want on_task_poke (elapsed={elapsed:.0f}s): {short(line)!r} "
                f"— waiting on speech arbiter",
            )
            return result

        result.debug["reason"] = "armed_idle"
        result.debug["poke_in_s"] = self.cfg.poke_interval_s - elapsed
        return result

    def clock_tick(self, now: float, local_dt: datetime) -> None:
        """Clock-only transitions (no presence).

        Same transitions as plan's clock pass via ``_apply_clock_transitions``:
        EOD → off, OFF→waiting_morning (before start window ends), morning
        window end → no_show, late_check timeout, pause expiry.
        """
        if not self.cfg.enabled:
            return
        with self.store.fsm_lock:
            date = _local_date(local_dt)
            rec = self.store.load_workday(date)
            prev = rec.mode.value
            rec, dirty, _outcome, note = self._apply_clock_transitions(
                rec, now, local_dt
            )
            if dirty:
                self.store.save_workday(rec)
                blog(_TAG, f"clock_tick: {prev} → {rec.mode.value} ({note}) date={date}")


def pause_until_ts(local_dt: datetime, until_hhmm: str, tz: tzinfo) -> float:
    """Convert HH:MM on the same local day (or next if already past) to epoch.

    Raises ValueError on invalid times.
    """
    h, m = parse_hhmm(until_hhmm)
    target = local_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.tzinfo is None and tz is not None:
        target = target.replace(tzinfo=tz)
    if target.timestamp() <= local_dt.timestamp():
        target = target + timedelta(days=1)
    return target.timestamp()


# -- User-text afternoon yes/no backstop (copy vision-intent pattern) --------
# Deterministic apply from spoken/chat user text so half-day report-in does
# not depend on the model emitting {{workAfternoon||yes}}. Mode guards stay
# on WorkDayBehavior.on_afternoon_yes / on_afternoon_no.

_AFTERNOON_YES_RE = re.compile(
    r"\b("
    r"yes|yeah|yep|yup|affirmative"
    r"|working\s+(?:this\s+)?afternoon"
    r"|i(?:'m|\s+am)\s+(?:here\s+)?(?:to\s+)?work(?:ing)?"
    r"|half[- ]?day"
    r"|report(?:ing)?\s+in"
    r"|i\s+am\s+working"
    r"|i(?:'ll|\s+will)\s+work"
    r")\b",
    re.IGNORECASE,
)

_AFTERNOON_NO_RE = re.compile(
    r"\b("
    r"no|nope|nah"
    r"|not\s+working"
    r"|day\s+off"
    r"|off\s+(?:sick|today)"
    r"|not\s+today"
    r")\b",
    re.IGNORECASE,
)

# Short STT-style affirmations only; long free-form → None (LLM + tags).
_AFTERNOON_REPLY_MAX_LEN = 240


def classify_afternoon_reply(user_text: str) -> Optional[str]:
    """Return 'yes' | 'no' | None from user utterance. Pure. No I/O.

    If both yes and no patterns match, prefer clear decline ('no') so phrases
    like "yes but not today" / "no I'm not working" do not arm the day.
    Exclusive yes or exclusive no returns that label; empty / too long → None.
    """
    if not user_text:
        return None
    text = user_text.strip()
    if not text or len(text) > _AFTERNOON_REPLY_MAX_LEN:
        return None
    yes = bool(_AFTERNOON_YES_RE.search(text))
    no = bool(_AFTERNOON_NO_RE.search(text))
    if yes and no:
        # Prefer exclusive-decline: clear no phrase wins over yes tokens.
        return "no"
    if yes:
        return "yes"
    if no:
        return "no"
    return None


def maybe_apply_afternoon_from_user_text(
    user_text: str, *, now: float | None = None
) -> Optional[str]:
    """If workday enabled and mode allows, apply yes/no from user text.

    Returns 'yes'|'no'|None for logging/tests. Does not short-circuit the LLM.
    Delegates mode guards to on_afternoon_yes / on_afternoon_no (no bypass).
    """
    rt = deps.BEHAVIOR_RUNTIME
    cfg = deps._workday_cfg
    if rt is None or getattr(rt, "workday", None) is None:
        return None
    if cfg is None or not getattr(cfg, "enabled", False):
        return None

    decision = classify_afternoon_reply(user_text)
    if decision is None:
        return None

    try:
        local_dt = datetime.now(cfg.tz)
        date_s = local_dt.strftime("%Y-%m-%d")
        ts = float(now) if now is not None else time.time()
        rec = rt.workday.store.load_workday(date_s)
        mode = rec.mode

        if decision == "yes":
            if mode not in (WorkdayMode.LATE_CHECK, WorkdayMode.NO_SHOW):
                blog(
                    _TAG,
                    f"user-text afternoon YES ignored (mode={mode.value})",
                    verbose=True,
                )
                return None
            rt.workday.on_afternoon_yes(date_s, now=ts)
            blog(_TAG, f"user-text afternoon YES for {date_s}")
            print(f"[workday] user-text afternoon YES for {date_s}")
            return "yes"

        if decision == "no":
            if mode != WorkdayMode.LATE_CHECK:
                blog(
                    _TAG,
                    f"user-text afternoon NO ignored (mode={mode.value})",
                    verbose=True,
                )
                return None
            rt.workday.on_afternoon_no(date_s)
            blog(_TAG, f"user-text afternoon NO for {date_s}")
            print(f"[workday] user-text afternoon NO for {date_s}")
            return "no"
    except Exception as e:
        blog(_TAG, f"user-text afternoon apply failed: {e}")
        print(f"[workday] user-text afternoon apply failed: {e}")
    return None

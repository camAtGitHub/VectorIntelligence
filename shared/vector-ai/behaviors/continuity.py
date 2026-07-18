from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .types import WorkdayMode


@dataclass
class WorkdayRecord:
    date: str
    mode: WorkdayMode = WorkdayMode.OFF
    primary_face_id: Optional[int] = None
    primary_face_name: str = ""
    started_at: float = 0.0
    arm_source: str = ""  # morning | late_yes
    last_poke_at: float = 0.0
    absence_started_at: float = 0.0
    absence_count: int = 0
    total_away_s: float = 0.0
    late_check_asked_at: float = 0.0
    pause_until: float = 0.0
    notes: str = ""
    # Transient: previous armed mode when paused (not always persisted older DBs)
    paused_from: str = ""
    away_scold_spoken: bool = False
    reid_pending: bool = False
    # After late no/timeout: stay silent rest of day (no re-ask).
    late_check_done: bool = False
    # Cooldown after stranger/non-primary at a juncture (epoch when next probe ok).
    identity_reject_until: float = 0.0


class ContinuityStore:
    """Per-date work-day record in SQLite (survives restarts).

    Individual load/save use a lock. For multi-step FSM updates (load → mutate
    → save) callers should use ``mutate`` or hold ``fsm_lock``.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Cross-request FSM critical section (clock vs tick vs chat commands).
        self.fsm_lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS workday_days (
                    date TEXT PRIMARY KEY,
                    mode TEXT,
                    primary_face_id INTEGER,
                    primary_face_name TEXT,
                    started_at REAL,
                    arm_source TEXT,
                    last_poke_at REAL,
                    absence_started_at REAL,
                    absence_count INTEGER,
                    total_away_s REAL,
                    late_check_asked_at REAL,
                    pause_until REAL,
                    notes TEXT,
                    paused_from TEXT,
                    away_scold_spoken INTEGER,
                    reid_pending INTEGER,
                    late_check_done INTEGER,
                    identity_reject_until REAL
                )
                """
            )
            cols = {r["name"] for r in c.execute("PRAGMA table_info(workday_days)").fetchall()}
            for col, decl in (
                ("primary_face_name", "TEXT"),
                ("paused_from", "TEXT"),
                ("away_scold_spoken", "INTEGER"),
                ("reid_pending", "INTEGER"),
                ("late_check_done", "INTEGER"),
                ("identity_reject_until", "REAL"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE workday_days ADD COLUMN {col} {decl}")

    def _row_to_record(self, date: str, row: sqlite3.Row) -> WorkdayRecord:
        try:
            mode = WorkdayMode(row["mode"] or WorkdayMode.WAITING_MORNING.value)
        except ValueError:
            mode = WorkdayMode.WAITING_MORNING
        return WorkdayRecord(
            date=date,
            mode=mode,
            primary_face_id=row["primary_face_id"],
            primary_face_name=row["primary_face_name"] or "",
            started_at=float(row["started_at"] or 0.0),
            arm_source=row["arm_source"] or "",
            last_poke_at=float(row["last_poke_at"] or 0.0),
            absence_started_at=float(row["absence_started_at"] or 0.0),
            absence_count=int(row["absence_count"] or 0),
            total_away_s=float(row["total_away_s"] or 0.0),
            late_check_asked_at=float(row["late_check_asked_at"] or 0.0),
            pause_until=float(row["pause_until"] or 0.0),
            notes=row["notes"] or "",
            paused_from=row["paused_from"] or "",
            away_scold_spoken=bool(row["away_scold_spoken"] or 0),
            reid_pending=bool(row["reid_pending"] or 0),
            late_check_done=bool(row["late_check_done"] or 0),
            identity_reject_until=float(row["identity_reject_until"] or 0.0),
        )

    def load_workday(self, date: str) -> WorkdayRecord:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM workday_days WHERE date = ?", (date,)
            ).fetchone()
        if not row:
            return WorkdayRecord(date=date, mode=WorkdayMode.WAITING_MORNING)
        return self._row_to_record(date, row)

    def save_workday(self, rec: WorkdayRecord) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO workday_days (
                    date, mode, primary_face_id, primary_face_name, started_at,
                    arm_source, last_poke_at, absence_started_at, absence_count,
                    total_away_s, late_check_asked_at, pause_until, notes,
                    paused_from, away_scold_spoken, reid_pending,
                    late_check_done, identity_reject_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    mode=excluded.mode,
                    primary_face_id=excluded.primary_face_id,
                    primary_face_name=excluded.primary_face_name,
                    started_at=excluded.started_at,
                    arm_source=excluded.arm_source,
                    last_poke_at=excluded.last_poke_at,
                    absence_started_at=excluded.absence_started_at,
                    absence_count=excluded.absence_count,
                    total_away_s=excluded.total_away_s,
                    late_check_asked_at=excluded.late_check_asked_at,
                    pause_until=excluded.pause_until,
                    notes=excluded.notes,
                    paused_from=excluded.paused_from,
                    away_scold_spoken=excluded.away_scold_spoken,
                    reid_pending=excluded.reid_pending,
                    late_check_done=excluded.late_check_done,
                    identity_reject_until=excluded.identity_reject_until
                """,
                (
                    rec.date,
                    rec.mode.value if isinstance(rec.mode, WorkdayMode) else str(rec.mode),
                    rec.primary_face_id,
                    rec.primary_face_name,
                    rec.started_at,
                    rec.arm_source,
                    rec.last_poke_at,
                    rec.absence_started_at,
                    rec.absence_count,
                    rec.total_away_s,
                    rec.late_check_asked_at,
                    rec.pause_until,
                    rec.notes,
                    rec.paused_from,
                    1 if rec.away_scold_spoken else 0,
                    1 if rec.reid_pending else 0,
                    1 if rec.late_check_done else 0,
                    rec.identity_reject_until,
                ),
            )

    def mutate(self, date: str, fn: Callable[[WorkdayRecord], Optional[WorkdayRecord]]) -> WorkdayRecord:
        """Atomic load → mutate → save under fsm_lock + db lock path."""
        with self.fsm_lock:
            rec = self.load_workday(date)
            out = fn(rec)
            if out is not None:
                self.save_workday(out)
                return out
            return rec

    def day_strip(self, date: str) -> str:
        """One short English line for chat injection."""
        rec = self.load_workday(date)
        mode = rec.mode
        if mode == WorkdayMode.OFF:
            return "Work day mode is off for today."
        if mode == WorkdayMode.WAITING_MORNING:
            return "Work day: still waiting for morning desk arrival."
        if mode == WorkdayMode.NO_SHOW:
            if rec.late_check_done:
                return "Work day: no morning start; afternoon work declined or timed out."
            return "Work day: no morning start detected; quiet until afternoon arm."
        if mode == WorkdayMode.LATE_CHECK:
            return "Work day: asked if user is working this afternoon (awaiting answer)."
        if mode == WorkdayMode.PAUSED:
            return "Work day: paused (user took a break from accountability)."
        bits = []
        if mode == WorkdayMode.WORKING:
            bits.append("working" + (" since morning" if rec.arm_source == "morning" else ""))
        elif mode == WorkdayMode.LATE_WORKING:
            bits.append("late-armed afternoon work")
        else:
            bits.append(mode.value)
        if rec.absence_count:
            bits.append(f"{rec.absence_count} long absence(s)")
        if rec.total_away_s >= 60:
            mins = int(rec.total_away_s // 60)
            bits.append(f"~{mins}m total away")
        if rec.arm_source == "late_yes":
            bits.append("armed after late check")
        return "Work day: " + "; ".join(bits) + "."

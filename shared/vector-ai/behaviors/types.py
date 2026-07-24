from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol


class WorkdayMode(str, Enum):
    OFF = "off"
    WAITING_MORNING = "waiting_morning"
    WORKING = "working"
    NO_SHOW = "no_show"
    LATE_CHECK = "late_check"
    LATE_WORKING = "late_working"
    PAUSED = "paused"


@dataclass
class FaceIdentity:
    face_id: int
    name: str
    is_stranger: bool = False


@dataclass
class PresenceSnapshot:
    """Shared sensor view. occupied is cheap; face is juncture-only."""
    occupied: bool = False
    face: Optional[FaceIdentity] = None
    face_ts: float = 0.0
    image_b64: Optional[str] = None
    image_ts: float = 0.0
    on_charger: bool = False
    voice_recent: bool = False
    # Heartbeat: last ingest/sensor write (refreshes every tick — not for dwell).
    updated_at: float = 0.0
    # Continuous occupancy session start (empty→occupied only; for dwell gates).
    session_started_at: float = 0.0


@dataclass
class SpeechRequest:
    text: str
    priority: int
    behavior_id: str
    reason: str = ""


@dataclass
class TickResult:
    speak: str = ""
    need_identity: bool = False
    debug: dict[str, Any] = field(default_factory=dict)
    # Applied only after speech arbiter allows the speak line (speech-gated
    # side effects: last_poke_at, away scold counters, late_check entry).
    on_speak_allowed: Optional[Callable[[], None]] = None


@dataclass
class BehaviorContext:
    now: float
    local_dt: Any  # datetime
    presence: PresenceSnapshot
    quiet: bool
    config: Any  # WorkdayConfig or RuntimeConfig subset
    identity_fresh: bool = False
    # Epoch of last user chat / voice (for quiet-dwell). 0 = never.
    last_user_voice_at: float = 0.0


class Behavior(Protocol):
    id: str
    priority: int

    def enabled(self) -> bool: ...
    def tick(self, ctx: BehaviorContext) -> TickResult: ...

    # Optional observability (duck-typed by runtime/routes — not required here):
    #   def status_summary(self, now: float) -> str: ...
    #   def status(self, now: float, **kwargs) -> dict: ...
    # Prefer implementing both on new FSMs. Runtime uses getattr, not Protocol.

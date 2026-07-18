from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol


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
    updated_at: float = 0.0


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


@dataclass
class BehaviorContext:
    now: float
    local_dt: Any  # datetime
    presence: PresenceSnapshot
    quiet: bool
    config: Any  # WorkdayConfig or RuntimeConfig subset
    identity_fresh: bool = False


class Behavior(Protocol):
    id: str
    priority: int

    def enabled(self) -> bool: ...
    def tick(self, ctx: BehaviorContext) -> TickResult: ...

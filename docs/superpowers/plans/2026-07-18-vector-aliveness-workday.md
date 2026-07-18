# Work Day Mode + Multi-Behavior Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship toggleable Work Day Mode (morning arm, 90m on-task pokes, 30m away scolds, late-arrival arm) with continuity-of-self, on a multi-behavior runtime that shares occupancy/identity cache and arbitrates speech—without per-tick named-face probes.

**Architecture:** New modules under `shared/vector-ai/behaviors/` own the runtime, presence cache, speech arbiter, continuity day record, and `WorkDayBehavior`. `service.py` wires HTTP tick, chat commands, day-strip injection, and quiet/voice hooks. Chipper gets a thin presence-tick loop (patch) that POSTs occupancy every tick and face only when `need_identity` is true.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite (existing `MemoryStore` patterns), pytest-style standalone tests (same as `shared/test_supervisor_wedge.py`), Go patch scripts for Wire-Pod.

**Spec:** `docs/superpowers/specs/2026-07-18-vector-aliveness-workday-design.md`

---

## File map

| Path | Responsibility |
|------|----------------|
| `shared/vector-ai/behaviors/__init__.py` | Package exports |
| `shared/vector-ai/behaviors/config.py` | Env/config parsing for runtime + workday |
| `shared/vector-ai/behaviors/presence.py` | `PresenceSnapshot`, occupancy/identity cache |
| `shared/vector-ai/behaviors/arbiter.py` | Global proactive speech gate |
| `shared/vector-ai/behaviors/runtime.py` | Register behaviors, `tick()`, clock transitions |
| `shared/vector-ai/behaviors/types.py` | `Behavior`, `TickResult`, `SpeechRequest`, modes |
| `shared/vector-ai/behaviors/continuity.py` | Per-date work-day record in SQLite |
| `shared/vector-ai/behaviors/workday.py` | Work Day state machine + template lines |
| `shared/vector-ai/service.py` | Mount endpoints; inject day strip; parse work commands |
| `shared/vector-ai/env-default` | Documented knobs, `WORKDAY_ENABLED=0` |
| `shared/vector-ai/test_behaviors.py` | Unit tests for runtime + workday |
| `shared/patches/add-behavior-tick.py` | Go loop: occupancy tick + optional face + speak |
| `linux/install.sh`, `windows/install.ps1` | Apply new patch after ambient |
| `README.md` / `NEXT_STEPS.md` | Ops cheatsheet for workday |

**Do not** put the full FSM inside `service.py` (already ~1900 lines). Keep service as wiring only.

---

### Task 1: Types + config

**Files:**
- Create: `shared/vector-ai/behaviors/__init__.py`
- Create: `shared/vector-ai/behaviors/types.py`
- Create: `shared/vector-ai/behaviors/config.py`
- Create: `shared/vector-ai/test_behaviors.py`

- [ ] **Step 1: Write failing config tests**

Create `shared/vector-ai/test_behaviors.py`:

```python
#!/usr/bin/env python3
"""Unit tests for behaviors runtime + Work Day Mode.

Run from shared/vector-ai:
  python test_behaviors.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from behaviors.config import WorkdayConfig, load_workday_config, parse_hhmm


def check(name: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        raise SystemExit(1)


def test_parse_hhmm() -> None:
    assert parse_hhmm("09:00") == (9, 0)
    assert parse_hhmm("18:00") == (18, 0)
    try:
        parse_hhmm("9")
        raise AssertionError("should fail")
    except ValueError:
        pass


def test_load_workday_disabled_by_default() -> None:
    env = {}
    cfg = load_workday_config(env)
    check("default disabled", cfg.enabled is False)
    check("default poke 5400", cfg.poke_interval_s == 5400)
    check("default away 1800", cfg.away_s == 1800)


def test_load_workday_enabled() -> None:
    env = {
        "WORKDAY_ENABLED": "1",
        "WORKDAY_START_BEGIN": "09:00",
        "WORKDAY_START_END": "10:30",
        "WORKDAY_END": "18:00",
        "WORKDAY_TZ": "UTC",
    }
    cfg = load_workday_config(env)
    check("enabled", cfg.enabled is True)
    check("tz UTC", str(cfg.tz) == "UTC")


if __name__ == "__main__":
    print("test_behaviors (partial)")
    test_parse_hhmm()
    test_load_workday_disabled_by_default()
    test_load_workday_enabled()
    print("OK so far")
```

- [ ] **Step 2: Run tests — expect fail (module missing)**

```bash
cd shared/vector-ai && python test_behaviors.py
```

Expected: `ModuleNotFoundError: No module named 'behaviors'`

- [ ] **Step 3: Implement types + config**

`shared/vector-ai/behaviors/__init__.py`:

```python
"""Multi-behavior runtime for Vector aliveness (Work Day first)."""
from .runtime import BehaviorRuntime
from .config import load_workday_config, load_runtime_config

__all__ = ["BehaviorRuntime", "load_workday_config", "load_runtime_config"]
```

`shared/vector-ai/behaviors/types.py`:

```python
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


class Behavior(Protocol):
    id: str
    priority: int

    def enabled(self) -> bool: ...
    def tick(self, ctx: BehaviorContext) -> TickResult: ...
```

`shared/vector-ai/behaviors/config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from zoneinfo import ZoneInfo


def parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time {s!r}")
    return h, m


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class RuntimeConfig:
    face_cache_max_age_s: int = 120
    image_cache_max_age_s: int = 45
    speech_min_gap_s: int = 90
    speech_suppress_after_voice_s: int = 120
    behaviors_enabled: tuple[str, ...] = ("workday",)


@dataclass(frozen=True)
class WorkdayConfig:
    enabled: bool = False
    tz: ZoneInfo = ZoneInfo("UTC")
    start_begin: tuple[int, int] = (9, 0)
    start_end: tuple[int, int] = (10, 30)
    away_window_begin: tuple[int, int] = (9, 30)
    end: tuple[int, int] = (18, 0)
    poke_interval_s: int = 5400
    away_s: int = 1800
    late_check_timeout_s: int = 900
    reid_after_away_s: int = 3600  # 0 = never re-ID
    priority: int = 80


def load_runtime_config(env: Optional[Mapping[str, str]] = None) -> RuntimeConfig:
    env = env if env is not None else os.environ
    raw = (env.get("BEHAVIORS_ENABLED") or "workday").strip()
    behaviors = tuple(b.strip() for b in raw.split(",") if b.strip())
    return RuntimeConfig(
        face_cache_max_age_s=_int(env, "FACE_CACHE_MAX_AGE_S", 120),
        image_cache_max_age_s=_int(env, "IMAGE_CACHE_MAX_AGE_S", 45),
        speech_min_gap_s=_int(env, "SPEECH_MIN_GAP_S", 90),
        speech_suppress_after_voice_s=_int(env, "SPEECH_SUPPRESS_AFTER_VOICE_S", 120),
        behaviors_enabled=behaviors or ("workday",),
    )


def load_workday_config(env: Optional[Mapping[str, str]] = None) -> WorkdayConfig:
    env = env if env is not None else os.environ
    tz_name = (env.get("WORKDAY_TZ") or env.get("TZ") or "UTC").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return WorkdayConfig(
        enabled=_truthy(env.get("WORKDAY_ENABLED")),
        tz=tz,
        start_begin=parse_hhmm(env.get("WORKDAY_START_BEGIN") or "09:00"),
        start_end=parse_hhmm(env.get("WORKDAY_START_END") or "10:30"),
        away_window_begin=parse_hhmm(env.get("WORKDAY_AWAY_WINDOW_BEGIN") or "09:30"),
        end=parse_hhmm(env.get("WORKDAY_END") or "18:00"),
        poke_interval_s=_int(env, "WORKDAY_POKE_INTERVAL_S", 5400),
        away_s=_int(env, "WORKDAY_AWAY_S", 1800),
        late_check_timeout_s=_int(env, "WORKDAY_LATE_CHECK_TIMEOUT_S", 900),
        reid_after_away_s=_int(env, "WORKDAY_REID_AFTER_AWAY_S", 3600),
        priority=_int(env, "WORKDAY_PRIORITY", 80),
    )


def minutes_since_midnight(h: int, m: int) -> int:
    return h * 60 + m
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd shared/vector-ai && python test_behaviors.py
```

Expected: `OK so far`

- [ ] **Step 5: Commit**

```bash
git add shared/vector-ai/behaviors shared/vector-ai/test_behaviors.py
git commit -m "feat(behaviors): add types and workday config loader"
```

---

### Task 2: Presence cache + speech arbiter

**Files:**
- Create: `shared/vector-ai/behaviors/presence.py`
- Create: `shared/vector-ai/behaviors/arbiter.py`
- Modify: `shared/vector-ai/test_behaviors.py`

- [ ] **Step 1: Add failing tests for presence + arbiter**

Append to `test_behaviors.py`:

```python
from behaviors.presence import PresenceCache
from behaviors.arbiter import SpeechArbiter
from behaviors.types import FaceIdentity, SpeechRequest


def test_presence_occupancy_without_face() -> None:
    cache = PresenceCache(face_max_age_s=120, image_max_age_s=45)
    snap = cache.update(now=1000.0, occupied=True, face=None)
    check("occupied true", snap.occupied is True)
    check("no face", snap.face is None)
    check("identity not fresh", cache.identity_fresh(1000.0) is False)


def test_presence_identity_cached() -> None:
    cache = PresenceCache(face_max_age_s=120, image_max_age_s=45)
    face = FaceIdentity(face_id=1, name="Cam", is_stranger=False)
    cache.update(now=1000.0, occupied=True, face=face)
    check("identity fresh at 1000", cache.identity_fresh(1000.0) is True)
    check("identity fresh at 1119", cache.identity_fresh(1119.0) is True)
    check("identity stale at 1121", cache.identity_fresh(1121.0) is False)


def test_arbiter_min_gap_and_quiet() -> None:
    arb = SpeechArbiter(min_gap_s=90, suppress_after_voice_s=120)
    req = SpeechRequest(text="hi", priority=80, behavior_id="workday")
    ok, why = arb.allow(req, now=1000.0, quiet=True, voice_recent_ts=0.0)
    check("quiet blocks", ok is False)
    ok, why = arb.allow(req, now=1000.0, quiet=False, voice_recent_ts=0.0)
    check("first allow", ok is True)
    arb.record_speech(1000.0)
    ok, why = arb.allow(req, now=1050.0, quiet=False, voice_recent_ts=0.0)
    check("gap blocks", ok is False)
    ok, why = arb.allow(req, now=1091.0, quiet=False, voice_recent_ts=0.0)
    check("after gap allow", ok is True)
```

Call these from `__main__`.

- [ ] **Step 2: Run — expect import fail**

```bash
cd shared/vector-ai && python test_behaviors.py
```

- [ ] **Step 3: Implement presence.py and arbiter.py**

`presence.py`:

```python
from __future__ import annotations

from typing import Optional

from .types import FaceIdentity, PresenceSnapshot


class PresenceCache:
    def __init__(self, face_max_age_s: int = 120, image_max_age_s: int = 45):
        self.face_max_age_s = face_max_age_s
        self.image_max_age_s = image_max_age_s
        self._snap = PresenceSnapshot()

    @property
    def snapshot(self) -> PresenceSnapshot:
        return self._snap

    def update(
        self,
        now: float,
        occupied: bool,
        face: Optional[FaceIdentity] = None,
        image_b64: Optional[str] = None,
        on_charger: bool = False,
        voice_recent: bool = False,
    ) -> PresenceSnapshot:
        s = self._snap
        s.occupied = bool(occupied)
        s.on_charger = bool(on_charger)
        s.voice_recent = bool(voice_recent)
        s.updated_at = now
        if face is not None:
            s.face = face
            s.face_ts = now
        if image_b64 is not None:
            s.image_b64 = image_b64
            s.image_ts = now
        return s

    def identity_fresh(self, now: float) -> bool:
        if self._snap.face is None or self._snap.face_ts <= 0:
            return False
        return (now - self._snap.face_ts) <= self.face_max_age_s

    def image_fresh(self, now: float) -> bool:
        if not self._snap.image_b64 or self._snap.image_ts <= 0:
            return False
        return (now - self._snap.image_ts) <= self.image_max_age_s

    def effective_face(self, now: float) -> Optional[FaceIdentity]:
        return self._snap.face if self.identity_fresh(now) else None
```

`arbiter.py`:

```python
from __future__ import annotations

from typing import Optional, Tuple

from .types import SpeechRequest


class SpeechArbiter:
    def __init__(self, min_gap_s: int = 90, suppress_after_voice_s: int = 120):
        self.min_gap_s = min_gap_s
        self.suppress_after_voice_s = suppress_after_voice_s
        self._last_speech_at: float = 0.0

    def allow(
        self,
        req: SpeechRequest,
        now: float,
        quiet: bool,
        voice_recent_ts: float,
    ) -> Tuple[bool, str]:
        if not (req.text or "").strip():
            return False, "empty"
        if quiet:
            return False, "quiet"
        if voice_recent_ts > 0 and (now - voice_recent_ts) < self.suppress_after_voice_s:
            return False, "recent_voice"
        if self._last_speech_at > 0 and (now - self._last_speech_at) < self.min_gap_s:
            return False, "min_gap"
        return True, "ok"

    def record_speech(self, now: float) -> None:
        self._last_speech_at = now
```

- [ ] **Step 4: Run tests — pass**

```bash
cd shared/vector-ai && python test_behaviors.py
```

- [ ] **Step 5: Commit**

```bash
git add shared/vector-ai/behaviors shared/vector-ai/test_behaviors.py
git commit -m "feat(behaviors): presence cache and speech arbiter"
```

---

### Task 3: Continuity store (work-day record)

**Files:**
- Create: `shared/vector-ai/behaviors/continuity.py`
- Modify: `shared/vector-ai/test_behaviors.py`

- [ ] **Step 1: Failing tests for load/save day record**

```python
from behaviors.continuity import ContinuityStore, WorkdayRecord
from behaviors.types import WorkdayMode


def test_continuity_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ContinuityStore(Path(td) / "workday.db")
        rec = WorkdayRecord(
            date="2026-07-18",
            mode=WorkdayMode.WORKING,
            primary_face_id=1,
            started_at=1000.0,
            arm_source="morning",
        )
        store.save_workday(rec)
        loaded = store.load_workday("2026-07-18")
        check("loaded mode", loaded.mode == WorkdayMode.WORKING)
        check("face id", loaded.primary_face_id == 1)
        check("day strip non-empty", "working" in store.day_strip("2026-07-18").lower())
```

- [ ] **Step 2: Implement continuity.py**

Use SQLite with a single table `workday_days` (JSON blob or columns). Prefer explicit columns matching the spec:

`date TEXT PRIMARY KEY, mode TEXT, primary_face_id INT, started_at REAL, arm_source TEXT, last_poke_at REAL, absence_started_at REAL, absence_count INT, total_away_s REAL, late_check_asked_at REAL, pause_until REAL, notes TEXT`

Implement:

- `load_workday(date: str) -> WorkdayRecord` (defaults for missing day)
- `save_workday(rec: WorkdayRecord) -> None`
- `day_strip(date: str) -> str` — one short English line for chat injection

- [ ] **Step 3: Run tests — pass**

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(behaviors): workday continuity SQLite store"
```

---

### Task 4: WorkDayBehavior state machine (core)

**Files:**
- Create: `shared/vector-ai/behaviors/workday.py`
- Modify: `shared/vector-ai/test_behaviors.py`

**Critical rules from spec:**

- Identity required only: morning start, late_check entry, optional re-ID after long away.
- Occupancy drives away/return while armed.
- No morning start → `no_show` → no pokes until late yes.
- Templates only for lines (no LLM in v1).

- [ ] **Step 1: Write transition tests (frozen clock)**

Implement tests that construct `WorkDayBehavior` with:

- `WorkdayConfig(enabled=True, tz=ZoneInfo("UTC"), ...)`
- In-memory or temp `ContinuityStore`
- Fake `now` / `local_dt` via injectable `clock` function **or** pass `BehaviorContext` with fixed times

Minimum cases:

| Case | Setup | Tick inputs | Expect |
|------|--------|-------------|--------|
| disabled | enabled=False | occupied | mode off, no speak, no need_identity |
| morning start | 09:15, waiting | occupied + face Cam | working, started_at set |
| morning no face | 09:15 | occupied, no face | need_identity True OR stay waiting (if identity not fresh → need_identity) |
| no_show | 10:31, never started | — | mode no_show, no speak |
| late check | no_show, 13:00 | occupied + face | late_check, speak question, need_identity was used |
| late yes | late_check | `on_user_afternoon(True)` | late_working |
| poke interval | working since 09:15, now 10:46 | occupied | speak on-task (if arbiter would allow — test behavior intent before arbiter) |
| away 29m | working, unoccupied 29m | | no away speak |
| away 30m | unoccupied 30m in window | | away speak once |
| pause | working + pause_until future | | no pokes |

**Design note for implementation:** Split:

- `WorkDayBehavior.plan(ctx) -> TickResult` — raw desire (speak text, need_identity)
- Runtime applies arbiter later

So unit tests assert `plan()` without quiet/gap.

Example test skeleton:

```python
def _ctx(behavior, occupied, face, hour, minute, now_base=1_800_000_000.0):
    from datetime import datetime
    from behaviors.types import BehaviorContext, PresenceSnapshot, FaceIdentity
    local = datetime(2026, 7, 18, hour, minute, tzinfo=behavior.cfg.tz)
    # convert local to epoch carefully or inject local_dt only
    ...
```

Prefer injecting `local_dt` on context and using `now` as monotonic epoch for intervals; set `started_at` relative to `now`.

- [ ] **Step 2: Implement workday.py**

Core structure:

```python
class WorkDayBehavior:
    id = "workday"
    priority = 80  # from config

    def __init__(self, cfg: WorkdayConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store

    def enabled(self) -> bool:
        return self.cfg.enabled

    def tick(self, ctx: BehaviorContext) -> TickResult:
        ...

    def on_afternoon_yes(self, local_date: str) -> None: ...
    def on_afternoon_no(self, local_date: str) -> None: ...
    def on_pause(self, local_date: str, until_ts: float) -> None: ...
    def on_resume(self, local_date: str) -> None: ...
```

Helper methods:

- `_in_window(local_dt, begin, end) -> bool` (handle same-day windows only for v1)
- `_line_on_task(rec) -> str`
- `_line_away(rec) -> str`
- `_line_late_check(rec) -> str`

Template examples (persona-neutral dry tone; persona lives in chat separately):

- On-task: `"Ninety minutes. Still on the work, or inventing a new sideline?"`
- Away: `"You've been gone a while. Shouldn't you be working?"`
- Late: `"Didn't see you this morning. What happened — working this afternoon?"`

Tint with continuity when `absence_count >= 2` etc.

- [ ] **Step 3: Run full unit suite — pass**

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(behaviors): WorkDayBehavior state machine and templates"
```

---

### Task 5: BehaviorRuntime + HTTP tick endpoint

**Files:**
- Create: `shared/vector-ai/behaviors/runtime.py`
- Modify: `shared/vector-ai/service.py` (imports + routes only)
- Modify: `shared/vector-ai/test_behaviors.py`

- [ ] **Step 1: Runtime unit test**

```python
def test_runtime_picks_highest_priority_and_sets_need_identity():
    # mock two behaviors; one need_identity; arbiter allows
    ...
```

- [ ] **Step 2: Implement BehaviorRuntime**

```python
class BehaviorRuntime:
    def __init__(self, runtime_cfg, workday_cfg, store: ContinuityStore, quiet_fn, voice_ts_fn):
        self.presence = PresenceCache(...)
        self.arbiter = SpeechArbiter(...)
        self.behaviors: list[Behavior] = []
        if "workday" in runtime_cfg.behaviors_enabled and workday_cfg.enabled:
            self.behaviors.append(WorkDayBehavior(workday_cfg, store))

    def ingest_tick_payload(self, now, occupied, face_dict, ...) -> PresenceSnapshot:
        ...

    def tick(self, now: float) -> TickResult:
        # build ctx, collect TickResults, merge need_identity OR
        # pick highest-priority non-empty speak that arbiter allows
        ...
```

- [ ] **Step 3: Wire service.py**

Near startup (after MEMORY init):

```python
from behaviors.runtime import BehaviorRuntime
from behaviors.config import load_runtime_config, load_workday_config
from behaviors.continuity import ContinuityStore

_runtime_cfg = load_runtime_config()
_workday_cfg = load_workday_config()
_continuity = ContinuityStore(Path(__file__).resolve().parent / "workday.db")
# quiet_fn reads _ambient_state["quiet"]
# voice_ts_fn: track last chat completion time in a module global updated in generate()

BEHAVIOR_RUNTIME = BehaviorRuntime(...)
```

Add models + route:

```python
class BehaviorTickRequest(BaseModel):
    occupied: bool = False
    face: Optional[dict] = None  # {face_id, name, is_stranger}
    on_charger: bool = False
    voice_recent: bool = False

@app.post("/v1/behaviors/tick")
async def behaviors_tick(req: BehaviorTickRequest):
    now = time.time()
    BEHAVIOR_RUNTIME.ingest(...)
    result = BEHAVIOR_RUNTIME.tick(now)
    return {
        "speak": result.speak,
        "need_identity": result.need_identity,
        "debug": result.debug if DEBUG else {},
    }
```

Also add lightweight `@app.on_event("startup")` asyncio task: every 60s call `BEHAVIOR_RUNTIME.clock_tick(now)` so `waiting_morning` → `no_show` without chipper (spec §7.3).

- [ ] **Step 4: Manual curl smoke (optional if service not running)**

```bash
curl -s -X POST http://127.0.0.1:8090/v1/behaviors/tick \
  -H 'Content-Type: application/json' \
  -d '{"occupied":true}'
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(vector-ai): BehaviorRuntime and /v1/behaviors/tick"
```

---

### Task 6: Chat integration (day strip + commands)

**Files:**
- Modify: `shared/vector-ai/service.py` — `prepare_messages`, `extract_memory_commands` or new parser
- Modify: `shared/vector-ai/test_behaviors.py` for command parsing helpers (prefer pure functions in `workday.py` or `commands.py`)

- [ ] **Step 1: Tests for command parse**

```python
from behaviors.workday import parse_work_commands

def test_parse_work_commands():
    text, actions = parse_work_commands("Sure {{workAfternoon||yes}}")
    check("yes action", actions == [("afternoon", "yes")])
    text, actions = parse_work_commands("{{workPause||until=14:00}} ok")
    check("pause", actions[0][0] == "pause")
```

Supported tags (v1):

- `{{workAfternoon||yes}}` / `{{workAfternoon||no}}`
- `{{workPause||until=HH:MM}}`
- `{{workResume}}`

- [ ] **Step 2: Implement parse + apply in generate path after extract_memory_commands**

- Inject day strip into `_build_context_note` or `prepare_messages`:

```python
strip = _continuity.day_strip(local_date)
if strip:
    context_note += f"\n\nWork day: {strip}"
```

- Update `_last_user_voice_ts = time.time()` on each chat generate for arbiter.

- [ ] **Step 3: Persona hint (optional one line in persona.txt or system addendum)**

Document for LLM: when user answers afternoon work / break, emit the `{{work…}}` tags. Keep small.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(vector-ai): workday chat commands and day-strip context"
```

---

### Task 7: env-default + docs

**Files:**
- Modify: `shared/vector-ai/env-default`
- Modify: `README.md` (config cheatsheet section)
- Modify: `NEXT_STEPS.md` (optional short “Work Day Mode” blurb)

- [ ] **Step 1: Append to env-default**

```env
# -- Work Day Mode (accountability; default OFF) -------------------------------
# Requires full patched chipper with behavior-tick loop.
WORKDAY_ENABLED=0
# WORKDAY_TZ=Australia/Sydney
# WORKDAY_START_BEGIN=09:00
# WORKDAY_START_END=10:30
# WORKDAY_AWAY_WINDOW_BEGIN=09:30
# WORKDAY_END=18:00
# WORKDAY_POKE_INTERVAL_S=5400
# WORKDAY_AWAY_S=1800
# WORKDAY_LATE_CHECK_TIMEOUT_S=900
# WORKDAY_REID_AFTER_AWAY_S=3600
# FACE_CACHE_MAX_AGE_S=120
# SPEECH_MIN_GAP_S=90
# SPEECH_SUPPRESS_AFTER_VOICE_S=120
# BEHAVIORS_ENABLED=workday
```

- [ ] **Step 2: README config rows for the above**

- [ ] **Step 3: Commit**

```bash
git commit -am "docs: Work Day Mode config (default off)"
```

---

### Task 8: Chipper patch `add-behavior-tick.py`

**Files:**
- Create: `shared/patches/add-behavior-tick.py`
- Modify: `linux/install.sh` (call after `add-ambient-loop.py`)
- Modify: `windows/install.ps1` (same order)

Mirror structure of `add-ambient-loop.py`: write `behavior_tick.go`, patch `startserver.go` with `go ttr.StartBehaviorTickLoop()`.

**Go loop responsibilities (v1 occupancy heuristic):**

Without a separate person detector, v1 occupancy can be:

1. `occupied = true` if last successful face sighting (any enrolled) within last N minutes **OR**
2. If `need_identity` just succeeded with primary → occupied true  
3. **Preferred cheap signal:** treat `occupied` as sticky: once identified at juncture, remain occupied until a **dedicated empty check** fails.

**Pragmatic v1 approach for plan implementers:**

- Each tick (default 60s): try a **very short** (2–3s) `robot_observed_face` **only if** runtime last response had `need_identity`, else:
  - Send `occupied: true` if `time.Since(lastSeenAnyFace) < 15*time.Minute` (face events from greeting/sensor notify or last ID)
  - Else `occupied: false`
- Track `lastSeenAnyFace` when any face event is known (hook: if sensor/greeting already notifies face, reuse; else optional short probe every **5 minutes** only for occupancy “still someone?” — **not every tick full ID**).

Document limitation: occupancy is approximate; long facing-away may look “away.”

Speak path: reuse `ambientReact(robot, line, false)` or `sayText`.

On vector-ai HTTP failure: log and do not speak.

- [ ] **Step 1: Implement patch script with SENTINEL `StartBehaviorTickLoop`**

- [ ] **Step 2: Wire install.sh + install.ps1**

```bash
sudo python3 "$SHARED_DIR/patches/add-behavior-tick.py" "$WIREPOD_DIR"
```

- [ ] **Step 3: Commit**

```bash
git add shared/patches/add-behavior-tick.py linux/install.sh windows/install.ps1
git commit -m "feat(chipper): behavior tick loop patch for workday presence"
```

---

### Task 9: Simulated full-day integration test

**Files:**
- Modify: `shared/vector-ai/test_behaviors.py`

- [ ] **Step 1: Scripted day**

```python
def test_simulated_workday():
    """
    09:15 identify Cam -> working
    10:46 poke
    unoccupied 30m -> away line
    occupied again -> clear
    18:01 -> no more pokes
    """
```

Drive `WorkDayBehavior` / `BehaviorRuntime` with synthetic times only (no HTTP).

- [ ] **Step 2: Run**

```bash
cd shared/vector-ai && python test_behaviors.py
```

Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git commit -am "test(behaviors): simulated workday integration"
```

---

### Task 10: Manual verification checklist (human)

- [ ] Full install or apply patch + rebuild chipper (if testing on robot)
- [ ] Set `WORKDAY_ENABLED=1` and correct `WORKDAY_TZ` in vector-ai `.env`
- [ ] Restart vector-ai / supervisor
- [ ] Confirm holiday: `WORKDAY_ENABLED=0` → no workday speaks
- [ ] Morning: Vector sees you 9–10:30 → day arms (log line)
- [ ] ~90m later → on-task line (if quiet off and not mid-chat)
- [ ] Leave 30m+ during day → away line once
- [ ] Next day no morning face → silent; afternoon appear → late question; say yes → afternoon pokes

---

## Spec coverage checklist

| Spec item | Task |
|-----------|------|
| Multi-behavior runtime | 5 |
| Presence occupancy vs identity junctures | 2, 4, 8 |
| Speech arbiter / quiet / voice suppress | 2, 5, 6 |
| Work day modes + transitions | 4 |
| 90m poke / 30m away / late arm | 4, 9 |
| Continuity + day strip | 3, 6 |
| Config default off | 1, 7 |
| `/v1/behaviors/tick` + need_identity | 5, 8 |
| Chat pause/yes commands | 6 |
| Chipper thin loop + install | 8 |
| Clock no_show without tick | 5 |
| Tests | 1–4, 9 |

---

## Self-review notes

- No TBD placeholders in task steps.
- Templates-first speech (no LLM dependency for pokes) keeps tests deterministic and OpenRouter cost low.
- Occupancy v1 is approximate by design; do not reintroduce per-tick face ID.
- `service.py` stays wiring-only; logic lives under `behaviors/`.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-vector-aliveness-workday.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — this session, batch with checkpoints  

Which approach?

"""Joke / question idle FSM: dwell-gated one-liners when someone is at the desk.

Pure and synchronous. No network/LLM/camera work in tick().
Daily count and cooldown commit only via on_speak_allowed (speech-gated).
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from .config import JokeConfig
from .continuity import ContinuityStore
from .joke_sources import pop_line
from .types import BehaviorContext, FaceIdentity, TickResult

JOKE_IDLE_ID = "joke_idle"


class JokeIdleBehavior:
    id = JOKE_IDLE_ID
    min_tick_interval: float = 30.0

    def __init__(self, cfg: JokeConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def tick(self, ctx: BehaviorContext) -> TickResult:
        r = TickResult()
        date = datetime.fromtimestamp(ctx.now, tz=self.cfg.tz).date().isoformat()
        daily = self.store.joke_load_daily(date)
        cfg = self.cfg

        # 3. Daily cap
        if daily["count"] >= cfg.max_per_day:
            r.debug = {"mode": "idle", "reason": "capped"}
            return r

        # 4. Cooldown since last spoke
        if ctx.now - daily["last_spoke_at"] < cfg.cooldown_s:
            r.debug = {"mode": "idle", "reason": "cooldown"}
            return r

        # 5. Desk empty
        if not ctx.presence.occupied:
            r.debug = {"mode": "idle", "reason": "empty"}
            return r

        # 6. Dwell (quiet time at desk since presence update or last spoke)
        quiet_dwell = ctx.now - max(ctx.presence.updated_at, daily["last_spoke_at"])
        if quiet_dwell < cfg.min_dwell_s:
            r.debug = {"mode": "idle", "reason": "dwell_building"}
            return r

        # Voice activity breaks quiet; bail early (arbiter also guards)
        if ctx.presence.voice_recent:
            r.debug = {"mode": "idle", "reason": "voice_recent"}
            return r

        # 7. Identity juncture
        face: Optional[FaceIdentity] = None
        if cfg.audience == "known":
            if not ctx.identity_fresh:
                if ctx.now - daily["last_reject_at"] < cfg.identity_reject_cooldown_s:
                    r.debug = {"mode": "idle", "reason": "id_reject_cooldown"}
                    return r
                r.need_identity = True
                r.debug = {"mode": "idle", "reason": "requesting_identity"}
                return r
            face = ctx.presence.face
            if face is None or face.is_stranger:
                # Committed in-body on purpose (records silence, not speech)
                self.store.joke_mark_reject(date, ctx.now)
                r.debug = {"mode": "idle", "reason": "stranger_suppressed"}
                return r
        else:
            # anyone mode: never need_identity
            face = ctx.presence.face if ctx.identity_fresh else None

        # 8. Serve a line (pure SQLite; no network)
        line = pop_line(self.store, cfg, question_ratio_roll=random.random())
        if line is None:
            r.debug = {"mode": "idle", "reason": "no_line_available"}
            return r

        text = line["text"]

        # 9. Optional personalization (best-effort; never required)
        if (
            face is not None
            and not face.is_stranger
            and (face.name or "").strip()
        ):
            name = face.name.strip()
            # Avoid double-prefix if the line already opens with the name
            if not text.lower().startswith(name.lower()):
                text = f"{name}, {text}"

        # 10–11. Speech + speech-gated daily/cooldown commit
        r.speak = text
        # Capture date/now by value so the callback is stable after return
        spoke_date, spoke_at = date, ctx.now
        r.on_speak_allowed = lambda: self.store.joke_commit_spoke(spoke_date, spoke_at)

        # 12. Debug payload for the arm that spoke
        who = face.name if face is not None and (face.name or "").strip() else "unknown"
        r.debug = {
            "mode": "idle",
            "reason": "spoke",
            "kind": line["kind"],
            "source": line["source"],
            "who": who,
        }
        return r

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
from .logutil import blog, short
from .types import BehaviorContext, FaceIdentity, TickResult

JOKE_IDLE_ID = "joke_idle"
_TAG = "joke_idle"


class JokeIdleBehavior:
    id = JOKE_IDLE_ID
    min_tick_interval: float = 30.0

    def __init__(self, cfg: JokeConfig, store: ContinuityStore):
        self.cfg = cfg
        self.store = store
        self.priority = cfg.priority

    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _ops_snapshot(
        self,
        now: float,
        *,
        presence_updated_at: float = 0.0,
        occupied: bool = False,
    ) -> dict:
        """Read-only gate/counters for status (no mutations, no LLM)."""
        date = datetime.fromtimestamp(now, tz=self.cfg.tz).date().isoformat()
        daily = self.store.joke_load_daily(date)
        cfg = self.cfg
        last_spoke = float(daily["last_spoke_at"] or 0.0)
        # Same formula as tick(): now - max(presence.updated_at, last_spoke_at)
        quiet_base = max(float(presence_updated_at or 0.0), last_spoke)
        quiet_dwell = max(0.0, now - quiet_base)

        dwell_remaining = max(0.0, float(cfg.min_dwell_s) - quiet_dwell)
        if last_spoke > 0:
            cooldown_remaining = max(0.0, float(cfg.cooldown_s) - (now - last_spoke))
        else:
            cooldown_remaining = 0.0

        try:
            queue_len = int(self.store.joke_queue_len())
        except Exception:
            queue_len = 0

        # Cheap ops gate (subset of tick). Intentionally does NOT model
        # voice_recent, identity probe, or stranger suppress — those only
        # exist on the speak path. ``idle_ready`` means dwell/cooldown/cap/
        # queue look clear for ops; tick may still skip. See FSM-jokes-at-idle.
        if daily["count"] >= cfg.max_per_day:
            reason = "capped"
        elif last_spoke > 0 and (now - last_spoke) < cfg.cooldown_s:
            reason = "cooldown"
        elif not occupied:
            reason = "empty"
        elif quiet_dwell < cfg.min_dwell_s:
            reason = "dwell_building"
        elif queue_len <= 0:
            reason = "no_line_available"
        else:
            reason = "idle_ready"

        return {
            "date": date,
            "audience": cfg.audience,
            "min_dwell_s": cfg.min_dwell_s,
            "cooldown_s": cfg.cooldown_s,
            "max_per_day": cfg.max_per_day,
            "daily_count": int(daily["count"] or 0),
            "last_spoke_at": last_spoke if last_spoke > 0 else None,
            "last_reject_at": (
                float(daily["last_reject_at"])
                if float(daily["last_reject_at"] or 0) > 0
                else None
            ),
            "quiet_dwell_s": quiet_dwell,
            "dwell_remaining_s": dwell_remaining,
            "cooldown_remaining_s": cooldown_remaining,
            "queue_len": queue_len,
            "occupied": bool(occupied),
            "presence_updated_at": float(presence_updated_at or 0.0),
            "reason": reason,
            # Ops reason is a cheap subset of tick gates (not identity/voice).
            "reason_scope": "ops_subset",
        }

    def status_summary(self, now: float, **kwargs) -> str:
        """Card summary: last-known gate reason (cheap, no tick)."""
        # Runtime may call with only now; presence bits optional → assume empty desk.
        snap = self._ops_snapshot(
            now,
            presence_updated_at=float(kwargs.get("presence_updated_at") or 0.0),
            occupied=bool(kwargs.get("occupied", False)),
        )
        return str(snap["reason"])

    def status(
        self,
        now: float,
        *,
        presence_updated_at: float = 0.0,
        occupied: bool = False,
        **_kwargs,
    ) -> dict:
        """Full joke_idle detail for GET /v1/behaviors/joke_idle."""
        snap = self._ops_snapshot(
            now,
            presence_updated_at=presence_updated_at,
            occupied=occupied,
        )
        return {
            "id": self.id,
            "schema_version": 1,
            "enabled": bool(self.cfg.enabled),
            **snap,
        }

    def tick(self, ctx: BehaviorContext) -> TickResult:
        r = TickResult()
        date = datetime.fromtimestamp(ctx.now, tz=self.cfg.tz).date().isoformat()
        daily = self.store.joke_load_daily(date)
        cfg = self.cfg

        def _skip(reason: str, *, always: bool = False, **extra) -> TickResult:
            r.debug = {"mode": "idle", "reason": reason, **extra}
            remaining = ""
            if reason == "cooldown":
                left = cfg.cooldown_s - (ctx.now - daily["last_spoke_at"])
                remaining = f" ({left:.0f}s left)"
            elif reason == "dwell_building":
                quiet_dwell = ctx.now - max(
                    ctx.presence.updated_at, daily["last_spoke_at"]
                )
                remaining = f" ({quiet_dwell:.0f}/{cfg.min_dwell_s}s)"
            elif reason == "capped":
                remaining = f" ({daily['count']}/{cfg.max_per_day})"
            blog(
                _TAG,
                f"skip: {reason}{remaining}",
                verbose=not always,
                data=extra or None,
            )
            return r

        # 3. Daily cap
        if daily["count"] >= cfg.max_per_day:
            return _skip("capped", always=True)

        # 4. Cooldown since last spoke
        if ctx.now - daily["last_spoke_at"] < cfg.cooldown_s:
            return _skip("cooldown")

        # 5. Desk empty
        if not ctx.presence.occupied:
            return _skip("empty")

        # 6. Dwell (quiet time at desk since presence update or last spoke)
        quiet_dwell = ctx.now - max(ctx.presence.updated_at, daily["last_spoke_at"])
        if quiet_dwell < cfg.min_dwell_s:
            return _skip("dwell_building")

        # Voice activity breaks quiet; bail early (arbiter also guards)
        if ctx.presence.voice_recent:
            return _skip("voice_recent")

        # 7. Identity juncture
        face: Optional[FaceIdentity] = None
        if cfg.audience == "known":
            if not ctx.identity_fresh:
                if ctx.now - daily["last_reject_at"] < cfg.identity_reject_cooldown_s:
                    return _skip("id_reject_cooldown")
                r.need_identity = True
                r.debug = {"mode": "idle", "reason": "requesting_identity"}
                blog(_TAG, "need face probe before joke (identity not fresh)")
                return r
            face = ctx.presence.face
            if face is None or face.is_stranger:
                # Committed in-body on purpose (records silence, not speech)
                self.store.joke_mark_reject(date, ctx.now)
                blog(
                    _TAG,
                    "would have joked but stranger/unknown face — suppressed",
                )
                r.debug = {"mode": "idle", "reason": "stranger_suppressed"}
                return r
        else:
            # anyone mode: never need_identity
            face = ctx.presence.face if ctx.identity_fresh else None

        # 8. Serve a line (pure SQLite; no network)
        qlen = 0
        try:
            qlen = int(self.store.joke_queue_len())
        except Exception:
            pass
        line = pop_line(self.store, cfg, question_ratio_roll=random.random())
        if line is None:
            blog(
                _TAG,
                f"ready to joke but queue empty (queue_len={qlen}) — waiting for refill",
            )
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
        who = face.name if face is not None and (face.name or "").strip() else "unknown"
        kind = line["kind"]
        source = line["source"]
        r.speak = text
        # Capture by value so the callback is stable after return
        spoke_date, spoke_at = date, ctx.now
        q_left = max(0, qlen - 1)

        def _on_allowed() -> None:
            self.store.joke_commit_spoke(spoke_date, spoke_at)
            blog(
                _TAG,
                f"said {kind} ({source}) to {who}: {short(text)!r} "
                f"[day count advanced; queue left≈{q_left}]",
            )

        r.on_speak_allowed = _on_allowed

        # 12. Debug payload for the arm that proposed speech (arbiter may still deny)
        r.debug = {
            "mode": "idle",
            "reason": "spoke",
            "kind": kind,
            "source": source,
            "who": who,
        }
        blog(
            _TAG,
            f"want to say {kind} ({source}) to {who}: "
            f"{short(text)!r} — waiting on speech arbiter",
        )
        return r
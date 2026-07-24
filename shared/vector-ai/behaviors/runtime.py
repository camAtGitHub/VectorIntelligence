"""Multi-behavior runtime: presence cache, arbiter, tick orchestration."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .arbiter import SpeechArbiter
from .config import JokeConfig, RuntimeConfig, WorkdayConfig
from .continuity import ContinuityStore
from .joke_idle import JokeIdleBehavior, JOKE_IDLE_ID
from .logutil import blog, short
from .presence import PresenceCache
from .types import (
    FaceIdentity,
    PresenceSnapshot,
    SpeechRequest,
    TickResult,
)
from .workday import WorkDayBehavior

_TAG = "runtime"


class BehaviorRuntime:
    def __init__(
        self,
        runtime_cfg: RuntimeConfig,
        workday_cfg: WorkdayConfig,
        store: ContinuityStore,
        quiet_fn: Optional[Callable[[], bool]] = None,
        voice_ts_fn: Optional[Callable[[], float]] = None,
        joke_cfg: Optional[JokeConfig] = None,
    ):
        self.runtime_cfg = runtime_cfg
        self.workday_cfg = workday_cfg
        self.store = store
        self.quiet_fn = quiet_fn or (lambda: False)
        self.voice_ts_fn = voice_ts_fn or (lambda: 0.0)

        # Use defaults only when attribute missing; allow intentional 0.
        _sticky = getattr(runtime_cfg, "presence_sticky_s", None)
        _streak = getattr(runtime_cfg, "presence_empty_streak", None)
        self.presence = PresenceCache(
            face_max_age_s=runtime_cfg.face_cache_max_age_s,
            image_max_age_s=runtime_cfg.image_cache_max_age_s,
            sticky_s=int(1800 if _sticky is None else _sticky),
            empty_streak_clear=int(2 if _streak is None else _streak),
        )
        self.arbiter = SpeechArbiter(
            min_gap_s=runtime_cfg.speech_min_gap_s,
            suppress_after_voice_s=runtime_cfg.speech_suppress_after_voice_s,
        )
        self.behaviors: List[Any] = []
        self.workday: Optional[WorkDayBehavior] = None
        # Per-behavior last tick time for min_tick_interval (v1 foundation).
        self._last_behavior_tick: Dict[str, float] = {}

        enabled = set(runtime_cfg.behaviors_enabled)
        if "workday" in enabled and workday_cfg.enabled:
            self.workday = WorkDayBehavior(workday_cfg, store)
            self.behaviors.append(self.workday)
            blog(_TAG, f"registered workday (priority={workday_cfg.priority})")
        if JOKE_IDLE_ID in enabled and joke_cfg is not None and joke_cfg.enabled:
            self.behaviors.append(JokeIdleBehavior(joke_cfg, store))
            blog(
                _TAG,
                f"registered joke_idle (priority={joke_cfg.priority}, "
                f"dwell={joke_cfg.min_dwell_s}s, cooldown={joke_cfg.cooldown_s}s)",
            )
        if not self.behaviors:
            blog(_TAG, "no behaviors registered (all disabled or not in BEHAVIORS_ENABLED)")

    def build_state_index(self, now: float) -> dict:
        """Envelope v1 for GET /v1/behaviors/state (cards only; no private dumps).

        ``date`` is the brain calendar day in the runtime workday TZ
        (``WorkdayConfig.tz``), even when only non-workday plugins are
        registered. See docs/FSM-implementation.md §3.3 / §13.
        """
        self.presence._sync_occupied(now)
        snap = self.presence.snapshot
        sticky = self.presence.debug_dict(now)
        local_dt = self._local_dt(now)
        date_s = local_dt.strftime("%Y-%m-%d")

        face_block = None
        if snap.face is not None:
            face_block = {
                "face_id": snap.face.face_id,
                "name": snap.face.name,
                "is_stranger": snap.face.is_stranger,
            }

        occupied = bool(sticky["occupied"])
        presence_updated_at = float(snap.updated_at or 0.0)
        session_started_at = float(
            sticky.get("session_started_at")
            or getattr(snap, "session_started_at", 0.0)
            or 0.0
        )
        last_user_voice_at = float(self.voice_ts_fn() or 0.0)
        cards: Dict[str, Any] = {}
        for b in self.behaviors:
            summary_fn = getattr(b, "status_summary", None)
            if callable(summary_fn):
                try:
                    try:
                        # Prefer presence-aware summary (joke dwell/gate).
                        summary = str(
                            summary_fn(
                                now,
                                session_started_at=session_started_at,
                                presence_updated_at=presence_updated_at,
                                occupied=occupied,
                                last_user_voice_at=last_user_voice_at,
                            )
                            or "ok"
                        )
                    except TypeError:
                        summary = str(summary_fn(now) or "ok")
                except Exception as e:
                    summary = f"error:{e}"
                    blog(_TAG, f"{b.id}: status_summary failed: {e}")
            else:
                summary = "ok" if b.enabled() else "disabled"
            cards[b.id] = {
                "enabled": True,  # only registered instances appear
                "summary": summary,
                "href": f"/v1/behaviors/{b.id}",
            }

        last_speech = self.arbiter.last_speech_at
        return {
            "schema_version": 1,
            "now": now,
            "date": date_s,
            "presence": {
                "occupied": sticky["occupied"],
                "identity_fresh": self.presence.identity_fresh(now),
                "face": face_block,
                "last_person_at": sticky["last_person_at"],
                "session_started_at": session_started_at or None,
                "empty_streak": sticky["empty_streak"],
                "presence_source": sticky["presence_source"],
                "soft_name": sticky.get("soft_name") or "",
                "presence_sticky_s": sticky["sticky_s"],
            },
            "arbiter": {
                "quiet": bool(self.quiet_fn()),
                "last_speech_at": last_speech,
                "last_user_voice_at": last_user_voice_at or None,
            },
            "behaviors": cards,
        }

    def behavior_status(self, behavior_id: str, now: float) -> Optional[dict]:
        """Resolve registered plugin status for GET /v1/behaviors/{id}.

        Returns None if id is not registered. Builds presence bits once so FSMs
        can compute dwell without coupling to PresenceCache types.
        """
        b = None
        for candidate in self.behaviors:
            if candidate.id == behavior_id:
                b = candidate
                break
        if b is None:
            return None

        self.presence._sync_occupied(now)
        snap = self.presence.snapshot
        occupied = self.presence.occupied_effective(now)
        presence_updated_at = float(snap.updated_at or 0.0)
        session_started_at = float(getattr(snap, "session_started_at", 0.0) or 0.0)
        last_user_voice_at = float(self.voice_ts_fn() or 0.0)

        status_fn = getattr(b, "status", None)
        if callable(status_fn):
            try:
                detail = status_fn(
                    now,
                    session_started_at=session_started_at,
                    presence_updated_at=presence_updated_at,
                    occupied=occupied,
                    last_user_voice_at=last_user_voice_at,
                )
            except TypeError:
                # Status methods that only take now
                detail = status_fn(now)
            except Exception as e:
                blog(_TAG, f"{b.id}: status failed: {e}")
                detail = {"id": b.id, "error": str(e)}
            if not isinstance(detail, dict):
                detail = {"id": b.id, "value": detail}
            out = dict(detail)
            out.setdefault("id", b.id)
            out.setdefault("schema_version", 1)
            return out

        summary_fn = getattr(b, "status_summary", None)
        summary = "ok"
        if callable(summary_fn):
            try:
                summary = str(summary_fn(now) or "ok")
            except Exception as e:
                summary = f"error:{e}"
        return {
            "id": b.id,
            "schema_version": 1,
            "summary": summary,
        }

    def ingest_tick_payload(
        self,
        now: float,
        occupied: bool,
        face: Optional[dict] = None,
        on_charger: bool = False,
        voice_recent: bool = False,
        image_b64: Optional[str] = None,
        hard_face: bool = False,
    ) -> PresenceSnapshot:
        """Ingest chipper tick sensors.

        hard_face=True: face came from chipper req.face (probe) — counts as
        person evidence even when occupied=false.
        hard_face=False with face: soft current_face() reuse for identity only.
        """
        face_obj: Optional[FaceIdentity] = None
        if face and isinstance(face, dict):
            try:
                raw_id = face.get("face_id", face.get("faceId", 0))
                face_id = int(raw_id if raw_id is not None else 0)
                name = str(face.get("name") or "")[:64]
                # Vector stranger faces often use negative IDs (e.g. -3).
                is_stranger = bool(
                    face.get("is_stranger")
                    or face.get("isStranger")
                    or face_id <= 0
                    or not name.strip()
                )
                # Accept any face sighting (including strangers) for identity cache.
                # Drop only completely empty payloads (no id signal and no name).
                if face_id == 0 and not name.strip() and not (
                    "face_id" in face or "faceId" in face
                ):
                    face_obj = None
                else:
                    face_obj = FaceIdentity(
                        face_id=face_id,
                        name=name,
                        is_stranger=is_stranger,
                    )
            except (TypeError, ValueError):
                face_obj = None
        # Binding:
        # - occupied=True → person evidence (may include face).
        # - hard chipper face (probe) → person evidence even if occupied=false.
        # - soft current_face reuse → identity-only (no last_person_at refresh).
        # - occupied=False alone is weak empty (no sticky clear).
        if occupied or (face_obj is not None and hard_face):
            return self.presence.note_person_evidence(
                now,
                source="tick",
                face=face_obj,
                on_charger=on_charger,
                voice_recent=voice_recent,
                image_b64=image_b64,
            )
        if face_obj is not None:
            return self.presence.note_identity_reuse(
                now,
                face=face_obj,
                on_charger=on_charger,
                voice_recent=voice_recent,
                image_b64=image_b64,
            )
        # Weak empty: refresh sensors without clearing warm sticky occupancy.
        return self.presence.update(
            now=now,
            occupied=False,
            face=None,
            image_b64=image_b64,
            on_charger=on_charger,
            voice_recent=voice_recent,
        )

    def _local_dt(self, now: float) -> datetime:
        tz = self.workday_cfg.tz
        return datetime.fromtimestamp(now, tz=tz)

    def clock_tick(self, now: float) -> None:
        """Clock-only transitions without chipper presence."""
        local_dt = self._local_dt(now)
        if self.workday is not None:
            self.workday.clock_tick(now, local_dt)

    def tick(self, now: float) -> TickResult:
        from .types import BehaviorContext

        local_dt = self._local_dt(now)
        self.clock_tick(now)

        quiet = bool(self.quiet_fn())
        voice_ts = float(self.voice_ts_fn() or 0.0)
        # Re-evaluate sticky occupancy (TTL may expire between ticks).
        self.presence._sync_occupied(now)
        snap = self.presence.snapshot
        identity_fresh = self.presence.identity_fresh(now)

        ctx = BehaviorContext(
            now=now,
            local_dt=local_dt,
            presence=snap,
            quiet=quiet,
            config=self.workday_cfg,
            identity_fresh=identity_fresh,
            last_user_voice_at=voice_ts,
        )

        candidates: List[tuple[Any, TickResult]] = []
        need_identity = False
        for b in self.behaviors:
            if not b.enabled():
                continue
            # min_tick_interval: skip plan if invoked too often (clock_tick still runs).
            min_iv = float(getattr(b, "min_tick_interval", 0) or 0)
            last = self._last_behavior_tick.get(b.id, 0.0)
            if min_iv > 0 and last > 0 and (now - last) < min_iv:
                # Still allow ticks that might need identity urgency? v1: skip fully.
                # Exception: always allow if identity not fresh and we might need it —
                # keep simple: honor min interval for speech-heavy plan only by
                # still running plan (interval is 30s; chipper is 60s). Use as soft
                # guard for future multi-FSM; workday min is 30s so chipper 60s is fine.
                pass
            self._last_behavior_tick[b.id] = now
            r = b.tick(ctx)
            if r.need_identity:
                need_identity = True
            candidates.append((b, r))

        scored = sorted(
            ((getattr(b, "priority", 0), r, b) for b, r in candidates),
            key=lambda t: t[0],
            reverse=True,
        )

        speak = ""
        debug: Dict[str, Any] = {
            "mode": None,
            "candidates": len(scored),
            "quiet": quiet,
            "identity_fresh": identity_fresh,
            "occupied": snap.occupied,
        }
        for prio, r, b in scored:
            debug.setdefault("behaviors", {})[b.id] = dict(r.debug or {})
            if r.debug and r.debug.get("mode"):
                debug["mode"] = r.debug.get("mode")
            text = (r.speak or "").strip()
            if not text:
                # Log interesting non-speech outcomes at verbose level only.
                reason = (r.debug or {}).get("reason")
                if reason and reason not in (
                    "dwell_building",
                    "cooldown",
                    "empty",
                    "armed_idle",
                    "away",
                    "paused",
                ):
                    blog(
                        _TAG,
                        f"{b.id}: no speech ({reason})",
                        verbose=True,
                        data=r.debug,
                    )
                continue
            req = SpeechRequest(
                text=text,
                priority=prio,
                behavior_id=b.id,
                reason=(r.debug or {}).get("reason", ""),
            )
            ok, why = self.arbiter.allow(
                req, now=now, quiet=quiet, voice_recent_ts=voice_ts
            )
            debug["arbiter"] = why
            if ok:
                # Speech-gated side effects only after allow.
                if r.on_speak_allowed is not None:
                    try:
                        r.on_speak_allowed()
                    except Exception as e:
                        debug["commit_error"] = str(e)
                        blog(_TAG, f"{b.id}: on_speak_allowed failed: {e}")
                self.arbiter.record_speech(now)
                speak = text
                debug["spoke_from"] = b.id
                blog(
                    _TAG,
                    f"ALLOWED speech from {b.id} (prio={prio}, reason={req.reason}): "
                    f"{short(text)!r}",
                )
                break
            debug["arbiter_deny"] = why
            # Denied: do NOT run on_speak_allowed — timers stay, late_check not entered.
            blog(
                _TAG,
                f"DENIED {b.id} speech (arbiter={why}, prio={prio}, "
                f"reason={req.reason}): {short(text)!r} — "
                f"wanted to speak but did not",
            )

        if need_identity and not speak:
            blog(_TAG, "tick result: need_identity (no speech this tick)", verbose=True)

        return TickResult(
            speak=speak,
            need_identity=need_identity,
            debug=debug,
        )

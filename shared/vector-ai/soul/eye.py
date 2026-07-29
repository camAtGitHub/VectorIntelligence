"""Eye: shared vision path for ambient glances and chat frame notes.

Ambient: size guard → archive → VISION_MODEL multimodal → PRESENCE parse →
presence write via soul → VisionDigest buffer.

Chat: archive + light digest only (no second LLM; chat stream owns the reply).
"""
from __future__ import annotations

import random
import re
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Deque, Optional, Tuple

from debug_log import debug
from image_archive import save_submitted_image
from llm import (
    LLM_AMBIENT_TEMPERATURE,
    VISION_MODEL,
    _llm_timeout,
    image_payload_too_large,
    llm_chat_once,
)
from logging_util import print  # noqa: A001
from prompt_voice import PERSONA_MINI
from response_cleanup import _strip_for_speech

from .types import VisionDigest

if TYPE_CHECKING:
    from .soul import Soul

_VISION_BUFFER_MAX = 16

# -- Ambient PRESENCE protocol (moved from routes/ambient.py) ------------------

AMBIENT_SYSTEM = (
    PERSONA_MINI + "\n\n"
    "You are idling on your desk, camera open, nobody talking to you. You just "
    "glanced around and are looking at a photo of what is in front of you.\n\n"
    "Reply in this exact shape:\n"
    "Line 1 - presence, always one of:\n"
    "  PRESENCE: empty\n"
    "  PRESENCE: person\n"
    "  PRESENCE: person:<NameHint>\n"
    "Use 'person' for ANY human evidence - head, torso, arm, hand, or just a "
    "clothing silhouette like a hoodie - even cut off, turned away or "
    "occluded. Use 'empty' only when there is no human evidence at all. Add "
    ":<NameHint> only if you are confident who it is.\n\n"
    "After the presence line, either the single word NOTHING, or a novelty in "
    "two further lines.\n\n"
    "Your desk barely changes, and NOTHING is the right answer almost every "
    "time. Known desk layout items that are still where they belong are not "
    "news; static clutter, cables, and an empty or dim room are not news; a "
    "person you already noted recently is not news (still output PRESENCE for "
    "them, then NOTHING - do not re-roast the same person every glance). "
    "After PRESENCE, answer NOTHING unless something appeared, moved, or "
    "vanished relative to the known desk layout and recent notes you will be "
    "given — or an animal, a person arriving for the first time, or a real "
    "mess or event. Never invent detail you cannot actually see. When in any "
    "doubt, answer NOTHING.\n\n"
    "When there IS something new, give exactly two lines after the presence "
    "line:\n"
    "Line A - memory note, for your own records, never spoken. Plain and "
    "literal, not witty: what changed, e.g. 'a small plush toy appeared on the "
    "desk'.\n"
    "Line B - spoken. One dry line in your own voice. Sound like you actually "
    "just noticed it: name the thing so someone who cannot see the desk knows "
    "what you spotted, then turn it. Lead normal, swerve late; deadpan, not "
    "delighted. One or two sentences, no more. Plain speech only — no markdown, "
    "no quotes, no {{...}} tokens.\n"
    "If there is nothing new: presence line, then NOTHING."
)

_PRESENCE_RE = re.compile(
    r"^PRESENCE\s*:\s*(empty|person(?:\s*:\s*(.+))?)\s*$",
    re.IGNORECASE,
)


def parse_ambient_llm_raw(raw: str) -> Tuple[str, Optional[str], str]:
    """Parse ambient LLM output.

    Returns (presence_kind, name_hint|None, spoken_text).

    presence_kind is ``\"empty\"``, ``\"person\"``, or ``\"unknown\"`` when the
    PRESENCE line is missing/unparseable (caller must not invent empty).
    spoken_text is \"\" for NOTHING / no novelty lines.
    """
    if not raw or not str(raw).strip():
        return "unknown", None, ""

    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    if not lines:
        return "unknown", None, ""

    presence_kind = "unknown"
    name_hint: Optional[str] = None
    body_start = 0

    found_presence = False
    for i, ln in enumerate(lines):
        m = _PRESENCE_RE.match(ln)
        if m:
            found_presence = True
            kind_raw = (m.group(1) or "").strip().lower()
            if kind_raw.startswith("person"):
                presence_kind = "person"
                hint = (m.group(2) or "").strip()
                name_hint = hint[:64] if hint else None
            elif kind_raw == "empty":
                presence_kind = "empty"
                name_hint = None
            body_start = i + 1
            break
        if i == 0 and ln.upper().rstrip(".!").startswith("NOTHING"):
            return "unknown", None, ""

    if not found_presence:
        return "unknown", None, ""

    rest = lines[body_start:]
    if not rest:
        return presence_kind, name_hint, ""

    first = rest[0]
    if first.upper().rstrip(".!").startswith("NOTHING"):
        return presence_kind, name_hint, ""

    if len(rest) >= 2:
        spoken = " ".join(rest[1:])
    else:
        spoken = rest[0]
    spoken = _strip_for_speech(spoken)
    if not spoken or spoken.upper().startswith("NOTHING"):
        return presence_kind, name_hint, ""
    return presence_kind, name_hint, spoken


def novelty_note_from_raw(raw: str) -> str:
    """Extract the memory note line after PRESENCE (if novelty present)."""
    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    body = []
    seen_presence = False
    for ln in lines:
        if not seen_presence and _PRESENCE_RE.match(ln):
            seen_presence = True
            continue
        if not seen_presence and ln.upper().startswith("PRESENCE"):
            seen_presence = True
            continue
        body.append(ln)
    if not body:
        return ""
    if body[0].upper().rstrip(".!").startswith("NOTHING"):
        return ""
    note = _strip_for_speech(body[0])
    if not note or note.upper().startswith("NOTHING"):
        return ""
    return note


def soft_face_from_name_hint(name_hint: Optional[str], memory=None):
    """Map ambient name_hint to FaceIdentity; soft-match enrolled MEMORY faces."""
    from behaviors.types import FaceIdentity

    if not name_hint or not str(name_hint).strip():
        return None
    hint = str(name_hint).strip()[:64]
    try:
        if memory is not None:
            for face_id, face_name in memory.distinct_faces() or []:
                if face_name and str(face_name).strip().lower() == hint.lower():
                    fid = int(face_id)
                    if fid > 0:
                        return FaceIdentity(
                            face_id=fid, name=str(face_name)[:64], is_stranger=False
                        )
    except Exception as e:
        print(f"[eye] name_hint soft-match failed: {e}")
    return FaceIdentity(face_id=0, name=hint, is_stranger=True)


def _build_ambient_user_text(memory, mood_text: str = "") -> str:
    """Observations + desk map + mood preamble for the ambient glance prompt."""
    obs_note = "You have not noted anything recently."
    if memory is not None:
        try:
            obs = memory.list_observations(limit=8, max_age_seconds=24 * 3600)
            if obs:
                seen = "\n".join(
                    f"- (at {datetime.fromtimestamp(o['seen_at']).strftime('%I:%M %p')}) "
                    f"{o['text']}"
                    for o in reversed(obs)
                )
                obs_note = (
                    "Things you have already noticed recently - do NOT react "
                    "to any of these again:\n" + seen
                )
        except Exception as e:
            print(f"[eye] list_observations failed: {e}")

    desk_map = ""
    if memory is not None:
        try:
            desk_lines = memory.desk_map_prompt_lines(limit=12)
            if desk_lines:
                desk_map = (
                    "\n\nKnown desk layout (stable items → NOTHING after PRESENCE; "
                    "speak only on appear/move/vanish):\n" + desk_lines
                )
        except Exception as e:
            print(f"[eye] desk map load failed: {e}")

    mood_note = ""
    if mood_text:
        mood_note = (
            f"\n\nYour current state of mind: {mood_text}. "
            f"If you do react, let it tint your tone; never state it."
        )
    return (
        obs_note
        + desk_map
        + mood_note
        + "\n\nGlance at what is in front of you now. "
        "First line MUST be PRESENCE: empty or PRESENCE: person "
        "(or person:<NameHint>). Then NOTHING, or the two-line novelty "
        "format. Partial people count (torso/arms/hoodie OK without face)."
    )


class EyeService:
    """Vision digests + frame freshness for the soul pot."""

    def __init__(self, image_max_age_s: int = 45, buffer_max: int = _VISION_BUFFER_MAX):
        self.image_max_age_s = int(image_max_age_s) if image_max_age_s else 45
        self._buffer: Deque[VisionDigest] = deque(maxlen=max(1, int(buffer_max)))
        self._last_vision: Optional[VisionDigest] = None
        self._last_frame_ts: float = 0.0
        self._last_frame_path: str = ""

    @property
    def last_vision(self) -> Optional[VisionDigest]:
        return self._last_vision

    def buffer_list(self) -> list:
        return [d.to_dict() for d in self._buffer]

    def image_fresh(self, now: float) -> bool:
        """True when a frame was archived within IMAGE_CACHE_MAX_AGE_S."""
        now = float(now)
        if self._last_frame_ts <= 0:
            return False
        return (now - self._last_frame_ts) <= self.image_max_age_s

    def _store(self, digest: VisionDigest) -> VisionDigest:
        self._last_vision = digest
        self._buffer.append(digest)
        if digest.frame_path:
            self._last_frame_path = digest.frame_path
        if digest.t:
            self._last_frame_ts = float(digest.t)
        return digest

    def note_frame(
        self,
        now: float,
        *,
        reason: str,
        frame_path: str = "",
        note: str = "",
        presence: str = "",
        name_hint: str = "",
        spoken: str = "",
        model: str = "",
        error: str = "",
        occupied: bool = False,
    ) -> VisionDigest:
        """Light digest without LLM (chat archive path)."""
        digest = VisionDigest(
            t=float(now),
            reason=str(reason or ""),
            presence=str(presence or ""),
            name_hint=str(name_hint or ""),
            note=str(note or "")[:300],
            spoken=str(spoken or ""),
            model=str(model or ""),
            frame_path=str(frame_path or ""),
            error=str(error or ""),
            occupied=bool(occupied),
        )
        return self._store(digest)

    async def see(
        self,
        soul: "Soul",
        now: float,
        *,
        image: str,
        reason: str = "ambient",
    ) -> VisionDigest:
        """Full eye path. Ambient runs VISION_MODEL; chat archives only."""
        now = float(now)
        reason = str(reason or "ambient").strip().lower() or "ambient"

        if image_payload_too_large(image):
            print(
                f"[eye] image payload too large "
                f"(chars={len(image or '')}) — skipping {reason}"
            )
            debug("eye see image_too_large", {
                "reason": reason,
                "image_len": len(image or ""),
            })
            return self.note_frame(
                now,
                reason=reason,
                error="image_too_large",
                presence="",
            )

        if reason == "chat":
            return self._see_chat_archive(soul, now, image=image)

        if reason != "ambient":
            # Unknown reasons: archive + light note only (no invent presence).
            path = save_submitted_image(image, reason)
            frame_path = str(path) if path else ""
            return self.note_frame(
                now,
                reason=reason,
                frame_path=frame_path,
            )

        return await self._see_ambient(soul, now, image=image)

    def _see_chat_archive(
        self,
        soul: "Soul",
        now: float,
        *,
        image: str,
    ) -> VisionDigest:
        """Chat: archive frame + light digest; no second LLM."""
        path = save_submitted_image(image, "chat")
        frame_path = str(path) if path else ""
        # If face is already known, treat as mild visual presence refresh.
        face = None
        try:
            face = soul.current_face(now)
        except Exception:
            face = None
        if face is not None:
            try:
                soul.note_person(
                    now,
                    source="chat",
                    name_hint=(face.get("name") or None) if isinstance(face, dict) else None,
                )
            except Exception as e:
                print(f"[eye] chat note_person failed: {e}")
        return self.note_frame(
            now,
            reason="chat",
            frame_path=frame_path,
            presence="person" if face else "",
            name_hint=(face.get("name") or "") if isinstance(face, dict) else "",
            note="chat frame",
            occupied=bool(face) or soul.occupied(now),
        )

    async def _see_ambient(
        self,
        soul: "Soul",
        now: float,
        *,
        image: str,
    ) -> VisionDigest:
        """Ambient glance: VISION_MODEL + PRESENCE parse + presence write."""
        path = save_submitted_image(image, "ambient")
        frame_path = str(path) if path else ""

        mood_text = ""
        try:
            mood_text = (soul.mood_text() or "").strip()
        except Exception:
            mood_text = ""

        user_text = _build_ambient_user_text(soul.memory, mood_text=mood_text)
        user_msg = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image}"},
            },
        ]
        debug("eye ambient glance", {
            "image_len": len(image or ""),
            "user_text": user_text[:500],
        })

        try:
            raw = (
                await llm_chat_once(
                    [
                        {"role": "system", "content": AMBIENT_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    model=VISION_MODEL,
                    temperature=LLM_AMBIENT_TEMPERATURE,
                    top_p=0.9,
                    seed=random.randint(1, 2**31 - 1),
                    timeout=_llm_timeout(connect=12.0, read=45.0),
                    max_tokens=256,
                    tag="ambient",
                )
            ).strip()
        except Exception as e:
            print(f"[eye] ambient llm error: {e}")
            debug(f"eye ambient error: {e}")
            # No presence update on error (do not invent empty).
            return self.note_frame(
                now,
                reason="ambient",
                frame_path=frame_path,
                model=VISION_MODEL,
                error=str(e),
                presence="unknown",
                occupied=soul.occupied(now),
            )

        presence_kind, name_hint, spoken = parse_ambient_llm_raw(raw)
        note = novelty_note_from_raw(raw)
        if spoken and not note:
            note = spoken
        note = (note or "")[:300]

        occupied_out = presence_kind == "person"
        try:
            if presence_kind == "person":
                face = soft_face_from_name_hint(name_hint, memory=soul.memory)
                soul.note_person(
                    now,
                    source="ambient",
                    name_hint=name_hint,
                    face=face,
                    image_ref=frame_path or None,
                )
                occupied_out = soul.occupied(now)
                print(
                    f"[eye] presence=person name_hint={name_hint!r} "
                    f"occupied_effective={occupied_out}"
                )
            elif presence_kind == "empty":
                soul.note_empty(now, source="ambient")
                occupied_out = soul.occupied(now)
                streak = soul.presence.empty_streak
                clear_n = soul.presence.empty_streak_clear
                print(
                    f"[eye] presence=empty streak={streak}/{clear_n} "
                    f"occupied_effective={occupied_out}"
                )
            else:
                occupied_out = soul.occupied(now)
        except Exception as e:
            print(f"[eye] presence update failed: {e}")
            try:
                occupied_out = soul.occupied(now)
            except Exception:
                occupied_out = presence_kind == "person"

        digest = VisionDigest(
            t=now,
            reason="ambient",
            presence=presence_kind,
            name_hint=name_hint or "",
            note=note,
            spoken=spoken or "",
            model=VISION_MODEL,
            frame_path=frame_path,
            error="",
            occupied=bool(occupied_out),
        )
        return self._store(digest)

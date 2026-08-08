"""Eye: shared vision path for ambient glances and chat frame notes.

Ambient: size guard → archive → VISION_MODEL multimodal → PRESENCE + SCENE
parse → presence write via soul → store full scene for next glance compare →
VisionDigest buffer.

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
# Cap stored/resent scene text so prompts stay bounded.
# Room for a denser L→R inventory (zones + durable items) without ballooning.
_SCENE_MAX_CHARS = 1400

# -- Ambient PRESENCE + SCENE protocol -----------------------------------------

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
    "Line 2 - SCENE: always. One plain, thorough inventory of what is actually "
    "in the photo right now. Literal, not witty. This is the baseline for the "
    "next glance — denser is better than vague. Do not invent detail you cannot "
    "see.\n"
    "How to encode SCENE (required style):\n"
    "  - Scan the frame LEFT → RIGHT. Cover every horizontal band you can "
    "see: far-left, left, center, right, far-right. Mention depth when useful "
    "(near / mid / far / on-desk / behind-monitor).\n"
    "  - List durable items with location tags, e.g. "
    "'mug@left-of-monitor', 'keyboard@center-near', 'headphones@far-right', "
    "'laptop@right open', 'person@center grey hoodie'. Separate items with "
    "semicolons or commas.\n"
    "  - Include people (pose/clothing if clear), screens if content is "
    "obvious (e.g. code vs blank), lighting, and anything noteworthy — not "
    "only the biggest objects. Small movable things (mug, phone, plush, "
    "bottle, snack, notebook) matter as much as the monitor.\n"
    "  - Prefer specific labels over 'clutter' or 'stuff'. If something is "
    "half-visible at an edge, still name it and its side "
    "(e.g. 'bottle@far-left edge').\n\n"
    "Then either the single word NOTHING, or a novelty in two further lines.\n\n"
    "Compare the photo to the PREVIOUS SCENE you will be given (and recent "
    "notes / desk layout). Walk the previous inventory item-by-item and by "
    "zone (left vs center vs right): did each still-listed thing stay put?\n"
    "NOTHING is correct when the desk is truly unchanged: same items in the "
    "same zones, static cables/clutter, empty or dim room, person already "
    "present in the previous scene (still output PRESENCE + SCENE + NOTHING "
    "— do not re-roast them).\n"
    "Call out a change (do NOT answer NOTHING) when relative to the previous "
    "scene something clearly appeared, moved zone/side, vanished, changed "
    "state in a visible way (open/closed, screen content shift, posture if "
    "person newly sat/stood), or an animal / first-time person arrival / real "
    "mess or event shows up. A mug that was left and is now right IS news; a "
    "new object on any edge IS news; something gone from a listed zone IS "
    "news. Prefer calling a clear location change over silent NOTHING.\n"
    "If there is no previous scene, this glance only establishes the "
    "baseline: PRESENCE + SCENE + NOTHING. Never invent detail you cannot "
    "actually see. If you are unsure whether something moved, prefer NOTHING; "
    "if the location shift is obvious in the photo vs PREVIOUS SCENE, call it "
    "out.\n\n"
    "When there IS something new, give exactly two lines after the SCENE line:\n"
    "Line A - memory note, for your own records, never spoken. Plain and "
    "literal, not witty: what changed AND where "
    "(from→to when it moved), e.g. 'small plush appeared @right near keyboard' "
    "or 'mug moved left-of-monitor → on keyboard'.\n"
    "Line B - spoken. One dry line in your own voice. Sound like you actually "
    "just noticed it: name the thing and roughly where so someone who cannot "
    "see the desk knows what moved, then turn it. Lead normal, swerve late; "
    "deadpan, not delighted. One or two sentences, no more. Plain speech only "
    "— no markdown, no quotes, no {{...}} tokens.\n"
    "If there is nothing new: PRESENCE, SCENE, then NOTHING."
)

_PRESENCE_RE = re.compile(
    r"^PRESENCE\s*:\s*(empty|person(?:\s*:\s*(.+))?)\s*$",
    re.IGNORECASE,
)
_SCENE_LINE_RE = re.compile(r"^SCENE\s*:\s*(.*)$", re.IGNORECASE)


def _body_after_presence(lines: list) -> list:
    """Lines after the first PRESENCE line (empty if none)."""
    for i, ln in enumerate(lines):
        if _PRESENCE_RE.match(ln) or ln.upper().startswith("PRESENCE"):
            return lines[i + 1 :]
    return []


def _split_scene_and_rest(body: list) -> Tuple[str, list]:
    """Pull SCENE line (if present) off body; return (scene_text, remaining).

    Backward compatible: if the first body line is not SCENE, scene is \"\" and
    remaining is the full body (legacy NOTHING / novelty format).
    """
    if not body:
        return "", []
    m = _SCENE_LINE_RE.match(body[0])
    if not m:
        return "", body
    scene = (m.group(1) or "").strip()
    return scene[:_SCENE_MAX_CHARS], body[1:]


def parse_ambient_llm_raw(raw: str) -> Tuple[str, Optional[str], str]:
    """Parse ambient LLM output.

    Returns (presence_kind, name_hint|None, spoken_text).

    presence_kind is ``\"empty\"``, ``\"person\"``, or ``\"unknown\"`` when the
    PRESENCE line is missing/unparseable (caller must not invent empty).
    spoken_text is \"\" for NOTHING / no novelty lines.
    SCENE lines are ignored for spoken extraction (see scene_from_raw).
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

    # Skip mandatory SCENE inventory before NOTHING / novelty.
    _scene, rest = _split_scene_and_rest(rest)
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


def scene_from_raw(raw: str) -> str:
    """Extract SCENE inventory text after PRESENCE (empty if absent)."""
    if not raw or not str(raw).strip():
        return ""
    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    body = _body_after_presence(lines)
    scene, _rest = _split_scene_and_rest(body)
    return scene


def novelty_note_from_raw(raw: str) -> str:
    """Extract the memory note line after PRESENCE (+ optional SCENE)."""
    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    body = _body_after_presence(lines)
    _scene, rest = _split_scene_and_rest(body)
    if not rest:
        return ""
    if rest[0].upper().rstrip(".!").startswith("NOTHING"):
        return ""
    note = _strip_for_speech(rest[0])
    if not note or note.upper().startswith("NOTHING"):
        return ""
    # Do not treat a SCENE line as a novelty note if parser missed it.
    if _SCENE_LINE_RE.match(note):
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


def _build_ambient_user_text(
    memory,
    mood_text: str = "",
    previous_scene: str = "",
) -> str:
    """Prior scene + observations + desk map + mood for the ambient glance."""
    prev = (previous_scene or "").strip()
    if prev:
        prev_block = (
            "PREVIOUS SCENE (what you saw on the last glance — compare the "
            "photo to this LEFT→RIGHT by zone. Treat appear / move-zone / "
            "vanish / clear state change vs this inventory as news; same "
            "item still in the same zone is not news):\n"
            f"{prev[:_SCENE_MAX_CHARS]}"
        )
    else:
        prev_block = (
            "PREVIOUS SCENE: none (first glance or after sleep). Establish "
            "baseline only — PRESENCE + SCENE + NOTHING. Do not invent news."
        )

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
                    "Things you have already spoken about recently - do NOT "
                    "react to any of these again:\n" + seen
                )
        except Exception as e:
            print(f"[eye] list_observations failed: {e}")

    desk_map = ""
    if memory is not None:
        try:
            desk_lines = memory.desk_map_prompt_lines(limit=12)
            if desk_lines:
                desk_map = (
                    "\n\nKnown desk layout (item still in the same place → "
                    "NOTHING after SCENE; speak on appear / side-or-zone move "
                    "/ vanish):\n"
                    + desk_lines
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
        prev_block
        + "\n\n"
        + obs_note
        + desk_map
        + mood_note
        + "\n\nGlance at what is in front of you now. "
        "Line 1 MUST be PRESENCE: empty or PRESENCE: person "
        "(or person:<NameHint>). Line 2 MUST be SCENE: thorough LEFT→RIGHT "
        "inventory with location tags (item@zone), covering far-left through "
        "far-right — people, durable objects, edge items, lighting. Then "
        "NOTHING, or the two-line novelty format (note must include what + "
        "where / from→to). Partial people count (torso/arms/hoodie OK "
        "without face). Prefer calling out a clear zone/side change over "
        "silent NOTHING."
    )


class EyeService:
    """Vision digests + frame freshness for the soul pot."""

    def __init__(self, image_max_age_s: int = 45, buffer_max: int = _VISION_BUFFER_MAX):
        self.image_max_age_s = int(image_max_age_s) if image_max_age_s else 45
        self._buffer: Deque[VisionDigest] = deque(maxlen=max(1, int(buffer_max)))
        self._last_vision: Optional[VisionDigest] = None
        self._last_frame_ts: float = 0.0
        self._last_frame_path: str = ""
        # Full last ambient SCENE inventory — resent on next glance for compare.
        self._last_scene_text: str = ""

    @property
    def last_vision(self) -> Optional[VisionDigest]:
        return self._last_vision

    @property
    def last_scene_text(self) -> str:
        return self._last_scene_text or ""

    def clear_last_scene(self) -> None:
        """Drop prior scene baseline (e.g. ambient sleep gap / new session)."""
        self._last_scene_text = ""

    def set_last_scene(self, scene: str) -> None:
        """Store scene inventory for the next ambient compare (capped)."""
        s = (scene or "").strip()
        self._last_scene_text = s[:_SCENE_MAX_CHARS] if s else ""

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
        # Keep in-memory prior scene aligned with latest ambient digest.
        if digest.reason == "ambient" and (digest.scene or "").strip():
            self.set_last_scene(digest.scene)
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
        scene: str = "",
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
            scene=str(scene or "")[:_SCENE_MAX_CHARS],
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
        """Ambient glance: VISION_MODEL + PRESENCE/SCENE parse + presence write.

        Always stores the SCENE inventory as the next glance's previous scene.
        """
        path = save_submitted_image(image, "ambient")
        frame_path = str(path) if path else ""

        mood_text = ""
        try:
            mood_text = (soul.mood_text() or "").strip()
        except Exception:
            mood_text = ""

        previous_scene = self.last_scene_text
        user_text = _build_ambient_user_text(
            soul.memory,
            mood_text=mood_text,
            previous_scene=previous_scene,
        )
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
            "has_previous_scene": bool(previous_scene),
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
                    max_tokens=2048,
                    tag="ambient",
                )
            ).strip()
        except Exception as e:
            print(f"[eye] ambient llm error: {e}")
            debug(f"eye ambient error: {e}")
            # No presence update on error (do not invent empty).
            # Keep prior scene so a failed glance does not wipe the baseline.
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
        scene = scene_from_raw(raw)
        note = novelty_note_from_raw(raw)
        if spoken and not note:
            note = spoken
        note = (note or "")[:300]

        # First glance / baseline only: if we had no prior scene, never speak
        # novelty even if the model ignored instructions (establish inventory).
        if not previous_scene and spoken:
            print(
                "[eye] baseline glance — suppressing spoken novelty "
                f"({spoken!r:.80})"
            )
            spoken = ""
            note = ""

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

        if scene:
            print(f"[eye] scene stored ({len(scene)} chars): {scene[:120]!r}")
        else:
            print("[eye] ambient reply missing SCENE line — prior scene unchanged")

        digest = VisionDigest(
            t=now,
            reason="ambient",
            presence=presence_kind,
            name_hint=name_hint or "",
            note=note,
            spoken=spoken or "",
            scene=scene,
            model=VISION_MODEL,
            frame_path=frame_path,
            error="",
            occupied=bool(occupied_out),
        )
        # _store updates _last_scene_text only when scene is non-empty.
        return self._store(digest)

"""Strip markdown, extract memory/work/quiet tags, clean spoken responses.

Split into pure strip helpers (speech-safe text) and side-effect apply paths
(remember/forget/quiet/work). ``clean_response`` keeps the same call order as
before: strip markdown → forbidden cmds → apply tags → phrase fixes → || scrub.
"""
import re
import time
from datetime import datetime
from typing import List, Optional, Tuple

import deps
from logging_util import print  # noqa: F401
from process_state import _set_quiet
from behaviors.workday import parse_work_commands, pause_until_ts


def strip_markdown(text: str) -> str:
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}',     r'\1', text)
    text = re.sub(r'#{1,6}\s*',               '',    text)
    text = re.sub(r'`{1,3}[^`]*`{1,3}',       '',    text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)',   r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+',            '',    text, flags=re.MULTILINE)
    return text


# Safety net for "the image" / "the photo" phrasing if the model slips.
_PHRASE_FIXES = [
    (re.compile(r'\bthe image (shows?|depicts?|contains?|reveals?)\b', re.IGNORECASE), 'I see'),
    (re.compile(r'\bin the image\b',                                   re.IGNORECASE), 'in front of me'),
    (re.compile(r'\bthe photo (shows?|depicts?)\b',                    re.IGNORECASE), 'I see'),
    (re.compile(r'\bin the photo\b',                                   re.IGNORECASE), 'in front of me'),
    (re.compile(r'\bthe picture (shows?|depicts?)\b',                  re.IGNORECASE), 'I see'),
]

# Invented listen-like commands the LLM may hallucinate from playAnimation-style
# grammar. Strip them so they never reach TTS/chipper.
# {{newVoiceRequest||…}} is a REAL conversation command — chipper executes it;
# do NOT add it here (clean_response must leave it intact for multi-turn listen).
_FORBIDDEN_COMMAND = re.compile(
    r'\{\{(voiceRequest|listen|wakeWord|waitForUser)\|\|[^}]*\}\}',
    re.IGNORECASE,
)

# Memory commands the LLM may emit; captured + processed here, then stripped
# from the response so they don't get spoken aloud.
# Match {{remember-shared||...}} BEFORE {{remember||...}} or the shared form
# would be partially eaten - but Python's re.findall handles non-overlapping
# greedy matches fine if we apply shared first.
_REMEMBER_SHARED_RE = re.compile(r'\{\{remember-shared\|\|([^}]+)\}\}', re.IGNORECASE)
_REMEMBER_RE        = re.compile(r'\{\{remember\|\|([^}]+)\}\}',         re.IGNORECASE)
_FORGET_RE          = re.compile(r'\{\{forget\|\|([^}]+)\}\}',           re.IGNORECASE)
# Ambient quiet mode: the user can tell Vector to hush his spontaneous
# ambient commentary. Auto-expires after a sleep cycle (see /v1/ambient).
_QUIET_RE           = re.compile(r'\{\{quietMode\|\|(on|off)\}\}',        re.IGNORECASE)


def _scrub_double_pipes(text: str) -> str:
    """Strip leftover ``||`` outside intact ``{{...}}`` command blocks."""
    segments = re.split(r'(\{\{.*?\}\})', text)
    return "".join(
        s if s.startswith("{{") and s.endswith("}}") else s.replace("||", "")
        for s in segments
    )


def _remember_owner_face() -> Optional[dict]:
    """Same effective face as chat inject / prepare_messages."""
    try:
        from prompt_assembly import _effective_face
        return _effective_face()
    except Exception:
        return None


def parse_memory_tags(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Pure parse of memory/quiet tags. Returns (text_without_tags, actions).

    Actions are (kind, payload) where kind is remember_shared | remember |
    forget | quiet.
    """
    actions: List[Tuple[str, str]] = []
    if not text:
        return text or "", actions
    for fact in _REMEMBER_SHARED_RE.findall(text):
        actions.append(("remember_shared", fact.strip()))
    text = _REMEMBER_SHARED_RE.sub('', text)
    for fact in _REMEMBER_RE.findall(text):
        actions.append(("remember", fact.strip()))
    text = _REMEMBER_RE.sub('', text)
    for target in _FORGET_RE.findall(text):
        actions.append(("forget", target.strip()))
    text = _FORGET_RE.sub('', text)
    for state in _QUIET_RE.findall(text):
        actions.append(("quiet", state.strip().lower()))
    text = _QUIET_RE.sub('', text)
    return text, actions


def apply_memory_actions(actions: List[Tuple[str, str]]) -> None:
    """Apply parsed memory/quiet actions (side effects).

    Not idempotent if re-run with the same action list (duplicates skipped by
    memory store; quiet toggles would flip again). Chat path applies once per
    cleaned sentence; do not call twice on the same parse result.
    """
    if not actions:
        return
    MEMORY = deps.MEMORY
    face = _remember_owner_face()
    if face and not face.get("is_stranger"):
        owner_id, owner_name = face.get("face_id"), face.get("name")
    else:
        owner_id, owner_name = None, None
    for kind, payload in actions:
        if kind == "remember_shared":
            stored = MEMORY.remember(payload)
            if stored:
                print(f"[memory] +remember-shared #{stored.id}: {stored.text!r}")
            else:
                print(f"[memory] remember-shared skipped (dup): {payload!r}")
        elif kind == "remember":
            stored = MEMORY.remember(
                payload, face_id=owner_id, face_name=owner_name
            )
            if stored:
                tag = f" [{owner_name}]" if owner_name else " [shared]"
                print(f"[memory] +remember #{stored.id}{tag}: {stored.text!r}")
            else:
                print(f"[memory] remember skipped (dup or empty): {payload!r}")
        elif kind == "forget":
            n = MEMORY.forget(payload)
            print(f"[memory] -forget matched={n} for {payload!r}")
        elif kind == "quiet":
            _set_quiet(payload == "on")


def extract_memory_commands(text: str) -> str:
    """Find {{remember[-shared]||…}} / {{forget||…}} / quiet / work tags, apply
    side effects, return speech text with those commands removed."""
    text, mem_actions = parse_memory_tags(text)
    apply_memory_actions(mem_actions)
    # Work Day control tags (pause / resume / afternoon yes-no).
    text = _apply_work_commands(text)
    return text


def _apply_work_commands(text: str) -> str:
    """Parse {{work…}} tags, update Work Day state, strip tags from speech."""
    if not text or "{{work" not in text.lower():
        return text
    cleaned, actions = parse_work_commands(text)
    BEHAVIOR_RUNTIME = deps.BEHAVIOR_RUNTIME
    _workday_cfg = deps._workday_cfg
    if not actions or BEHAVIOR_RUNTIME is None or getattr(
        BEHAVIOR_RUNTIME, "workday", None
    ) is None:
        return cleaned if actions else text
    try:
        local_dt = datetime.now(_workday_cfg.tz)
        date_s = local_dt.strftime("%Y-%m-%d")
        now = time.time()
        for kind, arg in actions:
            if kind == "afternoon":
                if arg == "yes":
                    BEHAVIOR_RUNTIME.workday.on_afternoon_yes(date_s, now=now)
                    print(f"[workday] afternoon YES for {date_s}")
                else:
                    BEHAVIOR_RUNTIME.workday.on_afternoon_no(date_s)
                    print(f"[workday] afternoon NO for {date_s}")
            elif kind == "pause":
                try:
                    until = pause_until_ts(local_dt, arg, _workday_cfg.tz)
                except ValueError as e:
                    print(f"[workday] ignore bad pause time {arg!r}: {e}")
                    continue
                BEHAVIOR_RUNTIME.workday.on_pause(date_s, until_ts=until)
                print(f"[workday] pause until {arg} ({until})")
            elif kind == "resume":
                BEHAVIOR_RUNTIME.workday.on_resume(date_s)
                print(f"[workday] resume for {date_s}")
    except Exception as e:
        print(f"[workday] command apply failed: {e}")
    return cleaned


def clean_response(text: str) -> str:
    """Speech cleanup with side effects: markdown → forbidden → apply tags →
    phrase fixes → || scrub. Same order as before the parse/apply split."""
    text = strip_markdown(text)
    text = _FORBIDDEN_COMMAND.sub('', text)
    text = extract_memory_commands(text)
    for pattern, replacement in _PHRASE_FIXES:
        text = pattern.sub(replacement, text)
    return _scrub_double_pipes(text)


def _strip_for_speech(text: str) -> str:
    """Pure strip for one-shot spoken lines (sensor/greeting/ambient spoken).

    Markdown + all ``{{…}}`` shells removed; no side effects / no I/O.
    Used by ``llm.one_shot_spoken_line`` and ambient novelty parsing.
    """
    text = strip_markdown(text)
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    text = text.strip().strip('"').strip("'").strip()
    return text

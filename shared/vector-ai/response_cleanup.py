"""Strip markdown, extract memory/work/quiet tags, clean spoken responses."""
import re
import time
from datetime import datetime

import deps
from logging_util import print  # noqa: F401
from process_state import _set_quiet, current_face
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

# Wire-Pod commands the LLM should never emit on its own initiative. The model
# tends to generalise from {{playAnimationWI||x}} and invent these.
# newVoiceRequest is real but disabled here: when it fires, Vector's firmware
# opens a listening session and can hang noisily (~30s) if no speech follows.
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


def extract_memory_commands(text: str) -> str:
    """Find any {{remember[-shared]||...}} or {{forget||...}} in text, act on
    them, return the text with those commands removed."""
    MEMORY = deps.MEMORY
    # Shared memories first - they have no owner.
    for fact in _REMEMBER_SHARED_RE.findall(text):
        stored = MEMORY.remember(fact.strip())
        if stored:
            print(f"[memory] +remember-shared #{stored.id}: {stored.text!r}")
        else:
            print(f"[memory] remember-shared skipped (dup): {fact!r}")
    text = _REMEMBER_SHARED_RE.sub('', text)

    # Personal memories: auto-tag with whoever Vector is looking at right now.
    # If no face is current, fall back to shared (NULL owner) - better to keep
    # the fact untagged than to drop it.
    face = current_face()
    if face and not face["is_stranger"]:
        owner_id, owner_name = face["face_id"], face["name"]
    else:
        owner_id, owner_name = None, None
    for fact in _REMEMBER_RE.findall(text):
        stored = MEMORY.remember(fact.strip(), face_id=owner_id, face_name=owner_name)
        if stored:
            tag = f" [{owner_name}]" if owner_name else " [shared]"
            print(f"[memory] +remember #{stored.id}{tag}: {stored.text!r}")
        else:
            print(f"[memory] remember skipped (dup or empty): {fact!r}")
    text = _REMEMBER_RE.sub('', text)

    for target in _FORGET_RE.findall(text):
        n = MEMORY.forget(target.strip())
        print(f"[memory] -forget matched={n} for {target!r}")
    text = _FORGET_RE.sub('', text)

    # Quiet mode: {{quietMode||on}} when asked to stop commenting unprompted,
    # {{quietMode||off}} when told he may resume.
    for state in _QUIET_RE.findall(text):
        _set_quiet(state.strip().lower() == "on")
    text = _QUIET_RE.sub('', text)

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
    if not actions or BEHAVIOR_RUNTIME.workday is None:
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
    text = strip_markdown(text)
    text = _FORBIDDEN_COMMAND.sub('', text)
    text = extract_memory_commands(text)
    for pattern, replacement in _PHRASE_FIXES:
        text = pattern.sub(replacement, text)
    # Strip leftover `||` outside `{{...}}` blocks.
    segments = re.split(r'(\{\{.*?\}\})', text)
    return "".join(s if s.startswith("{{") and s.endswith("}}") else s.replace("||", "") for s in segments)


def _strip_for_speech(text: str) -> str:
    text = strip_markdown(text)
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    text = text.strip().strip('"').strip("'").strip()
    return text

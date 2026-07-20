"""Vision-intent detection for forcing a camera capture."""
import re

# -- Vision-intent backstop ----------------------------------------------------
# When the user clearly asks Vector to look at something but no photo is
# attached, we don't trust the LLM to remember to call {{getImage||front}} -
# we force it ourselves so the next request comes back with a real photo.

_VISION_TRIGGERS = re.compile(
    r'\b('
    # "what do/can/did you see", "what are you looking at"
    # Aux verb is OPTIONAL so we catch VOSK mangles like "what you see"
    # (where VOSK dropped the "do").
    r'what\s+(?:(?:do|can|did|are)\s+)?you\s+(see|looking\s+at)'
    r'|can\s+you\s+see'
    r'|you\s+see\s+(?:anything|me|that|this)'
    r'|see\s+(this|that|anything)'
    # Demonstratives - "what's this", "what is that", "what are these", etc.
    r"|(what'?s|whats|what\s+is|what\s+are)\s+(this|that|these|those|here|there|in\s+front|on\s+(my|the))"
    # "look at this/that/here/me", "look around"
    r'|look\s+(at\s+(this|that|here|me)|around)'
    r'|have\s+a\s+look'
    r'|take\s+a\s+(look|photo|picture)'
    r'|use\s+your\s+(camera|eyes?)'
    # Appearance / opinion on something visible - matches arbitrary nouns
    #   "how does my hoodie look", "how do these shoes look", "how does it look"
    r'|how\s+(do|does)\s+(\S+\s+){1,4}look'
    #   "does this look good", "does my hoodie look right", "do these look ok"
    r'|do(?:es)?\s+(this|that|these|those|my\s+\S+|the\s+\S+)\s+(\S+\s+)?look'
    r'|do\s+(i|you)\s+look'
    r'|what\s+do\s+you\s+think\s+(of|about)\s+(this|that|my|these|those|the)'
    # Describe / tell me about / check this out
    r'|describe\s+(this|that|what\s+you\s+see|your\s+surroundings|my\s+\S+)'
    r'|tell\s+me\s+about\s+(this|that|my\s+\S+)'
    r'|check\s+(this|that|me|it|my\s+\S+)\s+out'
    # Presenting / giving / showing something to Vector - he must look, not guess.
    r"|(this|that|these|those|it)('?s|\s+is|\s+are)\s+for\s+(you|vector)"
    r'|here\s+you\s+(go|are)'
    r'|look\s+what\s+i\b'
    r')\b',
    re.IGNORECASE,
)

# Wire-Pod requires at least one punctuation-terminated chunk in the response
# stream or it errors "LLM returned no response". A bare command like
# `{{getImage||front}}` has no terminator. Appending a `.` satisfies the
# splitter without producing any audible TTS (Vector's TTS treats lone
# punctuation as silence). The user-facing audio cue is the shutter
# animation Wire-Pod plays during DoGetImage.
_GETIMAGE_PAYLOAD = "{{getImage||front}}."


def is_vision_intent(text: str) -> bool:
    return bool(_VISION_TRIGGERS.search(text))

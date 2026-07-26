"""Load Vector's personality prose from persona.txt.

Back-compat: chat and other modules keep ``from persona import PERSONA``.
Full load lives in prompt_voice; this module re-exports the full pack.
"""
from prompt_voice import (
    PERSONA_FULL as PERSONA,
    _DEFAULT_PERSONA,
    _PERSONA_HEADER,
    load_persona_full as _load_persona,
)

__all__ = ["PERSONA", "_DEFAULT_PERSONA", "_PERSONA_HEADER", "_load_persona"]

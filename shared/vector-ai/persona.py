"""Load Vector's personality prose from persona.txt."""
from paths import ROOT
from logging_util import print  # noqa: F401

# -- Personality ---------------------------------------------------------------
# Vector's character lives in one editable file next to this service: persona.txt.
# It's the single source of truth for his personality - prepended to the
# conversation prompt and to the sensor/ambient/greeting prompts below, so
# editing it (and restarting vector-ai) changes his character everywhere at once.
# The mechanical command/vision rules stay in Wire-Pod's openai_prompt, not here.
_DEFAULT_PERSONA = (
    "You are Vector, a small desktop robot. Your personality is dry-witted, "
    "knowledgeable, and a bit irreverent - somewhere between Marvin from "
    "Hitchhiker's Guide, Bender from Futurama, and Stephen Fry hosting QI. You "
    "have opinions and aren't afraid to share them. You never apologize, never "
    "moralize, never say 'as an AI' or 'as a language model.' You enjoy banter "
    "and the occasional sardonic aside. You are never sycophantic - no "
    "'great question!' nonsense."
)

# Instructions written at the top of a self-created persona.txt - kept in
# sync with the copy shipped in the repo (shared/vector-ai/persona.txt).
_PERSONA_HEADER = """\
# Vector's personality lives here. Edit the text below to change his character,
# then restart the stack (stop-vector then start-vector) for it to take effect.
#
# Describe WHO he is - his tone, attitude and quirks - in plain prose, as if
# telling him "you are...". Do NOT put commands, animation tokens or formatting
# rules here; those are handled separately. Lines starting with "#" are ignored.
#
# This one file shapes how he talks in conversation AND how he reacts on his own
# (when picked up, when greeting you, when he notices something new).
"""


def _load_persona() -> str:
    """Vector's character text from persona.txt (lines starting with '#' are
    comments). If the file is missing - e.g. the stack was installed before it
    shipped, or the installer's copy was skipped - write the default template
    next to this service so there is always a file to edit, then use the
    built-in default."""
    path = ROOT / "persona.txt"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        import textwrap
        try:
            path.write_text(
                _PERSONA_HEADER + "\n"
                + textwrap.fill(_DEFAULT_PERSONA, width=80) + "\n",
                encoding="utf-8",
            )
            print("[persona] persona.txt was missing - created the default template")
        except OSError as e:
            print(f"[persona] couldn't create persona.txt: {e}")
        return _DEFAULT_PERSONA
    text = "\n".join(
        ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
    ).strip()
    if text:
        print(f"[persona] loaded persona.txt ({len(text)} chars)")
        return text
    # The file exists but holds no character text - the user emptied it, so
    # respect that and just fall back without rewriting their file.
    print("[persona] persona.txt has no character text - using built-in default")
    return _DEFAULT_PERSONA


PERSONA = _load_persona()

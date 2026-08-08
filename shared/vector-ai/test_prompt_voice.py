"""Tests for mini prompt packs (ambient / greeting / sensor voice)."""
import sys
from pathlib import Path

# Allow imports when pytest is run from VectorIntelligence root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def test_persona_mini_length_cap():
    from prompt_voice import PERSONA_MINI

    assert isinstance(PERSONA_MINI, str)
    assert len(PERSONA_MINI) > 0
    assert len(PERSONA_MINI) <= 400


def test_persona_mini_no_calibration_or_user_blocks():
    from prompt_voice import PERSONA_MINI

    assert "CALIBRATION" not in PERSONA_MINI
    # Multi User: example blocks must not appear in mini (full persona may have them).
    user_blocks = [ln for ln in PERSONA_MINI.splitlines() if ln.strip().startswith("User:")]
    assert len(user_blocks) <= 1


def test_persona_mini_no_workday_or_command_tokens():
    from prompt_voice import PERSONA_MINI

    assert "{{" not in PERSONA_MINI
    assert "Work Day" not in PERSONA_MINI


def test_persona_full_back_compat():
    from persona import PERSONA
    from prompt_voice import PERSONA_FULL

    assert PERSONA is PERSONA_FULL or PERSONA == PERSONA_FULL
    assert isinstance(PERSONA, str)
    assert len(PERSONA) > 10


def test_ambient_system_uses_mini_not_full_calibration():
    from prompt_voice import PERSONA_FULL, PERSONA_MINI
    from routes.ambient import _AMBIENT_SYSTEM

    assert _AMBIENT_SYSTEM.startswith(PERSONA_MINI)
    assert "PRESENCE: empty" in _AMBIENT_SYSTEM
    assert "CALIBRATION" not in _AMBIENT_SYSTEM
    # Full persona calibration must not be prepended.
    if "CALIBRATION" in PERSONA_FULL:
        assert not _AMBIENT_SYSTEM.startswith(PERSONA_FULL)
    assert PERSONA_FULL not in _AMBIENT_SYSTEM or PERSONA_FULL == PERSONA_MINI


def test_ambient_system_encodes_location_and_change_callouts():
    """SCENE inventory should demand L→R location tags and clearer novelty."""
    from routes.ambient import _AMBIENT_SYSTEM
    from soul.eye import _SCENE_MAX_CHARS, _build_ambient_user_text

    sys = _AMBIENT_SYSTEM
    assert "LEFT" in sys or "left" in sys.lower()
    assert "item@zone" in sys or "@left" in sys or "mug@left" in sys
    assert "from→to" in sys or "from->" in sys or "moved" in sys.lower()
    assert "far-left" in sys and "far-right" in sys
    # Prefer calling clear location changes; not pure "always NOTHING".
    assert "Prefer calling" in sys or "Call out a change" in sys
    assert _SCENE_MAX_CHARS >= 1400

    user = _build_ambient_user_text(
        None,
        mood_text="",
        previous_scene="mug@left-of-monitor; keyboard@center",
    )
    assert "PREVIOUS SCENE" in user
    assert "LEFT" in user or "left" in user.lower()
    assert "item@zone" in user or "location" in user.lower()


def test_greeting_system_uses_mini():
    from prompt_voice import PERSONA_MINI
    from routes.greeting import _GREETING_SYSTEM

    assert _GREETING_SYSTEM.startswith(PERSONA_MINI)
    assert "CALIBRATION" not in _GREETING_SYSTEM


def test_sensor_system_uses_mini():
    from prompt_voice import PERSONA_MINI
    from routes.sensor import _SENSOR_SYSTEM

    assert _SENSOR_SYSTEM.startswith(PERSONA_MINI)
    assert "CALIBRATION" not in _SENSOR_SYSTEM


def test_prompt_assembly_still_uses_full_persona():
    """Chat path must keep full PERSONA via persona re-export."""
    import prompt_assembly
    from persona import PERSONA
    from prompt_voice import PERSONA_FULL

    assert PERSONA == PERSONA_FULL
    # Source still imports from persona (full pack).
    src = Path(prompt_assembly.__file__).read_text(encoding="utf-8")
    assert "from persona import PERSONA" in src

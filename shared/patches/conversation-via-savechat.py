#!/usr/bin/env python3
"""Gate LLM 'conversation mode' (newVoiceRequest) on Save Chat only.

Stock wire-pod requires isKG && SaveChat for the CreatePrompt conversation
NOTE. Intent-graph LLM (normal Hey Vector path) always passes isKG=false, so
multi-turn stays off even when Save Chat is ticked in the UI.

This patch drops the isKG half of the gate so Save Chat alone enables
conversation mode on every StreamingKGSim path. isKG still controls anims /
BehaviorControl timing elsewhere — this file only edits CreatePrompt.

Idempotent. Targets chipper/pkg/wirepod/ttr/kgsim_cmds.go only.
Does not touch ValidLLMCommands, eyeColor, animations, or other kgsim_cmds
patches (unique anchor: isKG && SaveChat before the conversation NOTE).

Usage:
    conversation-via-savechat.py <path-to-kgsim_cmds.go>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Stock gate (Wire Jul 2024 conversations-via-KG design).
_STOCK = re.compile(
    r"if\s+isKG\s*&&\s*vars\.APIConfig\.Knowledge\.SaveChat\s*\{"
)

# Already applied: SaveChat alone immediately before conversation-mode NOTE.
_DONE = re.compile(
    r"if\s+vars\.APIConfig\.Knowledge\.SaveChat\s*\{"
    r"\s*\n\s*promptAppentage\s*:="
    r'\s*"\\n\\nNOTE:\s*You are in \'conversation\' mode\.',
    re.MULTILINE,
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")

    if _DONE.search(src) and not _STOCK.search(src):
        print(f"[conversation-savechat] {path.name} already patched.")
        return False

    m = _STOCK.search(src)
    if not m:
        print(
            f"[conversation-savechat] stock gate not found in {path} "
            f"(expected: if isKG && vars.APIConfig.Knowledge.SaveChat {{)",
            file=sys.stderr,
        )
        sys.exit(1)

    new_src = (
        src[: m.start()]
        + "if vars.APIConfig.Knowledge.SaveChat {"
        + src[m.end() :]
    )
    path.write_text(new_src, encoding="utf-8")
    print(
        f"[conversation-savechat] {path.name}: conversation mode now follows "
        f"Save Chat only (dropped isKG &&)."
    )
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-kgsim_cmds.go>", file=sys.stderr)
        sys.exit(2)
    target = Path(sys.argv[1])
    if not target.is_file():
        print(f"[conversation-savechat] not a file: {target}", file=sys.stderr)
        sys.exit(1)
    patch(target)

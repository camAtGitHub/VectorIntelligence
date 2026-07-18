#!/usr/bin/env python3
"""Stop Wire-Pod's name parser from swallowing the whole sentence.

When someone enrolls a face with "Hey Vector, my name is Sarah, remember my
face", Wire-Pod's intent_names_username handler splits on "my name is" and
takes *everything* after it - enrolling the face as the literal name
"sarah, remember my face." instead of "Sarah".

This patch trims the parsed name at the first comma and at any trailing
"remember my face"-type phrase, then strips stray punctuation - so the
enrolled name is just "Sarah". Applies to both name-handler blocks.

Idempotent. Modifies chipper/pkg/wirepod/ttr/intentparam.go.
"""
import sys
from pathlib import Path

SENTINEL = "Stop the name at a comma"

ANCHOR = (
    "\t\t\t} else if len(splitPhrase) > 4 {\n"
    "\t\t\t\tusername = username + \" \" + strings.TrimSpace(splitPhrase[2]) + \" \" + strings.TrimSpace(splitPhrase[3])\n"
    "\t\t\t}\n"
    "\t\t\tlogger.Println(\"Name parsed from speech: \" + \"`\" + username + \"`\")\n"
)

REPLACEMENT = (
    "\t\t\t} else if len(splitPhrase) > 4 {\n"
    "\t\t\t\tusername = username + \" \" + strings.TrimSpace(splitPhrase[2]) + \" \" + strings.TrimSpace(splitPhrase[3])\n"
    "\t\t\t}\n"
    "\t\t\t// Stop the name at a comma or a trailing \"remember my face\"-type\n"
    "\t\t\t// phrase, so \"my name is Sarah, remember my face\" enrolls just\n"
    "\t\t\t// \"Sarah\" rather than the whole spoken sentence.\n"
    "\t\t\tif ci := strings.Index(username, \",\"); ci != -1 {\n"
    "\t\t\t\tusername = username[:ci]\n"
    "\t\t\t}\n"
    "\t\t\tlowerName := strings.ToLower(username)\n"
    "\t\t\tfor _, tail := range []string{\"remember my face\", \"remember this face\", \"remember me\"} {\n"
    "\t\t\t\tif ti := strings.Index(lowerName, tail); ti != -1 {\n"
    "\t\t\t\t\tusername = username[:ti]\n"
    "\t\t\t\t\tbreak\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
    "\t\t\tusername = strings.Trim(strings.TrimSpace(username), \" .!?,\")\n"
    "\t\t\tlogger.Println(\"Name parsed from speech: \" + \"`\" + username + \"`\")\n"
)


def patch(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if SENTINEL in src:
        print(f"[name-extraction] {path.name} already patched.")
        return False
    if ANCHOR not in src:
        print(f"[name-extraction] anchor not found in {path}", file=sys.stderr)
        sys.exit(1)
    # Both intent_names_username handlers carry the same block - fix both.
    src = src.replace(ANCHOR, REPLACEMENT)
    path.write_text(src, encoding="utf-8", newline="\n")
    print(f"[name-extraction] {path.name} patched.")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-intentparam.go>", file=sys.stderr)
        sys.exit(2)
    patch(Path(sys.argv[1]))

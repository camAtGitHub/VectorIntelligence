"""Rank durable facts for chat memory inject.

Keyword / token-overlap only — no embeddings. Uses existing MemoryStore list
APIs; selection is capped by max_personal / max_shared / max_mentions.
"""
from __future__ import annotations

import os
import re
from typing import List, NamedTuple, Optional, Sequence

from memory import Memory, MemoryStore

# Light stopword set so "the" / "is" don't dominate overlap scores.
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "for",
        "it",
        "i",
        "you",
        "my",
        "me",
        "we",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "with",
        "at",
        "this",
        "that",
        "from",
        "as",
        "but",
        "not",
        "your",
        "so",
        "if",
        "about",
        "what",
        "how",
        "when",
        "who",
        "why",
        "can",
        "could",
        "would",
        "should",
        "will",
        "just",
        "like",
        "its",
        "im",
        "ive",
        "dont",
        "didnt",
    }
)


def _env_int(key: str, default: int) -> int:
    """Parse env int like behaviors/config._int; clamp to >= 0."""
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def inject_limits() -> tuple[int, int, int]:
    """(max_personal, max_shared, max_mentions) from env or defaults 8/4/3."""
    return (
        _env_int("MEMORY_INJECT_PERSONAL", 8),
        _env_int("MEMORY_INJECT_SHARED", 4),
        _env_int("MEMORY_INJECT_MENTIONS", 3),
    )


def _tokens(text: str) -> set[str]:
    toks = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return {t for t in toks if len(t) > 1 and t not in _STOP}


def _rank(candidates: Sequence[Memory], query: str, limit: int) -> List[Memory]:
    """Top-k by token overlap with query; tie-break: higher id (newer) first."""
    if limit <= 0 or not candidates:
        return []
    q = _tokens(query)
    scored: List[tuple[int, int, Memory]] = []
    for m in candidates:
        score = len(_tokens(m.text) & q) if q else 0
        scored.append((score, m.id, m))
    # Higher overlap first; on ties prefer newer rows (higher id).
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [m for _, _, m in scored[:limit]]


class SelectedMemories(NamedTuple):
    personal: List[Memory]
    shared: List[Memory]
    mentions: List[Memory]


def select_memories_for_turn(
    memory: MemoryStore,
    face: Optional[dict],
    last_user_text: str,
    *,
    max_personal: Optional[int] = None,
    max_shared: Optional[int] = None,
    max_mentions: Optional[int] = None,
) -> SelectedMemories:
    """Rank durable facts for this turn. No embeddings.

    Candidate load mirrors prompt_assembly identity branches:
    - known face → personal + shared + mentions
    - stranger → shared only (no personal leak)
    - no face / single primary → that profile's personal + shared
    - no face / multi-user → shared only

    Ranking: alphanumeric lower token overlap with last_user_text (stopword-
    light); stable tie-break by higher memory id (newer). Caps apply per bucket.
    """
    if max_personal is None or max_shared is None or max_mentions is None:
        env_p, env_s, env_m = inject_limits()
        if max_personal is None:
            max_personal = env_p
        if max_shared is None:
            max_shared = env_s
        if max_mentions is None:
            max_mentions = env_m

    max_personal = max(0, int(max_personal))
    max_shared = max(0, int(max_shared))
    max_mentions = max(0, int(max_mentions))

    personal_cands: List[Memory] = []
    mentions_cands: List[Memory] = []
    shared_cands = memory.list_shared(limit=100)

    if face and not face.get("is_stranger"):
        fid = face.get("face_id")
        if fid is not None:
            personal_cands = memory.list_for_face(fid, limit=100)
        name = (face.get("name") or "").strip()
        if name:
            mentions_cands = memory.list_mentions_of_name(
                name, exclude_face_id=fid, limit=20
            )
    elif face and face.get("is_stranger"):
        # Shared only — never personal lists of enrolled people.
        pass
    else:
        profiles = memory.distinct_faces()
        if len(profiles) == 1:
            pid, _pname = profiles[0]
            personal_cands = memory.list_for_face(pid, limit=100)

    return SelectedMemories(
        personal=_rank(personal_cands, last_user_text, max_personal),
        shared=_rank(shared_cands, last_user_text, max_shared),
        mentions=_rank(mentions_cands, last_user_text, max_mentions),
    )

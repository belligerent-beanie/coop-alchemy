"""The emotes players can send each other in a shared game.

This list is the one place that decides what an emote is. The glyphs are placeholders until the emotes are designed: change
`glyph` (or later add an `image`) here and every page picks the new look up through GET /emotes. Players can only send an `id`
from this list, never free text, so an emote can never carry a message the administrators would have to moderate.
"""
from __future__ import annotations

from typing import Any

EMOTES: tuple[dict[str, str], ...] = (
    {"id": "wave", "label": "Wave", "glyph": "👋"},
    {"id": "cheer", "label": "Cheer", "glyph": "🎉"},
    {"id": "wow", "label": "Wow", "glyph": "😮"},
    {"id": "think", "label": "Thinking", "glyph": "🤔"},
    {"id": "thanks", "label": "Thanks", "glyph": "👍"},
    {"id": "love", "label": "Love it", "glyph": "❤️"},
)

EMOTE_IDS = frozenset(e["id"] for e in EMOTES)
EMOTE_GAP_SECONDS = 1.0   # the least time between two emotes from one player


def emote_list() -> list[dict[str, Any]]:
    return [dict(e) for e in EMOTES]

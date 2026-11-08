"""A short note about an element, shown on its journal page once the element is found (notes.json, next to this file).

The file is {"notes": {"<element label>": "<text>"}}; labels are matched without regard to case. A note is plain text of at most
MAX_NOTE characters, a sentence or two that helps people remember what the thing is. An element with no note simply shows none.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_NOTE = 300
_FILE = Path(__file__).with_name("notes.json")

_loaded: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _loaded
    if _loaded is None:
        try:
            raw = json.loads(_FILE.read_text(encoding="utf-8"))["notes"]
            _loaded = {label.casefold(): text for label, text in raw.items() if isinstance(text, str) and text.strip()}
        except (OSError, ValueError, KeyError, AttributeError):
            _loaded = {}
    return _loaded


def of(label: str) -> str:
    return _load().get(label.casefold(), "")

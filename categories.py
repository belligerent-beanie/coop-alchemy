"""Which category each element belongs to (categories.json, next to this file).

An element that is not in the file (a newly approved one, or one in a test recipe book) falls into "Other", so adding elements to the
game never breaks anything; test_categories.py reminds us to sort the new ones.
"""
from __future__ import annotations

import json
from pathlib import Path

OTHER = "Other"
_FILE = Path(__file__).with_name("categories.json")

_loaded: tuple[list[str], dict[str, str]] | None = None


def _load() -> tuple[list[str], dict[str, str]]:
    global _loaded
    if _loaded is None:
        order: list[str] = []
        by_label: dict[str, str] = {}
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            for category in data["categories"]:
                order.append(category["name"])
                for label in category["elements"]:
                    by_label[label.casefold()] = category["name"]
        except (OSError, ValueError, KeyError, TypeError):
            order, by_label = [], {}
        _loaded = (order, by_label)
    return _loaded


def names() -> list[str]:
    """The categories in the order they are shown."""
    return list(_load()[0])


def of(label: str) -> str:
    return _load()[1].get(label.casefold(), OTHER)

"""A small file of past ratings, so asking the judging model again about the same recipe costs nothing.

Key: model + target + the two ingredients (in either order). Ratings from one model are never reused for another.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .propose import Judgement

MAX_ENTRIES = 20000   # per file; the oldest are dropped first


class RatingCache:
    def __init__(self, path: Path | str | None, model: str) -> None:
        self.path = Path(path) if path else None
        self.model = model or ""
        self.hits = 0
        self._dirty = False
        self._data: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("ratings"), dict):
                    self._data = loaded["ratings"]
            except (OSError, ValueError):
                self._data = {}   # missing or damaged: start again (it is only a cache)

    def _key(self, goal: str, a: str, b: str) -> str:
        x, y = sorted((a.casefold().strip(), b.casefold().strip()))
        return f"{self.model}|{goal.casefold().strip()}|{x}|{y}"

    def get(self, goal: str, a: str, b: str) -> Judgement | None:
        row = self._data.get(self._key(goal, a, b))
        if not isinstance(row, dict) or not isinstance(row.get("rating"), int):
            return None
        self.hits += 1
        others = row.get("others")
        return Judgement(row["rating"], str(row.get("reads_as", "")),
                         tuple(str(o) for o in others) if isinstance(others, list) else ())

    def put(self, goal: str, a: str, b: str, judgement: Judgement) -> None:
        self._data[self._key(goal, a, b)] = {"rating": judgement.rating, "reads_as": judgement.reads_as,
                                             "others": list(judgement.others)}
        self._dirty = True

    def save(self) -> None:
        if self.path is None or not self._dirty:
            return
        while len(self._data) > MAX_ENTRIES:
            self._data.pop(next(iter(self._data)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"ratings": self._data}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

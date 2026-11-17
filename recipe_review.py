"""A temporary once-over of every recipe in the book, one card at a time (the /admin/recipes page).

It follows the proposal review in drafts.py (look at `A + B = C`, answer, undo) but it reviews the recipes the game already
has. Each recipe gets one verdict, shared by all administrators: *ok* or *flag*, either with an optional comment.
Nothing here edits recipes.json: the flagged recipes are listed on the page for you to fix by hand, then run
`python3 reload.py`.

When the review is finished, delete this file, test_recipe_review.py, static/recipe-review.html, .js and .css, the
`/admin/recipes` routes in server.py and the `recipe_review.json` line in .gitignore.

The verdicts live in one JSON file (recipe_review.json), written atomically. A damaged file is never overwritten.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from accounts import key_of
from recipes import RecipeBook
from state import GameError

OK, FLAG = "ok", "flag"
MAX_NOTE_LEN = 300


def recipe_id(a: str, b: str, result: str) -> str:
    """A stable address for one recipe (a pair and one of its results), however the file orders them."""
    first, second = (a, b) if a <= b else (b, a)
    return json.dumps([first, second, result], ensure_ascii=False, separators=(",", ":"))


def every_recipe(book: RecipeBook) -> list[tuple[str, str, str, str]]:
    """(id, a, b, result) for every recipe the book makes, in a steady order: by result name, then by ingredients."""
    rows = [(recipe_id(a, b, r), a, b, r) for (a, b), results in book.lookup.items() for r in results]
    rows.sort(key=lambda row: (book.elements[row[3]].label.casefold(), row[1].casefold(), row[2].casefold()))
    return rows


class RecipeReview:
    """The verdicts. `path=None` keeps them in memory (tests)."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"version": 1, "verdicts": {}}

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "verdicts": {}}
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the recipe review file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("verdicts"), dict):
            raise GameError(500, f"the recipe review file ({self.path.name}) is not in the expected format")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the verdict could not be saved (see the server console)") from exc

    # ---- reading -------------------------------------------------------

    @staticmethod
    def _element(book: RecipeBook, eid: str) -> dict[str, Any]:
        e = book.elements[eid]
        return {"id": eid, "label": e.label, "emoji": e.emoji, "draft": False}

    def _card(self, book: RecipeBook, row: tuple[str, str, str, str]) -> dict[str, Any]:
        rid, a, b, result = row
        return {"id": rid, "a": self._element(book, a), "b": self._element(book, b), "result": self._element(book, result),
                "also": [book.elements[r].label for r in book.combine(a, b) if r != result]}

    def queue(self, book: RecipeBook, limit: int = 20) -> dict[str, Any]:
        """The next recipes nobody has answered yet, plus the counts."""
        with self._lock:
            verdicts = self._load()["verdicts"]
            rows = every_recipe(book)
            current = {row[0] for row in rows}
            left = [row for row in rows if row[0] not in verdicts]
            kept = [v for rid, v in verdicts.items() if rid in current]   # a recipe edited out of the book no longer counts
            return {"cards": [self._card(book, row) for row in left[:max(1, min(limit, 100))]],
                    "total": len(rows), "left": len(left),
                    "ok": sum(1 for v in kept if v["verdict"] == OK), "flagged": sum(1 for v in kept if v["verdict"] == FLAG)}

    def flagged(self, book: RecipeBook) -> list[dict[str, Any]]:
        """Every flagged recipe with the reviewer's note, for fixing by hand."""
        with self._lock:
            verdicts = self._load()["verdicts"]
            return [{**self._card(book, row), "note": verdicts[row[0]]["note"], "by": verdicts[row[0]]["by"]}
                    for row in every_recipe(book) if verdicts.get(row[0], {}).get("verdict") == FLAG]

    def commented(self, book: RecipeBook) -> list[dict[str, Any]]:
        """Recipes marked ok with a comment (the flagged ones carry theirs in `flagged`)."""
        with self._lock:
            verdicts = self._load()["verdicts"]
            return [{**self._card(book, row), "note": verdicts[row[0]]["note"], "by": verdicts[row[0]]["by"]}
                    for row in every_recipe(book)
                    if verdicts.get(row[0], {}).get("verdict") == OK and verdicts[row[0]]["note"]]

    # ---- answering -----------------------------------------------------

    def handle(self, admin: str, book: RecipeBook, rid: object, verdict: object, note: object) -> dict[str, Any]:
        """Record ok or flag for one recipe nobody has answered yet."""
        if verdict not in (OK, FLAG):
            raise GameError(400, "verdict must be ok or flag")
        if note is not None and not isinstance(note, str):
            raise GameError(400, "the note must be text")
        text = "".join(ch for ch in (note or "").strip() if ch == "\n" or ch.isprintable())[:MAX_NOTE_LEN]
        with self._lock:
            if not isinstance(rid, str) or rid not in {row[0] for row in every_recipe(book)}:
                raise GameError(404, "no such recipe")
            data = self._load()
            earlier = data["verdicts"].get(rid)
            if earlier is not None:
                raise GameError(409, f"that recipe was already marked {earlier['verdict']} by {earlier['by']}")
            data["verdicts"][rid] = {"verdict": verdict, "note": text, "by": admin, "at": self._clock()}
            self._save(data)
            return {"id": rid, "verdict": verdict}

    def undo(self, admin: str, rid: object) -> dict[str, Any]:
        """Take back this admin's own answer; the recipe waits again."""
        with self._lock:
            data = self._load()
            earlier = data["verdicts"].get(rid) if isinstance(rid, str) else None
            if earlier is None:
                raise GameError(409, "that recipe is already waiting")
            if key_of(earlier["by"]) != key_of(admin):
                raise GameError(403, f"only {earlier['by']} can take that answer back")
            del data["verdicts"][rid]
            self._save(data)
            return {"id": rid, "verdict": None}

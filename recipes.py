"""Recipe book: loading, validation and lookup (standard library only).

File format (recipes.json):

    {
      "starters": ["water", ...],
      "elements": {"water": {"label": "Water", "emoji": "💧"}, ...},
      "recipes": [{"a": "water", "b": "fire", "result": "steam"}, ...]
    }

Element ids are free-form text (spaces, capitals and punctuation are fine) as
long as they are non-empty, have no leading/trailing whitespace or control
characters, and no two ids differ only by capitalisation.
`result` may also be a list of element ids, in which case the combination
yields several elements at once. `emoji` is optional.

A pair may be listed more than once (a + b and b + a are the same pair). Exact
repeats are dropped; if the repeats give different results, the pair yields all
of them, in file order. Such cleanups are reported in RecipeBook.notes instead
of being rejected.
"""
from __future__ import annotations

import difflib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MAX_ID_LEN = 64


def id_problem(eid: str) -> str | None:
    """Why `eid` is not an acceptable element id, or None if it is fine.

    Ids may contain spaces, capitals and punctuation; they only need to be
    unambiguous and printable.
    """
    if not eid.strip():
        return "must not be empty"
    if eid != eid.strip():
        return "must not start or end with whitespace"
    if len(eid) > MAX_ID_LEN:
        return f"must be at most {MAX_ID_LEN} characters"
    if any(unicodedata.category(ch).startswith("C") for ch in eid):
        return "must not contain control characters"
    return None


class RecipeError(ValueError):
    """Raised when a recipe file is unreadable or invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid recipe file:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class Element:
    id: str
    label: str
    emoji: str = ""
    credit: str = ""    # who proposed it, for elements that came from an approved player proposal


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-independent key for a pair of element ids."""
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True)
class RecipeBook:
    starters: tuple[str, ...]
    elements: dict[str, Element]
    lookup: dict[tuple[str, str], tuple[str, ...]]
    notes: tuple[str, ...] = ()  # what parse_recipes cleaned up (repeated recipes)

    def combine(self, a: str, b: str) -> tuple[str, ...]:
        """Element ids produced by combining a and b (empty if no recipe)."""
        return self.lookup.get(pair_key(a, b), ())


def _suggest(name: object, known: Any) -> str:
    close = difflib.get_close_matches(str(name), list(known), n=1)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _result_ids(raw: object) -> list[str] | None:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and raw and all(isinstance(r, str) for r in raw):
        return list(raw)
    return None


def find_problems(data: Any) -> list[str]:
    """Return every structural problem in the parsed JSON (empty list = valid)."""
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]

    problems: list[str] = []
    elements = data.get("elements")
    starters = data.get("starters")
    recipes = data.get("recipes")

    if not isinstance(elements, dict) or not elements:
        problems.append("'elements' must be a non-empty object")
        elements = {}
    if not isinstance(starters, list) or not starters:
        problems.append("'starters' must be a non-empty list")
        starters = []
    if not isinstance(recipes, list):
        problems.append("'recipes' must be a list")
        recipes = []

    by_lower: dict[str, str] = {}
    for eid, info in elements.items():
        reason = id_problem(eid)
        if reason:
            problems.append(f"element id {eid!r} {reason}")
        elif eid.lower() in by_lower:
            problems.append(f"element ids {by_lower[eid.lower()]!r} and {eid!r} differ only by capitalisation")
        else:
            by_lower[eid.lower()] = eid
        if not isinstance(info, dict) or not isinstance(info.get("label"), str) or not info["label"].strip():
            problems.append(f"element {eid!r} needs a non-empty string 'label'")
        elif not isinstance(info.get("emoji", ""), str):
            problems.append(f"element {eid!r}: 'emoji' must be a string")
        elif not isinstance(info.get("credit", ""), str):
            problems.append(f"element {eid!r}: 'credit' must be a string")

    seen_starters: set[str] = set()
    for s in starters:
        if not isinstance(s, str) or s not in elements:
            problems.append(f"starter {s!r} is not a defined element{_suggest(s, elements)}")
        elif s in seen_starters:
            problems.append(f"starter {s!r} is listed twice")
        else:
            seen_starters.add(s)

    for i, recipe in enumerate(recipes, 1):
        where = f"recipe #{i}"
        if not isinstance(recipe, dict):
            problems.append(f"{where}: must be an object")
            continue
        a, b = recipe.get("a"), recipe.get("b")
        results = _result_ids(recipe.get("result"))
        if not isinstance(a, str) or not isinstance(b, str) or results is None:
            problems.append(f"{where}: needs string 'a' and 'b' and a 'result' string or non-empty list of strings")
            continue
        for role, name in (("a", a), ("b", b), *[("result", r) for r in results]):
            if name not in elements:
                problems.append(f"{where}: unknown element {name!r} in '{role}'{_suggest(name, elements)}")
    return problems


def _build_lookup(recipes: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], tuple[str, ...]], list[str]]:
    """Merge recipes into one entry per pair (input must already be valid)."""
    lookup: dict[tuple[str, str], list[str]] = {}
    seen: set[tuple[str, str]] = set()
    notes: list[str] = []
    for i, recipe in enumerate(recipes, 1):
        a, b = recipe["a"], recipe["b"]
        key = pair_key(a, b)
        results = lookup.setdefault(key, [])
        repeated_pair = key in seen
        seen.add(key)
        for r in _result_ids(recipe["result"]) or []:
            if r in results:
                if repeated_pair:
                    notes.append(f"recipe #{i}: {a} + {b} -> {r} is already defined for this pair; ignored")
            else:
                results.append(r)
                if repeated_pair:
                    notes.append(f"recipe #{i}: {a} + {b} also gives {r}; merged into one combination")
    return {key: tuple(results) for key, results in lookup.items()}, notes


def parse_recipes(data: Any) -> RecipeBook:
    """Validate parsed JSON and build a RecipeBook; raises RecipeError.

    Repeated pairs are cleaned up rather than rejected (see the module docstring).
    """
    problems = find_problems(data)
    if problems:
        raise RecipeError(problems)
    elements = {
        eid: Element(eid, info["label"].strip(), info.get("emoji", ""), info.get("credit", "").strip())
        for eid, info in data["elements"].items()
    }
    lookup, notes = _build_lookup(data["recipes"])
    return RecipeBook(tuple(data["starters"]), elements, lookup, tuple(notes))


def load_recipes(path: str | Path) -> RecipeBook:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecipeError([f"cannot read {path}: {exc}"]) from exc
    return parse_recipes(data)


def depths(book: RecipeBook, free: Iterable[str] = ()) -> dict[str, int]:
    """Minimum number of combination steps needed to obtain each reachable element.

    `free` are elements the game hands out without a recipe (Time), counted like starters: recipes that need them are reachable.
    """
    depth = {s: 0 for s in book.starters}
    depth.update({e: 0 for e in free if e in book.elements})
    changed = True
    while changed:
        changed = False
        for (a, b), results in book.lookup.items():
            if a in depth and b in depth:
                d = max(depth[a], depth[b]) + 1
                for r in results:
                    if d < depth.get(r, d + 1):
                        depth[r] = d
                        changed = True
    return depth


def reachable(book: RecipeBook) -> set[str]:
    """Elements a player can eventually discover starting from the starters."""
    return set(depths(book))

"""Hint logic: pure functions over a RecipeBook and the set of discovered elements.

Everything here works on element ids; state.py turns the answers into JSON.
"""
from __future__ import annotations

import random
from typing import Iterable

from recipes import RecipeBook

UNREACHABLE = 10**9


def new_combinations(book: RecipeBook, discovered: Iterable[str]) -> list[tuple[str, str, str]]:
    """Every (a, b, result) that can be made right now and gives something not yet discovered."""
    have = set(discovered)
    return [
        (a, b, result)
        for (a, b), results in book.lookup.items()
        if a in have and b in have
        for result in results
        if result not in have
    ]


def hint_can_make(book: RecipeBook, discovered: Iterable[str], rng: random.Random) -> str | None:
    """An undiscovered element that can be made with the current elements (not how)."""
    results = sorted({r for _, _, r in new_combinations(book, discovered)})
    return rng.choice(results) if results else None


def hint_try(book: RecipeBook, discovered: Iterable[str], rng: random.Random) -> tuple[str, str] | None:
    """A pair of current elements that makes something new (not what it makes)."""
    combos = new_combinations(book, discovered)
    if not combos:
        return None
    a, b, _ = rng.choice(combos)
    return (a, b) if rng.random() < 0.5 else (b, a)


def find_element(book: RecipeBook, text: str) -> str | None:
    """Look an element up by id or label, ignoring case and surrounding spaces."""
    wanted = " ".join(text.split()).casefold()
    if not wanted:
        return None
    for eid, element in book.elements.items():
        if eid.casefold() == wanted or element.label.casefold() == wanted:
            return eid
    return None


def hint_recipe(book: RecipeBook, discovered: Iterable[str], depth: dict[str, int],
                target: str) -> tuple[str, str] | None:
    """One recipe that makes `target`, preferring ingredients the players already have.

    Ties go to the recipe whose ingredients are closest to the starting elements.
    """
    have = set(discovered)
    best: tuple[tuple[int, int], tuple[str, str]] | None = None
    for (a, b), results in book.lookup.items():
        if target not in results:
            continue
        missing = (a not in have) + (b not in have)
        far = max(depth.get(a, UNREACHABLE), depth.get(b, UNREACHABLE))
        if best is None or (missing, far) < best[0]:
            best = ((missing, far), (a, b))
    return best[1] if best else None

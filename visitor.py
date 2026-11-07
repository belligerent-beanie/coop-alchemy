"""Mr Alchemy: a once-per-game visitor who hands out a handful of elements.

When a public game has had at least two people playing together for a random while, Mr Alchemy arrives, the page plays a short cutscene
("You've been struck by Mr Alchemy"), 5 to 10 new elements are unlocked for the whole room, and he is gone. This module holds the
rules that need no game state: the name, the schedule, and how the gift is chosen. state.py applies them.

The gift is chosen so that it always makes sense in the journal: every gifted element can be made from what the room already has plus the
gifts picked before it, so no gift is an element whose ingredients nobody has found.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Iterable

from hints import new_combinations
from recipes import RecipeBook

MR_ALCHEMY = "Mr Alchemy"     # also the "found by" of what he gives, and a name nobody can register
GIFT_MIN, GIFT_MAX = 5, 10    # how many elements he gives


@dataclass
class VisitorPolicy:
    """When and whether Mr Alchemy visits. A game built without one is never visited."""
    enabled: bool = True
    chance: float = 1.0           # the chance that a game gets its visit at all
    min_seconds: float = 240.0    # after this long with two or more people playing together, at the earliest...
    max_seconds: float = 900.0    # ...and by this long at the latest


def _flat(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def is_reserved(name: str, extra: Iterable[str] = ()) -> bool:
    """True for a username that would pass for Mr Alchemy (or any name in `extra`), however it is spaced or punctuated."""
    flat = _flat(name)
    return bool(flat) and flat in {_flat(MR_ALCHEMY), *(_flat(n) for n in extra)}


def pick_gift(book: RecipeBook, discovered: Iterable[str], rng: random.Random, count: int | None = None) -> list[str]:
    """5 to 10 undiscovered elements, chosen at random from what can be made now and then from what the first picks unlock.

    Fewer come back only when the book has fewer left. Each element can be made from the discovered ones plus the ones before it in the list.
    """
    want = rng.randint(GIFT_MIN, GIFT_MAX) if count is None else count
    have = set(discovered)
    chosen: list[str] = []
    while len(chosen) < want:
        frontier = sorted({result for _, _, result in new_combinations(book, have)})
        if not frontier:
            break
        take = rng.sample(frontier, min(len(frontier), want - len(chosen)))
        chosen.extend(take)
        have.update(take)
    return chosen

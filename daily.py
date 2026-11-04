"""The daily challenge: one random target element per day, and a board of the best scores.

Everyone gets the same target on a given day. They start from the starting elements alone
and try to discover the target while unlocking as few elements as possible (every element
discovered on the way counts, the target included, the starters do not). A player gets
MAX_ATTEMPTS attempts a day (starting one uses it up, finished or not; carrying on with one that is
still in progress is free); the board keeps each player's best finished attempt of the day.

The day's target is chosen once, from the date, and stored in daily.json so that it does not
change if the recipe file is edited later. It is picked from elements that are neither trivial
nor hopeless to find. The "reference" is the number of elements a good route unlocks. It comes
from a simple greedy search, so it is a number to beat, not a proven minimum.

"Today" is the date on the server's computer. daily.json is written atomically, and a damaged
file is never overwritten.
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from accounts import key_of
from recipes import RecipeBook, depths

MIN_DEPTH = 3        # a target needs at least this many steps from the starters...
MAX_DEPTH = 8        # ...and at most this many (deeper ones are too hard to find without hints)
MAX_REFERENCE = 18   # skip targets whose reference route unlocks more elements than this
SEARCH_PASSES = 40   # randomised greedy passes used to improve the reference route sizes
KEEP_DAYS = 30       # older days are dropped from the file
BOARD_SIZE = 10      # scores shown on the home page
MAX_ATTEMPTS = 3     # attempts per player per day


def reference_sizes(book: RecipeBook, depth: dict[str, int], passes: int = SEARCH_PASSES) -> dict[str, int]:
    """For each reachable element that is not a starter, how many elements a good route unlocks.

    Elements are handled from the shallowest up. Each one takes the recipe whose whole route (the
    ingredients' routes, plus everything the recipe itself makes) is smallest. A recipe may only
    use elements shallower than its result, so every route can be followed in order. Later passes
    break ties at random, and the best size found for each element is kept.
    """
    starters = set(book.starters)
    producers: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for (a, b), results in book.lookup.items():
        if a in depth and b in depth:
            for result in results:
                if depth[a] < depth.get(result, -1) and depth[b] < depth.get(result, -1):
                    producers.setdefault(result, []).append((a, b, results))
    order = sorted((e for e in depth if e not in starters and e in producers), key=lambda e: (depth[e], e))

    best: dict[str, int] = {}
    for n in range(passes + 1):
        rng = random.Random(n) if n else None   # pass 0 is the plain greedy choice
        route: dict[str, frozenset[str]] = {s: frozenset() for s in starters}
        for element in order:
            options = []
            for a, b, results in producers[element]:
                if a in route and b in route:
                    found = route[a] | route[b] | {r for r in results if r not in starters}
                    options.append(found)
            if not options:
                continue
            smallest = min(len(o) for o in options)
            pool = [o for o in options if len(o) <= smallest + (1 if rng else 0)]
            route[element] = rng.choice(pool) if rng else pool[0]
            best[element] = min(best.get(element, len(route[element])), len(route[element]))
    return best


def pick_target(book: RecipeBook, day: str) -> tuple[str, int]:
    """The target for `day` ("2026-11-04") and its reference size. The same day always gives the same pick."""
    depth = depths(book)
    sizes = reference_sizes(book, depth)
    pool = sorted(e for e, size in sizes.items()
                  if MIN_DEPTH <= depth[e] <= MAX_DEPTH and size <= MAX_REFERENCE)
    if not pool:           # a small recipe file: take anything that can be made
        pool = sorted(sizes)
    if not pool:
        raise ValueError("the recipes have nothing to use as a daily target")
    target = random.Random(day).choice(pool)
    return target, sizes[target]


class DailyStore:
    """Today's target and the scores. `path=None` keeps everything in memory (tests)."""

    def __init__(self, path: str | Path | None, book: RecipeBook, *,
                 today: Callable[[], str] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.book = book
        self._today = today or (lambda: date.today().isoformat())
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"version": 1, "days": {}}
        self._depth: dict[str, int] | None = None

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "days": {}}
        except (OSError, ValueError) as exc:
            raise ValueError(f"the daily challenge file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise ValueError(f"the daily challenge file ({self.path.name}) is not in the expected format")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"warning: could not save the daily challenge file: {exc}", file=sys.stderr)

    def _target_is_usable(self, target: object) -> bool:
        if self._depth is None:
            self._depth = depths(self.book)
        return isinstance(target, str) and target in self.book.elements and target in self._depth \
            and target not in self.book.starters

    # ---- the day's challenge -----------------------------------------------

    def today(self) -> dict[str, Any]:
        """{"date", "target" (element id), "reference"} for today, choosing and storing the target the first time."""
        with self._lock:
            day = self._today()
            data = self._load()
            days = data["days"]
            entry = days.get(day)
            changed = False
            if not isinstance(entry, dict) or not self._target_is_usable(entry.get("target")):
                target, reference = pick_target(self.book, day)
                kept = entry if isinstance(entry, dict) else {}   # a replaced target keeps the day's scores and attempts
                entry = days[day] = {"target": target, "reference": reference,
                                     "scores": kept.get("scores") if isinstance(kept.get("scores"), dict) else {},
                                     "attempts": kept.get("attempts") if isinstance(kept.get("attempts"), dict) else {}}
                changed = True
            for old in sorted(days)[:-KEEP_DAYS]:
                del days[old]
                changed = True
            if changed:
                self._save(data)
            reference = entry.get("reference")
            return {"date": day, "target": entry["target"],
                    "reference": reference if isinstance(reference, int) and not isinstance(reference, bool) else None}

    def submit(self, day: str, username: str, score: int) -> bool:
        """Record a finished attempt; only a player's best score of the day is kept. True if it is their new best."""
        with self._lock:
            data = self._load()
            entry = data["days"].get(day)
            if not isinstance(entry, dict) or not isinstance(entry.get("scores"), dict):
                return False
            key = key_of(username)
            old = entry["scores"].get(key)
            if isinstance(old, dict) and isinstance(old.get("score"), int) and old["score"] <= score:
                return False
            entry["scores"][key] = {"name": username, "score": score, "at": self._clock()}
            self._save(data)
            return True

    def attempts(self, day: str, username: str) -> int:
        """How many attempts this account has started on `day`."""
        with self._lock:
            entry = self._load()["days"].get(day)
            used = entry.get("attempts") if isinstance(entry, dict) else None
            count = used.get(key_of(username)) if isinstance(used, dict) else None
            return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0

    def use_attempt(self, day: str, username: str) -> int:
        """Count one more started attempt for this account; returns how many it has used on `day` now."""
        with self._lock:
            data = self._load()
            entry = data["days"].get(day)
            if not isinstance(entry, dict):
                return 0
            used = entry.get("attempts")
            if not isinstance(used, dict):
                used = entry["attempts"] = {}
            key = key_of(username)
            count = self.attempts(day, username) + 1
            used[key] = count
            self._save(data)
            return count

    def best(self, day: str, username: str) -> int | None:
        with self._lock:
            entry = self._load()["days"].get(day)
            scores = entry.get("scores") if isinstance(entry, dict) else None
            mine = scores.get(key_of(username)) if isinstance(scores, dict) else None
            return mine["score"] if isinstance(mine, dict) and isinstance(mine.get("score"), int) else None

    def board(self, day: str, limit: int = BOARD_SIZE) -> list[dict[str, Any]]:
        """The best scores of `day`, lowest first (the earlier finisher wins a tie)."""
        with self._lock:
            entry = self._load()["days"].get(day)
            scores = entry.get("scores") if isinstance(entry, dict) else None
            rows = [s for s in (scores or {}).values()
                    if isinstance(s, dict) and isinstance(s.get("score"), int) and isinstance(s.get("name"), str)]
            rows.sort(key=lambda s: (s["score"], s.get("at", 0)))
            return [{"name": s["name"], "score": s["score"]} for s in rows[:limit]]

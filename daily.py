"""The daily challenge: three random target elements a day (easy, medium, hard), and the boards.

Everyone gets the same targets on a given day. A player starts from the starting elements alone and tries
to discover a target while unlocking as few elements as possible (every element discovered on the way
counts, the target included, the starters do not). Each tier has its own board, and each player gets
MAX_ATTEMPTS attempts per tier per day: starting an attempt uses one up, finished or not, and carrying on
with one that is still in progress is free. A player's best finished attempt of each tier is kept.

Streaks, the past days and the weekly board are all worked out from the same stored scores:
  - the streak is the number of days in a row with at least one finished attempt;
  - a finished tier is worth 100 * reference / score points (matching the reference route is 100, using
    twice as many elements is 50), and the weekly board adds those up over the ISO week (Monday to Sunday).

The day's targets are chosen once, from the date, and stored in daily.json so that they do not change if
the recipe file is edited later. Each tier draws from elements in a band of distance from the starters.
The "reference" is the number of elements a good route unlocks, and the stored "route" is that route. It
comes from a simple greedy search, so it is a number to beat, not a proven minimum. A route is only shown
to a player who can no longer play that tier today (or for days that are over).

"Today" is the date on the server's computer. daily.json is written atomically, and a damaged file is never
overwritten.
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from accounts import key_of
from recipes import RecipeBook, depths

TIERS = ("easy", "medium", "hard")
# tier: (fewest steps from the starters, most steps, most elements the reference route may unlock)
TIER_BANDS = {"easy": (3, 5, 10), "medium": (6, 8, 16), "hard": (9, 10, 24)}
MAX_ATTEMPTS = 3     # attempts per player per tier per day
SEARCH_PASSES = 40   # randomised greedy passes used to improve the reference routes
KEEP_DAYS = 400      # older days are dropped from the file
BOARD_SIZE = 10      # scores shown on the home page
HISTORY_DAYS = 30    # past days shown

Step = tuple[str, str, tuple[str, ...]]   # ingredient, ingredient, everything the recipe makes


# ---- picking targets and routes -------------------------------------------------------------------

def _passes(book: RecipeBook, depth: dict[str, int], passes: int) -> Iterator[tuple[dict[str, frozenset[str]], dict[str, Step]]]:
    """Greedy route building, once plainly and then `passes` times with random tie-breaks.

    Elements are handled from the shallowest up. Each takes the recipe whose whole route (the ingredients'
    routes, plus everything the recipe itself makes) is smallest. A recipe may only use elements shallower
    than its result, so every route can be followed in order. Yields, per pass, the elements each route
    unlocks and the recipe chosen for each element.
    """
    starters = set(book.starters)
    producers: dict[str, list[Step]] = {}
    for (a, b), results in book.lookup.items():
        if a in depth and b in depth:
            for result in results:
                if depth[a] < depth.get(result, -1) and depth[b] < depth.get(result, -1):
                    producers.setdefault(result, []).append((a, b, results))
    order = sorted((e for e in depth if e not in starters and e in producers), key=lambda e: (depth[e], e))
    for n in range(passes + 1):
        rng = random.Random(n) if n else None   # pass 0 is the plain greedy choice
        route: dict[str, frozenset[str]] = {s: frozenset() for s in starters}
        how: dict[str, Step] = {}
        for element in order:
            options = [(route[a] | route[b] | {r for r in results if r not in starters}, (a, b, results))
                       for a, b, results in producers[element] if a in route and b in route]
            if not options:
                continue
            smallest = min(len(found) for found, _ in options)
            pool = [o for o in options if len(o[0]) <= smallest + (1 if rng else 0)]
            route[element], how[element] = rng.choice(pool) if rng else pool[0]
        yield route, how


def reference_sizes(book: RecipeBook, depth: dict[str, int], passes: int = SEARCH_PASSES) -> dict[str, int]:
    """For each reachable element that is not a starter, how many elements the best route found unlocks."""
    starters = set(book.starters)
    best: dict[str, int] = {}
    for route, _ in _passes(book, depth, passes):
        for element, found in route.items():
            if element not in starters:
                best[element] = min(best.get(element, len(found)), len(found))
    return best


def _extract(target: str, how: dict[str, Step], depth: dict[str, int], starters: set[str]) -> list[Step]:
    """The recipes one greedy pass uses to reach `target`, in an order that can be followed."""
    chosen: dict[tuple[str, str], tuple[str, ...]] = {}
    made: set[str] = set()
    needed = {target}
    while needed:
        element = max(needed, key=lambda e: (depth[e], e))   # deepest first, so side effects can cover shallower ones
        needed.discard(element)
        if element in starters or element in made:
            continue
        a, b, results = how[element]
        chosen[(a, b)] = results
        made.update(results)
        needed.update(i for i in (a, b) if i not in starters and i not in made)
    have = set(starters)
    steps: list[Step] = []
    while chosen:
        ready = sorted((p for p in chosen if p[0] in have and p[1] in have),
                       key=lambda p: (min(depth.get(r, 0) for r in chosen[p]), p))
        pair = ready[0] if ready else min(chosen, key=lambda p: (min(depth.get(r, 0) for r in chosen[p]), p))
        results = chosen.pop(pair)
        steps.append((pair[0], pair[1], results))
        have.update(results)
    return steps


def _unlocked(steps: list[Step], starters: set[str]) -> int:
    return len({r for _, _, results in steps for r in results if r not in starters})


def best_route(book: RecipeBook, depth: dict[str, int], target: str,
               passes: int = SEARCH_PASSES) -> tuple[int, list[Step]]:
    """(elements unlocked, recipes in order) for the smallest route to `target` that the search found."""
    starters = set(book.starters)
    best: tuple[int, list[Step]] | None = None
    for _, how in _passes(book, depth, passes):
        if target in how:
            steps = _extract(target, how, depth, starters)
            size = _unlocked(steps, starters)
            if best is None or size < best[0]:
                best = (size, steps)
    if best is None:
        raise ValueError(f"no route to {target!r}")
    return best


def target_pools(book: RecipeBook, depth: dict[str, int], sizes: dict[str, int]) -> dict[str, list[str]]:
    """The elements each tier may pick its target from. A small recipe file is split into thirds by depth instead."""
    pools = {tier: sorted(e for e, size in sizes.items() if lo <= depth[e] <= hi and size <= cap)
             for tier, (lo, hi, cap) in TIER_BANDS.items()}
    ranked = sorted(sizes, key=lambda e: (depth[e], e))
    for i, tier in enumerate(TIERS):
        if not pools[tier] and ranked:
            pools[tier] = ranked[round(i * len(ranked) / 3):round((i + 1) * len(ranked) / 3)] or ranked
    return pools


def pick_targets(book: RecipeBook, day: str) -> dict[str, dict[str, Any]]:
    """{tier: {"target", "reference", "route"}} for `day` ("2026-11-04"). The same day always gives the same picks."""
    depth = depths(book)
    pools = target_pools(book, depth, reference_sizes(book, depth))
    picks = {}
    for tier in TIERS:
        if not pools[tier]:
            raise ValueError("the recipes have nothing to use as a daily target")
        target = random.Random(f"{day}:{tier}").choice(pools[tier])
        reference, steps = best_route(book, depth, target)
        picks[tier] = {"target": target, "reference": reference, "route": [[a, b, list(r)] for a, b, r in steps]}
    return picks


# ---- the store -------------------------------------------------------------------------------------

def _week_of(day: str) -> tuple[int, int]:
    iso = date.fromisoformat(day).isocalendar()
    return iso[0], iso[1]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class DailyStore:
    """The day's targets, attempts and scores. `path=None` keeps everything in memory (tests)."""

    def __init__(self, path: str | Path | None, book: RecipeBook, *,
                 today: Callable[[], str] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self.book = book
        self._today = today or (lambda: date.today().isoformat())
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"version": 2, "days": {}}
        self._cache: tuple[tuple[int, int], dict[str, Any]] | None = None   # ((mtime_ns, size), parsed file)
        self._depth: dict[str, int] | None = None

    # ---- file ----------------------------------------------------------

    @staticmethod
    def _normalise(data: dict[str, Any]) -> None:
        """Bring an older file up to date: a day that has one `target` becomes its medium tier."""
        for entry in data["days"].values():
            if isinstance(entry, dict) and "tiers" not in entry and "target" in entry:
                entry["tiers"] = {"medium": {key: entry.pop(key) for key in
                                             ("target", "reference", "scores", "attempts") if key in entry}}
            if isinstance(entry, dict) and not isinstance(entry.get("tiers"), dict):
                entry["tiers"] = {}
        data["version"] = 2

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return {"version": 2, "days": {}}
        except OSError as exc:
            raise ValueError(f"the daily challenge file ({self.path.name}) cannot be read; fix or move it") from exc
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"the daily challenge file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise ValueError(f"the daily challenge file ({self.path.name}) is not in the expected format")
        self._normalise(data)
        self._cache = (stamp, data)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            stat = self.path.stat()
            self._cache = ((stat.st_mtime_ns, stat.st_size), data)
        except OSError as exc:
            self._cache = None
            print(f"warning: could not save the daily challenge file: {exc}", file=sys.stderr)

    def _target_is_usable(self, target: object) -> bool:
        if self._depth is None:
            self._depth = depths(self.book)
        return isinstance(target, str) and target in self.book.elements and target in self._depth \
            and target not in self.book.starters

    def _tier(self, day: str, tier: str) -> dict[str, Any] | None:
        entry = self._load()["days"].get(day)
        found = entry.get("tiers", {}).get(tier) if isinstance(entry, dict) else None
        return found if isinstance(found, dict) else None

    @staticmethod
    def _dict(entry: dict[str, Any], key: str) -> dict[str, Any]:
        value = entry.get(key)
        if not isinstance(value, dict):
            value = entry[key] = {}
        return value

    # ---- the day's challenges ----------------------------------------------

    def today(self) -> dict[str, Any]:
        """{"date", "tiers": {tier: {"target", "reference"}}} for today, choosing and storing the targets the first time."""
        with self._lock:
            day = self._today()
            data = self._load()
            days = data["days"]
            entry = days.setdefault(day, {"tiers": {}})
            tiers = entry["tiers"]
            changed = False
            missing = [t for t in TIERS if not isinstance(tiers.get(t), dict) or not self._target_is_usable(tiers[t].get("target"))]
            if missing:
                picks = pick_targets(self.book, day)
                for tier in missing:
                    kept = tiers.get(tier) if isinstance(tiers.get(tier), dict) else {}   # a replaced target keeps scores and attempts
                    tiers[tier] = {**picks[tier], "scores": kept.get("scores") if isinstance(kept.get("scores"), dict) else {},
                                   "attempts": kept.get("attempts") if isinstance(kept.get("attempts"), dict) else {}}
                changed = True
            for old in sorted(days)[:-KEEP_DAYS]:
                del days[old]
                changed = True
            if changed:
                self._save(data)
            return {"date": day, "tiers": {t: {"target": tiers[t]["target"], "reference": _int(tiers[t].get("reference"))}
                                           for t in TIERS}}

    def route(self, day: str, tier: str) -> list[Step]:
        """The reference route's recipes, in order (empty if there is none stored)."""
        with self._lock:
            entry = self._tier(day, tier)
            raw = entry.get("route") if entry else None
            steps: list[Step] = []
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) \
                        and isinstance(item[1], str) and isinstance(item[2], list):
                    steps.append((item[0], item[1], tuple(str(r) for r in item[2])))
            return steps

    # ---- attempts and scores ---------------------------------------------------

    def delete_user(self, username: str) -> int:
        """Remove an account's scores and attempt counts from every stored day (used when the account is deleted).

        Returns how many entries were removed.
        """
        key = key_of(username)
        removed = 0
        with self._lock:
            data = self._load()
            for entry in data["days"].values():
                tiers = entry.get("tiers") if isinstance(entry, dict) else None
                for tier in tiers.values() if isinstance(tiers, dict) else []:
                    for field in ("scores", "attempts"):
                        bucket = tier.get(field) if isinstance(tier, dict) else None
                        if isinstance(bucket, dict) and bucket.pop(key, None) is not None:
                            removed += 1
            if removed:
                self._save(data)
        return removed

    def attempts(self, day: str, tier: str, username: str) -> int:
        """How many attempts this account has started in `tier` on `day`."""
        with self._lock:
            entry = self._tier(day, tier)
            used = entry.get("attempts") if entry else None
            count = _int(used.get(key_of(username))) if isinstance(used, dict) else None
            return count if count and count > 0 else 0

    def use_attempt(self, day: str, tier: str, username: str) -> int:
        """Count one more started attempt; returns how many this account has used in `tier` on `day` now."""
        with self._lock:
            data = self._load()
            entry = self._tier(day, tier)
            if entry is None:
                return 0
            count = self.attempts(day, tier, username) + 1
            self._dict(entry, "attempts")[key_of(username)] = count
            self._save(data)
            return count

    def forfeit(self, day: str, tier: str, username: str) -> None:
        """Use up all of this account's attempts in `tier` on `day` (to see the reference route)."""
        with self._lock:
            data = self._load()
            entry = self._tier(day, tier)
            if entry is not None and self.attempts(day, tier, username) < MAX_ATTEMPTS:
                self._dict(entry, "attempts")[key_of(username)] = MAX_ATTEMPTS
                self._save(data)

    def submit(self, day: str, tier: str, username: str, score: int) -> bool:
        """Record a finished attempt; only a player's best score per tier and day is kept. True if it is their new best."""
        with self._lock:
            data = self._load()
            entry = self._tier(day, tier)
            if entry is None:
                return False
            scores = self._dict(entry, "scores")
            key = key_of(username)
            old = scores.get(key)
            if isinstance(old, dict) and isinstance(old.get("score"), int) and old["score"] <= score:
                return False
            scores[key] = {"name": username, "score": score, "at": self._clock()}
            self._save(data)
            return True

    def best(self, day: str, tier: str, username: str) -> int | None:
        with self._lock:
            entry = self._tier(day, tier)
            scores = entry.get("scores") if entry else None
            mine = scores.get(key_of(username)) if isinstance(scores, dict) else None
            return _int(mine.get("score")) if isinstance(mine, dict) else None

    @staticmethod
    def _rows(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
        scores = entry.get("scores") if entry else None
        rows = [s for s in (scores or {}).values()
                if isinstance(s, dict) and _int(s.get("score")) and isinstance(s.get("name"), str)]
        rows.sort(key=lambda s: (s["score"], s.get("at", 0)))   # lowest first; the earlier finisher wins a tie
        return rows

    def board(self, day: str, tier: str, limit: int = BOARD_SIZE) -> list[dict[str, Any]]:
        """The best scores of `tier` on `day`, lowest first."""
        with self._lock:
            return [{"name": s["name"], "score": s["score"]} for s in self._rows(self._tier(day, tier))[:limit]]

    # ---- streaks, history and the weekly board ----------------------------------

    def streak(self, username: str) -> dict[str, int]:
        """Days in a row with a finished attempt: {"current", "best"}.

        The current streak counts back from today, or from yesterday while today has not been played yet,
        so it only breaks once a whole day is missed.
        """
        with self._lock:
            key = key_of(username)
            played = sorted(day for day, entry in self._load()["days"].items() if isinstance(entry, dict)
                            and any(isinstance(t, dict) and isinstance(t.get("scores"), dict) and key in t["scores"]
                                    for t in entry["tiers"].values()))
            days = {date.fromisoformat(d) for d in played}
            best = run = 0
            previous: date | None = None
            for d in sorted(days):
                run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
                best, previous = max(best, run), d
            cursor = date.fromisoformat(self._today())
            if cursor not in days:
                cursor -= timedelta(days=1)
            current = 0
            while cursor in days:
                current += 1
                cursor -= timedelta(days=1)
            return {"current": current, "best": best}

    def history(self, username: str, limit: int = HISTORY_DAYS) -> list[dict[str, Any]]:
        """The latest days, newest first: per tier the target, reference, this account's best and the winner.

        A day's routes are included only once the day is over.
        """
        with self._lock:
            today = self._today()
            out = []
            for day in sorted(self._load()["days"], reverse=True)[:limit]:
                tiers = {}
                for tier in TIERS:
                    entry = self._tier(day, tier)
                    if entry is None or not self._target_is_usable(entry.get("target")):
                        continue
                    rows = self._rows(entry)
                    mine = self.best(day, tier, username)
                    tiers[tier] = {"target": entry["target"], "reference": _int(entry.get("reference")), "best": mine,
                                   "winner": {"name": rows[0]["name"], "score": rows[0]["score"]} if rows else None,
                                   "players": len(rows), "route": self.route(day, tier) if day < today else []}
                if tiers:
                    out.append({"date": day, "tiers": tiers})
            return out

    def week(self, limit: int = BOARD_SIZE) -> dict[str, Any]:
        """The weekly board for the ISO week containing today: {"label", "board": [{"name", "points", "days", "finishes"}]}."""
        with self._lock:
            today = self._today()
            monday = date.fromisoformat(today) - timedelta(days=date.fromisoformat(today).weekday())
            totals: dict[str, dict[str, Any]] = {}
            for day, entry in self._load()["days"].items():
                if not isinstance(entry, dict) or _week_of(day) != _week_of(today):
                    continue
                for tier in entry["tiers"].values():
                    reference = _int(tier.get("reference")) or 1
                    for key, row in (tier.get("scores") or {}).items():
                        if not isinstance(row, dict) or not _int(row.get("score")) or not isinstance(row.get("name"), str):
                            continue
                        mine = totals.setdefault(key, {"name": row["name"], "points": 0, "dates": set(), "finishes": 0})
                        mine["points"] += round(100 * reference / row["score"])
                        mine["dates"].add(day)
                        mine["finishes"] += 1
            rows = sorted(totals.values(), key=lambda r: (-r["points"], -len(r["dates"]), r["name"].casefold()))
            return {"label": f"{monday.isoformat()} to {(monday + timedelta(days=6)).isoformat()}",
                    "board": [{"name": r["name"], "points": r["points"], "days": len(r["dates"]), "finishes": r["finishes"]}
                              for r in rows[:limit]]}

"""Check a recipe file before playing.

Usage: python3 validate_recipes.py [path/to/recipes.json]

Errors (exit code 1) make the game incomplete or the file invalid:
  - structural problems: empty or ambiguous ids (e.g. two ids differing only by
    capitalisation), unknown element names (with "did you mean" suggestions),
    missing labels
  - orphans: elements no recipe produces and that are not starters
  - unreachable elements: produced by a recipe whose ingredients can never be obtained
Warnings and notes (exit code 0):
  - starters never used in any recipe
  - repeated recipes that were cleaned up (identical repeats dropped, different
    results for the same pair merged into one combination)
  - dead ends: reachable elements that are never used as an ingredient
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from recipes import depths, find_problems, parse_recipes


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "recipes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR cannot read {path}: {exc}")
        return 1

    problems = find_problems(data)
    if problems:
        for problem in problems:
            print("ERROR", problem)
        print(f"\n{len(problems)} error(s) in {path}")
        return 1

    book = parse_recipes(data)
    depth = depths(book)
    produced = {r for results in book.lookup.values() for r in results}
    used = {x for pair in book.lookup for x in pair}

    errors: list[str] = []
    for eid in sorted(set(book.elements) - set(depth)):
        if eid in produced:
            errors.append(f"unreachable: {eid!r} has a recipe, but its ingredients can never be obtained")
        else:
            errors.append(f"orphan: no recipe produces {eid!r} and it is not a starter")

    warnings = [f"starter {s!r} is not used in any recipe" for s in book.starters if s not in used]
    dead_ends = sorted(set(depth) - used)

    for message in errors:
        print("ERROR  ", message)
    for message in warnings:
        print("WARNING", message)
    if dead_ends:
        print("NOTE    dead ends (never used as an ingredient):", ", ".join(dead_ends))
    for message in book.notes:
        print("NOTE    cleaned up:", message)

    deepest = max(depth, key=lambda e: depth[e])
    multi = sum(1 for r in produced if sum(r in res for res in book.lookup.values()) > 1)
    selfs = sum(1 for a, b in book.lookup if a == b)
    print(
        f"\n{len(book.elements)} elements ({len(book.starters)} starters), "
        f"{len(book.lookup)} recipes, {len(depth)} reachable; "
        f"deepest: {deepest!r} at depth {depth[deepest]}; "
        f"{multi} element(s) with several recipes; {selfs} self-combination(s)."
    )
    if errors:
        print(f"{len(errors)} error(s) in {path}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

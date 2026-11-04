"""List every problem in a recipe file, each with a suggested change.

Usage: python3 recipe_report.py [path/to/recipes.json] [-o report.md]

Prints the report (and writes it to a file with -o). The recipe file is only
read, never changed: you decide which suggestions to apply. For a quick
pass/fail check use validate_recipes.py instead.

Each entry says where the problem is, what is wrong, and what to change.
Exit code 1 if there is anything marked ERROR.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from recipes import depths, find_problems, parse_recipes

# (text found in a structural message, suggested change)
STRUCTURAL_ADVICE = (
    ("differ only by capitalisation", "Keep one of the two ids and merge the other into it; update every recipe that used the removed one."),
    ("is not a defined element", "Add it to 'elements', or remove it from 'starters' (see 'did you mean')."),
    ("unknown element", "Fix the spelling (see 'did you mean'), or add the element to 'elements'."),
    ("is listed twice", "Delete the repeated entry from 'starters'."),
    ("needs a non-empty string 'label'", "Give the element a 'label' with the name players should see."),
    ("'emoji' must be a string", "Make 'emoji' a string, or delete it (emoji is optional)."),
    ("needs string 'a' and 'b'", "Write the recipe as {\"a\": id, \"b\": id, \"result\": id or [ids]}."),
    ("must be an object", "Replace the entry with a recipe object."),
    ("must not", "Rename the id (ids are free text but must be trimmed, non-empty, short and without control characters)."),
)


def advice_for(problem: str) -> str:
    for needle, advice in STRUCTURAL_ADVICE:
        if needle in problem:
            return advice
    return "Edit the entry so it follows the format described in recipes.py."


def build_report(data: object) -> tuple[list[str], int]:
    """The report lines and the number of errors."""
    problems = find_problems(data)
    if problems:
        lines = ["# Recipe problems", "",
                 "The file has structural problems. Fix these first; the other checks run once they are gone.", ""]
        for number, problem in enumerate(problems, 1):
            lines += [f"{number}. **ERROR** {problem}", f"   - Suggested change: {advice_for(problem)}"]
        return lines, len(problems)

    book = parse_recipes(data)  # type: ignore[arg-type]
    depth = depths(book)
    produced: dict[str, list[tuple[str, str]]] = {}
    for (a, b), results in book.lookup.items():
        for r in results:
            produced.setdefault(r, []).append((a, b))
    used = {x for pair in book.lookup for x in pair}
    label = {eid: e.label for eid, e in book.elements.items()}
    errors: list[tuple[str, str]] = []
    others: list[tuple[str, str, str]] = []

    for eid in sorted(set(book.elements) - set(depth)):
        if eid in produced:
            blocked = sorted({x for pair in produced[eid] for x in pair if x not in depth and x != eid}) or [eid]
            names = ", ".join(repr(label[x]) for x in blocked)
            errors.append((f"{label[eid]!r} can never be made: every recipe for it needs something that can't be "
                           f"obtained ({names}).",
                           f"Give {names} a recipe of its own, or add another recipe for {label[eid]!r} that uses "
                           "elements players can reach."))
        else:
            errors.append((f"{label[eid]!r} is an orphan: no recipe makes it and it is not a starter.",
                           f"Add a recipe that makes {label[eid]!r}, add it to 'starters', or delete the element."))
    for s in book.starters:
        if s not in used:
            others.append(("WARNING", f"Starter {label[s]!r} is not used in any recipe.",
                           f"Use it in a recipe, or remove it from 'starters' if it should not be there."))
    for note in book.notes:
        others.append(("NOTE", f"Repeated recipe, cleaned up automatically: {note}.",
                       "Delete the repeated entry from the file so it matches what the game uses."))
    for eid in sorted(set(depth) - used, key=lambda e: label[e].casefold()):
        others.append(("NOTE", f"{label[eid]!r} is a dead end: it can be made but is never used as an ingredient.",
                       "Fine for a final element; otherwise add a recipe that uses it."))

    lines = ["# Recipe problems", ""]
    if not errors and not others:
        lines.append("No problems found.")
    for number, (problem, advice) in enumerate(errors, 1):
        lines += [f"{number}. **ERROR** {problem}", f"   - Suggested change: {advice}"]
    for number, (kind, problem, advice) in enumerate(others, len(errors) + 1):
        lines += [f"{number}. **{kind}** {problem}", f"   - Suggested change: {advice}"]
    lines += ["", f"{len(book.elements)} elements, {len(book.lookup)} recipes, {len(errors)} error(s), "
                  f"{len(others)} warning(s)/note(s)."]
    return lines, len(errors)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Report recipe problems with suggested changes")
    parser.add_argument("recipes", nargs="?", default=str(Path(__file__).resolve().parent / "recipes.json"))
    parser.add_argument("-o", "--output", help="also write the report to this file")
    args = parser.parse_args(argv[1:])
    try:
        data = json.loads(Path(args.recipes).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR cannot read {args.recipes}: {exc}")
        return 1
    lines, errors = build_report(data)
    text = "\n".join(lines) + "\n"
    print(text)
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Asking the language model for recipe results, and cleaning up what it says."""
from __future__ import annotations

import json
import random
import re
from typing import Iterable, NamedTuple, Sequence

from recipes import RecipeBook

MAX_WORDS = 3
MAX_NAME_LEN = 32
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' \-]*$")

SYSTEM = ("You design combination recipes for an element-combination game in the style of Little Alchemy. "
          "Players combine two elements and get a new one. You answer with JSON only.")


class ProposeError(ValueError):
    """The model's reply could not be understood."""


# ---- names -------------------------------------------------------------------

def normalize_name(raw: object) -> str | None:
    """A clean element name, or None if the model's text is not usable as one."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.strip().strip("\"'`.,;:!").split())
    if not name or len(name) > MAX_NAME_LEN or len(name.split()) > MAX_WORDS or not _NAME_RE.match(name):
        return None
    # ALL CAPS becomes lowercase; other casing (proper nouns) is left as the model wrote it
    return name.lower() if name.isupper() else name


def name_key(name: str) -> str:
    """Spelling-insensitive key so 'Cats', 'cat' and 'CAT' count as the same element."""
    s = "".join(ch for ch in name.casefold() if ch.isalnum())
    if s.endswith("ies") and len(s) > 4:
        return s[:-3] + "y"
    if s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        return s[:-1]
    return s


class NameIndex:
    """Every element name already taken (real, draft, or invented this run), by spelling-insensitive key."""

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}

    def add(self, name: str, element_id: str | None = None) -> None:
        self._by_key.setdefault(name_key(name), element_id or name)

    def lookup(self, name: str) -> str | None:
        return self._by_key.get(name_key(name))

    @classmethod
    def from_book(cls, book: RecipeBook) -> "NameIndex":
        index = cls()
        for eid, element in book.elements.items():
            index.add(eid, eid)
            index.add(element.label, eid)
        return index


# ---- prompt ------------------------------------------------------------------

def pick_examples(book: RecipeBook, rng: random.Random, involving: Iterable[str], *,
                  general: int = 14, per_element: int = 3) -> list[str]:
    """Style-guide lines `a + b = result`: a random sample plus recipes that mention the given elements."""
    label = lambda e: book.elements[e].label  # noqa: E731
    all_rows = [(a, b, r) for (a, b), results in book.lookup.items() for r in results]
    chosen = rng.sample(all_rows, min(general, len(all_rows)))
    for eid in involving:
        mine = [row for row in all_rows if eid in row]
        chosen += rng.sample(mine, min(per_element, len(mine)))
    seen, lines = set(), []
    for a, b, r in chosen:
        if (a, b, r) not in seen:
            seen.add((a, b, r))
            lines.append(f"{label(a)} + {label(b)} = {label(r)}")
    return lines


def build_prompt(pairs: Sequence[tuple[str, str]], examples: Sequence[str], *, new_only: bool) -> str:
    goal = ("Invent a specific new concept for each result; do not reuse a name from the examples."
            if new_only else
            "A result may be an element from the examples or a new concept you invent.")
    pair_lines = "\n".join(f"{i}. {a} + {b}" for i, (a, b) in enumerate(pairs, 1))
    return (
        "Example recipes (match their style and level of abstraction):\n"
        + "\n".join(examples)
        + "\n\nRules:\n"
          "- A result is a common noun, usually one word and never more than three. Write it in lowercase; "
          "capitalise only proper nouns such as myths and places.\n"
          "- It must be something that sensibly comes from combining the two ingredients. Think about what each "
          "ingredient looks like (shape, colour), is made of, is used for, or comes from, not only the literal mix.\n"
          "- Never answer with one of the two ingredients.\n"
          f"- {goal}\n"
          "- If nothing sensible fits a pair, answer null for it.\n"
          "- Keep it suitable for all ages.\n\n"
          f"Pairs:\n{pair_lines}\n\n"
          'Reply with only a JSON array, one entry per pair, like [{"n": 1, "result": "name"}, {"n": 2, "result": null}].'
    )


_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def json_objects(text: str) -> list[dict]:
    """Every complete JSON object in `text`, in order. Survives truncated replies, code fences and trailing chatter."""
    decoder, found, i = json.JSONDecoder(), [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = end


def parse_reply(text: str, count: int) -> dict[int, str | None]:
    """Pair number (1-based) -> the proposed name or None. Raises ProposeError if no usable JSON array is found."""
    match = _ARRAY_RE.search(text)
    if not match:
        raise ProposeError("no JSON array in the reply")
    try:
        rows = json.loads(match.group(0))
    except ValueError as exc:
        raise ProposeError(f"the reply is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ProposeError("the reply is not a list")
    out: dict[int, str | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        n, result = row.get("n"), row.get("result")
        if isinstance(n, int) and 1 <= n <= count and (result is None or isinstance(result, str)):
            out[n] = result
    if not out:
        raise ProposeError("the reply has no usable entries")
    return out


# ---- goal-directed prompts (the 'target' command) -----------------------------

LINK_TYPES = ("shape", "material", "function", "origin", "other")
MAX_HINTS = 12


class RawOption(NamedTuple):
    a: str
    b: str
    link: str = ""      # how the ingredients relate to the goal: one of LINK_TYPES, or ""
    why: str = ""       # the model's short reason, e.g. "crescent shaped"
    rating: int = 0     # the model's own confidence, 1 to 5 (0 = not given)
    source: str = "model"   # 'model', or 'generated' when the code paired a kind with a look-alike


PROFILE_KEYS = ("kind_elements", "look_elements", "kind_of", "looks_like", "resembles", "made_of", "used_for",
                "comes_from")


def build_profile_prompt(goal: str, available: Sequence[str] | None = None) -> str:
    """Ask what the goal is like. With `available`, the model picks look-alikes and same-kind elements from that list."""
    if available:
        return (
            f'Element: "{goal}"\nAvailable elements: ' + "; ".join(available) + "\n\n"
            "Describe it for a recipe-design game. Pick ONLY from the available list for the first two keys. Use short "
            "noun phrases of one to three words, at most four per key. Reply with only JSON like "
            '{"kind_elements": ["lizard", "frog"], "look_elements": ["rope", "whip"], "made_of": ["scales"], '
            '"used_for": [], "comes_from": ["egg"]} (that example is for "snake"). '
            '"kind_elements" are available elements that are the same sort of thing as the goal; "look_elements" are '
            "available elements with a similar shape or look (think of the outline, not the colour)."
        )
    return (
        f'Element: "{goal}"\n\n'
        "Describe it for a recipe-design game, so that ingredients can be found for it. Use short concrete "
        "noun phrases of one to three words, at most four per key. Reply with only JSON like "
        '{"kind_of": ["reptile", "animal"], "looks_like": ["long", "coiled"], "resembles": ["rope", "whip"], '
        '"made_of": ["scales"], "used_for": [], "comes_from": ["egg"]} (that example is for "snake"). '
        '"kind_of" is the broader categories it belongs to; "looks_like" is shape, colour and texture; '
        '"resembles" is other everyday things with a similar shape or look.'
    )


def parse_profile(text: str) -> dict[str, list[str]]:
    """Key -> cleaned phrases from a reply to build_profile_prompt. Raises ProposeError if unreadable."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ProposeError("no JSON object in the reply")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise ProposeError(f"the reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProposeError("the reply is not an object")
    out: dict[str, list[str]] = {}
    total = 0
    for key in PROFILE_KEYS:
        value = data.get(key)
        phrases: list[str] = []
        for item in value if isinstance(value, list) else []:
            phrase = " ".join(item.split()).strip(" .,;:!\"'").lower() if isinstance(item, str) else ""
            if phrase and len(phrase) <= 24 and len(phrase.split()) <= 3 and phrase not in phrases and total < MAX_HINTS:
                phrases.append(phrase)
                total += 1
        if phrases:
            out[key] = phrases
    if not out:
        raise ProposeError("the reply has no usable phrases")
    return out


class Judgement(NamedTuple):
    rating: int                    # 1-5
    reads_as: str = ""             # what the pair literally suggests, e.g. "moon-shaped bread"
    others: tuple[str, ...] = ()   # other well-known things the same pair could just as easily make


def build_judge_prompt(goal: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Ask for an independent, specificity-aware rating of every candidate recipe for `goal`."""
    lines = "\n".join(f"{i}. {a} + {b}" for i, (a, b) in enumerate(pairs, 1))
    return (
        f'Goal: a new element called "{goal}" in an element-combination game (like Little Alchemy), where combining '
        "two elements gives a new one. A good recipe is SPECIFIC: a player who sees only the two ingredients should "
        "think of the goal, and of little else.\n\n"
        "For each pair:\n"
        '1. "reads_as": read the two ingredients together as a phrase and say what they literally suggest, in two to '
        "five words. Consider shape and look as well as content: 'rope' + 'animal' reads as 'a rope-like animal', "
        "which suggests a snake.\n"
        f'2. "others": up to four OTHER well-known things the same pair could just as easily suggest (not "{goal}").\n'
        '3. "rating" from 1 to 5. 5 = the pair points to the goal and almost nothing else. 3 = it fits but could just '
        "as well be several other things. 1 = it does not fit. Two common ingredients that suit many results are "
        "generic and get at most 3. Be strict and compare the pairs with each other.\n\n"
        f"Pairs:\n{lines}\n\n"
        'Reply with only a compact JSON array, one short line per pair, like '
        '[{"n": 1, "reads_as": "moon-shaped bread", "others": ["bagel"], "rating": 4}].'
    )


def parse_ratings(text: str, count: int) -> dict[int, Judgement]:
    """Pair number (1-based) -> Judgement, from every complete entry in the reply (a cut-off reply still counts).
    Raises ProposeError if there is none."""
    out: dict[int, Judgement] = {}
    for row in json_objects(text):
        try:
            n, rating = int(row["n"]), int(float(row["rating"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (1 <= n <= count and 1 <= rating <= 5) or n in out:
            continue
        others = row.get("others")
        out[n] = Judgement(rating, " ".join(str(row.get("reads_as", "")).split())[:60],
                           tuple(" ".join(o.split())[:30] for o in others if isinstance(o, str))[:4]
                           if isinstance(others, list) else ())
    if not out:
        raise ProposeError("the reply has no usable ratings")
    return out


def build_goal_prompt(goal: str, available: Sequence[str], examples: Sequence[str], *,
                      options: int, may_invent: bool, hints: Sequence[str] = ()) -> str:
    """Ask for ingredient pairs that could combine into `goal`, preferring the listed existing elements."""
    if may_invent:
        sources = ("Each ingredient must come from the available list, OR be a new intermediate element you invent "
                   "(a common noun, one to three words) when no available element fits well. "
                   "At most one new ingredient per option.")
    else:
        sources = "Every ingredient must come from the available list."
    hint_line = ("Things the goal looks like or relates to: " + "; ".join(hints) + "\n") if hints else ""
    return (
        "Example recipes (match their style and level of abstraction):\n"
        + "\n".join(examples)
        + f'\n\nGoal: make a new element called "{goal}" by combining two elements.\n'
          "Available elements: " + "; ".join(available) + "\n" + hint_line + "\n"
          "Rules:\n"
          f"- Give up to {options} different options. Each is a pair of ingredients that sensibly combine into "
          f'"{goal}".\n'
          f"- {sources}\n"
          "- Prefer options that use only available elements. Invent a helper only when no pair of available elements "
          "works, and never one that is just a broader kind of the goal, a synonym of it, or an existing element "
          "with an adjective added (not 'pastry' for a croissant, not 'crescent moon' when 'moon' exists).\n"
          "- Think about how each ingredient could relate to the goal: what it is made of, what it is used for, "
          "where it comes from, and also what it LOOKS like (shape, colour, texture). A shape link is welcome "
          "even when the words are unrelated; for example 'animal' + 'rope' can make 'snake' because a rope "
          "looks like a snake (that example is only an illustration). Scan the whole list for elements with a "
          "similar shape, material or use before settling for the obvious.\n"
          f'- Never use "{goal}" itself as an ingredient.\n'
          "- Keep it suitable for all ages.\n\n"
          'Reply with only a JSON array. For each option give "link" (one of shape, material, function, origin, '
          'other), "why" (under eight words) and "rating" (1 to 5, how well it works), like '
          '[{"a": "sewing machine", "b": "fabric", "link": "function", "why": "sews cloth", "rating": 4}].'
    )


def parse_options(text: str) -> list[RawOption]:
    """The ingredient pairs in a reply to build_goal_prompt (complete entries only). Raises ProposeError if none."""
    options: list[RawOption] = []
    for row in json_objects(text):
        if not (isinstance(row.get("a"), str) and isinstance(row.get("b"), str)):
            continue
        link = str(row.get("link", "")).strip().lower()
        why = " ".join(str(row.get("why", "")).split())[:80]
        try:
            rating = max(0, min(5, int(float(row.get("rating", 0)))))
        except (TypeError, ValueError):
            rating = 0
        options.append(RawOption(row["a"], row["b"], link if link in LINK_TYPES else "", why, rating))
    if not options:
        raise ProposeError("the reply has no usable options")
    return options

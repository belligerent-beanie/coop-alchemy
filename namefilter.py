"""A list-based check on the names strangers choose: usernames, game names and the names of recipes they create.

It is a speed bump, not a judge: it stops the obvious, and the administrators handle the rest (reports, disabling, deleting). The list
is blocklist.txt next to this file; --blocklist-file adds your own terms. Each line is a term; `#` starts a comment.

  term     blocked anywhere inside a name, however it is spaced, capitalised, accented or dressed up with look-alike digits and
           symbols and stretched letters (f.u.c.k, sh1t, FUUUCK, xX_fuck_Xx).
  =term    blocked only as a whole word of the name (a "word" ends at a space, punctuation or a capital letter), for short words
           that appear inside innocent ones (a whole-word rule blocks "ass" but lets "Classic" and "Assassin" through).
  -word    an innocent word that happens to contain a blocked term (Scunthorpe): it is ignored before the checks above.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable

from state import GameError

LOOKALIKES = str.maketrans({"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "|": "i", "0": "o", "$": "s", "5": "s", "7": "t", "+": "t"})
BUILTIN = Path(__file__).with_name("blocklist.txt")


def fold(text: str) -> str:
    """Lower case, accents removed, look-alike digits and symbols turned into the letters they imitate."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return plain.lower().translate(LOOKALIKES)


def letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", fold(text))


def words(text: str) -> list[str]:
    """The whole words of a name, split at spaces, punctuation and camel-case capitals (FuckYou -> Fuck, You)."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return [part for part in re.split(r"[^A-Za-z0-9@$!|+]+", spaced) if part]


def stretchy(term: str) -> re.Pattern[str]:
    """A pattern for `term` that tolerates repeated letters (fuck matches fuuuck) but needs doubled letters to be doubled."""
    return re.compile("".join(re.escape(ch) + "+" for ch in term))


class NameFilter:
    def __init__(self, lines: Iterable[str] = ()) -> None:
        self.whole: set[str] = set()
        self.inside: list[re.Pattern[str]] = []
        self.innocent: list[str] = []
        for raw in lines:
            line = raw.split("#", 1)[0].strip().lower()
            if not line:
                continue
            if line.startswith("-"):
                if letters(line[1:]):
                    self.innocent.append(letters(line[1:]))
            elif line.startswith("="):
                if line[1:].strip():
                    self.whole.add(line[1:].strip())
            else:
                self.inside.append(stretchy(line))

    @classmethod
    def load(cls, extra_file: str | None = None) -> "NameFilter":
        """The built-in list plus, if given, the administrator's own file (a missing built-in list is an error, not silence)."""
        lines = BUILTIN.read_text(encoding="utf-8").splitlines()
        if extra_file:
            lines += Path(extra_file).read_text(encoding="utf-8").splitlines()
        return cls(lines)

    @property
    def size(self) -> int:
        return len(self.whole) + len(self.inside)

    def allows(self, text: object) -> bool:
        if not isinstance(text, str):
            return True  # not text at all: other checks refuse it
        squashed = letters(text)
        for word in self.innocent:
            squashed = squashed.replace(word, "")
        if any(pattern.search(squashed) for pattern in self.inside):
            return False
        return not any(letters(word) in self.whole for word in words(text))

    def check(self, text: object, what: str = "name") -> None:
        """Raise a 400 if `text` is blocked. The answer never says which word, so the list cannot be probed."""
        if not self.allows(text):
            raise GameError(400, f"that {what} is not allowed: please choose another")

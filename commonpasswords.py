"""Refuse passwords that attackers try first.

`common-passwords.txt` (next to this file; lower case, one per line, `#` for comments) holds a few tens of thousands of passwords
of 10 or more characters that appear in public lists of the most-used passwords. A new password that is on it, in any capitals,
is refused, and so is one that is just one or two characters repeated or an obvious run (`aaaaaaaaaa`, `1234567890`, `abcdefghij`).
Everything is local: nothing about a password is sent anywhere.

If the file is missing the list check is skipped (with one warning), so a damaged copy never stops people signing up.
"""
from __future__ import annotations

import sys
from pathlib import Path

LIST_FILE = Path(__file__).resolve().parent / "common-passwords.txt"
_common: frozenset[str] | None = None


def load_common(path: Path = LIST_FILE) -> frozenset[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"warning: the common-password list {path.name} could not be read ({exc}); the check is off", file=sys.stderr)
        return frozenset()
    return frozenset(line.strip().casefold() for line in lines if line.strip() and not line.startswith("#"))


def _is_run(text: str) -> bool:
    """True for a straight run of characters in order, up or down (1234567890, abcdefghij, 9876543210)."""
    steps = {ord(b) - ord(a) for a, b in zip(text, text[1:])}
    return len(text) >= 6 and steps in ({1}, {-1})


def is_common(password: str, common: frozenset[str] | None = None) -> bool:
    """True if this password is on the list, a repeat of one or two characters, or an obvious run."""
    global _common
    if common is None:
        if _common is None:
            _common = load_common()
        common = _common
    folded = password.strip().casefold()
    return folded in common or len(set(folded)) <= 2 or _is_run(folded)

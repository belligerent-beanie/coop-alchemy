"""Solo-mode achievements: what an account has earned, worked out from its solo progress.

Each achievement is a name, a one-line description and a test on a `Progress` snapshot. Nothing here is stored except the ids
an account has earned and when (see solo.py), so the wording can change freely and a new achievement is one line in ACHIEVEMENTS.
An account that already meets a new achievement earns it the next time its solo game saves progress.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Progress:
    discovered: int      # elements found beyond the starting ones
    complete: bool       # every element found
    deepest: int         # the most combination steps from the starters among the elements found
    djinn: bool          # the Djinn has been unlocked
    wishes: int          # Djinn wishes used
    time: bool           # Time has been unlocked
    hints: int           # hints used in total


@dataclass(frozen=True)
class Achievement:
    id: str
    icon: str
    title: str
    description: str
    earned: Callable[[Progress], bool]


ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement("found_10", "🌱", "First steps", "Find 10 elements beyond the starting ones.", lambda p: p.discovered >= 10),
    Achievement("found_50", "⚗️", "Apprentice", "Find 50 elements.", lambda p: p.discovered >= 50),
    Achievement("found_100", "🧪", "Alchemist", "Find 100 elements.", lambda p: p.discovered >= 100),
    Achievement("found_250", "🔮", "Adept", "Find 250 elements.", lambda p: p.discovered >= 250),
    Achievement("found_500", "👑", "Master", "Find 500 elements.", lambda p: p.discovered >= 500),
    Achievement("complete", "🏆", "Completionist", "Find every element.", lambda p: p.complete),
    Achievement("deep_8", "⛓️", "Chain reaction", "Find an element 8 steps from the starting elements.", lambda p: p.deepest >= 8),
    Achievement("deep_14", "🕳️", "Deep diver", "Find an element 14 steps from the starting elements.", lambda p: p.deepest >= 14),
    Achievement("djinn", "🧞", "Wishmaster", "Unlock the Djinn.", lambda p: p.djinn),
    Achievement("wish", "✨", "Careful what you wish for", "Use a Djinn wish.", lambda p: p.wishes >= 1),
    Achievement("time", "⏳", "Time flies", "Unlock Time.", lambda p: p.time),
    Achievement("hint", "💡", "A little help", "Use your hint of the day.", lambda p: p.hints >= 1),
)

BY_ID = {a.id: a for a in ACHIEVEMENTS}


def earned_ids(progress: Progress) -> set[str]:
    return {a.id for a in ACHIEVEMENTS if a.earned(progress)}


def describe(earned: dict[str, int]) -> list[dict[str, Any]]:
    """Every achievement, with when it was earned (a unix time) or None, for the Solo tab."""
    return [{"id": a.id, "icon": a.icon, "title": a.title, "description": a.description,
             "earned_at": earned.get(a.id)} for a in ACHIEVEMENTS]


def one(achievement_id: str) -> dict[str, str]:
    a = BY_ID[achievement_id]
    return {"id": a.id, "icon": a.icon, "title": a.title, "description": a.description}

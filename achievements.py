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
    a = BY_ID.get(achievement_id) or BADGES_BY_ID[achievement_id]
    return {"id": a.id, "icon": a.icon, "title": a.title, "description": a.description}


# ---- account-wide achievements: shared games, daily challenges and races ------------------------------------------------
#
# Solo achievements above come from one solo game's progress. These come from a few counters kept per account (profiles.py): what you do in
# shared games, the daily challenge and races. Same idea: a name, a description and a test, here on the counters.

@dataclass(frozen=True)
class Badge:
    id: str
    icon: str
    title: str
    description: str
    scope: str                                    # "multiplayer" or "challenges": which tab of the home page lists it
    earned: Callable[[dict[str, int]], bool]


def _n(counters: dict[str, int], key: str) -> int:
    return counters.get(key, 0)


BADGES: tuple[Badge, ...] = (
    # shared games
    Badge("mp_together", "🤝", "Good company", "Play in a shared game with someone else.", "multiplayer", lambda c: _n(c, "games") >= 1),
    Badge("mp_games_10", "🎲", "Regular", "Play in 10 shared games with others.", "multiplayer", lambda c: _n(c, "games") >= 10),
    Badge("mp_games_50", "🪑", "A fixture", "Play in 50 shared games with others.", "multiplayer", lambda c: _n(c, "games") >= 50),
    Badge("mp_finds_10", "🔧", "Pitching in", "Discover 10 elements in shared games.", "multiplayer", lambda c: _n(c, "finds") >= 10),
    Badge("mp_finds_100", "🛠️", "Team player", "Discover 100 elements in shared games.", "multiplayer", lambda c: _n(c, "finds") >= 100),
    Badge("mp_finds_500", "🏛️", "Pillar of the community", "Discover 500 elements in shared games.", "multiplayer", lambda c: _n(c, "finds") >= 500),
    Badge("mp_epic", "💜", "Epic find", "Discover an epic element in a shared game.", "multiplayer", lambda c: _n(c, "epic_finds") >= 1),
    Badge("mp_full", "🏠", "Full house", "Be in a shared game with four other people.", "multiplayer", lambda c: _n(c, "full_room") >= 1),
    Badge("mp_struck", "🎩", "Struck by Mr Alchemy", "Be in a game when Mr Alchemy drops in.", "multiplayer", lambda c: _n(c, "struck") >= 1),
    Badge("mp_struck_5", "🪄", "Mr Alchemy's favourite", "Be struck by Mr Alchemy five times.", "multiplayer", lambda c: _n(c, "struck") >= 5),
    Badge("mp_emote", "😀", "Say it with a glyph", "Send an emote.", "multiplayer", lambda c: _n(c, "emotes") >= 1),
    # the daily challenge and races
    Badge("ch_daily", "📅", "Daily habit", "Finish a daily challenge.", "challenges", lambda c: _n(c, "daily_done") >= 1),
    Badge("ch_daily_7", "🗓️", "Creature of habit", "Finish 7 daily challenges.", "challenges", lambda c: _n(c, "daily_done") >= 7),
    Badge("ch_hard", "🔥", "The hard way", "Finish a hard daily challenge.", "challenges", lambda c: _n(c, "daily_hard") >= 1),
    Badge("ch_streak_3", "⚡", "On a roll", "Finish a daily challenge three days in a row.", "challenges", lambda c: _n(c, "daily_streak") >= 3),
    Badge("ch_streak_7", "🌟", "A whole week", "Finish a daily challenge seven days in a row.", "challenges", lambda c: _n(c, "daily_streak") >= 7),
    Badge("ch_race", "🏁", "Off the blocks", "Finish a race.", "challenges", lambda c: _n(c, "race_done") >= 1),
    Badge("ch_win", "🏆", "First past the post", "Win a race.", "challenges", lambda c: _n(c, "race_wins") >= 1),
    Badge("ch_wins_5", "🥇", "Serial winner", "Win five races.", "challenges", lambda c: _n(c, "race_wins") >= 5),
)

BADGES_BY_ID = {b.id: b for b in BADGES}
COUNTERS = frozenset({"games", "finds", "epic_finds", "full_room", "struck", "emotes", "daily_done", "daily_hard", "daily_streak",
                      "race_done", "race_wins"})


def earned_badges(counters: dict[str, int]) -> set[str]:
    return {b.id for b in BADGES if b.earned(counters)}


def describe_badges(earned: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
    """Every account-wide achievement grouped by tab, with when it was earned (a unix time) or None."""
    out: dict[str, list[dict[str, Any]]] = {"multiplayer": [], "challenges": []}
    for b in BADGES:
        out[b.scope].append({"id": b.id, "icon": b.icon, "title": b.title, "description": b.description, "earned_at": earned.get(b.id)})
    return out

"""One game's shared state: discoveries, the shared workspace, players, the genie
and event fan-out. Games live in memory only (see games.py for running several).

All mutation happens under one lock, and events are queued while the lock is
held, so every subscriber sees events in the same order the server applied them.

The shared workspace is a set of tiles with normalised positions (x and y in
0..1, relative to the workspace size), so players with different screen sizes
see the same layout. Only one player moves a tile at a time: they grab it, send
moves, then drop it (or combine it). A player's tiles are released if their
event stream disconnects.
"""
from __future__ import annotations

import json
import math
import queue
import random
import secrets
import sys
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from achievements import Progress, earned_ids
from achievements import one as achievement_json
from emotes import EMOTE_GAP_SECONDS, EMOTE_IDS
from hints import find_element, hint_can_make, hint_recipe, hint_try
from pictures import PictureIndex
from recipes import RecipeBook, depths
from visitor import MR_ALCHEMY, VisitorPolicy, pick_gift

MAX_NAME_LEN = 24
MAX_PLAYERS = 5
# A player who has just joined but not opened their event stream yet holds a seat
# for this long. A player who disconnects loses their seat at once, but their
# token stays valid for this long so a page refresh or network blip can reconnect
# (keeping the same colour) as long as the seat and name are still free.
JOIN_GRACE_SECONDS = 30.0
MAX_TILES = 300
SOLO_HINTS_PER_DAY = 1    # a solo game's hints: this many a day, per account
RARITY_CUTS = (0.5, 0.8, 0.95)   # an element is uncommon / rare / epic from these fractions of the depth ranking up
QUEUE_SIZE = 256

# The genie is unlocked when the Djinn element is discovered. The element is found by
# id or label, ignoring case, so it works whatever the recipe file calls it ("djinn",
# "Djinn"...). It starts with a few wishes and earns one more for every
# GENIE_BONUS_EVERY elements discovered after the unlock.
DJINN = "djinn"
TIME = "time"
TIME_UNLOCK_AT = 100   # found elements (starters included) at which Time is added to the book for free
UNLOCKED_BY = ""       # the "discovered by" of an element that was unlocked rather than made
GENIE_BASE_USES = 3
GENIE_BONUS_EVERY = 50

# Distinct, mid-tone colours that stay readable on light and dark backgrounds.
PALETTE = (
    "#e53935", "#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#00897b",
    "#d81b60", "#6d4c41", "#3949ab", "#7cb342", "#00acc1", "#f9a825",
)

# How far (in workspace fractions) extra result tiles are offset from the first.
RESULT_OFFSET = 0.03
RECENT_SECONDS = 30 * 60.0  # how long after leaving a game it still shows under "recently joined"


class GameError(Exception):
    """An error that maps directly onto an HTTP status and message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Player:
    token: str  # secret, used only by this player's browser
    pid: str    # public id, safe to broadcast
    name: str


@dataclass(eq=False)
class Subscriber:
    player: Player
    messages: "queue.Queue[str | None]" = field(default_factory=lambda: queue.Queue(QUEUE_SIZE))  # None = close
    dead: bool = False


@dataclass(frozen=True)
class Challenge:
    """Marks a game as one player's attempt at the daily challenge (see daily.py).

    The game then has one player, no hints, Djinn or free Time element, and cannot be saved. The first
    time `target` is discovered the attempt is over: its score is the number of elements discovered
    (starters excluded, the target included), and `on_finish(username, score)` is called once.
    """
    date: str
    target: str                    # element id
    reference: int | None = None   # elements unlocked by the game's own route, to show as a number to beat
    on_finish: Callable[[str, int], None] | None = None
    tier: str = "medium"           # easy, medium or hard
    streak: Callable[[str], int] | None = None   # the account's best run of days with a finished challenge, for achievements


@dataclass(frozen=True)
class Race:
    """Marks a game as one racer's private world in a race (see race.py).

    After every discovery the game calls `report(count, reached)` (count = elements discovered, starters
    included; reached = the goal is met) and `is_open()` says whether the race is running: nothing can be
    combined before the go or after somebody has won.
    """
    id: str
    goal: str                      # "target" or "count"
    target: str | None             # element id of a "target" race
    goal_count: int
    report: Callable[[int, bool], bool]
    is_open: Callable[[], bool]


@dataclass
class Tile:
    id: str
    element: str
    x: float
    y: float
    held_by: str | None = None  # pid of the player currently moving it

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "element": self.element, "x": self.x, "y": self.y, "held_by": self.held_by}


def format_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def clean_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise GameError(400, "name must be text")
    collapsed = " ".join(raw.split())
    cleaned = "".join(ch for ch in collapsed if unicodedata.category(ch)[0] != "C")
    cleaned = cleaned[:MAX_NAME_LEN].strip()
    if not cleaned:
        raise GameError(400, "please enter a name")
    return cleaned


def clamp01(value: object) -> float:
    """Validate a coordinate and clamp it into 0..1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GameError(400, "coordinates must be numbers")
    return min(1.0, max(0.0, float(value)))


class GameState:
    def __init__(self, book: RecipeBook, *, name: str = "game", slug: str = "game", creator: str = "",
                 clock: Callable[[], float] = time.monotonic,
                 grace: float = JOIN_GRACE_SECONDS,
                 rng: random.Random | None = None,
                 challenge: Challenge | None = None, race: Race | None = None,
                 solo: Callable[[dict[str, Any], int, int, bool, set[str]], list[str]] | None = None,
                 today: Callable[[], str] | None = None,
                 pictures: PictureIndex | None = None,
                 visitors: VisitorPolicy | None = None,
                 profile: Callable[[str, dict[str, int], dict[str, int]], list[str]] | None = None) -> None:
        self.book = book
        # profiles.py: called as profile(username, add, best) to count something for an account; returns the account-wide achievements it earned
        self.profile = profile
        self.visitors = visitors   # None: Mr Alchemy never visits this game (see visitor.py)
        self.pictures = pictures   # the address of each element's picture, sent only with that element's data
        self._today = today or (lambda: time.strftime("%Y-%m-%d", time.gmtime()))   # the day a solo hint is counted against
        # Solo mode (see solo.py): called as solo(export, found, total, complete, earned) after every discovery, so progress
        # is kept; it returns the achievements that are new, which are announced to the player.
        self.solo = solo
        self.challenge = challenge
        self.race = race
        self._won = False                # this racer has just won the race
        self._result: int | None = None  # a challenge attempt's score once the target has been found
        self.name = name
        self.slug = slug
        self.creator = creator  # username of the account that started the game; only they can save it
        self._rng = rng or random.Random()
        self._clock = clock
        self._grace = grace
        self._touched = clock()                   # last join / connect / disconnect, for idle expiry
        self._last_active: dict[str, float] = {}  # token -> when they joined or last disconnected
        self._connected_once: set[str] = set()    # tokens that have opened an event stream
        self._lock = threading.RLock()
        self._discovered: dict[str, str | None] = {s: None for s in book.starters}  # id -> discoverer
        self._time = find_element(book, TIME)  # id of the element that unlocks at TIME_UNLOCK_AT, if any
        self._djinn = find_element(book, DJINN)  # id of the element that unlocks the genie, if any
        if challenge is not None or race is not None:  # a challenge or a race is a plain puzzle: no free elements, no wishes
            self._time = self._djinn = None
        # fewest steps to make each reachable element; Time is handed out free, so what needs Time counts as reachable
        self._depth = depths(book, [self._time] if self._time else [])
        self.total = len(self._collectable())  # what can be found in all: reachable elements, and Time if the game adds it
        depth_ranking = sorted(self._depth.values())
        self._rarity_at = tuple(depth_ranking[min(len(depth_ranking) - 1, int(len(depth_ranking) * q))]
                                for q in RARITY_CUTS) if depth_ranking else (1, 1, 1)
        self._hints = {"day": "", "used": 0, "total": 0}   # a solo game's hint of the day
        self._stats: dict[str, dict[str, int]] = {}        # casefolded player name -> {"tries", "misses"}, for the awards
        self._first_finder: str | None = None              # who made the game's first discovery
        self._emote_at: dict[str, float] = {}              # pid -> when they last sent an emote
        self._counted: set[str] = set()                    # people already counted as having played this game with others
        self._counted_full: set[str] = set()               # ...and as having been in it when it was full
        self._race_counted = False                         # this racer's finish has been counted
        self._virtual: dict[str, Player] = {}              # token -> a computer-controlled player (bots.py): seated, but never a "person"
        self._visited = False                              # Mr Alchemy has been (or is not coming): once per game
        self._together = 0.0                               # seconds two or more people have played together
        self._visit_after: float | None = None             # how long of that before he comes; None until it is first drawn
        self._visit_tick: float | None = None              # the clock at the last visitor_tick
        self._genie_unlocked_at: int | None = None  # number of discoveries when the genie unlocked
        self._genie_used = 0
        self._players: dict[str, Player] = {}         # token -> player
        self._colors: dict[str, str] = {}             # pid -> colour
        self._subs: list[Subscriber] = []
        self._tiles: dict[str, Tile] = {}             # shared workspace
        self._tile_seq = 0
        self._last_seen: dict[str, float] = {}        # casefolded name -> when that player was last in this live game
        self._player_names: list[str] = []            # everyone who has played here, so a saved game can be found by them
        self.ended = False

    # ---- helpers (call with the lock held) ----------------------------

    def _element_json(self, eid: str, with_by: bool = False) -> dict[str, Any]:
        el = self.book.elements[eid]
        out: dict[str, Any] = {"id": eid, "label": el.label, "emoji": el.emoji}
        if el.credit:
            out["credit"] = el.credit
        url = self.pictures.url_for(eid) if self.pictures is not None else None
        if url:
            out["pic"] = url
        if with_by:
            out["by"] = self._discovered[eid]
            out["rarity"] = self._rarity(eid)
        return out

    def _rarity(self, eid: str) -> str:
        """common, uncommon, rare or epic: how far from the starting elements it is, compared with the other elements."""
        depth = self._depth.get(eid, 0)
        if depth == 0:
            return "common"
        uncommon, rare, epic = self._rarity_at
        return "epic" if depth >= epic else "rare" if depth >= rare else "uncommon" if depth >= uncommon else "common"

    def _assign_color(self, pid: str) -> None:
        if pid in self._colors:
            return
        used = Counter(self._colors.values())
        self._colors[pid] = min(PALETTE, key=lambda c: used[c])  # first least-used colour

    def _online_players(self) -> list[dict[str, str]]:
        seen: set[str] = set()
        out = []
        for sub in self._subs:
            pid = sub.player.pid
            if pid not in seen:
                seen.add(pid)
                out.append({"pid": pid, "name": sub.player.name, "color": self._colors[pid]})
        for player in self._virtual.values():
            if player.pid not in seen:
                seen.add(player.pid)
                out.append({"pid": player.pid, "name": player.name, "color": self._colors[player.pid]})
        return out

    def _score(self) -> int:
        """Elements discovered so far, not counting the starting ones."""
        return len(self._discovered) - len(self.book.starters)

    def _challenge_json(self) -> dict[str, Any] | None:
        if self.challenge is None:
            return None
        return {"date": self.challenge.date, "tier": self.challenge.tier,
                "target": self._element_json(self.challenge.target),
                "reference": self.challenge.reference, "starters": len(self.book.starters), "done": self._result}

    def _race_json(self) -> dict[str, Any] | None:
        if self.race is None:
            return None
        return {"id": self.race.id, "goal": self.race.goal, "goal_count": self.race.goal_count,
                "target": self._element_json(self.race.target) if self.race.target else None}

    @property
    def private(self) -> bool:
        """A private game belongs to one account: a daily challenge attempt, a racer's world or a solo game."""
        return self.challenge is not None or self.race is not None or self.solo is not None

    def _snapshot(self, player: Player) -> dict[str, Any]:
        return {
            "challenge": self._challenge_json(),
            "race": self._race_json(),
            "you": player.pid,
            "game": {"id": self.slug, "name": self.name, "creator": self.creator, "solo": self.solo is not None,
                     "hints_left": self._hints_left() if self.solo is not None else None},
            "genie": self._genie_status(),
            "total": self.total,
            "elements": [self._element_json(eid, with_by=True) for eid in self._discovered],
            "players": self._online_players(),
            "tiles": [tile.to_json() for tile in self._tiles.values()],
        }

    def _publish(self, event: str, data: Any, exclude: Subscriber | None = None,
                 exclude_pid: str | None = None) -> None:
        message = format_event(event, data)
        for sub in list(self._subs):
            if sub is exclude or sub.player.pid == exclude_pid:
                continue
            try:
                sub.messages.put_nowait(message)
            except queue.Full:  # client too slow: drop it, it can reconnect
                sub.dead = True
                self._subs.remove(sub)

    def _publish_to(self, name: str, event: str, data: Any) -> None:
        """Send an event only to the connections of one player (an achievement is nobody else's business)."""
        message = format_event(event, data)
        for sub in list(self._subs):
            if sub.player.name == name:
                try:
                    sub.messages.put_nowait(message)
                except queue.Full:
                    sub.dead = True
                    self._subs.remove(sub)

    def _account(self, name: str, add: dict[str, int] | None = None, best: dict[str, int] | None = None) -> None:
        """Count something for an account (see profiles.py) and tell the player about any achievement it earns. Never breaks the game."""
        if self.profile is None or not name:
            return
        try:
            earned = self.profile(name, add or {}, best or {})
        except Exception as exc:
            print(f"warning: could not count for an achievement: {exc}", file=sys.stderr)
            return
        for achievement in earned:
            self._publish_to(name, "achievement", achievement_json(achievement))

    def _note_company(self) -> None:
        """When two or more people are in a shared game, each is counted once for playing with others, and once more if it is full."""
        if self.profile is None or self.private:
            return
        people = self._people()
        if len(people) < 2:
            return
        for person in people:
            key, add = person.name.casefold(), {}
            if key not in self._counted:
                self._counted.add(key)
                add["games"] = 1
            if len(people) >= MAX_PLAYERS and key not in self._counted_full:
                self._counted_full.add(key)
                add["full_room"] = 1
            if add:
                self._account(person.name, add)

    def _forget(self, token: str) -> None:
        player = self._players.pop(token, None)
        self._virtual.pop(token, None)
        if player is not None:
            self._colors.pop(player.pid, None)
        self._last_active.pop(token, None)
        self._connected_once.discard(token)

    def _prune(self) -> None:
        """Forget players who are offline and past their grace period.

        Players are never saved, only kept in memory for this server run.
        """
        online = {s.player.token for s in self._subs}
        now = self._clock()
        for token in list(self._players):
            if token in self._virtual:
                continue   # a computer-controlled player has no connection to lose: bots.py takes it out when it is time
            if token not in online and now - self._last_active.get(token, 0.0) >= self._grace:
                self._forget(token)

    def _seated(self) -> list[Player]:
        """Players who count towards the player limit and the unique-name rule.

        That is everyone currently connected, plus anyone who joined moments ago
        and is still opening their connection. A player who disconnected is not
        seated, so their name and seat are free straight away.
        """
        online = {s.player.token for s in self._subs}
        now = self._clock()
        return [
            player for token, player in self._players.items()
            if token in self._virtual
            or token in online
            or (token not in self._connected_once and now - self._last_active[token] < self._grace)
        ]

    def _people(self) -> list[Player]:
        """The seated players who are people: what Mr Alchemy, a game's expiry and the companions' own rules count."""
        return [p for p in self._seated() if p.token not in self._virtual]

    def people_count(self) -> int:
        with self._lock:
            self._prune()
            return len(self._people())

    def _make_room(self) -> None:
        """A person always gets a seat: if the game is full only because of computer-controlled players, one of them leaves."""
        while len(self._seated()) >= MAX_PLAYERS and self._virtual:
            self._drop_virtual(next(iter(self._virtual)))

    def _require_player(self, token: object) -> Player:
        self._prune()
        player = self._players.get(token) if isinstance(token, str) else None
        if player is None:
            raise GameError(403, "unknown player, please rejoin")
        return player

    def _require_discovered(self, element: object) -> str:
        # One answer for "no such element" and "not found yet": two answers would let a script from the browser
        # console tell which guessed names exist in the book.
        if not isinstance(element, str) or element not in self._discovered:
            raise GameError(400, "that element has not been discovered yet")
        return element

    def _get_tile(self, tile_id: object) -> Tile:
        tile = self._tiles.get(tile_id) if isinstance(tile_id, str) else None
        if tile is None:
            raise GameError(404, "that tile no longer exists")
        return tile

    def _new_tile(self, element: str, x: float, y: float) -> Tile:
        self._tile_seq += 1
        tile = Tile(f"t{self._tile_seq}", element, x, y)
        self._tiles[tile.id] = tile
        return tile

    def _record(self, player: Player, results: list[str] | tuple[str, ...]) -> list[str]:
        """Mark results as discovered by `player`; returns the ones that were new."""
        new = [r for r in results if r not in self._discovered]
        for r in new:
            self._discovered[r] = player.name
        if new and self._first_finder is None and player.name != MR_ALCHEMY:
            self._first_finder = player.name
        if new and not self.private and player.token not in self._virtual and player.name not in ("", MR_ALCHEMY):
            self._account(player.name, {"finds": len(new), "epic_finds": sum(1 for e in new if self._rarity(e) == "epic")})
        if self._djinn in new and self._genie_unlocked_at is None:
            self._genie_unlocked_at = len(self._discovered)
        if self.challenge is not None and self._result is None and self.challenge.target in new:
            self._result = self._score()
            if self.challenge.on_finish is not None:
                try:
                    self.challenge.on_finish(player.name, self._result)
                except Exception as exc:  # a score that cannot be stored must not break the game
                    print(f"warning: could not record the challenge score: {exc}", file=sys.stderr)
                streak = 0
                try:
                    streak = self.challenge.streak(player.name) if self.challenge.streak is not None else 0
                except Exception as exc:
                    print(f"warning: could not work out the daily streak: {exc}", file=sys.stderr)
                self._account(player.name, {"daily_done": 1, **({"daily_hard": 1} if self.challenge.tier == "hard" else {})},
                              {"daily_streak": streak})
        if self.race is not None and new:
            count = len(self._discovered)
            reached = (self.race.target in self._discovered) if self.race.goal == "target" \
                else count >= self.race.goal_count
            won = False
            try:
                won = bool(self.race.report(count, reached))
                if won:
                    self._won = True
            except Exception as exc:  # the race must not break the game
                print(f"warning: could not report race progress: {exc}", file=sys.stderr)
            if reached and not self._race_counted:
                self._race_counted = True
                self._account(player.name, {"race_done": 1, **({"race_wins": 1} if won else {})})
        return new

    def _announce(self, player: Player, new: list[str], base: int) -> None:
        for i, r in enumerate(new, 1):
            self._publish("discovered", {
                **self._element_json(r, with_by=True),
                "pid": player.pid,
                "found": base + i,
                "total": self.total,
            })
        self._unlock_time()
        self.report_solo()
        if self._won:
            self._won = False
            self._publish("race_won", {"name": player.name})
        if self.challenge is not None and self._result is not None and self.challenge.target in new:
            self._publish("challenge_done", {"score": self._result, "reference": self.challenge.reference,
                                             "target": self._element_json(self.challenge.target)})

    def _collectable(self) -> set[str]:
        """Every element a player can end up with: what recipes can make, and Time, which is unlocked for free."""
        return set(self._depth) | ({self._time} if self._time else set())

    def report_solo(self) -> None:
        """Hand a solo game's progress to its store (call with the lock held). A failure must not break the game."""
        if self.solo is None:
            return
        collectable = self._collectable()
        found = len(self._discovered.keys() & collectable)
        complete = collectable <= self._discovered.keys()
        progress = Progress(
            discovered=len(self._discovered) - len(self.book.starters), complete=complete,
            deepest=max((self._depth.get(e, 0) for e in self._discovered), default=0),
            djinn=self._genie_unlocked_at is not None, wishes=self._genie_used,
            time=self._time is not None and self._time in self._discovered, hints=self._hints["total"])
        try:
            new = self.solo(self.export(), found, len(collectable), complete, earned_ids(progress))
        except Exception as exc:
            print(f"warning: could not save solo progress: {exc}", file=sys.stderr)
            return
        for achievement in new or []:
            self._publish("achievement", achievement_json(achievement))

    def _unlock_time(self) -> None:
        """Add Time to the book once the room has found TIME_UNLOCK_AT elements, if nobody has made it yet."""
        if self._time is None or self._time in self._discovered or len(self._discovered) < TIME_UNLOCK_AT:
            return
        self._discovered[self._time] = UNLOCKED_BY
        self._publish("discovered", {
            **self._element_json(self._time, with_by=True),
            "pid": None,
            "unlocked": TIME_UNLOCK_AT,
            "found": len(self._discovered),
            "total": self.total,
        })

    def _genie_status(self) -> dict[str, Any]:
        """The Djinn's state, or {"available": False} until it is unlocked: a locked Djinn is not shown at all."""
        if self._djinn is None or self._genie_unlocked_at is None:
            return {"available": False}
        label = self.book.elements[self._djinn].label
        since = len(self._discovered) - self._genie_unlocked_at
        total = GENIE_BASE_USES + since // GENIE_BONUS_EVERY
        return {
            "available": True,
            "element": label,
            "unlocked": True,
            "left": total - self._genie_used,
            "total": total,
            "next_in": GENIE_BONUS_EVERY - since % GENIE_BONUS_EVERY,
        }

    def _announce_genie(self, before: dict[str, Any], used_by: dict[str, Any] | None = None) -> None:
        after = self._genie_status()
        if after != before or used_by is not None:
            self._publish("genie", {"status": after, "used_by": used_by})

    # ---- players and event streams ------------------------------------

    def join(self, raw_name: object) -> Player:
        name = clean_name(raw_name)
        with self._lock:
            if self.private and name.casefold() != clean_name(self.creator).casefold():
                raise GameError(403, "this is someone else's private game")
            self._prune()
            self._make_room()
            seated = self._seated()
            if len(seated) >= MAX_PLAYERS:
                raise GameError(409, f"the game is full ({MAX_PLAYERS} players at most)")
            taken = {p.name.casefold() for p in seated}
            if name.casefold() in taken:
                raise GameError(409, f"the name {name!r} is already taken, please pick another")
            player = Player(secrets.token_urlsafe(16), secrets.token_hex(4), name)
            self._last_seen[name.casefold()] = self._clock()
            if name.casefold() not in {n.casefold() for n in self._player_names} and len(self._player_names) < 50:
                self._player_names.append(name)
            self._players[player.token] = player
            self._last_active[player.token] = self._touched = self._clock()
            return player

    def add_virtual(self, raw_name: object) -> Player:
        """Seat a computer-controlled player (see bots.py). It plays through the same methods as a person, appears in the player list like
        one, and is never saved, never counted as a person, and always leaves its seat to a person who needs it.

        Public games only, and it never takes the last free seat.
        """
        name = clean_name(raw_name)
        with self._lock:
            if self.private or self.ended:
                raise GameError(409, "companions only join public games that are still running")
            self._prune()
            seated = self._seated()
            if len(seated) >= MAX_PLAYERS - 1:
                raise GameError(409, "no free seat to spare")
            if name.casefold() in {p.name.casefold() for p in seated}:
                raise GameError(409, f"the name {name!r} is already taken")
            player = Player(secrets.token_urlsafe(16), secrets.token_hex(4), name)
            self._players[player.token] = player
            self._virtual[player.token] = player
            self._last_active[player.token] = self._clock()
            self._assign_color(player.pid)
            self._publish("players", {"players": self._online_players()})
            return player

    def remove_virtual(self, token: str) -> None:
        """A computer-controlled player leaves: its held tiles are put down and its cursor hidden, like anyone leaving."""
        with self._lock:
            self._drop_virtual(token)

    def _drop_virtual(self, token: str) -> None:
        player = self._virtual.get(token)
        if player is None:
            return
        for tile in self._tiles.values():
            if tile.held_by == player.pid:
                tile.held_by = None
                self._publish("tile_dropped", {"id": tile.id, "x": tile.x, "y": tile.y, "pid": player.pid})
        self._publish("cursor", {"pid": player.pid, "x": None, "y": None})
        self._forget(token)
        self._publish("players", {"players": self._online_players()})

    def virtual_players(self) -> list[Player]:
        with self._lock:
            return list(self._virtual.values())

    def subscribe(self, token: str) -> Subscriber | None:
        """Register an event stream; its first message is a full snapshot."""
        with self._lock:
            self._prune()
            player = self._players.get(token)
            if player is None:
                return None
            if not any(s.player.token == token for s in self._subs):
                # Coming (back) online needs a free seat and a name nobody else is using now.
                self._make_room()
                others = [p for p in self._seated() if p.token != token]
                if len(others) >= MAX_PLAYERS or player.name.casefold() in {p.name.casefold() for p in others}:
                    self._forget(token)
                    return None
            self._connected_once.add(token)
            self._touched = self._clock()
            sub = Subscriber(player)
            self._assign_color(player.pid)
            self._subs.append(sub)  # before the snapshot, so the player list includes them
            sub.messages.put_nowait(format_event("snapshot", self._snapshot(player)))
            self._publish("players", {"players": self._online_players()}, exclude=sub)
            self._note_company()
            return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            self._last_active[sub.player.token] = self._touched = self._clock()
            self._last_seen[sub.player.name.casefold()] = self._touched
            pid = sub.player.pid
            if not any(s.player.pid == pid for s in self._subs):
                for tile in self._tiles.values():  # don't leave tiles locked by someone who left
                    if tile.held_by == pid:
                        tile.held_by = None
                        self._publish("tile_dropped", {"id": tile.id, "x": tile.x, "y": tile.y, "pid": pid})
                self._publish("cursor", {"pid": pid, "x": None, "y": None})
            self._publish("players", {"players": self._online_players()})

    def discovered(self) -> dict[str, str | None]:
        with self._lock:
            return dict(self._discovered)

    def tiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [tile.to_json() for tile in self._tiles.values()]

    # ---- cursors --------------------------------------------------------

    def set_cursor(self, token: str, x: object, y: object) -> dict[str, Any]:
        """Share this player's pointer position; x and y both None hides it."""
        with self._lock:
            player = self._require_player(token)
            hidden = x is None and y is None
            self._publish(
                "cursor",
                {"pid": player.pid, "x": None if hidden else clamp01(x), "y": None if hidden else clamp01(y)},
                exclude_pid=player.pid,
            )
            return {}

    # ---- the shared workspace -------------------------------------------

    def add_tile(self, token: str, element: object, x: object, y: object) -> dict[str, Any]:
        with self._lock:
            player = self._require_player(token)
            self._require_discovered(element)
            assert isinstance(element, str)
            if len(self._tiles) >= MAX_TILES:
                raise GameError(409, "the workspace is full")
            tile = self._new_tile(element, clamp01(x), clamp01(y))
            self._publish("tile_added", {"tile": tile.to_json()}, exclude_pid=player.pid)
            return tile.to_json()

    def grab_tile(self, token: str, tile_id: object) -> dict[str, Any]:
        with self._lock:
            player = self._require_player(token)
            tile = self._get_tile(tile_id)
            if tile.held_by not in (None, player.pid):
                raise GameError(409, "someone else is moving that tile")
            tile.held_by = player.pid
            self._publish("tile_grabbed", {"id": tile.id, "pid": player.pid}, exclude_pid=player.pid)
            return {}

    def move_tile(self, token: str, tile_id: object, x: object, y: object,
                  cx: object = None, cy: object = None) -> dict[str, Any]:
        """Move a tile this player holds. cx and cy optionally carry their pointer position."""
        with self._lock:
            player = self._require_player(token)
            tile = self._get_tile(tile_id)
            if tile.held_by != player.pid:
                raise GameError(409, "grab the tile before moving it")
            tile.x, tile.y = clamp01(x), clamp01(y)
            self._publish("tile_moved", {
                "id": tile.id, "x": tile.x, "y": tile.y, "pid": player.pid,
                "cx": None if cx is None else clamp01(cx),
                "cy": None if cy is None else clamp01(cy),
            }, exclude_pid=player.pid)
            return {}

    def drop_tile(self, token: str, tile_id: object, x: object, y: object) -> dict[str, Any]:
        with self._lock:
            player = self._require_player(token)
            tile = self._get_tile(tile_id)
            if tile.held_by != player.pid:
                raise GameError(409, "grab the tile before dropping it")
            tile.x, tile.y = clamp01(x), clamp01(y)
            tile.held_by = None
            self._publish("tile_dropped", {"id": tile.id, "x": tile.x, "y": tile.y, "pid": player.pid},
                          exclude_pid=player.pid)
            return {}

    def remove_tile(self, token: str, tile_id: object) -> dict[str, Any]:
        with self._lock:
            player = self._require_player(token)
            tile = self._get_tile(tile_id)
            if tile.held_by not in (None, player.pid):
                raise GameError(409, "someone else is moving that tile")
            del self._tiles[tile.id]
            self._publish("tile_removed", {"id": tile.id}, exclude_pid=player.pid)
            return {}

    def clear_tiles(self, token: str) -> dict[str, Any]:
        with self._lock:
            player = self._require_player(token)
            cleared = len(self._tiles)
            self._tiles.clear()
            self._publish("tiles_cleared", {"pid": player.pid, "name": player.name}, exclude_pid=player.pid)
            return {"cleared": cleared}

    # ---- the genie ---------------------------------------------------------------

    def genie_use(self, token: str, element: object) -> dict[str, Any]:
        """Spend one wish: combine `element` with every discovered element (itself included).

        Every distinct result becomes a tile on the shared workspace, laid out in a grid.
        A wish that would make nothing is not spent.
        """
        with self._lock:
            player = self._require_player(token)
            before = self._genie_status()
            if not before["available"]:
                raise GameError(409, "the Djinn is not unlocked yet: discover the Djinn element first")
            if before["left"] <= 0:
                raise GameError(409, f"the Djinn has no wishes left (another one comes after "
                                     f"{before['next_in']} more discoveries)")
            chosen = self._require_discovered(element)

            results: list[str] = []
            for partner in list(self._discovered):
                for r in self.book.combine(chosen, partner):
                    if r not in results:
                        results.append(r)
            if not results:
                return {"element": chosen, "results": [], "new": [], "tiles": [], "genie": before}
            if len(self._tiles) + len(results) > MAX_TILES:
                raise GameError(409, f"the Djinn needs room for {len(results)} tiles: clear some space first")

            base = len(self._discovered)
            new = self._record(player, results)
            self._genie_used += 1
            self._announce(player, new, base)
            created = []
            for (x, y), r in zip(self._grid(len(results)), results):
                tile = self._new_tile(r, x, y)
                created.append(tile.to_json())
                self._publish("tile_added", {"tile": tile.to_json()}, exclude_pid=player.pid)
            self._announce_genie(before, {"name": player.name, "element": self._element_json(chosen)})
            return {
                "element": chosen,
                "results": [self._element_json(r, with_by=True) for r in results],
                "new": new,
                "tiles": created,
                "genie": self._genie_status(),
            }

    @staticmethod
    def _grid(n: int) -> list[tuple[float, float]]:
        """n evenly spaced workspace positions (fractions), roughly matching a wide screen."""
        cols = max(1, math.ceil(math.sqrt(n * 1.6)))
        rows = math.ceil(n / cols)
        return [
            (0.1 + 0.8 * (i % cols + 0.5) / cols, 0.12 + 0.76 * (i // cols + 0.5) / rows)
            for i in range(n)
        ]

    # ---- saving, resuming and ending (used by games.py and saves.py) ---------------

    def export(self) -> dict[str, Any]:
        """What is kept when a game is saved. The workspace tiles are not."""
        with self._lock:
            return {
                "version": 1,
                "name": self.name,
                "slug": self.slug,
                "creator": self.creator,
                "discovered": dict(self._discovered),
                "genie": {"unlocked_at": self._genie_unlocked_at, "used": self._genie_used},
                "players": list(self._player_names),
                **({"hints": dict(self._hints)} if self.solo is not None else {}),
            }

    def restore(self, data: dict[str, Any]) -> None:
        """Load saved discoveries, Djinn state and player names, ignoring anything that no longer fits the recipes."""
        with self._lock:
            saved = data.get("discovered")
            if isinstance(saved, dict):
                for eid, by in saved.items():
                    if eid in self.book.elements and eid not in self._discovered:
                        self._discovered[eid] = by if isinstance(by, str) else None
            if self._djinn in self._discovered:
                genie = data.get("genie") if isinstance(data.get("genie"), dict) else {}
                at, used = genie.get("unlocked_at"), genie.get("used")
                if not isinstance(at, int) or isinstance(at, bool) or not 0 <= at <= len(self._discovered):
                    at = len(self._discovered)
                self._genie_unlocked_at = at
                total = GENIE_BASE_USES + (len(self._discovered) - at) // GENIE_BONUS_EVERY
                used = used if isinstance(used, int) and not isinstance(used, bool) else 0
                self._genie_used = max(0, min(used, total))
            self._unlock_time()  # a saved room that already has enough elements
            hints = data.get("hints")
            if isinstance(hints, dict):
                day, used, total = hints.get("day"), hints.get("used"), hints.get("total")

                def count(n: object) -> int:
                    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
                self._hints = {"day": day if isinstance(day, str) else "", "used": count(used), "total": count(total)}
            names = data.get("players")
            if isinstance(names, list):
                self._player_names = [n[:MAX_NAME_LEN] for n in names if isinstance(n, str) and n.strip()][:50]

    # ---- hot reload (used by hotreload.py) ------------------------------------------

    def live_export(self) -> dict[str, Any]:
        """Everything needed to carry this running game across a server restart.

        This is more than a save: it also keeps the workspace tiles and the players' tokens,
        so nobody has to rejoin. Times are stored as ages in seconds because the clock
        restarts with the process.
        """
        with self._lock:
            now = self._clock()
            data = self.export()
            if self.solo is not None:
                data["solo"] = True
            data["awards"] = {"stats": {k: dict(v) for k, v in self._stats.items()}, "first": self._first_finder}
            data["visitor"] = {"visited": self._visited, "together": self._together, "after": self._visit_after}
            if self.challenge is not None:
                data["challenge"] = {"date": self.challenge.date, "tier": self.challenge.tier,
                                     "target": self.challenge.target,
                                     "reference": self.challenge.reference, "result": self._result}
            data["live"] = {
                "tile_seq": self._tile_seq,
                "tiles": [{"id": t.id, "element": t.element, "x": t.x, "y": t.y} for t in self._tiles.values()],
                "players": [{"token": p.token, "pid": p.pid, "name": p.name,
                             "color": self._colors.get(p.pid, "")} for p in self._players.values() if p.token not in self._virtual],
                "last_seen": {name: max(0.0, now - seen) for name, seen in self._last_seen.items()},
            }
            return data

    def live_restore(self, data: dict[str, Any]) -> None:
        """Load what live_export() wrote. Anything that no longer fits the recipes or the rules is skipped.

        Restored players count as just-joined, so each has JOIN_GRACE_SECONDS to reconnect.
        """
        self.restore(data)
        saved_awards = data.get("awards") if isinstance(data.get("awards"), dict) else {}
        stats = saved_awards.get("stats")
        if isinstance(stats, dict):
            for key, value in stats.items():
                if isinstance(key, str) and isinstance(value, dict):
                    self._stats[key[:MAX_NAME_LEN * 2]] = {
                        k: value[k] for k in ("tries", "misses")
                        if isinstance(value.get(k), int) and not isinstance(value.get(k), bool) and value[k] >= 0}
        first = saved_awards.get("first")
        self._first_finder = first if isinstance(first, str) else None
        saved_visit = data.get("visitor") if isinstance(data.get("visitor"), dict) else {}
        self._visited = saved_visit.get("visited") is True
        for key, attr in (("together", "_together"), ("after", "_visit_after")):
            value = saved_visit.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                setattr(self, attr, float(value))
        live = data.get("live") if isinstance(data.get("live"), dict) else {}
        with self._lock:
            saved = data.get("challenge")
            result = saved.get("result") if isinstance(saved, dict) else None
            if self.challenge is not None and isinstance(result, int) and not isinstance(result, bool):
                self._result = result
            now = self._clock()
            seq = live.get("tile_seq")
            self._tile_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0
            for raw in live.get("tiles", []):
                try:
                    element, x, y, tid = raw["element"], clamp01(raw["x"]), clamp01(raw["y"]), str(raw["id"])
                except (KeyError, TypeError, GameError):
                    continue
                if element in self._discovered and len(self._tiles) < MAX_TILES and tid not in self._tiles:
                    self._tiles[tid] = Tile(tid, element, x, y)
            for raw in live.get("players", [])[:MAX_PLAYERS]:
                try:
                    token, pid, name, color = raw["token"], raw["pid"], clean_name(raw["name"]), raw.get("color", "")
                except (KeyError, TypeError, GameError):
                    continue
                if not isinstance(token, str) or not isinstance(pid, str) or token in self._players:
                    continue
                self._players[token] = Player(token, pid, name)
                self._last_active[token] = now
                if color in PALETTE:
                    self._colors[pid] = color
            seen = live.get("last_seen")
            if isinstance(seen, dict):
                for name, age in seen.items():
                    if isinstance(name, str) and isinstance(age, (int, float)) and not isinstance(age, bool):
                        self._last_seen[name] = now - max(0.0, float(age))
            self._touched = now

    @property
    def finished(self) -> bool:
        """True once a challenge attempt has found its target."""
        with self._lock:
            return self._result is not None

    def has_progress(self) -> bool:
        with self._lock:
            return len(self._discovered) > len(self.book.starters)

    def name_of(self, pid: object) -> str | None:
        """The account name of the player with this pid in the game, or None."""
        with self._lock:
            return next((player.name for player in self._players.values() if player.pid == pid), None)

    def authorize(self, token: object) -> Player:
        """The player behind `token`, or a 403 GameError."""
        with self._lock:
            return self._require_player(token)

    def close(self, by: str, saved: bool) -> None:
        """Tell everyone connected that the game has ended, then close their streams."""
        with self._lock:
            self.ended = True
            self._publish("ended", {"by": by, "saved": saved, "awards": [] if self.private else self.awards()})
            for sub in list(self._subs):
                try:
                    sub.messages.put_nowait(None)
                except queue.Full:
                    sub.dead = True

    # ---- listing and expiry (used by games.py) -----------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            self._prune()
            return {
                "id": self.slug,
                "name": self.name,
                "players": len(self._seated()),
                "max_players": MAX_PLAYERS,
                "found": len(self._discovered),
                "total": self.total,
                "genie": self._genie_status(),
            }

    def recently_left(self, username: str) -> bool:
        """True if this account was in the live game lately but is not in it now."""
        with self._lock:
            key = username.casefold()
            seen = self._last_seen.get(key)
            if seen is None or self._clock() - seen >= RECENT_SECONDS:
                return False
            return key not in {p.name.casefold() for p in self._seated()}

    def is_expired(self, idle_seconds: float) -> bool:
        """True once nobody has been in the game for `idle_seconds`."""
        with self._lock:
            self._prune()
            return not self._people() and not self._subs and self._clock() - self._touched >= idle_seconds

    # ---- the journal ----------------------------------------------------------

    def journal(self, token: str) -> dict[str, Any]:
        """Every discovered element with the recipes around it, for the journal page. Private; nothing is broadcast.

        A recipe is listed only when its ingredients and its result have all been discovered, so the
        journal never gives away an element nobody has found. `made_from` is [ingredient, ingredient]
        pairs; `used_in` is [other ingredient, result] pairs.
        """
        with self._lock:
            self._require_player(token)
            have = self._discovered
            made_from: dict[str, list[list[str]]] = {eid: [] for eid in have}
            used_in: dict[str, list[list[str]]] = {eid: [] for eid in have}
            for (a, b), results in self.book.lookup.items():
                if a not in have or b not in have:
                    continue
                for result in results:
                    if result not in have:
                        continue
                    made_from[result].append([a, b])
                    used_in[a].append([b, result])
                    if b != a:
                        used_in[b].append([a, result])

            def label(eid: str) -> str:
                return self.book.elements[eid].label.casefold()

            entries = []
            for eid in sorted(have, key=label):
                entries.append({
                    **self._element_json(eid, with_by=True),
                    "starter": eid in self.book.starters,
                    "made_from": sorted(made_from[eid], key=lambda p: (label(p[0]), label(p[1]))),
                    "used_in": sorted(used_in[eid], key=lambda p: (label(p[1]), label(p[0]))),
                })
            # Only the number of elements still to find is sent, never which: the silhouettes in the collection
            # view are drawn from this count. Time is unlocked for free, so it counts although no recipe makes it.
            collectable = self._collectable()
            found = len(have.keys() & collectable)
            return {"elements": entries,
                    "progress": {"found": found, "total": len(collectable), "locked": len(collectable) - found}}

    # ---- Mr Alchemy -----------------------------------------------------------

    def _may_be_visited(self) -> bool:
        return self.visitors is not None and self.visitors.enabled and not self.private and not self.ended

    def visitor_tick(self) -> bool:
        """Called every few seconds by the server. Counts the time two or more people have played together and, once a random amount
        of it has passed, lets Mr Alchemy visit (once per game). Returns True if he came."""
        with self._lock:
            now = self._clock()
            last, self._visit_tick = self._visit_tick, now
            if self._visited or not self._may_be_visited():
                return False
            assert self.visitors is not None
            self._prune()
            if len(self._people()) < 2:
                return False
            self._together += min(now - last, 30.0) if last is not None else 0.0   # a long gap (a paused server) is not time together
            if self._visit_after is None:
                if self._rng.random() >= self.visitors.chance:
                    self._visited = True    # this game is not getting a visit
                    return False
                low, high = sorted((self.visitors.min_seconds, self.visitors.max_seconds))
                self._visit_after = self._rng.uniform(low, high)
            return self._together >= self._visit_after and self.visit() is not None

    def visit(self, force: bool = False) -> dict[str, Any] | None:
        """Mr Alchemy arrives: unlocks 5 to 10 elements for everyone, tells the pages to play the cutscene, and leaves.

        Public games only. `force` (an administrator's button) allows a visit with fewer than two people and a second visit. Returns
        what was announced, or None if no visit happened.
        """
        with self._lock:
            if self.private or self.ended or (self._visited and not force):
                return None
            if not force and len(self._people()) < 2:
                return None
            gift = pick_gift(self.book, self._discovered, self._rng)
            if not gift:
                return None
            self._visited = True
            base = len(self._discovered)
            self._record(Player("", "", MR_ALCHEMY), gift)
            self._unlock_time()
            announced = {"name": MR_ALCHEMY, "elements": [self._element_json(e, with_by=True) for e in gift],
                         "found": len(self._discovered), "total": self.total, "before": base}
            self._publish("visitor", announced)
            for person in self._people():
                self._account(person.name, {"struck": 1})
            return announced

    # ---- awards ---------------------------------------------------------------

    def awards(self) -> list[dict[str, Any]]:
        """Minor awards for the end of a shared game. Each is {"id", "icon", "title", "player", "detail"}.

        Only awards that mean something are given: "most found" needs at least two players, and nobody is named for an
        element that was unlocked for free rather than made.
        """
        with self._lock:
            made = {eid: by for eid, by in self._discovered.items() if by and by != MR_ALCHEMY}   # found by a player, not a gift
            out: list[dict[str, Any]] = []

            def label(eid: str) -> str:
                el = self.book.elements[eid]
                return f"{el.emoji} {el.label}".strip()

            if len({self._key(n) for n in self._player_names}) >= 2 and made:
                counts = Counter(made.values())
                best = max(counts.values())
                leaders = sorted(n for n, c in counts.items() if c == best)
                out.append({"id": "most_found", "icon": "🥇", "title": "Most elements found", "player": " & ".join(leaders),
                            "detail": f"{best} element{'s' if best != 1 else ''}"})
            if made:
                deepest = max(made, key=lambda e: (self._depth.get(e, 0), e))
                if self._depth.get(deepest, 0) > 0:
                    out.append({"id": "longest_chain", "icon": "⛓️", "title": "Longest chain", "player": made[deepest],
                                "detail": f"{label(deepest)}, {self._depth[deepest]} steps from the starting elements"})
                ways: Counter[str] = Counter()
                for results in self.book.lookup.values():
                    for r in results:
                        ways[r] += 1
                rarest = min((e for e in made if e != deepest), key=lambda e: (ways[e], -self._depth.get(e, 0), e), default=None)
                if rarest is not None:
                    n = ways[rarest]
                    out.append({"id": "rarest_find", "icon": "💎", "title": "Rarest find", "player": made[rarest],
                                "detail": f"{label(rarest)}, only {n} way{'s' if n != 1 else ''} to make it"})
            if self._first_finder:
                out.append({"id": "first_find", "icon": "⚡", "title": "First off the mark", "player": self._first_finder,
                            "detail": "made the game's first discovery"})
            misses = {n: s.get("misses", 0) for n, s in self._stats.items()}
            if misses and max(misses.values()) >= 5:
                top = max(misses, key=lambda n: misses[n])
                name = next((p for p in self._player_names if self._key(p) == top), top)
                out.append({"id": "persistent", "icon": "🔨", "title": "Never gives up", "player": name,
                            "detail": f"{misses[top]} combinations that did nothing"})
            return out

    @staticmethod
    def _key(name: str) -> str:
        return name.casefold()

    # ---- emotes ---------------------------------------------------------------

    def emote(self, token: str, emote: object) -> dict[str, Any]:
        """Show everyone in the game one of the emotes in emotes.py next to the sender's name. Only ids from that list work."""
        with self._lock:
            player = self._require_player(token)
            if self.private:
                raise GameError(403, "emotes are for shared games")
            if not isinstance(emote, str) or emote not in EMOTE_IDS:
                raise GameError(400, "unknown emote")
            now = self._clock()
            last = self._emote_at.get(player.pid)
            if last is not None and now - last < EMOTE_GAP_SECONDS:
                raise GameError(429, "one emote a second, please")
            self._emote_at[player.pid] = now
            self._publish("emote", {"pid": player.pid, "name": player.name, "emote": emote})
            self._account(player.name, {"emotes": 1})
            return {}

    # ---- hints ----------------------------------------------------------------

    def hint(self, token: str, kind: object, target: object = None) -> dict[str, Any]:
        """Private help for one player, based on what everyone has discovered. Nothing is broadcast.

        kind "can_make": an undiscovered element the current elements can make.
        kind "try":      two current elements that make something new.
        kind "find":     one recipe for the element named in `target`.
        """
        with self._lock:
            self._require_player(token)
            if self.private and self.solo is None:   # a solo game is private but is allowed its hints
                raise GameError(403, "hints are switched off in the daily challenge and in races")
            if self.solo is not None and self._hints_left() <= 0:
                raise GameError(429, "you have used today's hint in Solo: a new one comes tomorrow")
            answer = self._hint(kind, target)
            if self.solo is not None:
                if answer.pop("useful"):   # a hint that found nothing does not use up the day's hint
                    self._use_hint()
                answer["hints_left"] = self._hints_left()
            else:
                answer.pop("useful")
            return answer

    def _hints_left(self) -> int:
        """Solo hints still available today (the count starts again each day, UTC)."""
        used = self._hints["used"] if self._hints["day"] == self._today() else 0
        return max(0, SOLO_HINTS_PER_DAY - used)

    def _use_hint(self) -> None:
        today = self._today()
        if self._hints["day"] != today:
            self._hints["day"], self._hints["used"] = today, 0
        self._hints["used"] += 1
        self._hints["total"] += 1
        self.report_solo()

    def _hint(self, kind: object, target: object) -> dict[str, Any]:
        """The hint itself, with `useful` saying whether it told the player anything (call with the lock held)."""
        have = self._discovered
        if kind == "can_make":
            result = hint_can_make(self.book, have, self._rng)
            return {"kind": kind, "result": None if result is None else self._element_json(result),
                    "useful": result is not None}
        if kind == "try":
            pair = hint_try(self.book, have, self._rng)
            return {"kind": kind, "pair": None if pair is None else [self._element_json(e) for e in pair],
                    "useful": pair is not None}
        if kind == "find":
            if not isinstance(target, str) or not target.strip() or len(target) > 200:
                raise GameError(400, "type the name of an element")
            eid = find_element(self.book, target)
            if eid is None:
                raise GameError(404, f"there is no element called {' '.join(target.split())!r}")
            starter = eid in self.book.starters
            recipe = None
            if not starter:
                pair = hint_recipe(self.book, have, self._depth, eid)
                if pair:
                    recipe = [{**self._element_json(e), "known": e in have} for e in pair]
            return {"kind": kind, "target": self._element_json(eid), "have": eid in have,
                    "starter": starter, "recipe": recipe, "useful": recipe is not None}
        raise GameError(400, "unknown hint type")

    def combine(self, token: str, target_tile: object, *, tile: object = None, element: object = None,
                x: object = None, y: object = None) -> dict[str, Any]:
        """Combine the target tile with either another tile the player holds
        (`tile`) or a discovered element (`element`, dragged from the list).

        On success both inputs are replaced by the result tiles at the target's
        position. With no recipe nothing is consumed; a dragged tile is dropped
        at (x, y) and released.
        """
        with self._lock:
            player = self._require_player(token)
            if self.race is not None and not self.race.is_open():
                raise GameError(409, "the race is not running")
            if self._result is not None:
                raise GameError(409, "you have found the target: start a new attempt from the home page to try again")
            if (tile is None) == (element is None):
                raise GameError(400, "give exactly one of 'a' (a tile) or 'element'")
            b = self._get_tile(target_tile)
            if b.held_by is not None:
                raise GameError(409, "someone is moving that tile")

            a: Tile | None = None
            if tile is not None:
                a = self._get_tile(tile)
                if a.id == b.id:
                    raise GameError(400, "cannot combine a tile with itself")
                if a.held_by != player.pid:
                    raise GameError(409, "grab the tile before combining it")
                element_a = a.element
            else:
                element_a = self._require_discovered(element)

            results = self.book.combine(element_a, b.element)
            stat = self._stats.setdefault(player.name.casefold(), {})
            stat["tries"] = stat.get("tries", 0) + 1
            if not results:
                stat["misses"] = stat.get("misses", 0) + 1
                if a is not None:
                    if x is not None and y is not None:
                        a.x, a.y = clamp01(x), clamp01(y)
                    a.held_by = None
                    self._publish("tile_dropped", {"id": a.id, "x": a.x, "y": a.y, "pid": player.pid},
                                  exclude_pid=player.pid)
                return {"a": element_a, "b": b.element, "results": [], "new": [], "tiles": [], "removed": []}

            genie_before = self._genie_status()
            base = len(self._discovered)
            new = self._record(player, results)

            removed = [b.id] + ([a.id] if a is not None else [])
            for tile_id in removed:
                del self._tiles[tile_id]
                self._publish("tile_removed", {"id": tile_id}, exclude_pid=player.pid)
            self._announce(player, new, base)
            self._announce_genie(genie_before)
            created = []
            for i, r in enumerate(results):
                offset = RESULT_OFFSET * i
                result_tile = self._new_tile(r, min(1.0, b.x + offset), min(1.0, b.y + offset))
                created.append(result_tile.to_json())
                self._publish("tile_added", {"tile": result_tile.to_json()}, exclude_pid=player.pid)
            return {
                "a": element_a,
                "b": b.element,
                "results": [self._element_json(r, with_by=True) for r in results],
                "new": new,
                "tiles": created,
                "removed": removed,
            }

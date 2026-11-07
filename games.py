"""Running several games at once.

Each game has a readable id (a slug of its name) that is also its URL: /g/<id>.
At most MAX_GAMES run at a time. A game closes by itself once nobody has been in
it for a while, which frees its slot (it is not saved).
A game can also be ended by a player, optionally saving it; a saved game can be
resumed later (for three days) even when all the slots are taken.

A daily challenge attempt (see daily.py) is also a game, but a private one: it does not appear in the
list of running games, does not use one of the slots, and cannot be saved. So is each racer's world in a race
(see race.py).
"""
from __future__ import annotations

import random
import re
import secrets
import string
import threading
import time
import unicodedata
from typing import Any, Callable

from accounts import key_of
from daily import MAX_ATTEMPTS, TIERS, DailyStore, reference_sizes, target_pools
from pictures import PictureIndex
from race import GOALS, RaceRoom
from recipes import RecipeBook, depths
from saves import SaveStore
from solo import SoloStore
from state import Challenge, GameError, GameState
from visitor import VisitorPolicy

MAX_GAMES = 3
MAX_RACES = 4
IDLE_CLOSE_SECONDS = 600.0
MAX_GAME_NAME_LEN = 30


def clean_game_name(raw: object) -> str:
    """Collapse spaces, drop control characters and cap the length; '' if nothing is left."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise GameError(400, "the game name must be text")
    collapsed = " ".join(raw.split())
    cleaned = "".join(ch for ch in collapsed if unicodedata.category(ch)[0] != "C")
    return cleaned[:MAX_GAME_NAME_LEN].strip()


def random_room_name() -> str:
    """A random 6-character lowercase alphanumeric room name, for games created without one."""
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def slugify(name: str) -> str:
    """URL-safe id from a name: lowercase letters, digits and single hyphens ('' if none)."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


class GameRegistry:
    def __init__(self, book: RecipeBook, *, max_games: int = MAX_GAMES,
                 idle_seconds: float = IDLE_CLOSE_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 saves: SaveStore | None = None,
                 daily: DailyStore | None = None,
                 solo: SoloStore | None = None,
                 pictures: PictureIndex | None = None,
                 visitors: VisitorPolicy | None = None,
                 max_per_account: int = 0) -> None:
        self.book = book
        self.max_per_account = max_per_account  # running public games one account may have started (0 = no limit)
        self.saves = saves or SaveStore(None)
        self.daily = daily or DailyStore(None, book)
        self.solo = solo or SoloStore(None)
        self.pictures = pictures   # None: no element has a picture
        self.visitors = visitors   # when Mr Alchemy visits public games; None: never
        self.max_games = max_games
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._games: dict[str, GameState] = {}
        self._rooms: dict[str, RaceRoom] = {}
        self._race_targets: list[str] | None = None   # elements a target race may use, worked out on first use
        self._lock = threading.Lock()

    def _expire(self) -> None:
        for slug in [s for s, g in self._games.items() if g.is_expired(self.idle_seconds)]:
            del self._games[slug]
        for room in list(self._rooms.values()):
            room.prune()
            if room.expired():
                self._drop_room(room.id)

    def _drop_room(self, rid: str) -> None:
        """Remove a race room and its racers' games (call with the lock held)."""
        self._rooms.pop(rid, None)
        for slug in [s for s, g in self._games.items() if g.race is not None and g.race.id == rid]:
            game = self._games.pop(slug)
            game.close("", False)

    def _check_per_account(self, creator: str) -> None:
        """Refuse a new game from an account that already has one running, so one person cannot hold every slot (lock held)."""
        if self.max_per_account <= 0 or not creator:
            return
        mine = [g for g in self._games.values() if not g.private and key_of(g.creator) == key_of(creator)]
        if len(mine) >= self.max_per_account:
            raise GameError(409, f"you already have a game running ({mine[0].name}): join it, or end it first")

    def create(self, raw_name: object, creator: str = "") -> GameState:
        name = clean_game_name(raw_name)
        with self._lock:
            self._expire()
            if sum(1 for g in self._games.values() if not g.private) >= self.max_games:
                raise GameError(409, f"{self.max_games} games are already running: join one, or wait for one "
                                     f"to end (a game closes {round(self.idle_seconds / 60)} minutes after "
                                     f"the last player leaves)")
            self._check_per_account(creator)
            if not name:
                name = next(n for n in (random_room_name() for _ in range(100)) if slugify(n) not in self._games)
            slug = slugify(name) or secrets.token_hex(3)
            if slug in self._games:
                raise GameError(409, f"a game called {self._games[slug].name!r} is already running")
            game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=name, slug=slug, creator=creator, clock=self._clock)
            self._games[slug] = game
            return game

    # ---- solo mode ----------------------------------------------------------------

    def start_solo(self, username: str) -> GameState:
        """The account's solo game: the one running, or a new one that carries on from what the account found before.

        Solo games are private, do not count towards the limit on running games, and save after every discovery.
        """
        with self._lock:
            self._expire()
            for game in self._games.values():
                if game.solo is not None and key_of(game.creator) == key_of(username):
                    return game
            slug = next(s for s in (f"solo-{secrets.token_hex(4)}" for _ in range(100)) if s not in self._games)
            game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=f"Solo: {username}", slug=slug, creator=username, clock=self._clock,
                             solo=self._solo_saver(username))
            game.restore(self.solo.load(username))
            game.report_solo()   # so the status is right before the first discovery
            self._games[slug] = game
            return game

    def _solo_saver(self, username: str) -> Callable[[dict[str, Any], int, int, bool, set[str]], list[str]]:
        def save(export: dict[str, Any], found: int, total: int, complete: bool, earned: set[str]) -> list[str]:
            return self.solo.record(username, export, found, total, complete, earned)
        return save

    def solo_status(self, username: str) -> dict[str, Any]:
        """{"found", "total", "complete"}: the account's solo progress. A running game is more up to date than the file."""
        return self.solo.status(username)

    def overview(self) -> list[dict[str, Any]]:
        """Every running game for the administrator: its summary, its kind (game, daily or race) and whether it is private."""
        with self._lock:
            self._expire()
            return [{**game.summary(), "kind": "daily" if game.challenge is not None else "race" if game.race is not None
                     else "solo" if game.solo is not None else "game",
                     "private": game.private} for game in self._games.values()]

    def visitor_tick(self) -> list[str]:
        """Let Mr Alchemy visit whichever public games are due (the server calls this every few seconds). Returns their names."""
        with self._lock:
            games = list(self._games.values())
        return [game.name for game in games if game.visitor_tick()]

    def admin_visit(self, slug: str) -> dict[str, Any]:
        """An administrator sends Mr Alchemy to one game now, whatever the schedule says (a public game, with something left to give)."""
        with self._lock:
            game = self._games.get(slug)
        if game is None:
            raise GameError(404, "that game does not exist (it may have ended)")
        announced = game.visit(force=True)
        if announced is None:
            raise GameError(409, "Mr Alchemy only visits public games that still have elements to find")
        return {"gave": [e["label"] for e in announced["elements"]]}

    def admin_end(self, slug: str) -> None:
        """End a running game for everyone, without saving it (the administrator's way; players use end())."""
        with self._lock:
            game = self._games.pop(slug, None)
        if game is None:
            raise GameError(404, "that game does not exist (it may have ended)")
        game.close("an administrator", False)

    def end(self, slug: str, token: object, save: bool) -> dict[str, Any]:
        """End a running game on behalf of one of its players, optionally saving it first."""
        with self._lock:
            game = self._games.get(slug)
            if game is None:
                raise GameError(404, "that game does not exist (it may have ended)")
            player = game.authorize(token)
            if save and game.challenge is not None:
                raise GameError(400, "a daily challenge attempt cannot be saved")
            if game.solo is not None:
                save = False   # a solo game keeps its progress by itself, after every discovery
            if save and game.creator and key_of(player.name) != key_of(game.creator):
                raise GameError(403, f"only {game.creator}, who started this game, can save it")
            if save:
                self.saves.save(game)  # raises (game stays open) if it cannot be written
            del self._games[slug]
        game.close(player.name, save)
        return {"saved": save}

    # ---- the daily challenge ----------------------------------------------------

    def _challenge(self, date: str, tier: str, target: str, reference: int | None) -> Challenge:
        return Challenge(date, target, reference, lambda name, score: self.daily.submit(date, tier, name, score), tier)

    def _attempts(self, username: str) -> list[GameState]:
        """This account's daily challenge attempts that are still running (call with the lock held)."""
        return [g for g in self._games.values()
                if g.challenge is not None and key_of(g.creator) == key_of(username)]

    def _element(self, eid: str) -> dict[str, str]:
        element = self.book.elements.get(eid)
        return {"id": eid, "label": element.label if element else eid, "emoji": element.emoji if element else ""}

    def _route_json(self, steps: list[tuple[str, str, tuple[str, ...]]]) -> list[dict[str, Any]]:
        return [{"a": self._element(a), "b": self._element(b), "makes": [self._element(r) for r in results]}
                for a, b, results in steps]

    def _today(self) -> dict[str, Any]:
        try:
            return self.daily.today()
        except ValueError as exc:  # a damaged daily.json, or no element that could be a target
            raise GameError(500, str(exc)) from exc

    @staticmethod
    def _tier(raw: object) -> str:
        if raw not in TIERS:
            raise GameError(400, "the difficulty must be easy, medium or hard")
        return str(raw)

    def daily_status(self, username: str) -> dict[str, Any]:
        """Today's challenges for the home page: per tier the target, reference, board, the caller's best,
        attempts and attempt in progress (and the reference route once the caller can no longer play that tier),
        plus the caller's streak and the weekly board."""
        info = self._today()
        day = info["date"]
        with self._lock:
            self._expire()
            mine = self._attempts(username)
        tiers = []
        for tier in TIERS:
            running = next((g for g in mine if g.challenge.tier == tier and g.challenge.date == day
                            and not g.finished), None)
            used = self.daily.attempts(day, tier, username)
            tiers.append({
                "tier": tier,
                "target": self._element(info["tiers"][tier]["target"]),
                "reference": info["tiers"][tier]["reference"],
                "best": self.daily.best(day, tier, username),
                "board": self.daily.board(day, tier),
                "attempts_used": used,
                "running": f"/g/{running.slug}" if running else None,
                "route": self._route_json(self.daily.route(day, tier)) if used >= MAX_ATTEMPTS and running is None
                else None,
            })
        return {"date": day, "attempts_max": MAX_ATTEMPTS, "streak": self.daily.streak(username),
                "tiers": tiers, "week": self.daily.week()}

    def daily_history(self, username: str) -> dict[str, Any]:
        """The latest days with each tier's target, the caller's best and the winner; routes of finished days."""
        self._today()  # also reports a damaged file
        days = self.daily.history(username)
        for day in days:
            for entry in day["tiers"].values():
                entry["target"] = self._element(entry["target"])
                entry["route"] = self._route_json(entry["route"])
        return {"days": days}

    def start_daily(self, username: str, tier: object, fresh: bool = False) -> GameState:
        """Today's attempt in `tier` for this account: the one in progress, or a new one (always new if `fresh`).

        Carrying on with an attempt in progress is free. A new one uses up one of the account's MAX_ATTEMPTS
        for that tier today, and is refused (409) when none are left.
        """
        tier = self._tier(tier)
        info = self._today()
        day = info["date"]
        with self._lock:
            self._expire()
            same = [g for g in self._attempts(username) if g.challenge.tier == tier]
            for game in same:
                if not fresh and game.challenge.date == day and not game.finished:
                    return game
            if self.daily.attempts(day, tier, username) >= MAX_ATTEMPTS:
                raise GameError(409, f"you have used all {MAX_ATTEMPTS} of today's {tier} attempts: "
                                     f"come back tomorrow")
            for game in same:
                del self._games[game.slug]
            self.daily.use_attempt(day, tier, username)
            slug = next(s for s in (f"daily-{secrets.token_hex(4)}" for _ in range(100)) if s not in self._games)
            data = info["tiers"][tier]
            game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=f"Daily challenge: {tier} {day}", slug=slug, creator=username,
                             clock=self._clock, challenge=self._challenge(day, tier, data["target"], data["reference"]))
            self._games[slug] = game
        for old in same:  # tell any open page of the replaced attempt that it has ended
            old.close(username, False)
        return game

    def reveal_daily(self, username: str, tier: object) -> dict[str, Any]:
        """Give up the rest of today's attempts in `tier` (closing one in progress) and show its reference route."""
        tier = self._tier(tier)
        info = self._today()
        day = info["date"]
        with self._lock:
            self._expire()
            running = [g for g in self._attempts(username)
                       if g.challenge.tier == tier and g.challenge.date == day and not g.finished]
            for game in running:
                del self._games[game.slug]
        self.daily.forfeit(day, tier, username)
        for game in running:
            game.close(username, False)
        return {"tier": tier, "route": self._route_json(self.daily.route(day, tier))}

    def find_saved(self, username: str) -> list[dict[str, Any]]:
        """Saved games this account played in that are not running right now."""
        with self._lock:
            self._expire()
            return [m for m in self.saves.for_player(username) if m["id"] not in self._games]

    def resume(self, slug: object, username: str) -> GameState:
        """Bring a saved game back for an account that played in it. This ignores the limit on running games."""
        if not isinstance(slug, str):
            raise GameError(400, "missing game id")
        with self._lock:
            self._expire()
            if slug in self._games:
                raise GameError(409, "that game is already running")
            data = self.saves.load(slug)
            if data is None:
                raise GameError(404, "there is no saved game like that (saves are kept for 3 days)")
            if not self.saves.has_player(data, username):
                raise GameError(403, "only players who were in that game can resume it")
            name = clean_game_name(data.get("name")) or slug
            creator = data.get("creator") if isinstance(data.get("creator"), str) else ""
            self._check_per_account(creator or username)
            # Saves from before games had a creator belong to whoever brings them back first.
            game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=name, slug=slug, creator=creator or username, clock=self._clock)
            game.restore(data)
            self._games[slug] = game
            return game

    def live_export(self) -> list[dict[str, Any]]:
        """Every running game with its workspace and players, to carry across a server restart."""
        with self._lock:
            self._expire()   # races are not carried across a restart: they end with it
            return [game.live_export() for game in self._games.values() if game.race is None]

    def live_restore(self, items: object) -> int:
        """Bring back games from live_export(); returns how many came back. Bad entries are skipped."""
        restored = 0
        if not isinstance(items, list):
            return 0
        with self._lock:
            for data in items:
                if not isinstance(data, dict):
                    continue
                slug = data.get("slug")
                name = clean_game_name(data.get("name")) or str(slug)
                if not isinstance(slug, str) or not slugify(slug) == slug or slug in self._games:
                    continue
                creator = data.get("creator") if isinstance(data.get("creator"), str) else ""
                challenge = None
                saved = data.get("challenge")
                if isinstance(saved, dict):
                    day, target, reference = saved.get("date"), saved.get("target"), saved.get("reference")
                    tier = saved.get("tier") if saved.get("tier") in TIERS else "medium"
                    if not isinstance(day, str) or target not in self.book.elements:
                        continue
                    challenge = self._challenge(day, tier, target, reference if isinstance(reference, int)
                                                and not isinstance(reference, bool) else None)
                solo = self._solo_saver(creator) if data.get("solo") is True and creator else None
                game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=name, slug=slug, creator=creator, clock=self._clock,
                                 challenge=challenge, solo=solo)
                game.live_restore(data)
                self._games[slug] = game
                restored += 1
        return restored

    def get(self, slug: str) -> GameState | None:
        with self._lock:
            self._expire()
            return self._games.get(slug)

    def list(self, username: str = "") -> list[dict[str, Any]]:
        """Every running game; with a username, each is marked `recent` if that account just left it.

        A daily challenge attempt or a racer's world is listed only to its owner, marked `"private": true`, so that its own
        page can look it up. The home page leaves such entries out.
        """
        with self._lock:
            self._expire()
            return [{**game.summary(), "recent": bool(username) and game.recently_left(username),
                     **({"private": True} if game.private else {})}
                    for game in self._games.values()
                    if not game.private or (username and key_of(game.creator) == key_of(username))]

    # ---- races ----------------------------------------------------------------------

    def _race_target(self) -> str:
        """A random element of medium difficulty (computed once: the search takes about half a second)."""
        if self._race_targets is None:
            depth = depths(self.book)
            self._race_targets = target_pools(self.book, depth, reference_sizes(self.book, depth))["medium"]
        if not self._race_targets:
            raise GameError(500, "there is no element that could be a race target")
        return random.choice(self._race_targets)

    def _room(self, rid: object) -> RaceRoom:
        room = self._rooms.get(rid) if isinstance(rid, str) else None
        if room is None:
            raise GameError(404, "that race does not exist (it may have ended)")
        return room

    def _room_of(self, username: str) -> RaceRoom | None:
        """The race this account is in and that has not finished (call with the lock held)."""
        for room in self._rooms.values():
            if room.state != "finished" and room.has(username):
                return room
        return None

    def _race_json(self, room: RaceRoom, username: str) -> dict[str, Any]:
        status = room.status()
        status["target"] = self._element(room._target_id()) if room.goal == "target" else None
        status["you"] = room.has(username)
        status["game"] = f"/g/{room.slug_of(username)}" if status["you"] and room.slug_of(username) else None
        status["is_host"] = key_of(status["host"]) == key_of(username)
        return status

    def create_race(self, username: str, goal: object, count: int | None = None) -> dict[str, Any]:
        if goal not in GOALS:
            raise GameError(400, "the goal must be 'target' or 'count'")
        target = self._race_target() if goal == "target" else None
        with self._lock:
            self._expire()
            current = self._room_of(username)
            if current is not None:
                raise GameError(409, "you are already in a race: leave it first")
            if sum(1 for r in self._rooms.values() if r.state != "finished") >= MAX_RACES:
                raise GameError(409, f"{MAX_RACES} races are already open or running: join one, or wait")
            rid = next(r for r in (f"r{secrets.token_hex(3)}" for _ in range(100)) if r not in self._rooms)
            kwargs: dict[str, Any] = {"target": target, "clock": self._clock}
            if count is not None:
                kwargs["count"] = count
            room = RaceRoom(rid, username, str(goal), **kwargs)
            self._rooms[rid] = room
            return self._race_json(room, username)

    def list_races(self, username: str) -> dict[str, Any]:
        """Races waiting for racers, and the one this account is in."""
        with self._lock:
            self._expire()
            mine = self._room_of(username)
            lobbies = []
            for room in self._rooms.values():
                status = room.status()
                if status["state"] != "lobby":
                    continue
                lobbies.append({"id": room.id, "host": status["host"], "goal": room.goal,
                                "goal_count": room.goal_count, "racers": len(status["racers"]),
                                "max_racers": status["max_racers"],
                                "target": self._element(room._target_id()) if room.goal == "target" else None})
            return {"races": lobbies, "mine": mine.id if mine else None, "max_races": MAX_RACES}

    def race_status(self, rid: object, username: str) -> dict[str, Any]:
        with self._lock:
            self._expire()
            room = self._room(rid)
            room.touch(username)
            return self._race_json(room, username)

    def join_race(self, rid: object, username: str) -> dict[str, Any]:
        with self._lock:
            self._expire()
            room = self._room(rid)
            if not room.has(username):
                other = self._room_of(username)
                if other is not None:
                    raise GameError(409, "you are already in a race: leave it first")
                room.join(username)
            return self._race_json(room, username)

    def leave_race(self, rid: object, username: str) -> dict[str, Any]:
        with self._lock:
            self._expire()
            room = self._room(rid)
            games = [g for g in self._games.values() if g.race is not None and g.race.id == room.id
                     and key_of(g.creator) == key_of(username)]
            if room.leave(username):
                self._drop_room(room.id)
            else:
                for game in games:
                    self._games.pop(game.slug, None)
        for game in games:
            game.close(username, False)
        return {"left": True}

    def start_race(self, rid: object, username: str) -> dict[str, Any]:
        """The host starts the countdown; every racer gets a private world with only the starting elements."""
        with self._lock:
            self._expire()
            room = self._room(rid)
            names = room.start(username)
            for name in names:
                slug = next(s for s in (f"race-{secrets.token_hex(4)}" for _ in range(100)) if s not in self._games)
                game = GameState(self.book, pictures=self.pictures, visitors=self.visitors, name=f"Race {room.id}", slug=slug, creator=name, clock=self._clock,
                                 race=room.hook(name))
                self._games[slug] = game
                room.attach(name, slug, len(game.discovered()))
            return self._race_json(room, username)

"""Running several games at once.

Each game has a readable id (a slug of its name) that is also its URL: /g/<id>.
At most MAX_GAMES run at a time. A game closes by itself once nobody has been in
it for a while, which frees its slot (it is not saved).
A game can also be ended by a player, optionally saving it; a saved game can be
resumed later (for three days) even when all the slots are taken.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable

from accounts import key_of
from recipes import RecipeBook
from saves import SaveStore
from state import GameError, GameState

MAX_GAMES = 3
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


def slugify(name: str) -> str:
    """URL-safe id from a name: lowercase letters, digits and single hyphens ('' if none)."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


class GameRegistry:
    def __init__(self, book: RecipeBook, *, max_games: int = MAX_GAMES,
                 idle_seconds: float = IDLE_CLOSE_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 saves: SaveStore | None = None) -> None:
        self.book = book
        self.saves = saves or SaveStore(None)
        self.max_games = max_games
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._games: dict[str, GameState] = {}
        self._lock = threading.Lock()

    def _expire(self) -> None:
        for slug in [s for s, g in self._games.items() if g.is_expired(self.idle_seconds)]:
            del self._games[slug]

    def create(self, raw_name: object, creator: str = "") -> GameState:
        name = clean_game_name(raw_name)
        with self._lock:
            self._expire()
            if len(self._games) >= self.max_games:
                raise GameError(409, f"{self.max_games} games are already running: join one, or wait for one "
                                     f"to end (a game closes {round(self.idle_seconds / 60)} minutes after "
                                     f"the last player leaves)")
            if not name:
                number = next(n for n in range(1, self.max_games + 2) if f"game-{n}" not in self._games)
                name = f"Game {number}"
            slug = slugify(name) or secrets.token_hex(3)
            if slug in self._games:
                raise GameError(409, f"a game called {self._games[slug].name!r} is already running")
            game = GameState(self.book, name=name, slug=slug, creator=creator, clock=self._clock)
            self._games[slug] = game
            return game

    def end(self, slug: str, token: object, save: bool) -> dict[str, Any]:
        """End a running game on behalf of one of its players, optionally saving it first."""
        with self._lock:
            game = self._games.get(slug)
            if game is None:
                raise GameError(404, "that game does not exist (it may have ended)")
            player = game.authorize(token)
            if save and game.creator and key_of(player.name) != key_of(game.creator):
                raise GameError(403, f"only {game.creator}, who started this game, can save it")
            if save:
                self.saves.save(game)  # raises (game stays open) if it cannot be written
            del self._games[slug]
        game.close(player.name, save)
        return {"saved": save}

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
            # Saves from before games had a creator belong to whoever brings them back first.
            game = GameState(self.book, name=name, slug=slug, creator=creator or username, clock=self._clock)
            game.restore(data)
            self._games[slug] = game
            return game

    def live_export(self) -> list[dict[str, Any]]:
        """Every running game with its workspace and players, to carry across a server restart."""
        with self._lock:
            self._expire()
            return [game.live_export() for game in self._games.values()]

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
                game = GameState(self.book, name=name, slug=slug, creator=creator, clock=self._clock)
                game.live_restore(data)
                self._games[slug] = game
                restored += 1
        return restored

    def get(self, slug: str) -> GameState | None:
        with self._lock:
            self._expire()
            return self._games.get(slug)

    def list(self, username: str = "") -> list[dict[str, Any]]:
        """Every running game; with a username, each is marked `recent` if that account just left it."""
        with self._lock:
            self._expire()
            return [{**game.summary(), "recent": bool(username) and game.recently_left(username)}
                    for game in self._games.values()]

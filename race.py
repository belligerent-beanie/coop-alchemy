"""Race rooms: several players each work in their own private world and the first to reach the goal wins.

Every racer gets a separate game that starts with only the starting elements (see GameState's `race`), so nobody
can help or hinder anyone else and "first to reach 100 elements" means something. A room only keeps the
standings; it never calls into a game or the registry (the game calls `report`), so it cannot cause a deadlock.

Goals:  "target" - be first to discover one named element;  "count" - be first to have `count` elements
(the starting ones included).

A room goes lobby -> countdown -> running -> finished. The state is worked out from the clock when asked.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from state import GameError, Race, clean_name

GOALS = ("target", "count")
RACE_COUNT = 100              # the goal of a "count" race
COUNTDOWN_SECONDS = 3.0
MIN_RACERS = 2
MAX_RACERS = 6
LOBBY_SEEN_SECONDS = 30.0     # a lobby member who has not checked in for this long is dropped
RUNNING_MAX_SECONDS = 3600.0  # a race is closed after an hour
FINISHED_KEEP_SECONDS = 600.0 # how long the results stay available


@dataclass
class Racer:
    name: str
    count: int = 0            # elements discovered so far (starters included)
    slug: str = ""            # the racer's private game, once the race has started
    left: bool = False
    seen: float = 0.0         # last time this racer's page asked for the standings
    reached_at: float | None = None


class RaceRoom:
    def __init__(self, rid: str, host: str, goal: str, *, target: str | None = None, count: int = RACE_COUNT,
                 clock: Callable[[], float] = time.monotonic, countdown: float = COUNTDOWN_SECONDS) -> None:
        if goal not in GOALS:
            raise GameError(400, "the goal must be 'target' or 'count'")
        if goal == "target" and not target:
            raise GameError(400, "a target race needs a target")
        self.id = rid
        self.goal = goal
        self.target = target if goal == "target" else None
        self.goal_count = count
        self.host = clean_name(host)
        self._clock = clock
        self._countdown = countdown
        self._lock = threading.RLock()
        self._racers: dict[str, Racer] = {}
        self._starts_at: float | None = None
        self._winner: str | None = None
        self._finished_at: float | None = None
        self._active = clock()   # last time anything happened, for expiry
        self.closed = False
        self._add(self.host)

    # ---- helpers (call with the lock held) -------------------------------------

    def _add(self, name: str) -> Racer:
        racer = Racer(name, seen=self._clock())
        self._racers[name.casefold()] = racer
        self._active = self._clock()
        return racer

    def _state(self) -> str:
        if self._winner is not None or self._finished_at is not None:
            return "finished"
        if self._starts_at is None:
            return "lobby"
        return "countdown" if self._clock() < self._starts_at else "running"

    def _present(self) -> list[Racer]:
        return [r for r in self._racers.values() if not r.left]

    def _finish(self, winner: str | None) -> None:
        self._winner = winner
        self._finished_at = self._active = self._clock()

    def _racer(self, name: str) -> Racer:
        racer = self._racers.get(clean_name(name).casefold())
        if racer is None or racer.left:
            raise GameError(403, "you are not in this race")
        return racer

    # ---- public API ---------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _target_id(self) -> str:
        return self.target or ""

    def has(self, name: str) -> bool:
        with self._lock:
            racer = self._racers.get(clean_name(name).casefold())
            return racer is not None and not racer.left

    def slug_of(self, name: str) -> str:
        with self._lock:
            racer = self._racers.get(clean_name(name).casefold())
            return racer.slug if racer else ""

    def join(self, name: str) -> None:
        name = clean_name(name)
        with self._lock:
            if self._state() != "lobby":
                raise GameError(409, "that race has already started")
            if self.has(name):
                return
            if len(self._present()) >= MAX_RACERS:
                raise GameError(409, f"the race is full ({MAX_RACERS} racers at most)")
            self._add(name)

    def leave(self, name: str) -> bool:
        """Leave the race. Returns True when the room should be removed (the host left the lobby, or nobody is left)."""
        with self._lock:
            racer = self._racer(name)
            state = self._state()
            self._active = self._clock()
            if state == "lobby":
                if racer.name.casefold() == self.host.casefold():
                    self.closed = True
                    return True
                del self._racers[racer.name.casefold()]
                return False
            racer.left = True
            if state != "finished":
                remaining = self._present()
                if not remaining:
                    self.closed = True
                    return True
                if len(remaining) == 1:   # everyone else gave up: the last racer wins
                    self._finish(remaining[0].name)
            return False

    def start(self, name: str) -> list[str]:
        """The host starts the countdown; returns the racers' names (the registry then makes their games)."""
        with self._lock:
            racer = self._racer(name)
            if racer.name.casefold() != self.host.casefold():
                raise GameError(403, f"only {self.host}, who made this race, can start it")
            if self._state() != "lobby":
                raise GameError(409, "that race has already started")
            if len(self._present()) < MIN_RACERS:
                raise GameError(409, f"a race needs at least {MIN_RACERS} racers")
            self._starts_at = self._clock() + self._countdown
            self._active = self._clock()
            return [r.name for r in self._present()]

    def attach(self, name: str, slug: str, count: int) -> None:
        with self._lock:
            racer = self._racer(name)
            racer.slug, racer.count = slug, count

    def hook(self, name: str) -> Race:
        """What the racer's game needs to talk to this room."""
        return Race(self.id, self.goal, self.target, self.goal_count,
                    lambda count, reached: self._report(name, count, reached), self._is_open)

    def _is_open(self) -> bool:
        with self._lock:
            return self._state() == "running"

    def _report(self, name: str, count: int, reached: bool) -> bool:
        """A racer's progress. Returns True if this racer has just won."""
        with self._lock:
            racer = self._racers.get(clean_name(name).casefold())
            if racer is None or racer.left or self._state() != "running":
                return False
            racer.count = count
            self._active = self._clock()
            if reached and self._winner is None:
                racer.reached_at = self._clock()
                self._finish(racer.name)
                return True
            return False

    def touch(self, name: str) -> None:
        with self._lock:
            racer = self._racers.get(clean_name(name).casefold())
            if racer is not None and not racer.left:
                racer.seen = self._clock()

    def prune(self) -> None:
        """Drop lobby members who stopped checking in (the host going quiet closes the lobby)."""
        with self._lock:
            if self._state() != "lobby":
                return
            now = self._clock()
            for key, racer in list(self._racers.items()):
                if now - racer.seen >= LOBBY_SEEN_SECONDS:
                    if racer.name.casefold() == self.host.casefold():
                        self.closed = True
                        return
                    del self._racers[key]

    def expired(self) -> bool:
        with self._lock:
            if self.closed:
                return True
            state = self._state()
            age = self._clock() - self._active
            if state == "finished":
                return age >= FINISHED_KEEP_SECONDS
            if state == "running":
                return age >= RUNNING_MAX_SECONDS
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            now = self._clock()
            racers = sorted(self._present(), key=lambda r: (-r.count, r.name.casefold()))
            return {
                "id": self.id, "state": state, "goal": self.goal, "goal_count": self.goal_count,
                "host": self.host, "winner": self._winner, "min_racers": MIN_RACERS, "max_racers": MAX_RACERS,
                "starts_in": max(0.0, round(self._starts_at - now, 2)) if state == "countdown" and self._starts_at else 0.0,
                "racers": [{"name": r.name, "count": r.count, "winner": r.name == self._winner} for r in racers],
            }

"""Companions: computer-controlled players that keep a lone player company in a public game.

A companion is a seated player like any other: it plays through the same methods a person's browser calls (add a tile, grab it, move it,
combine, move the cursor, send an emote), so every rule of the game applies to it. What makes it feel like a person is how it behaves:

  Pointer      a human-like cursor: movements take as long as Fitts's law says, follow a gentle curve with a bell-shaped speed, sometimes
               overshoot and correct, tremble a little, and drift when idle. Pure maths with a seeded random source, so it is testable.
  Bot          one companion's behaviour, written as a script that waits between steps. Level 0 only keeps a presence (wanders, now and then
               puts a tile down). Level 1 also plays like a beginner: tries pairs of elements, mostly guesses, sometimes gets one right.
  BotRunner    decides when a companion joins (a public game with exactly one person in it, after a random wait) and when it leaves
               (a person needs the seat, the room empties, or it has stayed long enough), and steps them all about 20 times a second.

Companions are never counted as people (Mr Alchemy, a game's expiry), never saved, and leave their seat to anyone who needs it.
Names come from cast.py. Everything is switched off unless the server starts with --bots on.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Generator

from cast import CAST
from hints import new_combinations
from state import MAX_PLAYERS, GameError, GameState, Player
from visitor import MR_ALCHEMY

log = logging.getLogger("alchemy.bots")

ASPECT = 1.6           # a board is about this much wider than tall, so distances are measured in board heights
TICK_SECONDS = 0.05    # how often companions are stepped: 20 times a second, like a person's pointer events


# ---- the cursor --------------------------------------------------------------------------------------------------------

def min_jerk(tau: float) -> float:
    """How far along a movement a hand is, from 0 to 1, as time goes from 0 to 1: slow start, fast middle, slow finish."""
    tau = min(1.0, max(0.0, tau))
    return tau ** 3 * (10 - 15 * tau + 6 * tau * tau)


def fitts_seconds(distance: float, width: float = 0.07) -> float:
    """How long a person takes to point at a target `width` wide that is `distance` away (both in board heights)."""
    return 0.16 + 0.12 * math.log2(distance / width + 1)


@dataclass
class Personality:
    """What makes one companion differ from another."""
    pace: float = 1.0        # above 1 is slower
    accuracy: float = 0.8    # the chance a movement lands first time; otherwise it overshoots and corrects
    patience: float = 1.0    # scales how long it thinks between things
    chatty: float = 0.25     # the chance it reacts to something with an emote

    @staticmethod
    def random(rng: random.Random) -> "Personality":
        return Personality(pace=rng.uniform(0.8, 1.45), accuracy=rng.uniform(0.6, 0.95), patience=rng.uniform(0.7, 1.5),
                           chatty=rng.uniform(0.05, 0.45))


@dataclass
class _Move:
    t0: float
    t1: float
    x0: float
    y0: float
    x1: float
    y1: float
    cx: float   # the control point of the curve the hand follows
    cy: float

    def at(self, t: float) -> tuple[float, float]:
        s = min_jerk((t - self.t0) / (self.t1 - self.t0)) if self.t1 > self.t0 else 1.0
        u = 1 - s
        return (u * u * self.x0 + 2 * u * s * self.cx + s * s * self.x1,
                u * u * self.y0 + 2 * u * s * self.cy + s * s * self.y1)


class Pointer:
    """A cursor on a board whose corners are (0, 0) and (1, 1)."""

    def __init__(self, rng: random.Random, personality: Personality, x: float = 0.5, y: float = 0.5, now: float = 0.0) -> None:
        self._rng = rng
        self.personality = personality
        self._moves: list[_Move] = []
        self._x, self._y = x, y   # where the last movement ends
        self._free = now          # when it ends
        self._phase = [rng.uniform(0, 2 * math.pi) for _ in range(4)]
        self._freq = [rng.uniform(6.5, 9.0), rng.uniform(10.0, 13.0), rng.uniform(0.15, 0.3), rng.uniform(0.35, 0.6)]

    def teleport(self, x: float, y: float, now: float) -> None:
        """Appear somewhere (the cursor entering the board from outside it)."""
        self._moves.clear()
        self._x, self._y, self._free = x, y, now

    def busy(self, now: float) -> bool:
        return now < self._free

    def _base(self, t: float) -> tuple[float, float]:
        for move in reversed(self._moves):
            if t >= move.t0:
                return move.at(min(t, move.t1))
        return self._x if not self._moves else self._moves[0].x0, self._y if not self._moves else self._moves[0].y0

    def position_at(self, t: float) -> tuple[float, float]:
        """Where the cursor is at time `t`: the planned movement, plus a hand's tremor, plus a slow drift when it is not moving."""
        x, y = self._base(t)
        moving = self.busy(t)
        tremor = 0.0007 if moving else 0.0003
        a = math.sin(2 * math.pi * self._freq[0] * t + self._phase[0]) + 0.6 * math.sin(2 * math.pi * self._freq[1] * t + self._phase[1])
        b = math.sin(2 * math.pi * self._freq[1] * t + self._phase[2]) + 0.6 * math.sin(2 * math.pi * self._freq[0] * t + self._phase[3])
        drift = 0.0 if moving else 0.003
        dx = tremor * a + drift * math.sin(2 * math.pi * self._freq[2] * t + self._phase[2])
        dy = tremor * b + drift * math.sin(2 * math.pi * self._freq[3] * t + self._phase[3])
        return min(1.0, max(0.0, x + dx / ASPECT)), min(1.0, max(0.0, y + dy))

    def move_to(self, tx: float, ty: float, now: float, react: bool = True) -> float:
        """Plan a movement to (tx, ty) starting now (after a reaction time if the hand was resting). Returns when it will have arrived."""
        rng, p = self._rng, self.personality
        start = max(now, self._free)
        if react and (not self._moves or start - self._free > 1.0):
            start += rng.uniform(0.12, 0.32)
        sx, sy = self._base(start)
        dx, dy = (tx - sx) * ASPECT, ty - sy
        distance = math.hypot(dx, dy)
        if distance < 1e-6:
            self._free = start
            return start
        ux, uy = dx / distance, dy / distance
        duration = fitts_seconds(distance) * p.pace * rng.uniform(0.85, 1.2)
        end_x, end_y = tx, ty
        correction = None
        if distance > 0.12 and rng.random() < (1 - p.accuracy) * 0.8:   # overshoot, then a short correction back
            over = distance * rng.uniform(0.03, 0.07)
            end_x = min(1.0, max(0.0, tx + ux * over / ASPECT))
            end_y = min(1.0, max(0.0, ty + uy * over))
            duration *= 0.9
            correction = rng.uniform(0.12, 0.26)
        bend = distance * rng.uniform(0.03, 0.10) * rng.choice((-1, 1))
        mid_x, mid_y = (sx + end_x) / 2 + (-uy * bend) / ASPECT, (sy + end_y) / 2 + ux * bend
        self._moves.append(_Move(start, start + duration, sx, sy, end_x, end_y, mid_x, mid_y))
        t = start + duration
        if correction is not None:
            self._moves.append(_Move(t, t + correction, end_x, end_y, tx, ty, (end_x + tx) / 2, (end_y + ty) / 2))
            t += correction
        self._x, self._y, self._free = tx, ty, t
        del self._moves[:-4]
        return t


# ---- one companion ------------------------------------------------------------------------------------------------------

Script = Generator[float, float, Any]


class Bot:
    """A companion in one game. Call step(now) about 20 times a second."""

    def __init__(self, game: GameState, player: Player, rng: random.Random, personality: Personality, level: int, now: float,
                 stay_seconds: float) -> None:
        self.game, self.player, self.token = game, player, player.token
        self.rng, self.p, self.level = rng, personality, level
        self.pointer = Pointer(rng, personality, now=now)
        self.visible = False
        self.dragging = False
        self.tiles: list[str] = []                  # the tiles it put down
        self.tried: set[frozenset[str]] = set()     # pairs it has already tried
        self.joined_at = now
        self.leave_at = now + stay_seconds
        self.leaving = False
        self.manual = False                         # put there by an administrator: the --bots setting does not send it away
        self.done = False
        self.errors = 0
        self.empty_since: float | None = None       # since when nobody else was in the room
        self.now = now
        self._sent: tuple[float, float] | None = None
        self._seen = len(game.discovered())
        self._reactions: list[tuple[float, str]] = []
        self._gen = self._life()
        self._wake = next(self._gen)

    @property
    def name(self) -> str:
        return self.player.name

    # -- stepping

    def step(self, now: float) -> None:
        if self.done:
            return
        self.now = now
        try:
            self._send_cursor(now)
            self._watch(now)
            if now >= self._wake:
                self._wake = self._gen.send(now)
        except StopIteration:
            self.done = True
        except GameError:   # the room changed under it (the seat is gone, the game ended): it simply stops
            self.errors += 1
            if self.game.ended or self.token not in {p.token for p in self.game.virtual_players()}:
                self.done = True

    def _send_cursor(self, now: float) -> None:
        if self.dragging:   # while a tile is held the pointer position travels with the tile moves, as in a browser
            return
        if self.visible:
            x, y = self.pointer.position_at(now)
            if self._sent is None or abs(x - self._sent[0]) + abs(y - self._sent[1]) > 0.0015:
                self.game.set_cursor(self.token, x, y)
                self._sent = (x, y)
        elif self._sent is not None:
            self.game.set_cursor(self.token, None, None)
            self._sent = None

    def _watch(self, now: float) -> None:
        """React to what other players find: now and then a wow or a cheer, a moment later, like a person who noticed."""
        found = self.game.discovered()
        if len(found) > self._seen:
            for eid, by in list(found.items())[self._seen:]:
                if by and by not in (self.name, MR_ALCHEMY) and not self._reactions and self.rng.random() < self.p.chatty * 0.7:
                    self._reactions.append((now + self.rng.uniform(0.9, 2.6), self.rng.choice(("wow", "cheer", "thanks"))))
            self._seen = len(found)
        for due, emote in list(self._reactions):
            if now >= due:
                self._reactions.remove((due, emote))
                self._emote(emote)

    def _emote(self, emote: str) -> None:
        try:
            self.game.emote(self.token, emote)
        except GameError:
            pass   # too soon after the last one, or not a shared game: nothing to say

    # -- the script

    def _wait(self, seconds: float) -> Script:
        self.now = yield self.now + seconds

    def _wait_until(self, when: float) -> Script:
        if when > self.now:
            self.now = yield when

    def _life(self) -> Script:
        yield from self._wait(self.rng.uniform(1.5, 4.0))
        if self.game.people_count() and self.rng.random() < 0.15 + self.p.chatty:
            self._emote("wave")
        yield from self._enter_board()
        while True:
            try:
                if self.leaving or self.now >= self.leave_at:
                    yield from self._leave()
                    return
                yield from (self._presence_round() if self.level == 0 else self._play_round())
            except GameError:
                self.errors += 1
                self.dragging = False
                self._release_held()
                yield from self._wait(self.rng.uniform(1.0, 3.0))

    def _release_held(self) -> None:
        """After something went wrong mid-drag, put down whatever it was holding so nobody finds a tile stuck to a cursor."""
        for tile in self.game.tiles():
            if tile["held_by"] == self.player.pid:
                try:
                    self.game.drop_tile(self.token, tile["id"], tile["x"], tile["y"])
                except GameError:
                    pass

    def _leave(self) -> Script:
        yield from self._exit_board()
        yield from self._wait(self.rng.uniform(0.3, 1.0))
        self.game.remove_virtual(self.token)

    def _enter_board(self) -> Script:
        self.pointer.teleport(0.995, self.rng.uniform(0.2, 0.8), self.now)
        self.visible = True
        yield from self._wander()

    def _exit_board(self) -> Script:
        if self.visible:
            yield from self._wait_until(self.pointer.move_to(1.0, min(0.95, max(0.05, self.pointer.position_at(self.now)[1])), self.now))
            self.visible = False

    def _wander(self) -> Script:
        yield from self._wait_until(self.pointer.move_to(self.rng.uniform(0.1, 0.9), self.rng.uniform(0.12, 0.88), self.now))
        yield from self._wait(self.rng.uniform(0.3, 2.0))

    def _think(self, low: float, high: float) -> Script:
        yield from self._wait(self.rng.uniform(low, high) * self.p.patience)
        if self.visible and self.rng.random() < 0.5:   # a hand rarely sits perfectly still
            x, y = self.pointer.position_at(self.now)
            self.pointer.move_to(min(0.95, max(0.05, x + self.rng.uniform(-0.08, 0.08))), min(0.9, max(0.1, y + self.rng.uniform(-0.08, 0.08))),
                                 self.now)

    # -- level 0: a presence

    def _presence_round(self) -> Script:
        yield from self._wander()
        yield from self._think(6, 22)
        if self.rng.random() < 0.25:
            tile = yield from self._place(self.rng.choice(sorted(self.game.discovered())))
            yield from self._wait(self.rng.uniform(0.5, 1.5))
            yield from self._pick_up(tile)
            yield from self._carry(tile, self.rng.uniform(0.15, 0.85), self.rng.uniform(0.2, 0.8))
            self.game.drop_tile(self.token, tile, *self.pointer.position_at(self.now))
            self.dragging = False
            yield from self._tidy_if_crowded()

    # -- level 1: plays like a beginner

    def _play_round(self) -> Script:
        pair = self._choose_pair()
        if pair is None:
            yield from self._wander()
            yield from self._think(8, 20)
            return
        yield from self._think(6, 18)
        yield from self._tidy_if_crowded()
        self.tried.add(frozenset(pair))
        a = yield from self._place(pair[0])
        b = yield from self._place(pair[1])
        yield from self._try_combine(a, b)

    def _choose_pair(self) -> tuple[str, str] | None:
        """Mostly a guess; now and then (more often for a sharper companion) a pair that really makes something new."""
        have = self.game.discovered()
        if self.rng.random() < 0.2 + 0.3 * self.p.accuracy:
            known = [(a, b) for a, b, _ in new_combinations(self.game.book, have) if frozenset((a, b)) not in self.tried]
            if known:
                return self.rng.choice(sorted(known))
        ids = sorted(have)
        for _ in range(30):
            a, b = self.rng.choice(ids), self.rng.choice(ids)
            if frozenset((a, b)) not in self.tried:
                return a, b
        return None

    def _free_spot(self) -> tuple[float, float]:
        """A place on the board away from the tiles already there."""
        others = [(t["x"], t["y"]) for t in self.game.tiles()]
        best, far = (0.5, 0.5), -1.0
        for _ in range(10):
            x, y = self.rng.uniform(0.15, 0.85), self.rng.uniform(0.2, 0.8)
            nearest = min((math.hypot((x - ox) * ASPECT, y - oy) for ox, oy in others), default=9.0)
            if nearest > far:
                best, far = (x, y), nearest
        return best

    def _place(self, element: str) -> Script:
        """Go to the element list (off the board), find the element, put a tile on the board, come back. Returns the tile's id."""
        yield from self._exit_board()
        yield from self._wait(self.rng.uniform(0.9, 2.4))
        x, y = self._free_spot()
        tile = self.game.add_tile(self.token, element, x, y)["id"]
        self.tiles.append(tile)
        self.pointer.teleport(0.995, self.rng.uniform(0.2, 0.8), self.now)
        self.visible = True
        return tile

    def _tile(self, tile_id: str) -> dict[str, Any] | None:
        return next((t for t in self.game.tiles() if t["id"] == tile_id), None)

    def _pick_up(self, tile_id: str) -> Script:
        tile = self._tile(tile_id)
        if tile is None:
            raise GameError(404, "that tile no longer exists")
        yield from self._wait_until(self.pointer.move_to(tile["x"], tile["y"], self.now))
        yield from self._wait(self.rng.uniform(0.08, 0.22))
        self.game.grab_tile(self.token, tile_id)
        self.dragging = True

    def _carry(self, tile_id: str, tx: float, ty: float) -> Script:
        """Drag the held tile to (tx, ty), sending its position the way a browser does while a tile is being moved."""
        end = self.pointer.move_to(tx, ty, self.now, react=False)
        while self.now < end + 0.05:
            x, y = self.pointer.position_at(self.now)
            self.game.move_tile(self.token, tile_id, x, y, x, y)
            yield from self._wait(0.045)

    def _try_combine(self, a_id: str, b_id: str) -> Script:
        b = self._tile(b_id)
        if b is None or self._tile(a_id) is None:
            return
        yield from self._pick_up(a_id)
        yield from self._carry(a_id, b["x"], b["y"])
        result = self.game.combine(self.token, b_id, tile=a_id, x=b["x"], y=b["y"])
        self.dragging = False
        if result["results"]:
            gone = set(result["removed"])
            self.tiles = [t for t in self.tiles if t not in gone] + [t["id"] for t in result["tiles"]]
            if result["new"] and self.rng.random() < self.p.chatty:
                self._emote("cheer")
            yield from self._wait(self.rng.uniform(1.0, 3.0))
        else:
            yield from self._wait(self.rng.uniform(0.6, 1.6))   # "nothing happens", a pause, then on to the next

    def _tidy_if_crowded(self) -> Script:
        live = [t for t in self.game.tiles() if t["id"] in self.tiles and t["held_by"] is None]
        self.tiles = [t["id"] for t in live]
        if len(live) <= 6:
            return
        for tile in live[:3]:   # put a few of its old tiles away, the way a person drags them off the board
            yield from self._pick_up(tile["id"])
            yield from self._carry(tile["id"], 1.0, tile["y"])
            self.game.remove_tile(self.token, tile["id"])
            self.dragging = False
            self.tiles = [t for t in self.tiles if t != tile["id"]]
            yield from self._wait(self.rng.uniform(0.3, 1.0))


# ---- joining and leaving -------------------------------------------------------------------------------------------------

@dataclass
class BotPolicy:
    """When companions come. Off unless the server starts with --bots on."""
    enabled: bool = False
    max_total: int = 3                  # across all games at once
    lonely_min: float = 20.0            # a person alone in a public game this long (at the earliest...)
    lonely_max: float = 75.0            # ...and this long at the most, before one joins
    stay_min: float = 360.0             # a companion stays this long at the least...
    stay_max: float = 1800.0            # ...and this long at the most
    presence_share: float = 0.3         # the share that only keep a presence (level 0) rather than play (level 1)


class BotRunner:
    def __init__(self, registry: Any, policy: BotPolicy, *, clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None, cast: tuple[str, ...] = CAST) -> None:
        self.registry = registry
        self.policy = policy
        self.clock = clock
        self.rng = rng or random.Random()
        self.cast = cast
        self._bots: list[Bot] = []
        self._lonely: dict[str, tuple[float, float]] = {}   # game -> (since when one person has been alone, how long until one joins)
        self._cooldown: dict[str, float] = {}               # game -> no new companion before this time
        self._next_manage = 0.0

    # -- the loop

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if now >= self._next_manage:
            self._next_manage = now + 2.0
            self._manage(now)
        for bot in list(self._bots):
            bot.step(now)
            if bot.done:
                self._bots.remove(bot)

    def _manage(self, now: float) -> None:
        policy = self.policy
        games = self.registry.public_games()
        live = {g.slug for g in games if not g.ended}
        self._bots = [b for b in self._bots if b.game.slug in live and not b.done]
        for bot in self._bots:
            people = bot.game.people_count()
            if people == 0:
                bot.empty_since = bot.empty_since if bot.empty_since is not None else now
            else:
                bot.empty_since = None
            if ((not policy.enabled and not bot.manual) or people >= MAX_PLAYERS - 1
                    or (bot.empty_since is not None and now - bot.empty_since >= 15.0) or now >= bot.leave_at) and not bot.leaving:
                bot.leaving = True
                if policy.enabled and now >= bot.leave_at:   # it had its stay: the room stays company-free for a good while
                    self._cooldown[bot.game.slug] = now + self.rng.uniform(300.0, 900.0)
        if not policy.enabled:
            self._lonely.clear()
            return
        busy = {b.game.slug for b in self._bots}
        for game in games:
            slug = game.slug
            alone = game.people_count() == 1 and slug not in busy and now >= self._cooldown.get(slug, 0.0)
            if not alone or len(self._bots) >= policy.max_total:
                self._lonely.pop(slug, None)
                continue
            since, wait = self._lonely.setdefault(slug, (now, self.rng.uniform(policy.lonely_min, policy.lonely_max)))
            if now - since >= wait:
                self._lonely.pop(slug, None)
                self._spawn(game, now)

    def _spawn(self, game: GameState, now: float, level: int | None = None, manual: bool = False) -> Bot | None:
        policy = self.policy
        in_use = {b.name.casefold() for b in self._bots}
        names = [n for n in self.cast if n.casefold() not in in_use]
        self.rng.shuffle(names)
        for name in names[:12]:
            try:
                player = game.add_virtual(name)
            except GameError as exc:
                if exc.status == 409 and "already taken" in exc.message:
                    continue   # a person in that game has the name: try another
                return None    # no free seat, or the game is closed
            seed = self.rng.getrandbits(48)
            rng = random.Random(seed)
            if level is None:
                level = 0 if rng.random() < policy.presence_share else 1
            bot = Bot(game, player, rng, Personality.random(rng), level, now, rng.uniform(policy.stay_min, policy.stay_max))
            bot.manual = manual
            self._bots.append(bot)
            log.info("companion %s joined game %s (level %d)", name, game.name, level)
            return bot
        return None

    # -- what the administrators see and do

    def listing(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [{"game": b.game.slug, "game_name": b.game.name, "name": b.name, "level": b.level,
                 "minutes": int((now - b.joined_at) // 60)} for b in self._bots]

    def add(self, slug: str, level: int | None = None) -> str:
        """An administrator puts a companion in a public game now (for testing, or to liven a quiet room)."""
        game = next((g for g in self.registry.public_games() if g.slug == slug), None)
        if game is None:
            raise GameError(404, "that game does not exist, or is not a public game")
        bot = self._spawn(game, self.clock(), level, manual=True)
        if bot is None:
            raise GameError(409, "no companion could join (no free seat to spare, or the game is closed)")
        return bot.name

    def remove(self, slug: str | None = None, name: str | None = None) -> int:
        """Take companions out at once: those in one game, one by name, or all of them."""
        gone = 0
        for bot in list(self._bots):
            if (slug is None or bot.game.slug == slug) and (name is None or bot.name.casefold() == name.casefold()):
                bot.game.remove_virtual(bot.token)
                bot.done = True
                self._bots.remove(bot)
                gone += 1
        return gone

    def active(self) -> list[Bot]:
        return list(self._bots)


def bot_loop(runner: BotRunner) -> None:
    """Step every companion about 20 times a second, forever. A failure must never stop the loop."""
    while True:
        time.sleep(TICK_SECONDS)
        try:
            runner.tick()
        except Exception as exc:
            log.warning("companions' rounds failed: %s", exc)

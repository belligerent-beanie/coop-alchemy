"""Companions: the cast, the human-like cursor, how a companion plays, when one joins and leaves, and the rules about seats."""

from __future__ import annotations

import math
import random
import statistics
import unittest
from typing import Any

from accounts import clean_username
from bots import ASPECT, TICK_SECONDS, Bot, BotPolicy, BotRunner, Personality, Pointer, fitts_seconds, min_jerk
from cast import CAST
from games import GameRegistry
from namefilter import NameFilter
from state import MAX_PLAYERS, GameError, GameState
from test_coop import RECIPES, call, drain, load_recipes, login_as, serve
from visitor import MR_ALCHEMY, is_reserved


# ---- the cast ----------------------------------------------------------------------------------------------------------

class CastTests(unittest.TestCase):
    def test_every_name_is_a_valid_username_and_passes_the_blocklist(self) -> None:
        names = NameFilter.load()
        for name in CAST:
            self.assertEqual(clean_username(name), name)
            names.check(name, "username")

    def test_the_names_are_distinct_and_there_are_plenty(self) -> None:
        self.assertGreaterEqual(len(CAST), 40)
        self.assertEqual(len({n.casefold() for n in CAST}), len(CAST))
        self.assertNotIn(MR_ALCHEMY.casefold(), {n.casefold() for n in CAST})

    def test_nobody_can_register_under_one(self) -> None:
        for name in CAST:
            self.assertTrue(is_reserved(name, CAST), name)
        self.assertFalse(is_reserved("Someone Else", CAST))
        server, port = serve(GameRegistry(load_recipes(RECIPES)))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for name in (CAST[0], CAST[0].upper(), CAST[33].replace("_", " ")):
            status, body = call(port, "POST", "/register", {"username": name, "password": "a-long-enough-pass"}, headers={"X-Session": ""})
            self.assertEqual(status, 409, name)
            self.assertIn("taken", body["error"])


# ---- the cursor --------------------------------------------------------------------------------------------------------

def sample(pointer: Pointer, start: float, end: float, step: float = TICK_SECONDS) -> list[tuple[float, float, float]]:
    out, t = [], start
    while t <= end:
        x, y = pointer.position_at(t)
        out.append((t, x, y))
        t += step
    return out


def speeds(points: list[tuple[float, float, float]]) -> list[float]:
    """Board heights per second between consecutive samples."""
    return [math.hypot((b[1] - a[1]) * ASPECT, b[2] - a[2]) / (b[0] - a[0]) for a, b in zip(points, points[1:])]


class PointerTests(unittest.TestCase):
    def pointer(self, seed: int = 1, accuracy: float = 0.8, pace: float = 1.0, x: float = 0.1, y: float = 0.1) -> Pointer:
        return Pointer(random.Random(seed), Personality(pace=pace, accuracy=accuracy), x, y, 0.0)

    def test_it_arrives_where_it_was_sent(self) -> None:
        for seed in range(20):
            pointer = self.pointer(seed)
            end = pointer.move_to(0.8, 0.7, 1.0)
            x, y = pointer.position_at(end + 1.0)
            self.assertLess(math.hypot((x - 0.8) * ASPECT, y - 0.7), 0.01, seed)

    def test_longer_movements_take_longer_as_fitts_says(self) -> None:
        self.assertLess(fitts_seconds(0.1), fitts_seconds(0.5))
        self.assertLess(fitts_seconds(0.5), fitts_seconds(1.5))
        short = statistics.mean(self.pointer(s).move_to(0.2, 0.15, 0.0) for s in range(30))
        far = statistics.mean(self.pointer(s).move_to(0.9, 0.85, 0.0) for s in range(30))
        self.assertGreater(far, short + 0.2)

    def test_it_never_moves_faster_than_a_hand_or_jumps(self) -> None:
        peaks: list[float] = []
        for seed in range(30):
            pointer = self.pointer(seed, accuracy=0.6)
            end = pointer.move_to(0.9, 0.9, 0.0)
            points = sample(pointer, 0.0, end + 1.0)
            peaks.append(max(speeds(points)))
            self.assertLess(peaks[-1], 6.0, seed)                  # a fast flick across the whole board
            self.assertTrue(all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for _, x, y in points))
            self.assertLess(max(math.hypot((b[1] - a[1]) * ASPECT, b[2] - a[2]) for a, b in zip(points, points[1:])), 0.3)   # no jumps
        self.assertLess(statistics.mean(peaks), 4.5)

    def test_speed_rises_and_falls_like_a_hand(self) -> None:
        pointer = self.pointer(3, accuracy=1.0)
        end = pointer.move_to(0.9, 0.85, 0.0, react=False)
        points = [p for p in sample(pointer, 0.0, end, 0.02)]
        v = speeds(points)
        peak = max(v)
        peak_at = v.index(peak) / len(v)
        self.assertTrue(0.25 < peak_at < 0.75, peak_at)
        self.assertLess(statistics.mean(v[:3]), 0.4 * peak)
        self.assertLess(statistics.mean(v[-3:]), 0.4 * peak)

    def test_it_is_repeatable_with_a_seed_and_differs_between_seeds(self) -> None:
        def path(seed: int) -> list[tuple[float, float, float]]:
            p = self.pointer(seed)
            return sample(p, 0.0, p.move_to(0.7, 0.6, 0.0) + 0.5)
        self.assertEqual(path(5), path(5))
        self.assertNotEqual(path(5), path(6))

    def test_it_sometimes_overshoots_when_it_is_clumsy_and_never_when_it_is_exact(self) -> None:
        def overshoots(accuracy: float) -> int:
            count = 0
            for seed in range(60):
                pointer = Pointer(random.Random(seed), Personality(accuracy=accuracy), 0.1, 0.5, 0.0)
                end = pointer.move_to(0.8, 0.5, 0.0, react=False)
                if max(x for _, x, _ in sample(pointer, 0.0, end, 0.01)) > 0.8 + 0.012:
                    count += 1
            return count
        self.assertEqual(overshoots(1.0), 0)
        self.assertGreater(overshoots(0.5), 8)

    def test_the_path_is_curved_not_ruler_straight(self) -> None:
        bends = []
        for seed in range(20):
            pointer = self.pointer(seed, accuracy=1.0, x=0.1, y=0.5)
            end = pointer.move_to(0.9, 0.5, 0.0, react=False)
            bends.append(max(abs(y - 0.5) for _, _, y in sample(pointer, 0.0, end, 0.02)))
        self.assertGreater(statistics.mean(bends), 0.01)

    def test_a_resting_hand_still_wobbles_and_a_first_move_waits_for_a_reaction(self) -> None:
        pointer = self.pointer(2, x=0.5, y=0.5)
        xs = [x for _, x, _ in sample(pointer, 0.0, 6.0, 0.05)]
        self.assertGreater(statistics.pstdev(xs), 0.0005)
        self.assertLess(max(xs) - min(xs), 0.02)   # but it does not wander off
        pointer.move_to(0.9, 0.9, 10.0)
        before = pointer.position_at(10.05)
        self.assertLess(math.hypot(before[0] - 0.5, before[1] - 0.5), 0.02)   # 50 ms in, it has not started

    def test_min_jerk_is_a_smooth_zero_to_one(self) -> None:
        self.assertEqual(min_jerk(-1), 0.0)
        self.assertEqual(min_jerk(2), 1.0)
        self.assertAlmostEqual(min_jerk(0.5), 0.5)
        steps = [min_jerk(i / 20) for i in range(21)]
        self.assertEqual(steps, sorted(steps))


# ---- a companion in a game -----------------------------------------------------------------------------------------------

REAL = None


def real_book() -> Any:
    global REAL
    if REAL is None:
        from pathlib import Path
        REAL = load_recipes(Path(__file__).resolve().parent / "recipes.json")
    return REAL


def room(people: int = 1, book: Any = None, seed: int = 1) -> tuple[GameState, list[Any], list[Any]]:
    game = GameState(book or real_book(), rng=random.Random(seed))
    joined = [game.join(f"Human{i}") for i in range(people)]
    subs = [game.subscribe(p.token) for p in joined]
    for sub in subs:
        assert sub
        drain(sub)
    return game, joined, subs


def run_bot(game: GameState, level: int, seconds: float, seed: int = 1, personality: Personality | None = None, name: str = "Wulfstan",
            watch: Any = None) -> Bot:
    """Play a companion for `seconds` of game time. `watch` is a person's event stream, read as often as a browser would, so nothing is
    lost to a full queue; what it sees is kept in bot.events."""
    player = game.add_virtual(name)
    rng = random.Random(seed)
    bot = Bot(game, player, rng, personality or Personality.random(rng), level, 0.0, 10_000.0)
    bot.events = []  # type: ignore[attr-defined]
    t, n = 0.0, 0
    while t < seconds and not bot.done:
        bot.step(t)
        t += TICK_SECONDS
        n += 1
        if watch is not None and n % 10 == 0:
            bot.events.extend(drain(watch))  # type: ignore[attr-defined]
    if watch is not None:
        bot.events.extend(drain(watch))  # type: ignore[attr-defined]
    return bot


class BotTests(unittest.TestCase):
    def test_a_beginner_plays_without_trouble_and_finds_things_at_a_human_pace(self) -> None:
        for seed in (1, 2, 3):
            game, _, subs = room(seed=seed)
            before = len(game.discovered())
            bot = run_bot(game, 1, 600, seed, watch=subs[0])
            found = len(game.discovered()) - before
            self.assertLessEqual(bot.errors, 3, seed)
            self.assertGreaterEqual(found, 1, f"seed {seed}: found nothing in ten minutes")
            self.assertLessEqual(found, 40, f"seed {seed}: {found} finds in ten minutes is not a beginner")
            self.assertLessEqual(len([t for t in game.tiles()]), 14, "it should put its old tiles away")
            self.assertEqual([n for n, _ in bot.events if n == "tiles_cleared"], [])   # it never uses the cat  # type: ignore[attr-defined]

    def test_a_presence_keeps_company_but_never_discovers_anything(self) -> None:
        game, _, _ = room()
        before = dict(game.discovered())
        run_bot(game, 0, 600)
        self.assertEqual(game.discovered(), before)

    def test_its_cursor_is_sent_at_most_twenty_times_a_second_and_never_jumps(self) -> None:
        game, _, subs = room()
        bot = run_bot(game, 1, 120, watch=subs[0])
        moves = [d for n, d in bot.events if n == "cursor" and d["pid"] == bot.player.pid and d["x"] is not None]  # type: ignore[attr-defined]
        self.assertGreater(len(moves), 100)
        self.assertLessEqual(len(moves) / 120, 20.0)
        jumps = [math.hypot((b["x"] - a["x"]) * ASPECT, b["y"] - a["y"]) for a, b in zip(moves, moves[1:])]
        self.assertLess(statistics.median(jumps), 0.05)
        # a cursor that appears at the edge after the list trip is the one big step allowed: it must not be common
        self.assertLess(sum(1 for j in jumps if j > 0.3), len(moves) * 0.05)

    def test_it_never_touches_what_people_put_on_the_board(self) -> None:
        game, humans, _ = room()
        mine = game.add_tile(humans[0].token, "water", 0.5, 0.5)
        run_bot(game, 1, 300)
        left = {t["id"]: t for t in game.tiles()}
        self.assertEqual((left[mine["id"]]["x"], left[mine["id"]]["y"]), (0.5, 0.5))
        self.assertIsNone(left[mine["id"]]["held_by"])

    def test_leaving_puts_everything_down_hides_the_cursor_and_frees_the_seat(self) -> None:
        game, _, subs = room()
        player = game.add_virtual("Wulfstan")
        rng = random.Random(4)
        bot = Bot(game, player, rng, Personality.random(rng), 1, 0.0, 10_000.0)
        t, n, events = 0.0, 0, []
        while t < 90:
            bot.step(t)
            t += TICK_SECONDS
            n += 1
            if n % 10 == 0:
                events.extend(drain(subs[0]))
        bot.leaving = True
        while not bot.done and t < 400:
            bot.step(t)
            t += TICK_SECONDS
            n += 1
            if n % 10 == 0:
                events.extend(drain(subs[0]))
        events.extend(drain(subs[0]))
        self.assertTrue(bot.done)
        self.assertEqual(game.virtual_players(), [])
        self.assertFalse([tile for tile in game.tiles() if tile["held_by"] == player.pid])
        self.assertTrue([d for n, d in events if n == "cursor" and d["pid"] == player.pid and d["x"] is None])
        last_players = [d for n, d in events if n == "players"][-1]
        self.assertNotIn("Wulfstan", [p["name"] for p in last_players["players"]])

    def test_it_reacts_to_other_players_finds_sometimes(self) -> None:
        game, humans, subs = room()
        player = game.add_virtual("Wulfstan")
        rng = random.Random(1)
        bot = Bot(game, player, rng, Personality(chatty=1.0), 0, 0.0, 10_000.0)
        for _ in range(8):
            bot.step(0.0)
        first = game.add_tile(humans[0].token, "water", 0.2, 0.2)
        other = game.add_tile(humans[0].token, "fire", 0.6, 0.6)
        game.grab_tile(humans[0].token, first["id"])
        game.combine(humans[0].token, other["id"], tile=first["id"], x=0.6, y=0.6)
        drain(subs[0])
        t = 1.0
        while t < 40:
            bot.step(t)
            t += TICK_SECONDS
        emotes = [d for n, d in drain(subs[0]) if n == "emote" and d["pid"] == player.pid]
        self.assertTrue(emotes, "with chatty=1 it should have said something")


class SeatTests(unittest.TestCase):
    def test_it_looks_like_anyone_else_in_the_player_list(self) -> None:
        game, humans, subs = room()
        player = game.add_virtual("Wulfstan")
        listing = [d for n, d in drain(subs[0]) if n == "players"][-1]["players"]
        row = next(p for p in listing if p["name"] == "Wulfstan")
        self.assertEqual(set(row), {"pid", "name", "color"})   # nothing marks it
        self.assertEqual(row["pid"], player.pid)
        snapshot = game.subscribe(game.join("Human9").token)
        assert snapshot
        first = drain(snapshot)[0][1]
        self.assertIn("Wulfstan", [p["name"] for p in first["players"]])
        self.assertTrue(all(set(p) == {"pid", "name", "color"} for p in first["players"]))

    def test_it_never_takes_the_last_free_seat(self) -> None:
        game, _, _ = room(people=MAX_PLAYERS - 1)
        with self.assertRaises(GameError):
            game.add_virtual("Wulfstan")
        game2, _, _ = room(people=MAX_PLAYERS - 2)
        game2.add_virtual("Wulfstan")
        with self.assertRaises(GameError):
            game2.add_virtual("Ealdred")   # that would be the last seat

    def test_a_person_always_gets_the_seat(self) -> None:
        game, _, _ = room(people=MAX_PLAYERS - 2)
        game.add_virtual("Wulfstan")
        self.assertEqual(len(game._seated()), MAX_PLAYERS - 1)
        newcomer = game.join("Late")                      # fills the room
        self.assertTrue(game.subscribe(newcomer.token))
        last = game.join("Last")                          # full only because of the companion: it leaves
        self.assertEqual(game.virtual_players(), [])
        self.assertTrue(game.subscribe(last.token))
        self.assertEqual(len(game._seated()), MAX_PLAYERS)

    def test_it_is_not_a_person(self) -> None:
        game, _, _ = room()
        game.add_virtual("Wulfstan")
        self.assertEqual(game.people_count(), 1)
        self.assertEqual(len(game._seated()), 2)
        self.assertIsNone(game.visit())   # Mr Alchemy needs two people

    def test_a_game_with_only_a_companion_still_expires(self) -> None:
        now = [0.0]
        game = GameState(load_recipes(RECIPES), clock=lambda: now[0])
        game.add_virtual("Wulfstan")
        now[0] += 700
        self.assertTrue(game.is_expired(600))

    def test_it_is_not_saved_or_carried_across_a_reload(self) -> None:
        game, _, _ = room()
        game.add_virtual("Wulfstan")
        self.assertNotIn("Wulfstan", game.export()["players"])
        self.assertNotIn("Wulfstan", [p["name"] for p in game.live_export()["live"]["players"]])

    def test_only_public_games_that_are_running(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        solo = registry.start_solo("Alice")
        with self.assertRaises(GameError):
            solo.add_virtual("Wulfstan")
        game, _, _ = room()
        game.close("Human0", False)
        with self.assertRaises(GameError):
            game.add_virtual("Wulfstan")

    def test_a_name_in_use_is_refused(self) -> None:
        game, _, _ = room()
        game.add_virtual("Wulfstan")
        with self.assertRaises(GameError):
            game.add_virtual("wulfstan")


# ---- joining and leaving -------------------------------------------------------------------------------------------------

class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1000.0]
        self.registry = GameRegistry(load_recipes(RECIPES), clock=lambda: self.now[0], max_games=10)
        self.policy = BotPolicy(enabled=True)
        self.runner = BotRunner(self.registry, self.policy, clock=lambda: self.now[0], rng=random.Random(7))
        self.registry.bots = self.runner

    def lobby(self, name: str, people: int = 1) -> GameState:
        game = self.registry.create(name, f"{name}-host")
        for i in range(people):
            assert game.subscribe(game.join(f"{name}-p{i}").token)
        return game

    def advance(self, seconds: float, step: float = 1.0) -> None:
        end = self.now[0] + seconds
        while self.now[0] < end:
            self.now[0] += step
            self.runner.tick()

    def test_off_means_off(self) -> None:
        self.policy.enabled = False
        self.lobby("Quiet")
        self.advance(200)
        self.assertEqual(self.runner.active(), [])

    def test_a_lone_person_gets_company_after_a_random_wait(self) -> None:
        game = self.lobby("Lonely")
        self.advance(19)
        self.assertEqual(self.runner.active(), [])
        self.advance(60)
        bots = self.runner.active()
        self.assertEqual(len(bots), 1)
        self.assertIn(bots[0].name, CAST)
        self.assertEqual(len(game.virtual_players()), 1)
        self.assertEqual(game.people_count(), 1)

    def test_not_to_games_with_two_people_none_or_a_private_one(self) -> None:
        self.lobby("Busy", people=2)
        empty = self.registry.create("Empty", "Nobody")   # nobody has joined
        self.registry.start_solo("Alice").join("Alice")
        self.advance(200)
        self.assertEqual(self.runner.active(), [])
        self.assertEqual(empty.virtual_players(), [])

    def test_one_per_game_and_a_cap_across_games(self) -> None:
        self.policy.max_total = 2
        games = [self.lobby(f"Room{i}") for i in range(4)]
        self.advance(200)
        self.assertEqual(len(self.runner.active()), 2)
        for game in games:
            self.assertLessEqual(len(game.virtual_players()), 1)
        names = [b.name for b in self.runner.active()]
        self.assertEqual(len(names), len(set(names)))

    def test_it_leaves_when_a_second_person_would_crowd_the_room(self) -> None:
        game = self.lobby("Growing")
        self.advance(100)
        self.assertEqual(len(game.virtual_players()), 1)
        for i in range(MAX_PLAYERS - 2):
            assert game.subscribe(game.join(f"Extra{i}").token)
        self.advance(120)
        self.assertEqual(game.virtual_players(), [])
        self.assertEqual(self.runner.active(), [])

    def test_it_leaves_when_the_room_empties(self) -> None:
        game = self.lobby("Emptying")
        self.advance(100)
        self.assertEqual(len(game.virtual_players()), 1)
        for sub in list(game._subs):
            game.unsubscribe(sub)
        self.advance(60)   # they are gone straight away; the companion waits 15 seconds, then walks off
        self.assertEqual(game.virtual_players(), [])

    def test_after_its_stay_it_leaves_and_that_room_is_left_alone_for_a_while(self) -> None:
        game = self.lobby("Stayer")
        self.advance(100)
        bot = self.runner.active()[0]
        bot.leave_at = self.now[0] + 5
        self.advance(120)
        self.assertEqual(game.virtual_players(), [])
        self.advance(200)
        self.assertEqual(game.virtual_players(), [], "a room that just had company is not given another at once")

    def test_administrators_can_add_and_remove_one(self) -> None:
        game = self.lobby("Test")
        self.policy.enabled = False
        name = self.runner.add(game.slug, level=1)
        self.assertIn(name, CAST)
        self.advance(30)
        self.assertEqual(len(game.virtual_players()), 1, "a companion added by hand is not sent away by --bots off")
        self.assertEqual(self.runner.listing()[0]["name"], name)
        with self.assertRaises(GameError) as missing:
            self.runner.add("no-such-game")
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(self.runner.remove(name=name.upper()), 1)
        self.assertEqual(game.virtual_players(), [])
        self.assertEqual(self.runner.listing(), [])

    def test_a_game_that_ends_takes_its_companion_with_it(self) -> None:
        game = self.lobby("Ending")
        self.advance(100)
        self.assertEqual(len(self.runner.active()), 1)
        game.close("Ending-p0", False)
        self.advance(10)
        self.assertEqual(self.runner.active(), [])


# ---- the administrators' controls over HTTP --------------------------------------------------------------------------------

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = GameRegistry(load_recipes(RECIPES))
        cls.server, cls.port = serve(cls.registry)
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.ann = login_as(cls.port, "Annie")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path: str, body: Any, session: str) -> tuple[int, Any]:
        return call(self.port, "POST", path, body, headers={"X-Session": session})

    def test_only_administrators_and_only_when_set_up(self) -> None:
        self.registry.bots = None
        self.assertEqual(self.req("/ops/bots/add", {"id": "x"}, self.admin)[0], 409)
        self.registry.bots = BotRunner(self.registry, BotPolicy(enabled=False))
        status, game = self.req("/games", {"name": "Bot room"}, self.ann)
        self.assertEqual(status, 200)
        self.req(f"/g/{game['id']}/join", {}, self.ann)
        self.assertEqual(self.req("/ops/bots/add", {"id": game["id"]}, self.ann)[0], 403)
        self.assertEqual(self.req("/ops/bots/add", {}, self.admin)[0], 400)
        self.assertEqual(self.req("/ops/bots/add", {"id": game["id"], "level": 7}, self.admin)[0], 400)
        self.assertEqual(self.req("/ops/bots/add", {"id": "nope"}, self.admin)[0], 404)
        status, added = self.req("/ops/bots/add", {"id": game["id"], "level": 0}, self.admin)
        self.assertEqual(status, 200)
        self.assertIn(added["name"], CAST)
        status, overview = call(self.port, "GET", "/ops/games", headers={"X-Session": self.admin})
        self.assertEqual([c["name"] for c in overview["companions"]], [added["name"]])
        self.assertFalse(overview["companions_on"])
        self.assertEqual(self.req("/ops/bots/remove", {"name": added["name"]}, self.ann)[0], 403)
        self.assertEqual(self.req("/ops/bots/remove", {"name": added["name"]}, self.admin), (200, {"removed": 1}))


if __name__ == "__main__":
    unittest.main()

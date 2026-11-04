"""Tests for race mode: the room's state machine, the racers' private worlds, and the HTTP routes.

With test_recipes.json: water + fire = steam, air + steam = cloud, so "cloud" takes two combinations and a "count"
race for 6 elements (4 starters + 2) is won by making steam and cloud.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from games import MAX_RACES, GameRegistry
from race import COUNTDOWN_SECONDS, MAX_RACERS, RaceRoom
from recipes import load_recipes
from state import GameError, GameState
from test_coop import call, login_as, serve

BOOK = load_recipes(Path(__file__).resolve().parent / "test_recipes.json")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def combine(game: GameState, player: Any, a: str, b: str) -> dict[str, Any]:
    first = game.add_tile(player.token, a, 0.2, 0.2)
    second = game.add_tile(player.token, b, 0.6, 0.6)
    game.grab_tile(player.token, first["id"])
    return game.combine(player.token, second["id"], tile=first["id"], x=0.6, y=0.6)


class RoomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.room = RaceRoom("r1", "Ann", "count", count=6, clock=self.clock)

    def test_lobby_rules(self) -> None:
        self.assertEqual(self.room.state, "lobby")
        with self.assertRaises(GameError):
            self.room.start("Ann")                      # one racer is not a race
        self.room.join("Bob")
        self.room.join("bob")                           # joining twice changes nothing
        self.assertEqual([r["name"] for r in self.room.status()["racers"]], ["Ann", "Bob"])
        with self.assertRaises(GameError) as err:
            self.room.start("Bob")
        self.assertEqual(err.exception.status, 403)     # only the host starts
        with self.assertRaises(GameError):
            self.room.start("Zed")                      # and only a member
        for n in range(MAX_RACERS - 2):
            self.room.join(f"P{n}")
        with self.assertRaises(GameError) as err:
            self.room.join("Late")
        self.assertEqual(err.exception.status, 409)

    def test_countdown_then_running_then_won(self) -> None:
        self.room.join("Bob")
        self.room.start("Ann")
        self.assertEqual(self.room.state, "countdown")
        self.assertEqual(self.room.status()["starts_in"], COUNTDOWN_SECONDS)
        hook = self.room.hook("Ann")
        self.assertFalse(hook.is_open())
        self.assertFalse(hook.report(5, True))          # too early: ignored
        with self.assertRaises(GameError):
            self.room.join("Cy")                        # no late joiners
        self.clock.now += COUNTDOWN_SECONDS
        self.assertEqual(self.room.state, "running")
        self.assertTrue(hook.is_open())
        self.assertFalse(hook.report(5, False))
        self.assertTrue(hook.report(6, True))           # Ann wins
        self.assertEqual(self.room.state, "finished")
        self.assertFalse(self.room.hook("Bob").report(6, True))   # a close second does not replace her
        self.assertFalse(hook.is_open())
        status = self.room.status()
        self.assertEqual(status["winner"], "Ann")
        self.assertEqual([(r["name"], r["count"]) for r in status["racers"]], [("Ann", 6), ("Bob", 0)])

    def test_leaving(self) -> None:
        self.room.join("Bob")
        self.room.join("Cy")
        self.assertFalse(self.room.leave("Bob"))
        self.assertFalse(self.room.has("Bob"))
        self.assertTrue(self.room.leave("Ann"))         # the host leaving the lobby closes it
        self.assertTrue(self.room.expired())

    def test_the_last_racer_standing_wins(self) -> None:
        self.room.join("Bob")
        self.room.start("Ann")
        self.clock.now += COUNTDOWN_SECONDS
        self.assertFalse(self.room.leave("Bob"))
        self.assertEqual((self.room.state, self.room.status()["winner"]), ("finished", "Ann"))

    def test_expiry(self) -> None:
        self.room.join("Bob")
        self.clock.now += 31
        self.room.touch("Ann")
        self.room.prune()
        self.assertFalse(self.room.has("Bob"))          # Bob stopped checking in
        self.assertFalse(self.room.expired())
        self.clock.now += 31
        self.room.prune()
        self.assertTrue(self.room.expired())            # the host went quiet too

    def test_rules_for_rooms(self) -> None:
        with self.assertRaises(GameError):
            RaceRoom("r", "Ann", "nope")
        with self.assertRaises(GameError):
            RaceRoom("r", "Ann", "target")


class RegistryRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.registry = GameRegistry(BOOK, max_games=1, clock=self.clock)

    def start(self, goal: str = "count") -> tuple[str, dict[str, Any]]:
        room = self.registry.create_race("Ann", goal, count=6) if goal == "count" else self.registry.create_race("Ann", goal)
        self.registry.join_race(room["id"], "Bob")
        return room["id"], self.registry.start_race(room["id"], "Ann")

    def game_of(self, rid: str, name: str) -> GameState:
        slug = self.registry.race_status(rid, name)["game"].split("/")[-1]
        game = self.registry.get(slug)
        assert game is not None
        return game

    def test_each_racer_gets_a_private_world(self) -> None:
        rid, status = self.start()
        self.assertEqual(status["state"], "countdown")
        ann, bob = self.game_of(rid, "Ann"), self.game_of(rid, "Bob")
        self.assertIsNot(ann, bob)
        self.assertTrue(ann.private)
        self.assertEqual(len(ann.discovered()), len(BOOK.starters))
        with self.assertRaises(GameError) as err:
            ann.join("Bob")
        self.assertEqual(err.exception.status, 403)
        player = ann.join("Ann")
        with self.assertRaises(GameError) as err:
            ann.hint(player.token, "can_make")
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(self.registry.list(), [])      # not in the public list, and it uses no slot
        self.assertEqual([g["private"] for g in self.registry.list("Ann")], [True])
        self.registry.create("Normal")
        snapshot = ann._snapshot(player)
        self.assertEqual(snapshot["race"], {"id": rid, "goal": "count", "goal_count": 6, "target": None})

    def test_nothing_can_be_combined_before_the_go(self) -> None:
        rid, _ = self.start()
        game = self.game_of(rid, "Ann")
        player = game.join("Ann")
        with self.assertRaises(GameError) as err:
            combine(game, player, "water", "fire")
        self.assertEqual(err.exception.status, 409)
        self.clock.now += COUNTDOWN_SECONDS
        combine(game, player, "water", "fire")

    def test_first_to_the_count_wins_and_the_other_is_stopped(self) -> None:
        rid, _ = self.start()
        self.clock.now += COUNTDOWN_SECONDS
        ann, bob = self.game_of(rid, "Ann"), self.game_of(rid, "Bob")
        pa, pb = ann.join("Ann"), bob.join("Bob")
        sub = ann.subscribe(pa.token)
        assert sub
        combine(bob, pb, "water", "fire")
        combine(ann, pa, "water", "fire")
        self.assertEqual(self.registry.race_status(rid, "Ann")["winner"], None)
        combine(ann, pa, "air", "steam")                  # Ann reaches 6 first
        status = self.registry.race_status(rid, "Bob")
        self.assertEqual((status["state"], status["winner"]), ("finished", "Ann"))
        self.assertEqual([(r["name"], r["count"]) for r in status["racers"]],
                         [("Ann", len(BOOK.starters) + 2), ("Bob", len(BOOK.starters) + 1)])
        events = []
        while not sub.messages.empty():
            events.append(sub.messages.get_nowait())
        self.assertTrue(any(e and "race_won" in e for e in events))
        with self.assertRaises(GameError) as err:
            combine(bob, pb, "air", "steam")
        self.assertEqual(err.exception.status, 409)

    def test_a_target_race(self) -> None:
        rid, status = self.start("target")
        target = status["target"]["id"]
        self.assertIn(target, BOOK.elements)
        self.assertNotIn(target, BOOK.starters)
        self.assertEqual(self.registry.race_status(rid, "Bob")["target"]["id"], target)
        game = self.game_of(rid, "Ann")
        self.assertEqual(game._snapshot(game.join("Ann"))["race"]["target"]["id"], target)

    def test_one_race_at_a_time_and_a_limit_on_races(self) -> None:
        room = self.registry.create_race("Ann", "count")
        with self.assertRaises(GameError):
            self.registry.create_race("Ann", "count")
        with self.assertRaises(GameError):
            self.registry.join_race(self.registry.create_race("Bob", "count")["id"], "Ann")   # Ann is already in one
        self.assertEqual(self.registry.list_races("Ann")["mine"], room["id"])
        self.assertEqual({r["host"] for r in self.registry.list_races("Zed")["races"]}, {"Ann", "Bob"})
        for name in ("C", "D"):
            self.registry.create_race(name, "count")
        with self.assertRaises(GameError) as err:
            self.registry.create_race("E", "count")
        self.assertEqual(err.exception.status, 409)
        self.assertEqual(MAX_RACES, 4)
        with self.assertRaises(GameError):
            self.registry.create_race("Zed", "bogus")

    def test_leaving_and_cleaning_up(self) -> None:
        rid, _ = self.start()
        game = self.game_of(rid, "Bob")
        self.clock.now += COUNTDOWN_SECONDS
        self.registry.leave_race(rid, "Bob")
        self.assertIsNone(self.registry.get(game.slug))
        self.assertEqual(self.registry.race_status(rid, "Ann")["winner"], "Ann")    # the only one left
        self.registry.leave_race(rid, "Ann")                                         # still finished, nobody left
        self.clock.now += 700
        self.registry.list_races("Ann")
        with self.assertRaises(GameError):
            self.registry.race_status(rid, "Ann")                                    # the results have been cleared
        self.assertEqual(self.registry.live_export(), [])

    def test_races_are_not_carried_across_a_reload(self) -> None:
        self.start()
        self.assertEqual(self.registry.live_export(), [])
        self.registry.create("Normal")
        self.assertEqual(len(self.registry.live_export()), 1)

    def test_the_host_starts_and_only_when_ready(self) -> None:
        room = self.registry.create_race("Ann", "count")
        with self.assertRaises(GameError):
            self.registry.start_race(room["id"], "Ann")
        self.registry.join_race(room["id"], "Bob")
        with self.assertRaises(GameError) as err:
            self.registry.start_race(room["id"], "Bob")
        self.assertEqual(err.exception.status, 403)
        with self.assertRaises(GameError) as err:
            self.registry.race_status("nothere", "Ann")
        self.assertEqual(err.exception.status, 404)


class RaceEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = GameRegistry(BOOK, max_games=3)
        cls.server, cls.port = serve(cls.registry)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_race_over_http(self) -> None:
        ann = {"X-Session": login_as(self.port, "RaceAnn")}
        bob = {"X-Session": login_as(self.port, "RaceBob")}
        status, room = call(self.port, "POST", "/races", {"goal": "target"}, headers=ann)
        self.assertEqual(status, 200)
        rid = room["id"]
        self.assertEqual((room["state"], room["is_host"], room["you"]), ("lobby", True, True))
        listing = call(self.port, "GET", "/races", headers=bob)[1]
        self.assertEqual([r["id"] for r in listing["races"]], [rid])
        self.assertIsNone(listing["mine"])
        self.assertEqual(call(self.port, "POST", f"/races/{rid}/join", {}, headers=bob)[0], 200)
        self.assertEqual(call(self.port, "POST", f"/races/{rid}/start", {}, headers=bob)[0], 403)
        status, room = call(self.port, "POST", f"/races/{rid}/start", {}, headers=ann)
        self.assertEqual((status, room["state"]), (200, "countdown"))
        status, room = call(self.port, "GET", f"/races/{rid}", headers=bob)
        self.assertEqual((status, len(room["racers"]), room["you"]), (200, 2, True))
        self.assertTrue(room["game"].startswith("/g/race-"))
        self.assertEqual(call(self.port, "GET", "/races/nope", headers=bob)[0], 404)
        self.assertEqual(call(self.port, "GET", f"/races/{rid}", headers={"X-Session": ""})[0], 401)
        self.assertEqual(call(self.port, "POST", "/races", {"goal": "count"}, headers=bob)[0], 409)   # Bob is in a race
        self.assertEqual(call(self.port, "POST", f"/races/{rid}/leave", {}, headers=bob)[0], 200)


if __name__ == "__main__":
    unittest.main()

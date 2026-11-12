"""Mr Alchemy: the gift, the schedule, the rules about where he visits, and what he must not touch."""

from __future__ import annotations

import random
import unittest
from pathlib import Path
from typing import Any

from games import GameRegistry
from hints import new_combinations
from state import GameError, GameState
from test_coop import RECIPES, call, drain, load_recipes, login_as, serve
from visitor import GIFT_MAX, GIFT_MIN, MR_ALCHEMY, VisitorPolicy, is_reserved, pick_gift

REAL = Path(__file__).resolve().parent / "recipes.json"
SOON = VisitorPolicy(enabled=True, chance=1.0, min_seconds=60.0, max_seconds=60.0)


@unittest.skipUnless(REAL.is_file(), "the private recipe book is not in this checkout")
class GiftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.real = load_recipes(REAL)

    def test_five_to_ten_undiscovered_elements(self) -> None:
        for seed in range(20):
            gift = pick_gift(self.real, self.real.starters, random.Random(seed))
            self.assertTrue(GIFT_MIN <= len(gift) <= GIFT_MAX, len(gift))
            self.assertEqual(len(set(gift)), len(gift))
            self.assertFalse(set(gift) & set(self.real.starters))

    def test_every_gift_can_be_made_from_what_is_there_and_the_gifts_before_it(self) -> None:
        for seed in range(10):
            have = set(self.real.starters)
            for element in pick_gift(self.real, have, random.Random(seed), count=10):
                self.assertIn(element, {r for _, _, r in new_combinations(self.real, have)}, f"{element} has no found ingredients")
                have.add(element)

    def test_it_is_random_but_repeatable_with_a_seed(self) -> None:
        a = pick_gift(self.real, self.real.starters, random.Random(1))
        self.assertEqual(a, pick_gift(self.real, self.real.starters, random.Random(1)))
        self.assertNotEqual(a, pick_gift(self.real, self.real.starters, random.Random(2)))

    def test_a_nearly_finished_game_gets_what_is_left_and_a_finished_one_nothing(self) -> None:
        everything = set(self.real.elements)
        left = sorted(everything)[:3]
        self.assertEqual(sorted(pick_gift(self.real, everything - set(left) - {"time"}, random.Random(0), count=10)) != [], True)
        self.assertEqual(pick_gift(self.real, everything, random.Random(0)), [])


class NameTests(unittest.TestCase):
    def test_the_name_is_reserved_however_it_is_written(self) -> None:
        for name in ("Mr Alchemy", "mr alchemy", "MR_ALCHEMY", "mr-alchemy", "mr.alchemy", "  Mr   Alchemy ", "mralchemy"):
            self.assertTrue(is_reserved(name), name)
        for name in ("Mr Alchemist", "Alchemy", "Mr", "", "mister alchemy"):
            self.assertFalse(is_reserved(name), name)
        self.assertTrue(is_reserved("Pip42", extra=["pip 42"]))


def room(policy: VisitorPolicy | None = SOON, clock: Any = None, players: int = 2, seed: int = 1) -> tuple[GameState, list[Any], list[Any]]:
    game = GameState(load_recipes(RECIPES), rng=random.Random(seed), visitors=policy, clock=clock or (lambda: 0.0))
    joined = [game.join(f"Player{i}") for i in range(players)]
    subs = [game.subscribe(p.token) for p in joined]
    for sub in subs:
        assert sub
        drain(sub)
    return game, joined, subs


class VisitTests(unittest.TestCase):
    def test_a_visit_unlocks_a_gift_for_the_whole_room_in_one_event(self) -> None:
        game, players, subs = room()
        before = set(game.discovered())
        announced = game.visit()
        assert announced
        gift = [e["id"] for e in announced["elements"]]
        self.assertTrue(1 <= len(gift) <= GIFT_MAX)
        self.assertFalse(set(gift) & before)
        self.assertEqual(set(game.discovered()), before | set(gift))
        self.assertTrue(all(game.discovered()[e] == MR_ALCHEMY for e in gift))
        for sub in subs:
            events = [(n, d) for n, d in drain(sub) if n in ("visitor", "discovered")]
            self.assertEqual([n for n, _ in events if n == "visitor"], ["visitor"])
            visitor = next(d for n, d in events if n == "visitor")
            self.assertEqual(visitor["name"], MR_ALCHEMY)
            self.assertEqual([e["id"] for e in visitor["elements"]], gift)
            self.assertEqual(visitor["found"], len(game.discovered()))
            self.assertEqual([n for n, _ in events if n == "discovered"], [])   # one cutscene, not a toast per element

    def test_he_comes_once_per_game(self) -> None:
        game, *_ = room()
        self.assertIsNotNone(game.visit())
        self.assertIsNone(game.visit())
        self.assertIsNotNone(game.visit(force=True))   # an administrator may send him again

    def test_not_to_a_game_with_fewer_than_two_people_unless_forced(self) -> None:
        game, *_ = room(players=1)
        self.assertIsNone(game.visit())
        self.assertIsNotNone(game.visit(force=True))

    def test_never_to_a_private_or_ended_game(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES), visitors=SOON)
        solo = registry.start_solo("Alice")
        solo.join("Alice")
        self.assertIsNone(solo.visit(force=True))
        self.assertFalse(solo.visitor_tick())
        game, *_ = room()
        game.close("Player0", False)
        self.assertIsNone(game.visit(force=True))

    def test_a_game_without_a_policy_is_never_visited(self) -> None:
        game, *_ = room(policy=None)
        self.assertFalse(game.visitor_tick())
        self.assertIsNotNone(game.visit(force=True))   # forcing is explicit; the schedule is what a missing policy turns off

    def test_what_he_gives_shows_in_the_journal_and_the_count(self) -> None:
        game, players, _ = room()
        announced = game.visit()
        assert announced
        journal = game.journal(players[0].token)
        gift = {e["id"] for e in announced["elements"]}
        self.assertEqual({e["id"] for e in journal["elements"] if e["by"] == MR_ALCHEMY}, gift)
        self.assertEqual(journal["progress"]["found"], len(game.discovered()))


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]

    def tick(self, game: GameState, seconds: float) -> bool:
        self.now[0] += seconds
        return game.visitor_tick()

    def test_he_comes_after_the_drawn_time_of_two_or_more_players_together(self) -> None:
        game, *_ = room(clock=lambda: self.now[0])
        self.tick(game, 0)   # starts the clock
        self.assertFalse(self.tick(game, 25))
        self.assertFalse(self.tick(game, 25))
        self.assertTrue(self.tick(game, 25))   # 75 s together; he was due at 60
        self.assertFalse(self.tick(game, 600))  # and only once

    def test_time_alone_does_not_count(self) -> None:
        game, players, subs = room(clock=lambda: self.now[0])
        self.tick(game, 0)
        game.disconnect(subs[1]) if hasattr(game, "disconnect") else game.unsubscribe(subs[1])
        for _ in range(10):
            self.assertFalse(self.tick(game, 20))   # 200 s with one person in the room
        self.assertEqual(game._together, 0.0)

    def test_a_long_gap_counts_as_at_most_thirty_seconds(self) -> None:
        game, *_ = room(policy=VisitorPolicy(min_seconds=300, max_seconds=300), clock=lambda: self.now[0])
        self.tick(game, 0)
        self.tick(game, 10_000)   # the server was paused
        self.assertLessEqual(game._together, 30.0)

    def test_the_time_is_drawn_between_the_limits_and_differs_from_game_to_game(self) -> None:
        drawn = set()
        for seed in range(8):
            game, *_ = room(policy=VisitorPolicy(min_seconds=240, max_seconds=900), clock=lambda: self.now[0], seed=seed)
            self.tick(game, 0)
            self.tick(game, 1)
            self.assertTrue(240 <= game._visit_after <= 900)  # type: ignore[operator]
            drawn.add(round(game._visit_after or 0))
        self.assertGreater(len(drawn), 4)

    def test_chance_zero_means_no_visit_and_a_disabled_policy_too(self) -> None:
        for policy in (VisitorPolicy(chance=0.0, min_seconds=1, max_seconds=1), VisitorPolicy(enabled=False, min_seconds=1, max_seconds=1)):
            game, *_ = room(policy=policy, clock=lambda: self.now[0])
            for _ in range(20):
                self.assertFalse(self.tick(game, 10))
            self.assertEqual(len([d for d in game.discovered().values() if d == MR_ALCHEMY]), 0)

    def test_a_hot_reload_does_not_bring_him_back(self) -> None:
        game, *_ = room(clock=lambda: self.now[0])
        game.visit()
        copy = GameState(load_recipes(RECIPES), rng=random.Random(2), visitors=SOON, clock=lambda: self.now[0])
        copy.live_restore(game.live_export())
        self.assertTrue(copy._visited)
        copy.join("A"), copy.join("B")
        self.assertFalse(copy.visitor_tick())


class FairnessTests(unittest.TestCase):
    def test_his_gifts_win_no_awards_and_are_not_the_first_find(self) -> None:
        game, players, _ = room()
        game.visit()
        self.assertEqual(game.awards(), [])
        a, b, _ = new_combinations(game.book, game.discovered())[0]   # something the gift did not already cover
        make(game, players[0].token, a, b)
        awards = {a["id"]: a for a in game.awards()}
        self.assertEqual(awards["first_find"]["player"], "Player0")
        self.assertNotIn(MR_ALCHEMY, {a["player"] for a in game.awards()})

    def test_he_is_not_a_player_in_saved_games_or_lists(self) -> None:
        game, players, _ = room()
        game.visit()
        self.assertNotIn(MR_ALCHEMY, game.export()["players"])
        self.assertNotIn(MR_ALCHEMY, [p["name"] for p in game._online_players()])


def make(game: GameState, token: str, a: str, b: str) -> None:
    first = game.add_tile(token, a, 0.2, 0.2)
    other = game.add_tile(token, b, 0.6, 0.6)
    game.grab_tile(token, first["id"])
    game.combine(token, other["id"], tile=first["id"], x=0.6, y=0.6)


class RegistryTests(unittest.TestCase):
    def test_the_registry_visits_due_public_games_and_gives_its_policy_to_new_games(self) -> None:
        now = [0.0]
        registry = GameRegistry(load_recipes(RECIPES), visitors=SOON, clock=lambda: now[0])
        game = registry.create("Lobby", "Player0")
        self.assertIs(game.visitors, SOON)
        for name in ("Player0", "Player1"):
            assert game.subscribe(game.join(name).token)
        registry.visitor_tick()
        now[0] += 25
        self.assertEqual(registry.visitor_tick(), [])
        now[0] += 25
        registry.visitor_tick()
        now[0] += 25
        self.assertEqual(registry.visitor_tick(), ["Lobby"])
        self.assertEqual(registry.visitor_tick(), [])

    def test_admin_visit(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        game = registry.create("Lobby", "Player0")
        game.join("Player0")
        gave = registry.admin_visit(game.slug)["gave"]
        self.assertTrue(gave)
        with self.assertRaises(GameError) as missing:
            registry.admin_visit("no-such-game")
        self.assertEqual(missing.exception.status, 404)
        solo = registry.start_solo("Alice")
        with self.assertRaises(GameError) as private:
            registry.admin_visit(solo.slug)
        self.assertEqual(private.exception.status, 409)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES)))
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.ann = login_as(cls.port, "Annie")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path: str, body: Any, session: str) -> tuple[int, Any]:
        return call(self.port, "POST", path, body, headers={"X-Session": session})

    def test_only_administrators_can_send_him(self) -> None:
        status, game = self.req("/games", {"name": "Visit room"}, self.ann)
        self.assertEqual(status, 200)
        self.assertEqual(self.req("/ops/games/visit", {"id": game["id"]}, self.ann)[0], 403)
        self.assertEqual(self.req("/ops/games/visit", {}, self.admin)[0], 400)
        self.assertEqual(self.req("/ops/games/visit", {"id": "nope"}, self.admin)[0], 404)
        self.req(f"/g/{game['id']}/join", {}, self.ann)
        status, data = self.req("/ops/games/visit", {"id": game["id"]}, self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["gave"])

    def test_nobody_can_register_as_him(self) -> None:
        for name in ("Mr Alchemy", "mr_alchemy", "MR-ALCHEMY"):
            status, body = call(self.port, "POST", "/register", {"username": name, "password": "a-long-enough-pass"}, headers={"X-Session": ""})
            self.assertEqual(status, 409, name)
            self.assertIn("taken", body["error"])
        status, _ = call(self.port, "POST", "/register", {"username": "Mr Alchemist", "password": "a-long-enough-pass"}, headers={"X-Session": ""})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()

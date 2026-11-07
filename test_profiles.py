"""Account-wide achievements: the counters, the store that keeps them, and the moments in a game that earn them."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from achievements import ACHIEVEMENTS, BADGES, COUNTERS, earned_badges
from erasure import erase_account
from games import GameRegistry
from hints import new_combinations
from profiles import MAX_COUNT, ProfileStore
from state import MAX_PLAYERS, Challenge, GameState, Race
from test_coop import RECIPES, call, drain, load_recipes, login_as, serve
from visitor import MR_ALCHEMY


class BadgeTests(unittest.TestCase):
    def test_ids_are_unique_across_every_achievement_and_every_scope_is_known(self) -> None:
        ids = [a.id for a in ACHIEVEMENTS] + [b.id for b in BADGES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({b.scope for b in BADGES}, {"multiplayer", "challenges"})
        self.assertEqual((sum(b.scope == "multiplayer" for b in BADGES), sum(b.scope == "challenges" for b in BADGES)), (11, 8))

    def test_nothing_is_earned_with_no_counters_and_everything_with_enough(self) -> None:
        self.assertEqual(earned_badges({}), set())
        everything = {key: 10_000 for key in COUNTERS}
        self.assertEqual(earned_badges(everything), {b.id for b in BADGES})

    def test_thresholds_are_where_the_descriptions_say(self) -> None:
        self.assertEqual(earned_badges({"games": 1}), {"mp_together"})
        self.assertIn("mp_games_10", earned_badges({"games": 10}))
        self.assertNotIn("mp_games_10", earned_badges({"games": 9}))
        self.assertIn("mp_finds_100", earned_badges({"finds": 100}))
        self.assertNotIn("ch_streak_7", earned_badges({"daily_streak": 6}))
        self.assertIn("ch_streak_7", earned_badges({"daily_streak": 7}))
        self.assertEqual(earned_badges({"race_wins": 5}) & {"ch_win", "ch_wins_5"}, {"ch_win", "ch_wins_5"})


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [5000.0]
        self.store = ProfileStore(None, clock=lambda: self.now[0])

    def test_counters_add_up_and_new_achievements_come_back_once(self) -> None:
        self.assertEqual(self.store.bump("Ann", {"games": 1}), ["mp_together"])
        self.assertEqual(self.store.bump("Ann", {"games": 1}), [])
        self.assertEqual(self.store.counters("Ann"), {"games": 2})
        self.assertEqual(self.store.achievements("Ann"), {"mp_together": 5000})
        self.assertEqual(self.store.achievements("Bob"), {})
        self.assertEqual(self.store.bump("ann", {"emotes": 1}), ["mp_emote"])   # accounts are not case sensitive

    def test_best_raises_a_counter_but_never_lowers_it(self) -> None:
        self.store.bump("Ann", best={"daily_streak": 4})
        self.store.bump("Ann", best={"daily_streak": 2})
        self.assertEqual(self.store.counters("Ann")["daily_streak"], 4)
        self.assertEqual(self.store.achievements("Ann").keys(), {"ch_streak_3"})

    def test_junk_is_ignored(self) -> None:
        self.store.bump("Ann", {"not_a_counter": 5, "games": -3, "finds": True, "emotes": "many"}, {"also_junk": 9})   # type: ignore[dict-item]
        self.assertEqual(self.store.counters("Ann"), {})
        self.store.bump("Ann", {"finds": 10 ** 9})
        self.assertEqual(self.store.counters("Ann")["finds"], MAX_COUNT)

    def test_deleting_an_account_removes_everything(self) -> None:
        self.store.bump("Ann", {"games": 3})
        self.assertEqual(self.store.delete_user("ANN"), 1)
        self.assertEqual((self.store.counters("Ann"), self.store.achievements("Ann")), ({}, {}))
        self.assertEqual(self.store.delete_user("Ann"), 0)


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "profiles.json"

    def test_counting_is_written_on_flush_not_on_every_change(self) -> None:
        store = ProfileStore(self.file)
        store.bump("Ann", {"finds": 12})
        self.assertFalse(self.file.exists())
        self.assertTrue(store.flush())
        self.assertFalse(store.flush(), "nothing changed since the last write")
        again = ProfileStore(self.file)
        self.assertEqual(again.counters("Ann"), {"finds": 12})
        self.assertIn("mp_finds_10", again.achievements("Ann"))

    def test_a_damaged_file_is_never_overwritten_and_the_store_still_works(self) -> None:
        self.file.write_text("{ this is not json", encoding="utf-8")
        store = ProfileStore(self.file)
        self.assertEqual(store.bump("Ann", {"games": 1}), ["mp_together"])
        self.assertFalse(store.flush())
        self.assertEqual(self.file.read_text(encoding="utf-8"), "{ this is not json")

    def test_the_file_holds_only_counters_and_achievements(self) -> None:
        store = ProfileStore(self.file)
        store.bump("Ann", {"games": 1})
        store.flush()
        data = json.loads(self.file.read_text(encoding="utf-8"))
        record = data["users"]["ann"]
        self.assertEqual(set(record), {"counters", "achievements"})


def public_room(clock: Any = None, people: int = 2, seed: int = 1) -> tuple[GameState, ProfileStore, list[Any], list[Any]]:
    store = ProfileStore(None)
    game = GameState(load_recipes(RECIPES), rng=random.Random(seed), clock=clock or (lambda: 0.0),
                     profile=lambda name, add, best: store.bump(name, add, best))
    joined = [game.join(f"Human{i}") for i in range(people)]
    subs = [game.subscribe(p.token) for p in joined]
    for sub in subs:
        assert sub
        drain(sub)
    return game, store, joined, subs


def make(game: GameState, token: str, a: str, b: str) -> dict[str, Any]:
    first = game.add_tile(token, a, 0.2, 0.2)
    other = game.add_tile(token, b, 0.6, 0.6)
    game.grab_tile(token, first["id"])
    return game.combine(token, other["id"], tile=first["id"], x=0.6, y=0.6)


class SharedGameTests(unittest.TestCase):
    def test_playing_with_others_counts_once_per_person_per_game(self) -> None:
        game, store, humans, subs = public_room(people=1)
        self.assertEqual(store.counters("Human0"), {})   # alone: nothing yet
        second = game.join("Human1")
        game.subscribe(second.token)
        self.assertEqual(store.counters("Human0").get("games"), 1)
        self.assertEqual(store.counters("Human1").get("games"), 1)
        game.subscribe(second.token)   # reconnecting is not another game
        self.assertEqual(store.counters("Human1").get("games"), 1)
        self.assertIn("mp_together", store.achievements("Human0"))

    def test_a_full_room_counts_once_each(self) -> None:
        game, store, humans, subs = public_room(people=MAX_PLAYERS)
        for i in range(MAX_PLAYERS):
            self.assertEqual(store.counters(f"Human{i}").get("full_room"), 1)
        self.assertIn("mp_full", store.achievements("Human0"))

    def test_finds_count_for_the_person_who_made_them(self) -> None:
        game, store, humans, subs = public_room()
        result = make(game, humans[0].token, "water", "fire")
        self.assertEqual(store.counters("Human0").get("finds"), len(result["new"]))
        self.assertIsNone(store.counters("Human1").get("finds"))

    def test_epic_finds_are_counted_separately(self) -> None:
        game, store, humans, _ = public_room()
        epic = next(e for e in game.book.elements if game._rarity(e) == "epic")
        game._record(humans[0], [epic])
        self.assertEqual(store.counters("Human0").get("epic_finds"), 1)
        self.assertIn("mp_epic", store.achievements("Human0"))

    def test_what_companions_and_mr_alchemy_find_is_nobodys(self) -> None:
        game, store, humans, _ = public_room(people=1)
        bot = game.add_virtual("Wulfstan")
        game._record(bot, ["steam"])
        self.assertEqual(store.counters("Wulfstan"), {})
        game2, store2, humans2, _ = public_room()
        game2.visit()
        self.assertEqual(store2.counters(MR_ALCHEMY), {})
        self.assertIsNone(store2.counters("Human0").get("finds"), "a gift is not a find")

    def test_being_there_when_mr_alchemy_visits_counts_for_people_only(self) -> None:
        game, store, humans, _ = public_room()
        game.add_virtual("Wulfstan")
        game.visit()
        self.assertEqual((store.counters("Human0").get("struck"), store.counters("Human1").get("struck")), (1, 1))
        self.assertEqual(store.counters("Wulfstan"), {})
        self.assertIn("mp_struck", store.achievements("Human1"))

    def test_emotes_count(self) -> None:
        now = [0.0]
        game, store, humans, _ = public_room(clock=lambda: now[0])
        game.emote(humans[0].token, "wave")
        self.assertEqual(store.counters("Human0").get("emotes"), 1)
        self.assertIn("mp_emote", store.achievements("Human0"))

    def test_only_the_player_who_earned_it_is_told(self) -> None:
        now = [0.0]
        game, store, humans, subs = public_room(clock=lambda: now[0])
        game.emote(humans[0].token, "wave")
        mine = [d for n, d in drain(subs[0]) if n == "achievement"]
        theirs = [d for n, d in drain(subs[1]) if n == "achievement"]
        self.assertEqual([a["id"] for a in mine], ["mp_emote"])
        self.assertEqual(theirs, [])

    def test_solo_games_count_nothing_here(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        solo = registry.start_solo("Alice")
        token = solo.join("Alice").token
        make(solo, token, "water", "fire")
        self.assertEqual(registry.profiles.counters("Alice"), {})

    def test_a_failing_counter_does_not_break_the_game(self) -> None:
        def broken(name: str, add: dict[str, int], best: dict[str, int]) -> list[str]:
            raise RuntimeError("disk full")
        game = GameState(load_recipes(RECIPES), rng=random.Random(1), profile=broken)
        a, b = game.join("A"), game.join("B")
        game.subscribe(a.token), game.subscribe(b.token)
        result = make(game, a.token, "water", "fire")
        self.assertTrue(result["new"])


class ChallengeTests(unittest.TestCase):
    def test_finishing_a_hard_daily_counts_with_its_streak(self) -> None:
        store = ProfileStore(None)
        book = load_recipes(RECIPES)
        challenge = Challenge("2026-11-07", "steam", 1, lambda name, score: None, "hard", lambda name: 4)
        game = GameState(book, rng=random.Random(1), challenge=challenge, creator="Dana", profile=lambda n, a, b: store.bump(n, a, b))
        player = game.join("Dana")
        sub = game.subscribe(player.token)
        assert sub
        drain(sub)
        make(game, player.token, "water", "fire")
        counters = store.counters("Dana")
        self.assertEqual((counters["daily_done"], counters["daily_hard"], counters["daily_streak"]), (1, 1, 4))
        self.assertEqual(set(store.achievements("Dana")), {"ch_daily", "ch_hard", "ch_streak_3"})
        self.assertEqual({d["id"] for n, d in drain(sub) if n == "achievement"}, {"ch_daily", "ch_hard", "ch_streak_3"})

    def test_an_easy_daily_is_not_a_hard_one(self) -> None:
        store = ProfileStore(None)
        challenge = Challenge("2026-11-07", "steam", 1, lambda name, score: None, "easy", lambda name: 1)
        game = GameState(load_recipes(RECIPES), rng=random.Random(1), challenge=challenge, creator="Dana",
                         profile=lambda n, a, b: store.bump(n, a, b))
        player = game.join("Dana")
        make(game, player.token, "water", "fire")
        self.assertIsNone(store.counters("Dana").get("daily_hard"))

    def test_a_race_finish_counts_once_and_a_win_counts_as_a_win(self) -> None:
        store = ProfileStore(None)
        race = Race("r1", "count", None, 5, lambda count, reached: reached, lambda: True)
        game = GameState(load_recipes(RECIPES), rng=random.Random(1), race=race, creator="Rae", profile=lambda n, a, b: store.bump(n, a, b))
        player = game.join("Rae")
        make(game, player.token, "water", "fire")
        self.assertEqual((store.counters("Rae").get("race_done"), store.counters("Rae").get("race_wins")), (1, 1))
        a, b, _ = new_combinations(game.book, game.discovered())[0]
        make(game, player.token, a, b)
        self.assertEqual(store.counters("Rae").get("race_done"), 1, "the finish is counted once")
        self.assertEqual(set(store.achievements("Rae")), {"ch_race", "ch_win"})

    def test_losing_a_race_still_counts_as_finishing_it(self) -> None:
        store = ProfileStore(None)
        race = Race("r1", "count", None, 5, lambda count, reached: False, lambda: True)
        game = GameState(load_recipes(RECIPES), rng=random.Random(1), race=race, creator="Rae", profile=lambda n, a, b: store.bump(n, a, b))
        player = game.join("Rae")
        make(game, player.token, "water", "fire")
        self.assertEqual((store.counters("Rae").get("race_done"), store.counters("Rae").get("race_wins")), (1, None))


class RegistryAndHttpTests(unittest.TestCase):
    def test_a_registry_game_counts_for_both_players(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        game = registry.create("Lobby", "Ann")
        for name in ("Ann", "Bob"):
            assert game.subscribe(game.join(name).token)
        self.assertIn("mp_together", registry.profiles.achievements("Ann"))
        self.assertIn("mp_together", registry.profiles.achievements("Bob"))

    def test_the_achievements_endpoint_and_erasure(self) -> None:
        registry = GameRegistry(load_recipes(RECIPES))
        server, port = serve(registry)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        session = login_as(port, "Ann")
        self.assertEqual(call(port, "GET", "/achievements", headers={"X-Session": ""})[0], 401)
        registry.profiles.bump("Ann", {"games": 1})
        status, data = call(port, "GET", "/achievements", headers={"X-Session": session})
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"solo", "multiplayer", "challenges"})
        self.assertEqual(len(data["solo"]), len(ACHIEVEMENTS))
        self.assertEqual({a["id"] for a in data["multiplayer"] if a["earned_at"]}, {"mp_together"})
        self.assertTrue(all(a["earned_at"] is None for a in data["challenges"]))
        summary = erase_account("Ann", accounts=server.accounts, saves=registry.saves, drafts=server.drafts, daily=registry.daily,
                                solo=registry.solo, profiles=registry.profiles)
        self.assertEqual(summary.profile, 1)
        self.assertEqual(registry.profiles.counters("Ann"), {})


class ProfileViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [10_000.0]
        self.registry = GameRegistry(load_recipes(RECIPES), profiles=ProfileStore(None, clock=lambda: self.now[0]))
        self.server, self.port = serve(self.registry)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.ann = login_as(self.port, "Ann")

    def profile(self, session: str | None = None) -> tuple[int, Any]:
        return call(self.port, "GET", "/profile", headers={"X-Session": self.ann if session is None else session})

    def test_a_login_is_needed(self) -> None:
        self.assertEqual(self.profile("")[0], 401)

    def test_a_new_account_starts_at_zero_with_every_achievement_still_to_find(self) -> None:
        status, data = self.profile()
        self.assertEqual(status, 200)
        self.assertEqual(data["username"], "Ann")
        self.assertIsInstance(data["member_since"], int)
        self.assertTrue(all(value == 0 for value in data["stats"].values()), data["stats"])
        self.assertEqual(data["achievements"], {"earned": 0, "total": len(ACHIEVEMENTS) + len(BADGES), "recent": []})
        self.assertEqual(data["solo"], {"found": 0, "total": 0, "complete": False})

    def test_it_reports_the_accounts_own_counters_solo_progress_and_latest_achievements(self) -> None:
        profiles = self.registry.profiles
        profiles.bump("Ann", {"finds": 12, "games": 3, "epic_finds": 1, "emotes": 2, "struck": 1, "daily_done": 2, "race_done": 4, "race_wins": 1},
                      {"daily_streak": 5})
        self.now[0] += 100
        self.registry.solo.record("Ann", {}, 40, 830, False, {"found_10"})
        data = self.profile()[1]
        stats = data["stats"]
        self.assertEqual((stats["shared_finds"], stats["shared_games"], stats["epic_finds"], stats["emotes"], stats["struck"]), (12, 3, 1, 2, 1))
        self.assertEqual((stats["daily_done"], stats["best_streak"], stats["races_done"], stats["race_wins"]), (2, 5, 4, 1))
        self.assertEqual((data["solo"]["found"], data["solo"]["total"]), (40, 830))
        achievements = data["achievements"]
        self.assertEqual(achievements["earned"], len(set(profiles.achievements("Ann")) | set(self.registry.solo.achievements("Ann"))))
        self.assertEqual(len(achievements["recent"]), 3)
        times = [a["earned_at"] for a in achievements["recent"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(achievements["recent"][0]["id"], "found_10")   # the newest, earned last
        self.assertEqual(set(achievements["recent"][0]), {"id", "icon", "title", "description", "earned_at"})

    def test_it_shows_nobody_elses_numbers(self) -> None:
        self.registry.profiles.bump("Bob", {"finds": 500, "games": 60})
        bob = login_as(self.port, "Bob")
        self.assertEqual(self.profile()[1]["stats"]["shared_finds"], 0)
        self.assertEqual(self.profile(bob)[1]["stats"]["shared_finds"], 500)
        self.assertNotIn("Bob", json.dumps(self.profile()[1]))

    def test_the_account_knows_when_it_was_made(self) -> None:
        self.assertIsNotNone(self.server.accounts.created_at("Ann"))
        self.assertIsNone(self.server.accounts.created_at("nobody-here"))


if __name__ == "__main__":
    unittest.main()

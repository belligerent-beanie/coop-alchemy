"""Discovery rarity, the end-of-game awards, emotes, solo achievements and the solo hint of the day."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from achievements import ACHIEVEMENTS, Progress, earned_ids
from emotes import EMOTE_IDS, EMOTES
from games import GameRegistry
from solo import SoloStore
from state import GameError, GameState
from test_coop import RECIPES, drain, load_recipes, names
from test_solo import find_everything

RARITIES = {"common", "uncommon", "rare", "epic"}


def drain_closed(sub: Any) -> list[tuple[str, Any]]:
    """drain() for a stream that was closed: the end-of-stream marker (None) is skipped."""
    from test_coop import parse_event
    out = []
    while not sub.messages.empty():
        message = sub.messages.get_nowait()
        if message is not None:
            out.append(parse_event(message))
    return out


def make(game: GameState, token: str, a: str, b: str) -> dict[str, Any]:
    first = game.add_tile(token, a, 0.2, 0.2)
    other = game.add_tile(token, b, 0.6, 0.6)
    game.grab_tile(token, first["id"])
    return game.combine(token, other["id"], tile=first["id"], x=0.6, y=0.6)


class RarityTests(unittest.TestCase):
    def test_starters_are_common_and_every_found_element_has_a_rarity(self) -> None:
        game = GameState(load_recipes(RECIPES), rng=random.Random(1))
        player = game.join("Alice")
        make(game, player.token, "water", "fire")
        entries = game.journal(player.token)["elements"]
        self.assertTrue(all(e["rarity"] in RARITIES for e in entries))
        self.assertTrue(all(e["rarity"] == "common" for e in entries if e["starter"]))

    def test_the_deepest_real_elements_are_epic_and_the_starters_common(self) -> None:
        book = load_recipes(Path(__file__).resolve().parent / "recipes.json")
        game = GameState(book, rng=random.Random(1))
        deepest = max(game._depth, key=lambda e: game._depth[e])
        self.assertEqual(game._rarity(deepest), "epic")
        self.assertEqual({game._rarity(s) for s in book.starters}, {"common"})
        counts = {r: sum(1 for e in game._depth if game._rarity(e) == r) for r in RARITIES}
        self.assertTrue(all(counts[r] > 0 for r in RARITIES), counts)
        self.assertLess(counts["epic"], counts["common"])


class AwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.game = GameState(load_recipes(RECIPES), rng=random.Random(2))
        self.alice = self.game.join("Alice")
        self.bob = self.game.join("Bob")
        self.sub = self.game.subscribe(self.alice.token)
        assert self.sub
        drain(self.sub)

    def ids(self) -> dict[str, dict[str, Any]]:
        return {a["id"]: a for a in self.game.awards()}

    def test_a_game_with_no_discoveries_has_no_awards(self) -> None:
        self.assertEqual(self.game.awards(), [])

    def test_most_found_names_the_leader_and_needs_two_players(self) -> None:
        make(self.game, self.alice.token, "water", "fire")     # steam
        make(self.game, self.alice.token, "water", "water")    # puddle
        make(self.game, self.bob.token, "earth", "water")      # mud
        awards = self.ids()
        self.assertEqual(awards["most_found"]["player"], "Alice")
        self.assertEqual(awards["most_found"]["detail"], "2 elements")
        self.assertEqual(awards["first_find"]["player"], "Alice")
        alone = GameState(load_recipes(RECIPES), rng=random.Random(2))
        solo_player = alone.join("Zed")
        make(alone, solo_player.token, "water", "fire")
        self.assertNotIn("most_found", {a["id"] for a in alone.awards()})

    def test_longest_chain_and_rarest_find_are_different_elements(self) -> None:
        for a, b in (("water", "fire"), ("water", "water"), ("earth", "water"), ("fire", "earth"), ("air", "fire")):
            make(self.game, self.alice.token, a, b)
        awards = self.ids()
        if "longest_chain" in awards and "rarest_find" in awards:
            self.assertNotEqual(awards["longest_chain"]["detail"], awards["rarest_find"]["detail"])
        self.assertIn("longest_chain", awards)
        self.assertTrue(awards["longest_chain"]["detail"].endswith("steps from the starting elements"))

    def test_never_gives_up_needs_five_misses(self) -> None:
        make(self.game, self.bob.token, "water", "fire")
        for _ in range(4):
            make(self.game, self.bob.token, "air", "air")
        self.assertNotIn("persistent", self.ids())
        make(self.game, self.bob.token, "air", "air")
        self.assertEqual(self.ids()["persistent"]["player"], "Bob")

    def test_the_ended_event_carries_the_awards_but_not_for_private_games(self) -> None:
        make(self.game, self.alice.token, "water", "fire")
        self.game.close("Alice", False)
        ended = [data for name, data in drain_closed(self.sub) if name == "ended"][0]
        self.assertIn("first_find", {a["id"] for a in ended["awards"]})
        registry = GameRegistry(load_recipes(RECIPES), solo=SoloStore(None))
        solo = registry.start_solo("Alice")
        token = solo.join("Alice").token
        events = solo.subscribe(token)
        assert events
        make(solo, token, "water", "fire")
        drain(events)
        solo.close("Alice", False)
        self.assertEqual([data["awards"] for name, data in drain_closed(events) if name == "ended"], [[]])

    def test_awards_survive_a_hot_reload(self) -> None:
        make(self.game, self.alice.token, "water", "fire")
        for _ in range(5):
            make(self.game, self.bob.token, "air", "air")
        before = self.game.awards()
        copy = GameState(load_recipes(RECIPES), rng=random.Random(2))
        copy.live_restore(self.game.live_export())
        self.assertEqual(copy.awards(), before)


class EmoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.game = GameState(load_recipes(RECIPES), clock=lambda: self.now[0])
        self.alice = self.game.join("Alice")
        self.bob = self.game.join("Bob")
        self.sub_b = self.game.subscribe(self.bob.token)
        assert self.sub_b
        drain(self.sub_b)

    def test_everyone_gets_the_emote_with_the_senders_name(self) -> None:
        self.game.emote(self.alice.token, "wave")
        events = drain(self.sub_b)
        self.assertEqual(names(events), ["emote"])
        self.assertEqual(events[0][1], {"pid": self.alice.pid, "name": "Alice", "emote": "wave"})

    def test_only_emotes_from_the_list_work(self) -> None:
        for bad in ("hello there", "", None, 5, "WAVE", ["wave"]):
            with self.assertRaises(GameError) as caught:
                self.game.emote(self.alice.token, bad)
            self.assertEqual(caught.exception.status, 400, bad)
        self.assertEqual(drain(self.sub_b), [])

    def test_one_a_second_per_player(self) -> None:
        self.game.emote(self.alice.token, "wave")
        with self.assertRaises(GameError) as caught:
            self.game.emote(self.alice.token, "cheer")
        self.assertEqual(caught.exception.status, 429)
        self.game.emote(self.bob.token, "cheer")     # a different player is not held up
        self.now[0] += 1.1
        self.game.emote(self.alice.token, "cheer")

    def test_needs_a_player_and_a_shared_game(self) -> None:
        with self.assertRaises(GameError):
            self.game.emote("not-a-token", "wave")
        solo = GameRegistry(load_recipes(RECIPES), solo=SoloStore(None)).start_solo("Alice")
        with self.assertRaises(GameError) as caught:
            solo.emote(solo.join("Alice").token, "wave")
        self.assertEqual(caught.exception.status, 403)

    def test_the_list_is_well_formed(self) -> None:
        self.assertEqual(len(EMOTE_IDS), len(EMOTES))
        for e in EMOTES:
            self.assertEqual(set(e), {"id", "label", "glyph"})
            self.assertTrue(e["glyph"])


class AchievementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "solo.json"
        self.book = load_recipes(RECIPES)

    def registry(self) -> GameRegistry:
        return GameRegistry(self.book, solo=SoloStore(self.file))

    def test_ids_are_unique_and_the_rules_follow_progress(self) -> None:
        self.assertEqual(len({a.id for a in ACHIEVEMENTS}), len(ACHIEVEMENTS))
        nothing = Progress(discovered=0, complete=False, deepest=0, djinn=False, wishes=0, time=False, hints=0)
        self.assertEqual(earned_ids(nothing), set())
        lots = Progress(discovered=300, complete=False, deepest=9, djinn=True, wishes=0, time=False, hints=1)
        self.assertEqual(earned_ids(lots), {"found_10", "found_50", "found_100", "found_250", "deep_8", "djinn", "hint"})

    def test_playing_earns_them_announces_them_and_keeps_them(self) -> None:
        registry = self.registry()
        game = registry.start_solo("Alice")
        token = game.join("Alice").token
        events = game.subscribe(token)
        assert events
        drain(events)
        find_everything(game, token)
        announced = [data["id"] for name, data in drain(events) if name == "achievement"]
        self.assertIn("found_10", announced)
        self.assertIn("complete", announced)
        self.assertEqual(len(announced), len(set(announced)))            # each is announced once
        have = registry.solo.achievements("Alice")
        self.assertTrue({"found_10", "complete"} <= set(have))
        # carrying on later announces nothing again, and Bob has none of Alice's
        again = self.registry().start_solo("Alice")
        events = again.subscribe(again.join("Alice").token)
        assert events
        again.report_solo()
        self.assertEqual([n for n, _ in drain(events) if n == "achievement"], [])
        self.assertEqual(registry.solo.achievements("Bob"), {})


class SoloHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "solo.json"
        self.book = load_recipes(RECIPES)
        self.day = ["2026-11-07"]

    def start(self) -> tuple[GameState, str]:
        game = GameRegistry(self.book, solo=SoloStore(self.file)).start_solo("Alice")
        game._today = lambda: self.day[0]
        return game, game.join("Alice").token

    def test_one_hint_a_day_and_a_new_one_tomorrow(self) -> None:
        game, token = self.start()
        self.assertEqual(game._hints_left(), 1)
        first = game.hint(token, "can_make")
        self.assertEqual(first["hints_left"], 0)
        self.assertNotIn("useful", first)
        with self.assertRaises(GameError) as caught:
            game.hint(token, "try")
        self.assertEqual(caught.exception.status, 429)
        self.day[0] = "2026-11-08"
        self.assertEqual(game.hint(token, "try")["hints_left"], 0)

    def test_a_hint_that_found_nothing_is_not_used_up(self) -> None:
        game, token = self.start()
        with self.assertRaises(GameError) as caught:
            game.hint(token, "find", "unicorn")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(game.hint(token, "find", "water")["hints_left"], 1)   # a starting element: nothing to tell
        self.assertEqual(game._hints_left(), 1)
        self.assertEqual(game.hint(token, "can_make")["hints_left"], 0)

    def test_the_day_is_remembered_across_a_restart_and_earns_an_achievement(self) -> None:
        game, token = self.start()
        game.hint(token, "can_make")
        again, token = self.start()
        with self.assertRaises(GameError):
            again.hint(token, "can_make")
        self.assertIn("hint", SoloStore(self.file).achievements("Alice"))

    def test_shared_games_have_unlimited_hints_and_no_counter(self) -> None:
        game = GameState(self.book, rng=random.Random(1))
        token = game.join("Alice").token
        for _ in range(5):
            answer = game.hint(token, "can_make")
            self.assertNotIn("hints_left", answer)
            self.assertNotIn("useful", answer)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from test_coop import login_as, serve
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES)))
        cls.alice = login_as(cls.port, "Alice")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method: str, path: str, body: Any = None, session: str | None = None) -> tuple[int, Any]:
        from test_coop import call
        return call(self.port, method, path, body, headers={"X-Session": self.alice if session is None else session})

    def test_the_emote_list_needs_a_login_and_lists_every_emote(self) -> None:
        self.assertEqual(self.req("GET", "/emotes", session="")[0], 401)
        status, data = self.req("GET", "/emotes")
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in data["emotes"]], [e["id"] for e in EMOTES])

    def test_sending_an_emote_over_http(self) -> None:
        status, game = self.req("POST", "/games", {"name": "Emote room"})
        self.assertEqual(status, 200, game)
        slug = game["id"]
        status, joined = self.req("POST", f"/g/{slug}/join", {})
        self.assertEqual(status, 200)
        token = joined["token"]
        self.assertEqual(self.req("POST", f"/g/{slug}/emote", {"player": token, "emote": "wave"}), (200, {}))
        self.assertEqual(self.req("POST", f"/g/{slug}/emote", {"player": token, "emote": "wave"})[0], 429)
        self.assertEqual(self.req("POST", f"/g/{slug}/emote", {"player": token, "emote": "shout something rude"})[0], 400)
        self.assertEqual(self.req("POST", f"/g/{slug}/emote", {"emote": "wave"})[0], 400)   # no player token

    def test_solo_status_lists_every_achievement(self) -> None:
        status, data = self.req("GET", "/solo")
        self.assertEqual(status, 200)
        self.assertEqual([a["id"] for a in data["achievements"]], [a.id for a in ACHIEVEMENTS])
        self.assertTrue(all(a["earned_at"] is None for a in data["achievements"]))


if __name__ == "__main__":
    unittest.main()

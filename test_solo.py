"""Solo mode: a private game per account that remembers what it found, and the Create Recipes lock it opens."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from games import GameRegistry
from solo import SoloStore
from state import GameError, GameState
from test_coop import RECIPES, call, load_recipes, login_as, serve


def find_everything(game: GameState, token: str) -> None:
    """Play a solo game to the end: combine every pair that has a recipe until nothing new turns up."""
    progress = True
    while progress:
        progress = False
        for (a, b), results in list(game.book.lookup.items()):
            have = game.discovered()
            if a in have and b in have and any(r not in have for r in results):
                first = game.add_tile(token, a, 0.2, 0.2)
                other = game.add_tile(token, b, 0.6, 0.6)
                game.grab_tile(token, first["id"])
                game.combine(token, other["id"], tile=first["id"], x=0.6, y=0.6)
                progress = True


class SoloGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "solo.json"
        self.book = load_recipes(RECIPES)
        self.registry = GameRegistry(self.book, solo=SoloStore(self.file), max_games=1)

    def test_a_solo_game_is_private_and_only_its_owner_can_join(self) -> None:
        game = self.registry.start_solo("Alice")
        self.assertTrue(game.private)
        self.assertIs(self.registry.start_solo("alice"), game)   # one per account, whatever the capitals
        game.join("Alice")
        with self.assertRaises(GameError) as caught:
            game.join("Bob")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual([g["id"] for g in self.registry.list("Bob")], [])
        self.assertEqual([g["id"] for g in self.registry.list("Alice")], [game.slug])

    def test_solo_games_do_not_use_up_the_public_game_slots(self) -> None:
        self.registry.start_solo("Alice")
        self.registry.start_solo("Bob")
        self.registry.create("A public room", "Carol")   # max_games is 1 and both solo games are running

    def test_hints_work_in_solo(self) -> None:
        game = self.registry.start_solo("Alice")
        player = game.join("Alice")
        self.assertEqual(game.hint(player.token, "can_make")["kind"], "can_make")

    def test_progress_is_saved_and_carried_on(self) -> None:
        game = self.registry.start_solo("Alice")
        player = game.join("Alice")
        first = game.add_tile(player.token, "water", 0.2, 0.2)
        other = game.add_tile(player.token, "fire", 0.6, 0.6)
        game.grab_tile(player.token, first["id"])
        game.combine(player.token, other["id"], tile=first["id"], x=0.6, y=0.6)
        self.assertIn("steam", game.discovered())
        # a brand new registry (the server restarted) reads the file
        again = GameRegistry(self.book, solo=SoloStore(self.file)).start_solo("Alice")
        self.assertIn("steam", again.discovered())
        self.assertEqual(again.discovered()["steam"], "Alice")
        status = self.registry.solo_status("Alice")
        self.assertEqual((status["found"], status["complete"]), (5, False))

    def test_finding_everything_completes_solo_for_good(self) -> None:
        game = self.registry.start_solo("Alice")
        player = game.join("Alice")
        self.assertFalse(self.registry.solo.is_complete("Alice"))
        find_everything(game, player.token)
        status = self.registry.solo_status("Alice")
        self.assertTrue(status["complete"])
        self.assertEqual(status["found"], status["total"])
        self.assertFalse(self.registry.solo.is_complete("Bob"))
        self.assertTrue(SoloStore(self.file).is_complete("Alice"))   # kept in the file
        # it stays complete even if the game is started again from a smaller save
        self.registry.solo.record("Alice", {"discovered": {}}, 4, status["total"], False)
        self.assertTrue(self.registry.solo.is_complete("Alice"))

    def test_ending_a_solo_game_never_writes_a_save_file(self) -> None:
        game = self.registry.start_solo("Alice")
        player = game.join("Alice")
        self.assertEqual(self.registry.end(game.slug, player.token, True), {"saved": False})
        self.assertEqual(self.registry.find_saved("Alice"), [])

    def test_deleting_solo_progress_removes_the_account_record(self) -> None:
        self.registry.start_solo("Alice")
        self.assertEqual(self.registry.solo.delete_user("Alice"), 1)
        self.assertEqual(self.registry.solo.delete_user("Alice"), 0)
        self.assertEqual(self.registry.solo_status("Alice"), {"found": 0, "total": 0, "complete": False})

    def test_a_solo_game_survives_a_hot_reload(self) -> None:
        game = self.registry.start_solo("Alice")
        game.join("Alice")
        items = self.registry.live_export()
        fresh = GameRegistry(self.book, solo=SoloStore(self.file))
        self.assertEqual(fresh.live_restore(items), 1)
        restored = fresh.get(game.slug)
        assert restored is not None
        self.assertTrue(restored.private)
        self.assertIs(fresh.start_solo("Alice"), restored)


class FreeElementTests(unittest.TestCase):
    def test_elements_that_need_time_count_towards_the_total(self) -> None:
        """Time is handed out free at 100 elements, and ruins and archeologist are made from it: they must be findable."""
        book = load_recipes(Path(__file__).resolve().parent / "recipes.json")
        game = GameState(book)
        self.assertEqual(game.total, len(book.elements))
        self.assertTrue({"time", "ruins", "archeologist"} <= game._collectable())
        # a challenge or a race has no Time, so it neither counts Time nor what needs it
        self.assertLess(GameState(book, challenge=object()).total, game.total)  # type: ignore[arg-type]


class SoloHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES)))
        cls.alice = login_as(cls.port, "Alice")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        return call(self.port, method, path, body, headers={"X-Session": self.alice})

    def test_status_start_and_the_create_lock(self) -> None:
        status, data = self.req("GET", "/solo")
        self.assertEqual(status, 200)
        self.assertEqual((data["complete"], data["may_create"]), (False, False))
        self.assertEqual(self.req("GET", "/create/data")[1]["locked"], True)
        status, started = self.req("POST", "/solo/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(started["url"], f"/g/{started['id']}")
        self.assertEqual(self.req("POST", "/solo/start", {})[1]["id"], started["id"])
        joined = self.req("POST", f"/g/{started['id']}/join", {})
        self.assertEqual(joined[0], 200)
        # finishing solo opens Create
        self.server.registry.solo.record("Alice", {}, 4, 4, True)
        self.assertTrue(self.req("GET", "/solo")[1]["may_create"])
        self.assertIn("elements", self.req("GET", "/create/data")[1])

    def test_login_is_needed(self) -> None:
        self.assertEqual(call(self.port, "GET", "/solo", headers={"X-Session": ""})[0], 401)
        self.assertEqual(call(self.port, "POST", "/solo/start", {}, headers={"X-Session": ""})[0], 401)


if __name__ == "__main__":
    unittest.main()

"""Tests for the daily challenge: target picking, the score board, and challenge games (daily.py and the /daily routes).

They use the bundled dummy test_recipes.json. In it, water + fire = steam and air + steam = cloud,
so "cloud" is two steps from the starters and needs 2 elements (steam and cloud).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from daily import KEEP_DAYS, DailyStore, pick_target, reference_sizes
from games import GameRegistry
from recipes import depths, load_recipes
from state import GameError, GameState
from test_coop import call, drain, login_as, names, parse_event, serve  # noqa: F401  (shared helpers)

BASE_DIR = Path(__file__).resolve().parent
BOOK = load_recipes(BASE_DIR / "test_recipes.json")
DAY = "2026-11-04"


def seeded_store(path: Path | None = None, target: str = "cloud", reference: int = 2,
                 day: str = DAY) -> DailyStore:
    """A store whose target for DAY is fixed, so the tests know the route."""
    store = DailyStore(path, BOOK, today=lambda: day, clock=iter(range(1, 1000)).__next__)
    store.today()
    data = store._load()
    data["days"][day].update(target=target, reference=reference)
    store._save(data)
    return store


class DailyStoreTests(unittest.TestCase):
    def test_the_pick_is_reproducible_and_can_be_made(self) -> None:
        first = pick_target(BOOK, DAY)
        self.assertEqual(first, pick_target(BOOK, DAY))
        target, reference = first
        self.assertNotIn(target, BOOK.starters)
        self.assertIn(target, depths(BOOK))
        self.assertGreaterEqual(reference, depths(BOOK)[target])  # a route is never shorter than its longest chain
        picks = {pick_target(BOOK, f"2026-12-{d:02d}")[0] for d in range(1, 29)}
        self.assertGreater(len(picks), 1)  # different days give different targets

    def test_reference_sizes(self) -> None:
        sizes = reference_sizes(BOOK, depths(BOOK))
        self.assertEqual((sizes["steam"], sizes["cloud"]), (1, 2))
        self.assertNotIn("water", sizes)  # starters have no route
        for element, size in sizes.items():
            self.assertGreaterEqual(size, depths(BOOK)[element])

    def test_the_target_is_chosen_once_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            store = DailyStore(path, BOOK, today=lambda: DAY)
            first = store.today()
            self.assertEqual(set(first), {"date", "target", "reference"})
            self.assertEqual(first["date"], DAY)
            data = json.loads(path.read_text())
            data["days"][DAY]["target"] = "steam"          # edited by hand, as if picked earlier
            path.write_text(json.dumps(data))
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).today()["target"], "steam")

    def test_a_target_that_no_longer_exists_is_replaced_and_scores_kept(self) -> None:
        store = seeded_store(target="cloud")
        store.submit(DAY, "Ann", 3)
        data = store._load()
        data["days"][DAY]["target"] = "gone"
        store._save(data)
        self.assertNotEqual(store.today()["target"], "gone")
        self.assertEqual(store.best(DAY, "Ann"), 3)

    def test_only_the_best_score_counts_and_ties_go_to_the_earlier_finisher(self) -> None:
        store = seeded_store()
        self.assertTrue(store.submit(DAY, "Ann", 5))
        self.assertFalse(store.submit(DAY, "ann", 6))   # worse, and the same account whatever the capitals
        self.assertFalse(store.submit(DAY, "Ann", 5))   # equal is not an improvement
        self.assertTrue(store.submit(DAY, "Bob", 4))
        self.assertTrue(store.submit(DAY, "Cy", 5))
        self.assertTrue(store.submit(DAY, "Ann", 4))
        self.assertEqual([(r["name"], r["score"]) for r in store.board(DAY)],
                         [("Bob", 4), ("Ann", 4), ("Cy", 5)])
        self.assertEqual(store.best(DAY, "ANN"), 4)
        self.assertIsNone(store.best(DAY, "Nobody"))
        self.assertEqual(len(store.board(DAY, limit=2)), 2)
        self.assertEqual(store.board("2000-01-01"), [])
        self.assertFalse(store.submit("2000-01-01", "Ann", 1))  # a day that was never opened

    def test_scores_survive_a_restart_and_old_days_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            store = seeded_store(path)
            store.submit(DAY, "Ann", 3)
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).board(DAY), [{"name": "Ann", "score": 3}])
            for n in range(KEEP_DAYS + 5):
                DailyStore(path, BOOK, today=lambda n=n: f"2027-01-{n + 1:02d}" if n < 28 else f"2027-02-{n - 27:02d}").today()
            self.assertLessEqual(len(json.loads(path.read_text())["days"]), KEEP_DAYS)

    def test_a_damaged_file_is_reported_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            path.write_text("{not json")
            with self.assertRaises(ValueError):
                DailyStore(path, BOOK, today=lambda: DAY).today()
            self.assertEqual(path.read_text(), "{not json")
            registry = GameRegistry(BOOK, daily=DailyStore(path, BOOK, today=lambda: DAY))
            with self.assertRaises(GameError) as err:
                registry.start_daily("Ann")
            self.assertEqual(err.exception.status, 500)


class ChallengeGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = seeded_store()
        self.registry = GameRegistry(BOOK, max_games=1, daily=self.store)
        self.game = self.registry.start_daily("Ann")
        self.ann = self.game.join("Ann")
        self.sub = self.game.subscribe(self.ann.token)
        assert self.sub
        drain(self.sub)

    def make(self, game: GameState, player: Any, a: str, b: str) -> dict[str, Any]:
        first = game.add_tile(player.token, a, 0.2, 0.2)
        second = game.add_tile(player.token, b, 0.6, 0.6)
        game.grab_tile(player.token, first["id"])
        return game.combine(player.token, second["id"], tile=first["id"], x=0.6, y=0.6)

    def test_finding_the_target_scores_the_elements_discovered(self) -> None:
        self.make(self.game, self.ann, "water", "fire")
        self.assertFalse(self.game.finished)
        self.assertEqual(self.store.board(DAY), [])
        self.make(self.game, self.ann, "air", "steam")
        self.assertTrue(self.game.finished)
        events = drain(self.sub)
        done = [data for name, data in events if name == "challenge_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["score"], done[0]["reference"], done[0]["target"]["id"]), (2, 2, "cloud"))
        self.assertEqual(names(events)[-1], "challenge_done")  # after the discovery announcements
        self.assertEqual(self.store.board(DAY), [{"name": "Ann", "score": 2}])

    def test_detours_cost_points(self) -> None:
        self.make(self.game, self.ann, "water", "earth")     # mud: not on the route
        self.make(self.game, self.ann, "water", "fire")
        self.make(self.game, self.ann, "air", "steam")
        self.assertEqual(self.store.best(DAY, "Ann"), 3)

    def test_nothing_more_can_be_combined_after_finishing(self) -> None:
        self.make(self.game, self.ann, "water", "fire")
        self.make(self.game, self.ann, "air", "steam")
        first = self.game.add_tile(self.ann.token, "water", 0.1, 0.1)
        second = self.game.add_tile(self.ann.token, "earth", 0.5, 0.5)
        self.game.grab_tile(self.ann.token, first["id"])
        with self.assertRaises(GameError) as err:
            self.game.combine(self.ann.token, second["id"], tile=first["id"])
        self.assertEqual(err.exception.status, 409)
        self.assertEqual(self.store.board(DAY), [{"name": "Ann", "score": 2}])

    def test_a_challenge_is_a_plain_puzzle(self) -> None:
        with self.assertRaises(GameError) as err:
            self.game.hint(self.ann.token, "can_make")
        self.assertEqual(err.exception.status, 403)
        with self.assertRaises(GameError):
            self.game.genie_use(self.ann.token, "water")
        snapshot = self.game._snapshot(self.ann)
        self.assertEqual(snapshot["challenge"], {"date": DAY, "target": {"id": "cloud", "label": "Cloud", "emoji": snapshot["challenge"]["target"]["emoji"]},
                                                 "reference": 2, "starters": 4, "done": None})
        self.assertEqual(snapshot["genie"], {"available": False})

    def test_it_is_private_and_not_a_normal_game(self) -> None:
        with self.assertRaises(GameError) as err:
            self.game.join("Bob")
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(self.registry.list(), [])                      # not in the list of running games
        self.assertEqual(self.registry.list("Bob"), [])
        mine = self.registry.list("ann")                                  # except for its owner, for the game page
        self.assertEqual([(g["id"], g["private"]) for g in mine], [(self.game.slug, True)])
        self.registry.create("Normal")                                    # and it does not use up the only slot
        with self.assertRaises(GameError):
            self.registry.create("One too many")
        with self.assertRaises(GameError) as err:
            self.registry.end(self.game.slug, self.ann.token, save=True)  # attempts are never saved
        self.assertEqual(err.exception.status, 400)
        self.assertEqual(self.registry.end(self.game.slug, self.ann.token, save=False), {"saved": False})

    def test_continue_or_start_over(self) -> None:
        self.assertIs(self.registry.start_daily("Ann"), self.game)       # the unfinished attempt is reused
        self.assertIs(self.registry.start_daily("ann"), self.game)
        other = self.registry.start_daily("Bob")
        self.assertIsNot(other, self.game)
        fresh = self.registry.start_daily("Ann", fresh=True)
        self.assertIsNot(fresh, self.game)
        self.assertIsNone(self.registry.get(self.game.slug))             # the old one is gone...
        messages = []
        while not self.sub.messages.empty():
            messages.append(self.sub.messages.get_nowait())
        self.assertEqual(messages[-1:], [None])                          # ...and its page is told: stream closed
        self.assertEqual(names([parse_event(m) for m in messages if m]), ["ended"])
        player = fresh.join("Ann")
        self.make(fresh, player, "water", "fire")
        self.make(fresh, player, "air", "steam")
        self.assertIsNot(self.registry.start_daily("Ann"), fresh)        # a finished attempt is not reused

    def test_status_for_the_home_page(self) -> None:
        status = self.registry.daily_status("Ann")
        self.assertEqual((status["date"], status["target"]["id"], status["reference"]), (DAY, "cloud", 2))
        self.assertEqual((status["board"], status["best"], status["running"]), ([], None, f"/g/{self.game.slug}"))
        self.make(self.game, self.ann, "water", "fire")
        self.make(self.game, self.ann, "air", "steam")
        status = self.registry.daily_status("Ann")
        self.assertEqual((status["best"], status["running"]), (2, None))  # finished: nothing to continue
        self.assertEqual(status["board"], [{"name": "Ann", "score": 2}])
        self.assertEqual(self.registry.daily_status("Bob")["best"], None)

    def test_a_reload_keeps_the_attempt_and_its_result(self) -> None:
        self.make(self.game, self.ann, "water", "fire")
        saved = json.loads(json.dumps(self.registry.live_export()))
        store = seeded_store()
        registry = GameRegistry(BOOK, daily=store)
        self.assertEqual(registry.live_restore(saved), 1)
        game = registry.get(self.game.slug)
        assert game is not None and game.challenge is not None
        self.assertEqual((game.challenge.target, game.challenge.reference, game.finished), ("cloud", 2, False))
        self.assertEqual(registry.list(), [])
        player = game.authorize(self.ann.token)                          # the same login token still works
        self.make(game, player, "air", "steam")
        self.assertEqual(store.board(DAY), [{"name": "Ann", "score": 2}])  # the restored game reports to the store
        again = GameRegistry(BOOK, daily=seeded_store())
        again.live_restore(json.loads(json.dumps(registry.live_export())))
        restored = again.get(self.game.slug)
        assert restored is not None
        self.assertTrue(restored.finished)

    def test_a_normal_game_is_unaffected(self) -> None:
        normal = self.registry.create("Normal")
        self.assertIsNone(normal.challenge)
        self.assertIsNone(normal._snapshot(normal.join("Zed"))["challenge"])
        self.assertFalse(normal.finished)


class DailyEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = seeded_store()
        cls.registry = GameRegistry(BOOK, max_games=50, daily=cls.store)
        cls.server, cls.port = serve(cls.registry)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def as_user(self, name: str) -> dict[str, str]:
        return {"X-Session": login_as(self.port, name)}

    def post(self, headers: dict[str, str], slug: str, token: str, path: str, **body: Any) -> tuple[int, Any]:
        return call(self.port, "POST", f"/g/{slug}{path}", {"player": token, **body}, headers=headers)

    def test_daily_status_and_a_full_attempt(self) -> None:
        ann = self.as_user("DailyAnn")
        status, data = call(self.port, "GET", "/daily", headers=ann)
        self.assertEqual(status, 200)
        self.assertEqual((data["date"], data["target"]["id"], data["reference"], data["best"], data["running"]),
                         (DAY, "cloud", 2, None, None))

        status, started = call(self.port, "POST", "/daily/start", {}, headers=ann)
        self.assertEqual(status, 200)
        slug = started["id"]
        self.assertEqual(started["url"], f"/g/{slug}")
        self.assertEqual(call(self.port, "POST", "/daily/start", {}, headers=ann)[1]["id"], slug)  # continue
        self.assertEqual(call(self.port, "GET", "/daily", headers=ann)[1]["running"], f"/g/{slug}")
        self.assertNotEqual(call(self.port, "POST", "/daily/start", {"fresh": True}, headers=ann)[1]["id"], slug)
        slug = call(self.port, "GET", "/daily", headers=ann)[1]["running"].split("/")[-1]

        listing = call(self.port, "GET", "/games", headers=ann)[1]["games"]
        self.assertEqual([g["id"] for g in listing if g.get("private")], [slug])   # only its owner sees it
        self.assertEqual([g for g in call(self.port, "GET", "/games", headers=self.as_user("DailyBob"))[1]["games"]
                          if g["id"] == slug], [])

        bob = self.as_user("DailyBob")
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/join", {}, headers=bob)[0], 403)  # someone else's attempt
        status, joined = call(self.port, "POST", f"/g/{slug}/join", {}, headers=ann)
        self.assertEqual(status, 200)
        token = joined["token"]
        self.assertEqual(self.post(ann, slug, token, "/hint", kind="can_make")[0], 403)

        def combine(a: str, b: str) -> tuple[int, Any]:
            first = self.post(ann, slug, token, "/tile/add", element=a, x=0.2, y=0.2)[1]
            second = self.post(ann, slug, token, "/tile/add", element=b, x=0.6, y=0.6)[1]
            self.post(ann, slug, token, "/tile/grab", tile=first["id"])
            return self.post(ann, slug, token, "/combine", a=first["id"], b=second["id"], x=0.6, y=0.6)

        self.assertEqual(combine("water", "fire")[0], 200)
        self.assertEqual(call(self.port, "GET", "/daily", headers=ann)[1]["board"], [])
        self.assertEqual(combine("air", "steam")[0], 200)
        status, data = call(self.port, "GET", "/daily", headers=ann)
        self.assertEqual((data["best"], data["running"], data["board"]), (2, None, [{"name": "DailyAnn", "score": 2}]))
        self.assertEqual(combine("water", "earth")[0], 409)  # the attempt is over
        # Everybody sees the same board.
        self.assertEqual(call(self.port, "GET", "/daily", headers=bob)[1]["board"], data["board"])
        self.assertEqual(call(self.port, "GET", "/daily", headers=bob)[1]["best"], None)

    def test_login_is_required(self) -> None:
        self.assertEqual(call(self.port, "GET", "/daily", headers={"X-Session": ""})[0], 401)
        self.assertEqual(call(self.port, "POST", "/daily/start", {}, headers={"X-Session": ""})[0], 401)


if __name__ == "__main__":
    unittest.main()

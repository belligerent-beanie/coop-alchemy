"""Tests for the daily challenge: target picking, routes, scores, streaks, history, the weekly board, and challenge games.

They use the bundled dummy test_recipes.json. In it water + fire = steam, air + steam = cloud, cloud + water = rain and
cloud + rain = storm, so "steam", "cloud" and "storm" need 1, 2 and 4 elements.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from daily import (KEEP_DAYS, MAX_ATTEMPTS, TIERS, DailyStore, best_route, pick_targets, reference_sizes)
from games import GameRegistry
from recipes import depths, load_recipes
from state import GameError, GameState
from test_coop import call, drain, login_as, names, parse_event, serve

BASE_DIR = Path(__file__).resolve().parent
BOOK = load_recipes(BASE_DIR / "test_recipes.json")
DAY = "2026-11-04"   # a Wednesday: its ISO week runs from 2026-11-02 to 2026-11-08

ROUTES = {
    "easy": ("steam", 1, [["water", "fire", ["steam"]]]),
    "medium": ("cloud", 2, [["water", "fire", ["steam"]], ["air", "steam", ["cloud"]]]),
    "hard": ("storm", 4, [["water", "fire", ["steam"]], ["air", "steam", ["cloud"]],
                          ["cloud", "water", ["rain"]], ["cloud", "rain", ["storm"]]]),
}


def seeded_store(path: Path | None = None, day: str = DAY) -> DailyStore:
    """A store whose targets for `day` are fixed, so the tests know the routes."""
    ticks = iter(range(1, 100000))
    store = DailyStore(path, BOOK, today=lambda: day, clock=lambda: next(ticks))
    store.today()
    data = store._load()
    for tier, (target, reference, route) in ROUTES.items():
        data["days"][day]["tiers"][tier].update(target=target, reference=reference, route=route)
    store._save(data)
    return store


def put(store: DailyStore, day: str, tier: str, scores: dict[str, tuple[int, float]], reference: int = 2) -> None:
    """Write scores for a past day straight into an in-memory store."""
    entry = store._memory["days"].setdefault(day, {"tiers": {}})
    entry["tiers"][tier] = {"target": "cloud", "reference": reference, "route": ROUTES["medium"][2], "attempts": {},
                            "scores": {name.casefold(): {"name": name, "score": s, "at": at}
                                       for name, (s, at) in scores.items()}}


class DailyPickTests(unittest.TestCase):
    def test_picks_are_reproducible_and_valid(self) -> None:
        picks = pick_targets(BOOK, DAY)
        self.assertEqual(picks, pick_targets(BOOK, DAY))
        self.assertEqual(list(picks), list(TIERS))
        depth = depths(BOOK)
        for tier, pick in picks.items():
            self.assertNotIn(pick["target"], BOOK.starters)
            self.assertIn(pick["target"], depth)
            self.assertGreaterEqual(pick["reference"], depth[pick["target"]])  # never shorter than the longest chain
        by_tier = [depth[picks[t]["target"]] for t in TIERS]
        self.assertEqual(by_tier, sorted(by_tier))                              # easy <= medium <= hard
        distinct = {pick_targets(BOOK, f"2026-12-{d:02d}")["medium"]["target"] for d in range(1, 29)}
        self.assertGreater(len(distinct), 1)                                    # different days give different targets

    def test_a_route_can_be_followed_and_matches_the_reference(self) -> None:
        for tier, pick in pick_targets(BOOK, DAY).items():
            have, unlocked = set(BOOK.starters), set()
            for a, b, makes in pick["route"]:
                self.assertTrue(a in have and b in have, f"{tier}: {a} + {b} is not available yet")
                self.assertEqual(tuple(makes), BOOK.combine(a, b))
                have.update(makes)
                unlocked.update(m for m in makes if m not in BOOK.starters)
            self.assertIn(pick["target"], have)
            self.assertEqual(len(unlocked), pick["reference"])

    def test_known_routes(self) -> None:
        depth = depths(BOOK)
        self.assertEqual([best_route(BOOK, depth, t)[0] for t in ("steam", "cloud", "storm")], [1, 2, 4])
        steps = [(frozenset((a, b)), makes) for a, b, makes in best_route(BOOK, depth, "cloud")[1]]
        self.assertEqual(steps, [(frozenset(("water", "fire")), ("steam",)), (frozenset(("air", "steam")), ("cloud",))])
        sizes = reference_sizes(BOOK, depth)
        self.assertEqual((sizes["steam"], sizes["cloud"]), (1, 2))
        self.assertNotIn("water", sizes)
        with self.assertRaises(ValueError):
            best_route(BOOK, depth, "water")


class DailyStoreTests(unittest.TestCase):
    def test_the_targets_are_chosen_once_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            first = DailyStore(path, BOOK, today=lambda: DAY).today()
            self.assertEqual(first["date"], DAY)
            self.assertEqual(list(first["tiers"]), list(TIERS))
            self.assertEqual(set(first["tiers"]["easy"]), {"target", "reference"})
            data = json.loads(path.read_text())
            data["days"][DAY]["tiers"]["easy"]["target"] = "steam"     # edited by hand, as if picked earlier
            path.write_text(json.dumps(data))
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).today()["tiers"]["easy"]["target"], "steam")

    def test_a_target_that_no_longer_exists_is_replaced_and_scores_kept(self) -> None:
        store = seeded_store()
        store.submit(DAY, "medium", "Ann", 3)
        store.use_attempt(DAY, "medium", "Ann")
        data = store._load()
        data["days"][DAY]["tiers"]["medium"]["target"] = "gone"
        store._save(data)
        self.assertNotEqual(store.today()["tiers"]["medium"]["target"], "gone")
        self.assertEqual((store.best(DAY, "medium", "Ann"), store.attempts(DAY, "medium", "Ann")), (3, 1))

    def test_an_older_file_with_one_target_a_day_is_brought_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            path.write_text(json.dumps({"version": 1, "days": {DAY: {
                "target": "cloud", "reference": 2, "scores": {"ann": {"name": "Ann", "score": 3, "at": 1}},
                "attempts": {"ann": 2}}}}))
            store = DailyStore(path, BOOK, today=lambda: DAY)
            tiers = store.today()["tiers"]
            self.assertEqual(tiers["medium"], {"target": "cloud", "reference": 2})   # it became the medium tier
            self.assertEqual((store.best(DAY, "medium", "Ann"), store.attempts(DAY, "medium", "Ann")), (3, 2))
            self.assertEqual(store.board(DAY, "easy"), [])                            # the others start empty
            self.assertEqual(set(json.loads(path.read_text())["days"][DAY]["tiers"]), set(TIERS))

    def test_only_the_best_score_counts_and_ties_go_to_the_earlier_finisher(self) -> None:
        store = seeded_store()
        self.assertTrue(store.submit(DAY, "medium", "Ann", 5))
        self.assertFalse(store.submit(DAY, "medium", "ann", 6))   # worse, and the same account whatever the capitals
        self.assertFalse(store.submit(DAY, "medium", "Ann", 5))   # equal is not an improvement
        self.assertTrue(store.submit(DAY, "medium", "Bob", 4))
        self.assertTrue(store.submit(DAY, "medium", "Cy", 5))
        self.assertTrue(store.submit(DAY, "medium", "Ann", 4))
        self.assertEqual([(r["name"], r["score"]) for r in store.board(DAY, "medium")],
                         [("Bob", 4), ("Ann", 4), ("Cy", 5)])
        self.assertEqual(store.board(DAY, "easy"), [])             # every tier has its own board
        self.assertEqual(store.best(DAY, "medium", "ANN"), 4)
        self.assertIsNone(store.best(DAY, "medium", "Nobody"))
        self.assertEqual(len(store.board(DAY, "medium", limit=2)), 2)
        self.assertFalse(store.submit("2000-01-01", "medium", "Ann", 1))   # a day that was never opened

    def test_attempts_are_counted_per_account_tier_and_day_and_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            store = seeded_store(path)
            self.assertEqual([store.use_attempt(DAY, "easy", "Ann"), store.use_attempt(DAY, "easy", "ann")], [1, 2])
            self.assertEqual((store.attempts(DAY, "hard", "Ann"), store.attempts(DAY, "easy", "Bob")), (0, 0))
            self.assertEqual(store.use_attempt("2000-01-01", "easy", "Ann"), 0)   # a day that was never opened
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).attempts(DAY, "easy", "Ann"), 2)
            store.forfeit(DAY, "hard", "Ann")
            self.assertEqual(store.attempts(DAY, "hard", "Ann"), MAX_ATTEMPTS)
            store.forfeit(DAY, "hard", "Ann")                                      # twice changes nothing
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).attempts(DAY, "hard", "Ann"), MAX_ATTEMPTS)

    def test_routes_are_stored_and_read_back(self) -> None:
        store = seeded_store()
        self.assertEqual(store.route(DAY, "medium"), [("water", "fire", ("steam",)), ("air", "steam", ("cloud",))])
        self.assertEqual(store.route("2000-01-01", "medium"), [])

    def test_streaks(self) -> None:
        store = DailyStore(None, BOOK, today=lambda: DAY)
        for day in ("2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"):   # an old run of 4
            put(store, day, "easy", {"Ann": (3, 1)})
        for day in ("2026-11-02", "2026-11-03"):                              # a run that reaches yesterday
            put(store, day, "easy", {"Ann": (3, 1)})
        put(store, "2026-11-03", "hard", {"Bob": (9, 1)})
        self.assertEqual(store.streak("Ann"), {"current": 2, "best": 4})      # today not played yet: still alive
        put(store, DAY, "medium", {"Ann": (3, 1)})
        self.assertEqual(store.streak("ann"), {"current": 3, "best": 4})
        self.assertEqual(store.streak("Bob"), {"current": 1, "best": 1})      # any tier counts
        self.assertEqual(store.streak("Nobody"), {"current": 0, "best": 0})
        gap = DailyStore(None, BOOK, today=lambda: DAY)
        put(gap, "2026-11-02", "easy", {"Cy": (3, 1)})                         # nothing yesterday or today: broken
        self.assertEqual(gap.streak("Cy"), {"current": 0, "best": 1})

    def test_history(self) -> None:
        store = DailyStore(None, BOOK, today=lambda: DAY)
        put(store, "2026-11-03", "easy", {"Ann": (3, 1), "Bob": (2, 2)})
        put(store, DAY, "easy", {"Ann": (5, 1)})
        days = store.history("Ann")
        self.assertEqual([d["date"] for d in days], [DAY, "2026-11-03"])      # newest first
        past = days[1]["tiers"]["easy"]
        self.assertEqual((past["best"], past["winner"], past["players"]), (3, {"name": "Bob", "score": 2}, 2))
        self.assertEqual(past["route"], ROUTES["medium"][2] and store.route("2026-11-03", "easy"))
        self.assertTrue(past["route"])
        self.assertEqual(days[0]["tiers"]["easy"]["route"], [])               # today's route is not given away
        self.assertIsNone(store.history("Bob")[0]["tiers"]["easy"]["best"])  # Bob has not played today
        self.assertEqual(len(store.history("Ann", limit=1)), 1)

    def test_the_weekly_board(self) -> None:
        store = DailyStore(None, BOOK, today=lambda: DAY)
        put(store, "2026-11-01", "easy", {"Ann": (1, 1)}, reference=1)          # last week (a Sunday): not counted
        put(store, "2026-11-02", "easy", {"Ann": (2, 1), "Bob": (1, 2)}, reference=1)
        put(store, "2026-11-03", "medium", {"Ann": (2, 1)}, reference=2)
        put(store, DAY, "hard", {"Ann": (8, 1), "Cy": (4, 1)}, reference=4)
        week = store.week()
        self.assertEqual(week["label"], "2026-11-02 to 2026-11-08")
        # Ann: 100*1/2 + 100*2/2 + 100*4/8 = 50 + 100 + 50. Bob: 100. Cy: 100.
        self.assertEqual(week["board"], [
            {"name": "Ann", "points": 200, "days": 3, "finishes": 3},
            {"name": "Bob", "points": 100, "days": 1, "finishes": 1},     # ties: more days, then name
            {"name": "Cy", "points": 100, "days": 1, "finishes": 1},
        ])
        self.assertEqual(len(store.week(limit=2)["board"]), 2)
        self.assertEqual(DailyStore(None, BOOK, today=lambda: "2026-12-30").week()["board"], [])

    def test_old_days_are_dropped(self) -> None:
        store = seeded_store()
        for n in range(KEEP_DAYS + 10):
            store._memory["days"][f"2000-01-{n % 28 + 1:02d}-{n}"] = {"tiers": {}}   # ids that sort before DAY
        store.today()
        self.assertLessEqual(len(store._memory["days"]), KEEP_DAYS)
        self.assertIn(DAY, store._memory["days"])

    def test_scores_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            store = seeded_store(path)
            store.submit(DAY, "hard", "Ann", 6)
            self.assertEqual(DailyStore(path, BOOK, today=lambda: DAY).board(DAY, "hard"), [{"name": "Ann", "score": 6}])

    def test_a_damaged_file_is_reported_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily.json"
            path.write_text("{not json")
            with self.assertRaises(ValueError):
                DailyStore(path, BOOK, today=lambda: DAY).today()
            self.assertEqual(path.read_text(), "{not json")
            registry = GameRegistry(BOOK, daily=DailyStore(path, BOOK, today=lambda: DAY))
            with self.assertRaises(GameError) as err:
                registry.start_daily("Ann", "easy")
            self.assertEqual(err.exception.status, 500)


class HintRewardTests(unittest.TestCase):
    """Finishing a daily challenge pays one solo hint when the streak, today included, is at least two days."""

    def setUp(self) -> None:
        self.store = seeded_store()
        self.registry = GameRegistry(BOOK, max_games=5, daily=self.store)

    def finish(self, tier: str, fresh: bool = False) -> list[tuple[str, Any]]:
        game = self.registry.start_daily("Ann", tier, fresh=fresh)
        player = game.join("Ann")
        sub = game.subscribe(player.token)
        assert sub
        drain(sub)
        for a, b, _ in ROUTES[tier][2]:
            first = game.add_tile(player.token, a, 0.2, 0.2)
            second = game.add_tile(player.token, b, 0.6, 0.6)
            game.grab_tile(player.token, first["id"])
            game.combine(player.token, second["id"], tile=first["id"], x=0.6, y=0.6)
        return drain(sub)

    def reward(self, events: list[tuple[str, Any]]) -> Any:
        done = [data for name, data in events if name == "challenge_done"]
        return done[0].get("hint_reward") if done else "no event"

    def test_a_first_day_pays_nothing(self) -> None:
        self.assertIsNone(self.reward(self.finish("easy")))
        self.assertEqual(self.registry.solo.hints("Ann"), 3)

    def test_a_streak_of_two_days_pays_one_hint_per_challenge_per_day(self) -> None:
        put(self.store, "2026-11-03", "easy", {"Ann": (1, 5)})            # she played yesterday
        self.assertEqual(self.reward(self.finish("easy")), {"earned": 1, "hints": 4})
        self.assertEqual(self.registry.solo.hints("Ann"), 4)
        self.assertIsNone(self.reward(self.finish("easy", fresh=True)))   # playing the same challenge again pays nothing
        self.assertEqual(self.reward(self.finish("medium")), {"earned": 1, "hints": 5})   # another challenge does
        self.assertEqual(self.registry.solo.hints("Ann"), 5)

    def test_a_missed_day_breaks_the_streak(self) -> None:
        put(self.store, "2026-11-02", "easy", {"Ann": (1, 5)})            # two days ago, nothing yesterday
        self.assertIsNone(self.reward(self.finish("easy")))
        self.assertEqual(self.registry.solo.hints("Ann"), 3)


class ChallengeGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = seeded_store()
        self.registry = GameRegistry(BOOK, max_games=1, daily=self.store)
        self.game = self.registry.start_daily("Ann", "medium")    # cloud: steam, then cloud
        self.ann = self.game.join("Ann")
        self.sub = self.game.subscribe(self.ann.token)
        assert self.sub
        drain(self.sub)

    def make(self, game: GameState, player: Any, a: str, b: str) -> dict[str, Any]:
        first = game.add_tile(player.token, a, 0.2, 0.2)
        second = game.add_tile(player.token, b, 0.6, 0.6)
        game.grab_tile(player.token, first["id"])
        return game.combine(player.token, second["id"], tile=first["id"], x=0.6, y=0.6)

    def finish_medium(self, game: GameState, player: Any) -> None:
        self.make(game, player, "water", "fire")
        self.make(game, player, "air", "steam")

    def test_finding_the_target_scores_the_elements_discovered(self) -> None:
        self.make(self.game, self.ann, "water", "fire")
        self.assertFalse(self.game.finished)
        self.assertEqual(self.store.board(DAY, "medium"), [])
        self.make(self.game, self.ann, "air", "steam")
        self.assertTrue(self.game.finished)
        events = drain(self.sub)
        done = [data for name, data in events if name == "challenge_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual((done[0]["score"], done[0]["reference"], done[0]["target"]["id"]), (2, 2, "cloud"))
        self.assertEqual(names(events)[-1], "challenge_done")      # after the discovery announcements
        self.assertEqual(self.store.board(DAY, "medium"), [{"name": "Ann", "score": 2}])
        self.assertEqual(self.store.board(DAY, "easy"), [])         # it counted for its own tier only

    def test_detours_cost_points(self) -> None:
        self.make(self.game, self.ann, "water", "earth")     # mud: not on the route
        self.finish_medium(self.game, self.ann)
        self.assertEqual(self.store.best(DAY, "medium", "Ann"), 3)

    def test_nothing_more_can_be_combined_after_finishing(self) -> None:
        self.finish_medium(self.game, self.ann)
        first = self.game.add_tile(self.ann.token, "water", 0.1, 0.1)
        second = self.game.add_tile(self.ann.token, "earth", 0.5, 0.5)
        self.game.grab_tile(self.ann.token, first["id"])
        with self.assertRaises(GameError) as err:
            self.game.combine(self.ann.token, second["id"], tile=first["id"])
        self.assertEqual(err.exception.status, 409)
        self.assertEqual(self.store.board(DAY, "medium"), [{"name": "Ann", "score": 2}])

    def test_a_challenge_is_a_plain_puzzle(self) -> None:
        with self.assertRaises(GameError) as err:
            self.game.hint(self.ann.token, "can_make")
        self.assertEqual(err.exception.status, 403)
        with self.assertRaises(GameError):
            self.game.genie_use(self.ann.token, "water")
        snapshot = self.game._snapshot(self.ann)
        target = snapshot["challenge"].pop("target")
        self.assertEqual(target["id"], "cloud")
        self.assertEqual(snapshot["challenge"], {"date": DAY, "tier": "medium", "reference": 2, "starters": 4, "done": None})
        self.assertEqual(snapshot["genie"], {"available": False})

    def test_it_is_private_and_not_a_normal_game(self) -> None:
        with self.assertRaises(GameError) as err:
            self.game.join("Bob")
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.registry.list("Bob"), [])
        mine = self.registry.list("ann")                           # except for its owner, for the game page
        self.assertEqual([(g["id"], g["private"]) for g in mine], [(self.game.slug, True)])
        self.registry.create("Normal")                              # and it does not use up the only slot
        with self.assertRaises(GameError):
            self.registry.create("One too many")
        with self.assertRaises(GameError) as err:
            self.registry.end(self.game.slug, self.ann.token, save=True)
        self.assertEqual(err.exception.status, 400)
        self.assertEqual(self.registry.end(self.game.slug, self.ann.token, save=False), {"saved": False})

    def test_tiers_are_separate_attempts(self) -> None:
        self.assertEqual(self.game.challenge.tier, "medium")        # type: ignore[union-attr]
        easy = self.registry.start_daily("Ann", "easy")
        self.assertIsNot(easy, self.game)
        self.assertIs(self.registry.get(self.game.slug), self.game)  # starting another tier drops nothing
        self.assertIs(self.registry.start_daily("Ann", "medium"), self.game)    # continue
        self.assertIs(self.registry.start_daily("Ann", "easy"), easy)
        with self.assertRaises(GameError) as err:
            self.registry.start_daily("Ann", "impossible")
        self.assertEqual(err.exception.status, 400)
        with self.assertRaises(GameError):
            self.registry.start_daily("Ann", None)

    def test_continue_or_start_over(self) -> None:
        self.assertIs(self.registry.start_daily("Ann", "medium"), self.game)    # the unfinished attempt is reused
        self.assertIs(self.registry.start_daily("ann", "medium"), self.game)
        other = self.registry.start_daily("Bob", "medium")
        self.assertIsNot(other, self.game)
        fresh = self.registry.start_daily("Ann", "medium", fresh=True)
        self.assertIsNot(fresh, self.game)
        self.assertIsNone(self.registry.get(self.game.slug))                     # the old one is gone...
        messages = []
        while not self.sub.messages.empty():
            messages.append(self.sub.messages.get_nowait())
        self.assertEqual(messages[-1:], [None])                                  # ...and its page is told: stream closed
        self.assertEqual(names([parse_event(m) for m in messages if m]), ["ended"])
        player = fresh.join("Ann")
        self.finish_medium(fresh, player)
        self.assertIsNot(self.registry.start_daily("Ann", "medium"), fresh)      # a finished attempt is not reused

    def test_three_attempts_a_day_in_each_tier(self) -> None:
        self.assertEqual(self.registry.daily_status("Ann")["tiers"][1]["attempts_used"], 1)   # setUp started one
        self.assertIs(self.registry.start_daily("Ann", "medium"), self.game)                   # carrying on is free
        self.assertEqual(self.store.attempts(DAY, "medium", "Ann"), 1)
        second = self.registry.start_daily("Ann", "medium", fresh=True)                        # starting over uses one
        third = self.registry.start_daily("ann", "medium", fresh=True)
        self.assertEqual(self.store.attempts(DAY, "medium", "Ann"), MAX_ATTEMPTS)
        with self.assertRaises(GameError) as err:
            self.registry.start_daily("Ann", "medium", fresh=True)
        self.assertEqual(err.exception.status, 409)
        self.assertIn("come back tomorrow", err.exception.message)
        self.assertIs(self.registry.get(third.slug), third)                                   # a refusal drops nothing
        self.assertIs(self.registry.start_daily("Ann", "medium"), third)
        self.assertIsNone(self.registry.get(second.slug))
        self.finish_medium(third, third.join("Ann"))
        with self.assertRaises(GameError):
            self.registry.start_daily("Ann", "medium")                                         # nothing left to start...
        self.registry.start_daily("Ann", "easy")                                               # ...but easy has its own three
        self.assertEqual(self.store.attempts(DAY, "easy", "Ann"), 1)
        self.assertEqual(self.store.attempts(DAY, "medium", "Bob"), 0)

    def test_a_new_day_gives_new_attempts_and_targets(self) -> None:
        day = {"value": DAY}
        store = seeded_store()
        store._today = lambda: day["value"]
        registry = GameRegistry(BOOK, daily=store)
        for _ in range(MAX_ATTEMPTS):
            registry.start_daily("Ann", "easy", fresh=True)
        with self.assertRaises(GameError):
            registry.start_daily("Ann", "easy", fresh=True)
        day["value"] = "2026-11-05"
        game = registry.start_daily("Ann", "easy", fresh=True)
        assert game.challenge is not None
        self.assertEqual((game.challenge.date, store.attempts("2026-11-05", "easy", "Ann")), ("2026-11-05", 1))

    def test_status_for_the_home_page(self) -> None:
        status = self.registry.daily_status("Ann")
        self.assertEqual((status["date"], status["attempts_max"]), (DAY, MAX_ATTEMPTS))
        self.assertEqual([t["tier"] for t in status["tiers"]], list(TIERS))
        medium = status["tiers"][1]
        self.assertEqual((medium["target"]["id"], medium["reference"], medium["best"], medium["board"], medium["attempts_used"],
                          medium["running"], medium["route"]), ("cloud", 2, None, [], 1, f"/g/{self.game.slug}", None))
        self.assertEqual(status["streak"], {"current": 0, "best": 0})
        self.assertEqual(status["week"]["board"], [])
        self.finish_medium(self.game, self.ann)
        status = self.registry.daily_status("Ann")
        medium = status["tiers"][1]
        self.assertEqual((medium["best"], medium["running"]), (2, None))          # finished: nothing to continue
        self.assertEqual(medium["board"], [{"name": "Ann", "score": 2}])
        self.assertIsNone(medium["route"])                                        # attempts are left, so no route yet
        self.assertEqual(status["streak"], {"current": 1, "best": 1})
        self.assertEqual(status["week"]["board"], [{"name": "Ann", "points": 100, "days": 1, "finishes": 1}])
        self.assertEqual(self.registry.daily_status("Bob")["tiers"][1]["best"], None)

    def test_the_route_is_shown_only_once_the_tier_cannot_be_played_any_more(self) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            self.registry.start_daily("Ann", "medium", fresh=True)
        self.assertIsNone(self.registry.daily_status("Ann")["tiers"][1]["route"])      # the last attempt is in progress
        game = self.registry.start_daily("Ann", "medium")
        self.finish_medium(game, game.join("Ann"))
        route = self.registry.daily_status("Ann")["tiers"][1]["route"]
        self.assertEqual([[step["a"]["id"], step["b"]["id"], [m["id"] for m in step["makes"]]] for step in route],
                         ROUTES["medium"][2])
        self.assertEqual(set(route[0]["a"]), {"id", "label", "emoji"})

    def test_giving_up_shows_the_route_and_ends_the_tier(self) -> None:
        self.assertIsNone(self.registry.daily_status("Ann")["tiers"][1]["route"])
        shown = self.registry.reveal_daily("Ann", "medium")
        self.assertEqual((shown["tier"], len(shown["route"])), ("medium", 2))
        self.assertIsNone(self.registry.get(self.game.slug))                          # the attempt in progress was closed
        status = self.registry.daily_status("Ann")["tiers"][1]
        self.assertEqual((status["attempts_used"], status["running"], len(status["route"])), (MAX_ATTEMPTS, None, 2))
        with self.assertRaises(GameError):
            self.registry.start_daily("Ann", "medium")
        self.assertIsNone(self.registry.daily_status("Ann")["tiers"][0]["route"])      # other tiers are untouched
        self.assertIsNone(self.registry.daily_status("Bob")["tiers"][1]["route"])
        with self.assertRaises(GameError):
            self.registry.reveal_daily("Ann", "nope")

    def test_history_for_the_home_page(self) -> None:
        put(self.store, "2026-11-03", "medium", {"Ann": (3, 1)})
        self.store._memory["days"]["2026-11-03"]["tiers"]["medium"]["target"] = "gone"   # no longer in the recipes
        put(self.store, "2026-11-02", "easy", {"Bob": (2, 1)})
        data = self.registry.daily_history("Ann")
        self.assertEqual([d["date"] for d in data["days"]], [DAY, "2026-11-02"])           # the unusable day is skipped
        today = data["days"][0]["tiers"]["medium"]
        self.assertEqual((today["target"]["label"], today["route"]), ("Cloud", []))
        past = data["days"][1]["tiers"]["easy"]
        self.assertEqual((past["winner"], past["best"], len(past["route"])), ({"name": "Bob", "score": 2}, None, 2))

    def test_a_reload_keeps_the_attempt_its_tier_and_its_result(self) -> None:
        self.make(self.game, self.ann, "water", "fire")
        hard = self.registry.start_daily("Ann", "hard")
        saved = json.loads(json.dumps(self.registry.live_export()))
        store = seeded_store()
        registry = GameRegistry(BOOK, daily=store)
        self.assertEqual(registry.live_restore(saved), 2)
        game = registry.get(self.game.slug)
        assert game is not None and game.challenge is not None
        self.assertEqual((game.challenge.tier, game.challenge.target, game.challenge.reference, game.finished),
                         ("medium", "cloud", 2, False))
        self.assertEqual(registry.get(hard.slug).challenge.tier, "hard")                    # type: ignore[union-attr]
        self.assertEqual(registry.list(), [])
        player = game.authorize(self.ann.token)                                             # the same login token still works
        self.make(game, player, "air", "steam")
        self.assertEqual(store.board(DAY, "medium"), [{"name": "Ann", "score": 2}])          # the restored game reports in
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

    def test_status_start_and_a_full_attempt(self) -> None:
        ann = self.as_user("DailyAnn")
        status, data = call(self.port, "GET", "/daily", headers=ann)
        self.assertEqual(status, 200)
        self.assertEqual((data["date"], data["attempts_max"], [t["tier"] for t in data["tiers"]]),
                         (DAY, 3, ["easy", "medium", "hard"]))
        medium = data["tiers"][1]
        self.assertEqual((medium["target"]["id"], medium["reference"], medium["best"], medium["running"],
                          medium["attempts_used"], medium["route"]), ("cloud", 2, None, None, 0, None))
        self.assertEqual(set(data), {"date", "attempts_max", "streak", "tiers", "week"})

        status, started = call(self.port, "POST", "/daily/start", {"tier": "medium"}, headers=ann)
        self.assertEqual(status, 200)
        slug = started["id"]
        self.assertEqual(started["url"], f"/g/{slug}")
        self.assertEqual(call(self.port, "POST", "/daily/start", {"tier": "medium"}, headers=ann)[1]["id"], slug)  # continue
        self.assertEqual(call(self.port, "GET", "/daily", headers=ann)[1]["tiers"][1]["running"], f"/g/{slug}")
        self.assertNotEqual(call(self.port, "POST", "/daily/start", {"tier": "medium", "fresh": True}, headers=ann)[1]["id"], slug)
        data = call(self.port, "GET", "/daily", headers=ann)[1]
        slug = data["tiers"][1]["running"].split("/")[-1]
        self.assertEqual(data["tiers"][1]["attempts_used"], 2)

        listing = call(self.port, "GET", "/games", headers=ann)[1]["games"]
        self.assertEqual([g["id"] for g in listing if g.get("private")], [slug])          # only its owner sees it
        bob = self.as_user("DailyBob")
        self.assertEqual([g for g in call(self.port, "GET", "/games", headers=bob)[1]["games"] if g["id"] == slug], [])
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/join", {}, headers=bob)[0], 403)   # someone else's attempt
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
        self.assertEqual(call(self.port, "GET", "/daily", headers=ann)[1]["tiers"][1]["board"], [])
        self.assertEqual(combine("air", "steam")[0], 200)
        data = call(self.port, "GET", "/daily", headers=ann)[1]
        medium = data["tiers"][1]
        self.assertEqual((medium["best"], medium["running"], medium["board"]), (2, None, [{"name": "DailyAnn", "score": 2}]))
        self.assertEqual(data["streak"]["current"], 1)
        self.assertEqual(data["week"]["board"][0], {"name": "DailyAnn", "points": 100, "days": 1, "finishes": 1})
        self.assertEqual(combine("water", "earth")[0], 409)                                    # the attempt is over
        self.assertEqual(call(self.port, "GET", "/daily", headers=bob)[1]["tiers"][1]["board"], medium["board"])
        status, history = call(self.port, "GET", "/daily/history", headers=ann)
        self.assertEqual(status, 200)
        self.assertEqual(history["days"][0]["date"], DAY)
        self.assertEqual(history["days"][0]["tiers"]["medium"]["best"], 2)

    def test_giving_up_shows_the_route(self) -> None:
        di = self.as_user("DailyDi")
        status, data = call(self.port, "POST", "/daily/reveal", {"tier": "easy"}, headers=di)
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in data["route"][0]["makes"]], ["steam"])
        easy = call(self.port, "GET", "/daily", headers=di)[1]["tiers"][0]
        self.assertEqual((easy["attempts_used"], len(easy["route"])), (3, 1))
        self.assertEqual(call(self.port, "POST", "/daily/start", {"tier": "easy"}, headers=di)[0], 409)
        self.assertEqual(call(self.port, "POST", "/daily/reveal", {"tier": "bogus"}, headers=di)[0], 400)

    def test_the_fourth_attempt_is_refused(self) -> None:
        cy = self.as_user("DailyCy")
        for _ in range(3):
            self.assertEqual(call(self.port, "POST", "/daily/start", {"tier": "hard", "fresh": True}, headers=cy)[0], 200)
        status, data = call(self.port, "POST", "/daily/start", {"tier": "hard", "fresh": True}, headers=cy)
        self.assertEqual(status, 409)
        self.assertIn("all 3", data["error"])
        self.assertEqual(call(self.port, "POST", "/daily/start", {"tier": "hard"}, headers=cy)[0], 200)   # carrying on is fine
        self.assertEqual(call(self.port, "POST", "/daily/start", {"tier": "easy"}, headers=cy)[0], 200)   # another tier is fine
        self.assertEqual(call(self.port, "POST", "/daily/start", {}, headers=cy)[0], 400)                 # a tier is required

    def test_login_is_required(self) -> None:
        for method, path in (("GET", "/daily"), ("GET", "/daily/history"), ("POST", "/daily/start"),
                             ("POST", "/daily/reveal")):
            body = {"tier": "easy"} if method == "POST" else None
            self.assertEqual(call(self.port, method, path, body, headers={"X-Session": ""})[0], 401, path)


if __name__ == "__main__":
    unittest.main()

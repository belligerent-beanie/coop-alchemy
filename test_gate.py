"""Shared games open after 10 achievements (achievements.unlock_progress and the checks in server.py)."""
from __future__ import annotations

import unittest

from achievements import ACHIEVEMENTS, MULTIPLAYER_UNLOCK_AT, unlock_progress
from games import GameRegistry
from test_coop import RECIPES, call, load_recipes, login_as, serve

SOLO_IDS = [a.id for a in ACHIEVEMENTS]
DAILY_BADGES = {"daily_done": 7, "daily_hard": 1}


class UnlockProgressTests(unittest.TestCase):
    def solo(self, n: int) -> dict[str, int]:
        return {a: 1 for a in SOLO_IDS[:n]}

    def test_ten_achievements_unlock_nine_do_not(self) -> None:
        self.assertEqual(MULTIPLAYER_UNLOCK_AT, 10)
        nine = unlock_progress(self.solo(9), {}, {})
        self.assertEqual((nine["unlocked"], nine["earned"], nine["needed"]), (False, 9, 10))
        ten = unlock_progress(self.solo(10), {}, {})
        self.assertEqual((ten["unlocked"], ten["reason"]), (True, "achievements"))

    def test_only_solo_and_challenge_achievements_count(self) -> None:
        badges = {"mp_together": 1, "mp_emote": 1, "mp_finds_10": 1,           # shared-game ones: cannot be earned while locked
                  "ch_daily": 1, "ch_race": 1, "no_such_badge": 1}              # these count, the unknown id does not
        progress = unlock_progress(self.solo(3), badges, {})
        self.assertEqual(progress["earned"], 3 + 2)

    def test_unknown_solo_ids_are_ignored(self) -> None:
        self.assertEqual(unlock_progress({"made_up": 1}, {}, {})["earned"], 0)

    def test_an_account_that_already_played_shared_games_keeps_access_and_admins_are_never_locked(self) -> None:
        self.assertEqual(unlock_progress({}, {}, {"games": 1})["reason"], "played")
        self.assertEqual(unlock_progress({}, {}, {}, admin=True)["reason"], "admin")
        self.assertFalse(unlock_progress({}, {}, {"games": 0, "finds": 50})["unlocked"])

    def test_the_threshold_can_be_changed_and_zero_opens_everything(self) -> None:
        self.assertTrue(unlock_progress({}, {}, {}, needed=0)["unlocked"])
        self.assertFalse(unlock_progress(self.solo(4), {}, {}, needed=5)["unlocked"])
        self.assertTrue(unlock_progress(self.solo(5), {}, {}, needed=5)["unlocked"])


class GateHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = GameRegistry(load_recipes(RECIPES), max_games=20)
        cls.server, cls.port = serve(cls.registry)
        cls.registry.multiplayer_unlock_at = MULTIPLAYER_UNLOCK_AT   # serve() opens it for the other tests

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def user(self, name: str) -> dict[str, str]:
        return {"X-Session": login_as(self.port, name)}

    def req(self, who: dict[str, str], method: str, path: str, body: object = None) -> tuple[int, object]:
        return call(self.port, method, path, body, headers=who)

    def grant_solo(self, name: str, n: int) -> None:
        self.registry.solo.record(name, {}, 0, 0, False, earned=SOLO_IDS[:n])

    def test_a_new_account_cannot_create_resume_or_join_shared_games(self) -> None:
        who = self.user("gatenew")
        status, data = self.req(who, "POST", "/games", {"name": "My room"})
        self.assertEqual(status, 403)
        self.assertIn("10 achievements", data["error"])
        self.assertIn("you have 0", data["error"])
        self.assertEqual(self.req(who, "POST", "/games/resume", {"id": "nothing"})[0], 403)
        shared = self.registry.create("Open room", "Somebody")
        status, _ = self.req(who, "POST", f"/g/{shared.slug}/join", {})
        self.assertEqual(status, 403)

    def test_solo_and_the_daily_challenge_are_never_gated(self) -> None:
        who = self.user("gatesolo")
        self.assertEqual(self.req(who, "POST", "/solo/start", {})[0], 200)
        self.assertEqual(self.req(who, "GET", "/daily")[0], 200)
        status, data = self.req(who, "GET", "/solo")
        self.assertEqual(status, 200)
        self.assertEqual(data["multiplayer"], {"unlocked": False, "earned": 0, "needed": 10, "reason": ""})

    def test_the_lobby_list_carries_the_progress(self) -> None:
        who = self.user("gatelobby")
        self.grant_solo("gatelobby", 4)
        status, data = self.req(who, "GET", "/games")
        self.assertEqual(status, 200)
        self.assertEqual((data["multiplayer"]["earned"], data["multiplayer"]["unlocked"]), (4, False))

    def test_ten_achievements_open_everything_and_nine_do_not(self) -> None:
        who = self.user("gateten")
        self.grant_solo("gateten", 9)
        self.assertEqual(self.req(who, "POST", "/games", {"name": "Almost"})[0], 403)
        self.grant_solo("gateten", 10)
        status, created = self.req(who, "POST", "/games", {"name": "Unlocked"})
        self.assertEqual(status, 200)
        self.assertEqual(self.req(who, "POST", f"/g/{created['id']}/join", {})[0], 200)

    def test_daily_challenge_achievements_count_but_shared_game_ones_do_not(self) -> None:
        who = self.user("gatedaily")
        self.registry.profiles.bump("gatedaily", add={"emotes": 1, "finds": 500}, best={})   # mp_ badges only
        self.assertEqual(self.req(who, "GET", "/games")[1]["multiplayer"]["earned"], 0)
        self.registry.profiles.bump("gatedaily", add=DAILY_BADGES, best={"daily_streak": 7})  # five challenge badges
        self.assertEqual(self.req(who, "GET", "/games")[1]["multiplayer"]["earned"], 5)
        self.grant_solo("gatedaily", 5)
        self.assertEqual(self.req(who, "POST", "/games", {"name": "Habit"})[0], 200)

    def test_players_of_shared_games_before_the_gate_and_admins_keep_access(self) -> None:
        veteran = self.user("gateveteran")
        self.registry.profiles.bump("gateveteran", add={"games": 1})
        self.assertEqual(self.req(veteran, "POST", "/games", {"name": "Old hand"})[0], 200)
        boss = self.user("gateboss")
        self.server.accounts.set_admin("gateboss", True)
        self.assertEqual(self.req(boss, "POST", "/games", {"name": "Admin room"})[0], 200)

    def test_races_stay_open_to_locked_accounts(self) -> None:
        who = self.user("gaterace")
        self.assertEqual(self.req(who, "GET", "/races")[0], 200)

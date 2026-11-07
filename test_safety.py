"""Strangers' safety: blocked words in names, one running game per account, and reports that never record who sent them."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from erasure import erase_account
from games import GameRegistry
from namefilter import NameFilter
from recipes import load_recipes
from reports import DEDUPE_SECONDS, MAX_NOTE, MAX_REPORTS, RETENTION_SECONDS, ReportStore, clean_note
from state import GameError
from test_coop import RECIPES, call, login_as, serve
from throttle import Throttle


class NameFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.filter = NameFilter.load()

    def test_ordinary_names_pass_including_the_famous_traps(self) -> None:
        for name in ("Ann", "Classic", "Assassin", "Scunthorpe", "Therapist", "Dickinson", "Hancock", "Peacock", "Grape", "Analyst", "Essex",
                     "Nigeria", "Niger", "Cumberland", "Cockburn", "Shiitake Fan", "Class of 2026", "Alchemist", "Steam Engine", "Passion",
                     "Assam Tea", "Mass Effect", "Raptor", "Bassist", "Cocktail", "Sussex", "Document", "player_42", "Wizard of Oz", "Zoë"):
            self.assertTrue(self.filter.allows(name), name)

    def test_obvious_ones_are_blocked_however_they_are_dressed_up(self) -> None:
        for name in ("fuck", "xX_fuck_Xx", "F u c k", "f.u.c.k", "FUUUCK", "sh1t", "SHIT", "FuckYou", "b1tch", "BitchBoy", "asshole",
                     "Big Ass", "BigAss", "Dick Pic", "sex bot", "nazi lover", "KKK", "p0rn", "N@zi", "Rapist", "xXcuntXx"):
            self.assertFalse(self.filter.allows(name), name)

    def test_whole_word_rules_only_match_whole_words(self) -> None:
        self.assertFalse(self.filter.allows("Big Ass"))
        self.assertTrue(self.filter.allows("Classy"))  # a short term inside another word is fine
        self.assertFalse(self.filter.allows("the-ass-club"))
        self.assertTrue(self.filter.allows("Assistant"))

    def test_the_error_never_says_which_word(self) -> None:
        with self.assertRaises(GameError) as ctx:
            self.filter.check("xX_fuck_Xx", "username")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("username is not allowed", ctx.exception.message)
        self.assertNotIn("fuck", ctx.exception.message.lower())

    def test_non_text_is_left_to_the_other_checks(self) -> None:
        for value in (None, 5, ["fuck"], {"a": "b"}):
            self.assertTrue(self.filter.allows(value))

    def test_the_built_in_list_is_there_and_is_not_trivial(self) -> None:
        self.assertGreater(self.filter.size, 40)

    def test_an_administrators_own_file_adds_terms_and_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "mine.txt"
            extra.write_text("# my words\nzzzword\n=qqq   # whole word only\n-bazzzwordy\n\n", encoding="utf-8")
            custom = NameFilter.load(str(extra))
            self.assertFalse(custom.allows("my Z.Z.Z.W.O.R.D"))
            self.assertFalse(custom.allows("qqq"))
            self.assertTrue(custom.allows("qqqq"))
            self.assertTrue(custom.allows("bazzzwordy"))   # allowed by the exception
            self.assertFalse(custom.allows("fuck"))        # the built-in list still applies
            self.assertTrue(self.filter.allows("zzzword"))  # ... and the file only affected the filter that loaded it
        with self.assertRaises(OSError):
            NameFilter.load("/no/such/blocklist.txt")


class ReportStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1_000_000.0]
        self.store = ReportStore(None, clock=lambda: self.now[0])

    def test_reports_about_one_player_for_one_reason_merge_with_a_count(self) -> None:
        self.assertTrue(self.store.add("Zed", "name", "first note", "room 1", "reporter-a"))
        self.assertTrue(self.store.add("zed", "name", "", "room 2", "reporter-b"))
        self.assertTrue(self.store.add("Zed", "behaviour", "rude", "room 2", "reporter-c"))
        rows = self.store.listing()
        self.assertEqual([(r["reason"], r["count"]) for r in rows], [("name", 2), ("behaviour", 1)])
        name_row = rows[0]
        self.assertEqual((name_row["note"], name_row["game"]), ("first note", "room 2"))  # an empty note keeps the earlier one

    def test_the_same_reporter_cannot_pile_on_within_a_day_but_can_after(self) -> None:
        self.assertTrue(self.store.add("Zed", "name", "", "g", "reporter-a"))
        self.assertFalse(self.store.add("Zed", "behaviour", "", "g", "Reporter-A"))  # same person, same player, whatever the reason
        self.assertEqual(sum(r["count"] for r in self.store.listing()), 1)
        self.assertTrue(self.store.add("Zed", "name", "", "g", "reporter-b"))        # someone else counts
        self.now[0] += DEDUPE_SECONDS + 1
        self.assertTrue(self.store.add("Zed", "name", "", "g", "reporter-a"))        # a day later it may be raised again

    def test_nothing_in_a_report_or_its_file_identifies_the_reporter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            store = ReportStore(path, clock=lambda: self.now[0])
            store.add("Zed", "name", "a note", "the game", "SecretReporterName")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secretreportername", text.lower())
            self.assertNotIn("SecretReporterName", json.dumps(store.listing()))
            self.assertEqual(set(store.listing()[0]), {"id", "user", "reason", "note", "game", "count", "first", "at"})

    def test_reports_survive_a_restart_and_expire_after_two_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            ReportStore(path, clock=lambda: self.now[0]).add("Zed", "other", "", "g", "r1")
            again = ReportStore(path, clock=lambda: self.now[0])
            self.assertEqual(again.count(), 1)
            self.now[0] += RETENTION_SECONDS + 1
            self.assertEqual(again.count(), 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["reports"], [])  # gone from the file too

    def test_dismissing_and_deleting_an_account_remove_reports(self) -> None:
        self.store.add("Zed", "name", "", "g", "r1")
        self.store.add("Amy", "name", "", "g", "r2")
        report_id = next(r["id"] for r in self.store.listing() if r["user"] == "Amy")
        self.assertTrue(self.store.dismiss(report_id))
        self.assertFalse(self.store.dismiss(report_id))
        self.assertFalse(self.store.dismiss(None))
        self.assertEqual(self.store.delete_user("ZED"), 1)
        self.assertEqual(self.store.count(), 0)

    def test_bad_input_is_refused(self) -> None:
        with self.assertRaises(GameError):
            self.store.add("Zed", "spam", "", "g", "r1")
        self.assertEqual(clean_note(None), "")
        self.assertEqual(clean_note("  a   b\n\tc  "), "a b c")
        self.assertEqual(len(clean_note("x" * 1000)), MAX_NOTE)
        self.assertNotIn("\x07", clean_note("bell\x07ring"))
        with self.assertRaises(GameError):
            clean_note(["not text"])

    def test_the_store_has_a_size_limit_and_drops_the_oldest(self) -> None:
        for n in range(MAX_REPORTS + 5):
            self.now[0] += 1
            self.store.add(f"player{n}", "other", "", "g", f"reporter{n}")
        users = {r["user"] for r in self.store.listing()}
        self.assertEqual(len(users), MAX_REPORTS)
        self.assertNotIn("player0", users)
        self.assertIn(f"player{MAX_REPORTS + 4}", users)

    def test_a_damaged_file_is_reported_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(GameError) as ctx:
                ReportStore(path).listing()
            self.assertEqual(ctx.exception.status, 500)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


class GamesPerAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = load_recipes(RECIPES)

    def test_one_account_cannot_hold_every_slot(self) -> None:
        registry = GameRegistry(self.book, max_games=5, max_per_account=1)
        first = registry.create("one", "Ann")
        with self.assertRaises(GameError) as ctx:
            registry.create("two", "ann")  # any capitals
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("you already have a game running", ctx.exception.message)
        self.assertIn(first.name, ctx.exception.message)
        registry.create("bobs", "Bob")  # other accounts are unaffected

    def test_ending_the_game_frees_the_slot_and_private_games_do_not_count(self) -> None:
        registry = GameRegistry(self.book, max_games=5, max_per_account=1)
        game = registry.create("one", "Ann")
        player = game.join("Ann")
        registry.end("one", player.token, False)
        registry.create("again", "Ann")
        registry.start_daily("Bob", "easy", False)  # a daily attempt is private
        registry.create("bobs", "Bob")

    def test_zero_means_no_limit_and_games_without_a_creator_are_not_limited(self) -> None:
        registry = GameRegistry(self.book, max_games=5, max_per_account=0)
        for n in range(3):
            registry.create(f"g{n}", "Ann")
        limited = GameRegistry(self.book, max_games=5, max_per_account=1)
        limited.create("a")
        limited.create("b")

    def test_resuming_a_saved_game_counts_too(self) -> None:
        from saves import SaveStore
        with tempfile.TemporaryDirectory() as tmp:
            saves = SaveStore(Path(tmp))
            registry = GameRegistry(self.book, max_games=5, saves=saves, max_per_account=1)
            game = registry.create("saved", "Ann")
            registry.end("saved", game.join("Ann").token, True)
            registry.create("other", "Ann")
            with self.assertRaises(GameError) as ctx:
                registry.resume("saved", "Ann")
            self.assertEqual(ctx.exception.status, 409)


class SafetyOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=50))
        cls.server.accounts.set_admin("Host", True)
        cls.counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def name(self, prefix: str) -> str:
        type(self).counter += 1
        return f"{prefix}{self.counter}"

    def session(self, name: str) -> dict[str, str]:
        return {"X-Session": login_as(self.port, name)}

    def join(self, slug: str, name: str) -> dict[str, Any]:
        status, data = call(self.port, "POST", f"/g/{slug}/join", {}, headers=self.session(name))
        self.assertEqual(status, 200, data)
        return data

    def room(self, *names: str) -> tuple[str, list[dict[str, Any]]]:
        status, game = call(self.port, "POST", "/games", {"name": self.name("room")}, headers=self.session(names[0]))
        self.assertEqual(status, 200, game)
        return game["id"], [self.join(game["id"], n) for n in names]

    def report(self, slug: str, reporter: dict[str, Any], target: dict[str, Any] | str, **extra: Any) -> tuple[int, Any]:
        target_pid = target if isinstance(target, str) else target["pid"]
        body = {"player": reporter["token"], "target": target_pid, "reason": "name", **extra}
        return call(self.port, "POST", f"/g/{slug}/report", body)

    # ---- names ----

    def test_blocked_words_are_refused_in_usernames_game_names_and_recipe_names(self) -> None:
        status, data = call(self.port, "POST", "/register", {"username": "xX_fuck_Xx", "password": "a-long-enough-pass"}, headers={"X-Session": ""})
        self.assertEqual(status, 400)
        self.assertIn("not allowed", data["error"])
        self.assertNotIn("fuck", data["error"])
        status, _ = call(self.port, "POST", "/register", {"username": self.name("friendly"), "password": "a-long-enough-pass"}, headers={"X-Session": ""})
        self.assertEqual(status, 200)
        namer = self.name("namer")
        user = self.session(namer)
        self.server.registry.solo.record(namer, {}, 1, 1, True)   # Create Recipes is open only after Solo mode
        self.assertEqual(call(self.port, "POST", "/games", {"name": "bitch club"}, headers=user)[0], 400)
        self.assertEqual(call(self.port, "POST", "/games", {"name": "Scunthorpe Wizards"}, headers=user)[0], 200)
        self.assertEqual(call(self.port, "POST", "/create/recipe", {"a": "air", "b": "air", "name": "Shit Storm"}, headers=user)[0], 400)
        self.assertEqual(call(self.port, "POST", "/create/recipe", {"a": "air", "b": "air", "name": "Wind Storm"}, headers=user)[0], 200)

    def test_a_blocked_name_does_not_burn_an_invite_code(self) -> None:
        import invites
        with tempfile.TemporaryDirectory() as tmp:
            self.server.invites = invites.InviteStore(Path(tmp) / "invites.json")
            self.addCleanup(setattr, self.server, "invites", None)
            (code,) = self.server.invites.create()
            status, _ = call(self.port, "POST", "/register", {"username": "asshole", "password": "a-long-enough-pass", "invite": code},
                             headers={"X-Session": ""})
            self.assertEqual(status, 400)
            status, _ = call(self.port, "POST", "/register", {"username": self.name("nice"), "password": "a-long-enough-pass", "invite": code},
                             headers={"X-Session": ""})
            self.assertEqual(status, 200)  # the code still worked

    # ---- games per account ----

    def test_the_game_limits_over_http(self) -> None:
        registry, original = self.server.registry, self.server.games_throttle
        registry.max_per_account = 1
        self.addCleanup(setattr, registry, "max_per_account", 0)
        self.addCleanup(setattr, self.server, "games_throttle", original)
        user = self.session(self.name("hog"))
        self.assertEqual(call(self.port, "POST", "/games", {"name": self.name("first")}, headers=user)[0], 200)
        status, data = call(self.port, "POST", "/games", {"name": self.name("second")}, headers=user)
        self.assertEqual(status, 409)
        self.assertIn("already have a game running", data["error"])
        self.assertEqual(call(self.port, "POST", "/games", {"name": self.name("other")}, headers=self.session(self.name("fair")))[0], 200)
        registry.max_per_account = 0
        self.server.games_throttle = Throttle(2, 3600.0)
        busy = self.session(self.name("busy"))
        statuses = [call(self.port, "POST", "/games", {"name": self.name("burst")}, headers=busy)[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    # ---- reports ----

    def test_a_report_reaches_the_administrator_with_no_trace_of_the_sender(self) -> None:
        a, b, c = (self.name(p) for p in ("alice", "bobby", "carol"))
        slug, (pa, pb, pc) = self.room(a, b, c)
        status, data = self.report(slug, pa, pb, note="  rude   name \x07 ", reason="behaviour")
        self.assertEqual((status, data), (200, {}))
        self.assertEqual(self.report(slug, pa, pb, reason="behaviour")[0], 200)  # a repeat is quietly ignored
        self.assertEqual(self.report(slug, pc, pb, reason="behaviour")[0], 200)
        status, data = call(self.port, "GET", "/ops/reports")
        self.assertEqual(status, 200)
        mine = [r for r in data["reports"] if r["user"] == b]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["count"], mine[0]["reason"], mine[0]["note"]), (2, "behaviour", "rude name"))
        self.assertTrue(mine[0]["game"].startswith("room"))
        everything = json.dumps(data) + json.dumps(call(self.port, "GET", "/ops/status")[1])
        for reporter in (a, c):
            self.assertNotIn(reporter, everything)
        self.assertGreaterEqual(call(self.port, "GET", "/ops/status")[1]["reports"]["open"], 1)
        # the activity log line has no names either
        with self.assertLogs("alchemy.auth", "INFO") as logged:
            self.report(slug, pb, pa, reason="other", note="text")
        self.assertTrue(all(n not in line for line in logged.output for n in (a, b, c)))

    def test_bad_reports_are_refused(self) -> None:
        a, b = self.name("dana"), self.name("evan")
        slug, (pa, pb) = self.room(a, b)
        self.assertEqual(self.report(slug, pa, pa)[0], 400)                          # yourself
        self.assertEqual(self.report(slug, pa, "zzzzzzzz")[0], 404)                  # nobody with that pid
        self.assertEqual(self.report(slug, pa, pb, reason="spam")[0], 400)           # not one of the reasons
        self.assertEqual(self.report(slug, pa, pb, note=["x"])[0], 400)              # a note must be text
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/report", {"player": "forged", "target": pb["pid"], "reason": "name"})[0], 403)
        self.assertEqual(call(self.port, "POST", "/g/no-such-game/report", {"player": pa["token"], "target": pb["pid"], "reason": "name"})[0], 404)
        self.assertEqual(call(self.port, "POST", f"/g/{slug}/report", {"target": pb["pid"], "reason": "name"})[0], 400)  # no token at all

    def test_reports_are_limited_per_account(self) -> None:
        original = self.server.reports_throttle
        self.server.reports_throttle = Throttle(1, 3600.0)
        self.addCleanup(setattr, self.server, "reports_throttle", original)
        names = [self.name(p) for p in ("fran", "gus", "hana")]
        slug, (pf, pg, ph) = self.room(*names)
        self.assertEqual(self.report(slug, pf, pg)[0], 200)
        status, data = self.report(slug, pf, ph)
        self.assertEqual(status, 429)
        self.assertIn("several reports", data["error"])

    def test_only_administrators_see_or_dismiss_reports(self) -> None:
        ordinary = self.session(self.name("plain"))
        self.assertEqual(call(self.port, "GET", "/ops/reports", headers=ordinary)[0], 403)
        self.assertEqual(call(self.port, "POST", "/ops/reports/dismiss", {"id": "x"}, headers=ordinary)[0], 403)
        self.assertEqual(call(self.port, "GET", "/ops/reports", headers={"X-Session": ""})[0], 401)
        a, b = self.name("ivy"), self.name("jack")
        slug, (pa, pb) = self.room(a, b)
        self.report(slug, pa, pb)
        report = next(r for r in call(self.port, "GET", "/ops/reports")[1]["reports"] if r["user"] == b)
        self.assertEqual(call(self.port, "POST", "/ops/reports/dismiss", {"id": report["id"]})[1], {"dismissed": True})
        self.assertEqual(call(self.port, "POST", "/ops/reports/dismiss", {"id": report["id"]})[1], {"dismissed": False})
        self.assertEqual(call(self.port, "POST", "/ops/reports/dismiss", {})[0], 400)

    def test_deleting_an_account_removes_reports_about_it(self) -> None:
        a, b = self.name("kim"), self.name("lee")
        slug, (pa, pb) = self.room(a, b)
        self.report(slug, pa, pb)
        status, data = call(self.port, "POST", "/ops/accounts/delete", {"username": b})
        self.assertEqual(status, 200)
        self.assertEqual(data["deleted"]["reports"], 1)
        self.assertFalse([r for r in call(self.port, "GET", "/ops/reports")[1]["reports"] if r["user"] == b])

    def test_erasure_without_a_report_store_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from accounts import AccountStore
            from daily import DailyStore
            from drafts import DraftStore
            from saves import SaveStore
            accounts = AccountStore(Path(tmp) / "a.json", iterations=1000)
            accounts.register("Someone", "a-long-enough-pass")
            summary = erase_account("Someone", accounts=accounts, saves=SaveStore(None), drafts=DraftStore(None),
                                    daily=DailyStore(None, load_recipes(RECIPES)))
            self.assertEqual(summary.reports, 0)


if __name__ == "__main__":
    unittest.main()

"""Account tools: disabling, changing passwords, erasing an account everywhere, and the administrator's operations page."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from accounts import AccountStore
from drafts import DraftStore
from erasure import erase_account
from games import GameRegistry
from invites import InviteStore
from recipes import load_recipes
from saves import SaveStore
from state import GameError
from test_coop import RECIPES, call, login_as, serve
from test_daily import BOOK as DAILY_BOOK
from test_daily import DAY, seeded_store
from throttle import Throttle

PASSWORD = "a-long-enough-pass"


class AccountToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.tmp.name) / "accounts.json", iterations=1000)
        for name in ("ann", "bob"):
            self.store.register(name, PASSWORD)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_admin_saves_a_temporary_password_to_a_file_and_never_prints_it(self) -> None:
        import contextlib
        import io
        import accounts_admin
        path = Path(self.tmp.name) / "accounts.json"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(accounts_admin.main(["x", "--file", str(path), "create-admin"]), 0)
        password_file = Path(self.tmp.name) / "admin-first-password.txt"
        temporary = password_file.read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(temporary), 20)
        self.assertNotIn(temporary, out.getvalue() + err.getvalue())
        fresh = AccountStore(path, iterations=1000)
        self.assertTrue(fresh.is_admin("admin"))
        self.assertTrue(fresh.must_change_password("admin"))
        fresh.login("admin", temporary)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(accounts_admin.main(["x", "--file", str(path), "create-admin"]), 1)  # no second one on top
        self.assertEqual(password_file.read_text(encoding="utf-8").strip(), temporary)

    def test_a_disabled_account_cannot_log_in_and_its_sessions_stop(self) -> None:
        token = self.store.login("ann", PASSWORD)[0]
        self.store.set_disabled("ann", True)
        self.assertIsNone(self.store.username(token))
        with self.assertRaises(GameError) as wrong:
            self.store.login("ann", "not the password")
        self.assertEqual(wrong.exception.status, 401)  # a guesser learns nothing about the account's state
        with self.assertRaises(GameError) as disabled:
            self.store.login("ann", PASSWORD)
        self.assertEqual(disabled.exception.status, 403)
        self.assertTrue(self.store.is_disabled("ann"))
        self.store.set_disabled("ann", False)
        self.assertEqual(self.store.login("ann", PASSWORD)[1], "ann")
        with self.assertRaises(GameError):
            self.store.set_disabled("nobody", True)

    def test_changing_a_password_checks_the_old_one_and_ends_other_sessions(self) -> None:
        other = self.store.login("ann", PASSWORD)[0]
        with self.assertRaises(GameError) as wrong:
            self.store.change_password("ann", "not it", "a-new-long-password")
        self.assertEqual(wrong.exception.status, 401)
        with self.assertRaises(GameError) as weak:
            self.store.change_password("ann", PASSWORD, "short")
        self.assertEqual(weak.exception.status, 400)
        self.assertEqual(self.store.username(other), "ann")  # failed attempts changed nothing
        fresh = self.store.change_password("ann", PASSWORD, "a-new-long-password")
        self.assertEqual(self.store.username(fresh), "ann")
        self.assertIsNone(self.store.username(other))
        self.assertEqual(self.store.login("ann", "a-new-long-password")[1], "ann")
        with self.assertRaises(GameError):
            self.store.login("ann", PASSWORD)

    def test_wrong_current_passwords_count_toward_the_lockout(self) -> None:
        for _ in range(5):
            with self.assertRaises(GameError):
                self.store.change_password("ann", "guess", "a-new-long-password", client="9.9.9.9")
        with self.assertRaises(GameError) as locked:
            self.store.change_password("ann", PASSWORD, "a-new-long-password", client="9.9.9.9")
        self.assertEqual(locked.exception.status, 429)

    def test_a_temporary_password_must_be_replaced(self) -> None:
        self.store.set_password("ann", "temporary-secret-1", must_change=True)
        self.assertTrue(self.store.must_change_password("ann"))
        self.assertFalse(self.store.must_change_password("bob"))
        self.store.change_password("ann", "temporary-secret-1", "my-own-new-password")
        self.assertFalse(self.store.must_change_password("ann"))

    def test_the_listing_has_no_secrets(self) -> None:
        self.store.set_admin("bob", True)
        self.store.set_disabled("ann", True)
        rows = {row["name"]: row for row in self.store.listing()}
        self.assertEqual(set(rows["ann"]), {"name", "admin", "bot", "pending", "disabled", "created"})
        self.assertTrue(rows["ann"]["disabled"] and rows["bob"]["admin"])
        self.assertNotIn("hash", json.dumps(rows))
        self.assertEqual(self.store.counts(), {"total": 2, "pending": 0, "disabled": 1, "admins": 1})
        self.store.login("bob", PASSWORD)
        self.assertEqual(self.store.session_count(), 1)


class ErasureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = AccountStore(root / "accounts.json", iterations=1000)
        for name in ("Ann", "Bob"):
            self.accounts.register(name, f"password-{name}")
        self.saves = SaveStore(root / "saves")
        registry = GameRegistry(load_recipes(RECIPES), saves=self.saves)
        for slug, players in (("solo", ["Ann"]), ("duo", ["Ann", "Bob"]), ("bobs", ["Bob"])):
            game = registry.create(slug, "Ann" if "Ann" in players else "Bob")
            tokens = [game.join(p) for p in players]
            tile = game.add_tile(tokens[0].token, "water", 0.3, 0.3)
            other = game.add_tile(tokens[0].token, "fire", 0.6, 0.6)
            game.grab_tile(tokens[0].token, tile["id"])
            game.combine(tokens[0].token, other["id"], tile=tile["id"])
            registry.end(slug, tokens[0].token, True)
        self.drafts = DraftStore(root / "drafts.json")
        self.drafts.create("Ann", DAILY_BOOK, "air", "air", "Wind Storm")
        self.drafts.create("Bob", DAILY_BOOK, "air", "fire", "Steamy")
        self.daily = seeded_store(root / "daily.json")
        for who in ("Ann", "Bob"):
            self.daily.use_attempt(DAY, "medium", who)
            self.daily.submit(DAY, "medium", who, 4)
        self.invites = InviteStore(root / "invites.json")
        (code,) = self.invites.create()
        self.invites.finish(self.invites.claim(code), "Ann")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def erase(self, name: str = "Ann") -> object:
        return erase_account(name, accounts=self.accounts, saves=self.saves, drafts=self.drafts, daily=self.daily,
                             invites=self.invites)

    def test_everything_under_the_name_goes(self) -> None:
        summary = self.erase("ann")  # any capitals
        self.assertEqual((summary.saves_deleted, summary.saves_kept_anonymised), (1, 1))
        self.assertEqual((summary.drafts, summary.invite_codes), (1, 1))
        self.assertGreaterEqual(summary.daily_entries, 2)
        self.assertEqual(self.accounts.usernames(), ["Bob"])
        self.assertEqual(self.saves.for_player("Ann"), [])
        self.assertFalse(self.saves.exists("solo"))
        self.assertEqual(self.daily.best(DAY, "medium", "Ann"), None)
        self.assertEqual(self.daily.attempts(DAY, "medium", "Ann"), 0)
        self.assertEqual(self.invites.listing()[0]["used_by"], "(deleted)")

    def test_other_players_keep_their_things_and_the_shared_game(self) -> None:
        self.erase()
        self.assertEqual(sorted(g["id"] for g in self.saves.for_player("Bob")), ["bobs", "duo"])
        duo = self.saves.load("duo")
        assert duo is not None
        self.assertEqual(duo["players"], ["deleted player", "Bob"])
        self.assertEqual(duo["creator"], "")  # Ann created it: nobody can save it again, which is what "no creator" means
        self.assertNotIn("Ann", json.dumps(duo))
        self.assertEqual(self.daily.best(DAY, "medium", "Bob"), 4)
        self.assertEqual(len(self.drafts.view("Bob", DAILY_BOOK)["recipes"]), 1)

    def test_an_unknown_account_changes_nothing(self) -> None:
        with self.assertRaises(GameError) as ctx:
            self.erase("Nobody")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(self.accounts.usernames(), ["Ann", "Bob"])
        self.assertEqual(len(self.saves.for_player("Ann")), 2)
        self.erase()
        with self.assertRaises(GameError):
            self.erase()  # erasing twice is a clean 404, not a half-done mess

    def test_the_same_name_can_register_again_afterwards_with_nothing_inherited(self) -> None:
        self.erase()
        self.accounts.register("Ann", "password-Ann-2")
        self.assertEqual(self.saves.for_player("Ann"), [])
        self.assertEqual(self.drafts.view("Ann", DAILY_BOOK)["recipes"], [])
        self.assertEqual(self.daily.attempts(DAY, "medium", "Ann"), 0)


class AccountEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=10))
        cls.server.login_throttle = Throttle(100_000, 1.0)  # these tests log in constantly from one address
        cls.server.accounts.set_admin("Host", True)  # the default session of call() is the Host account
        cls.counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def user(self) -> tuple[str, str]:
        """A fresh ordinary account: (name, session)."""
        type(self).counter += 1
        name = f"player{self.counter}"
        return name, login_as(self.port, name)

    def as_user(self, session: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return call(self.port, method, path, body, headers={"X-Session": session})

    # ---- self service -----------------------------------------------------

    def test_changing_my_password(self) -> None:
        name, session = self.user()
        old = f"password-{name}"
        status, data = self.as_user(session, "POST", "/account/change-password", {"current": "wrong", "new": "x" * 12})
        self.assertEqual(status, 403)  # not 401: the pages sign you out on a 401, and a typo must not do that
        self.assertIn("not right", data["error"])
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 200)  # still logged in
        self.assertEqual(self.as_user(session, "POST", "/account/change-password", {"current": old, "new": "short"})[0], 400)
        status, data = self.as_user(session, "POST", "/account/change-password", {"current": old, "new": "my-brand-new-password"})
        self.assertEqual(status, 200)
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 401)  # the old session ended
        self.assertEqual(self.as_user(data["session"], "GET", "/me")[1]["username"], name)  # the new one works
        self.assertEqual(call(self.port, "POST", "/login", {"username": name, "password": old}, headers={"X-Session": ""})[0], 401)
        self.assertEqual(call(self.port, "POST", "/login", {"username": name, "password": "my-brand-new-password"},
                              headers={"X-Session": ""})[0], 200)

    def test_deleting_my_account_needs_my_password(self) -> None:
        name, session = self.user()
        self.assertEqual(self.as_user(session, "POST", "/account/delete", {"password": "wrong"})[0], 403)
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 200)  # still here
        status, data = self.as_user(session, "POST", "/account/delete", {"password": f"password-{name}"})
        self.assertEqual(status, 200)
        self.assertEqual(set(data["deleted"]), {"saves_deleted", "saves_kept_anonymised", "drafts", "daily_entries", "invite_codes", "reports", "solo"})
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 401)
        self.assertEqual(call(self.port, "POST", "/login", {"username": name, "password": f"password-{name}"},
                              headers={"X-Session": ""})[0], 401)

    def test_both_need_a_login(self) -> None:
        for path in ("/account/change-password", "/account/delete"):
            self.assertEqual(self.as_user("", "POST", path, {"current": "a", "new": "b", "password": "c"})[0], 401)

    # ---- the operations page ---------------------------------------------

    def test_every_operation_is_for_administrators_only(self) -> None:
        _, session = self.user()
        for path in ("/ops/status", "/ops/accounts", "/ops/games", "/ops/invites"):
            self.assertEqual(self.as_user(session, "GET", path)[0], 403, path)
        for path in ("/ops/invites/create", "/ops/invites/revoke", "/ops/games/end", "/ops/accounts/verify",
                     "/ops/accounts/enable", "/ops/accounts/disable", "/ops/accounts/reset-password", "/ops/accounts/delete"):
            self.assertEqual(self.as_user(session, "POST", path, {"username": "x", "id": "x", "count": 1})[0], 403, path)
        self.assertEqual(self.as_user("", "GET", "/ops/status")[0], 401)

    def test_the_overview_has_counts_and_no_secrets(self) -> None:
        self.user()
        status, data = call(self.port, "GET", "/ops/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"uptime_seconds", "accounts", "sessions", "games", "since_start", "reports", "signup"})
        self.assertGreaterEqual(data["accounts"]["total"], 2)
        self.assertGreaterEqual(data["since_start"].get("logins", 0), 1)
        text = json.dumps(data) + json.dumps(call(self.port, "GET", "/ops/accounts")[1])
        for secret in ("hash", "salt", "@", "password", "iterations"):
            self.assertNotIn(secret, text.lower().replace("passwords", ""))

    def test_disabling_and_enabling_an_account(self) -> None:
        name, session = self.user()
        self.assertEqual(call(self.port, "POST", "/ops/accounts/disable", {"username": name})[0], 200)
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 401)
        status, data = call(self.port, "POST", "/login", {"username": name, "password": f"password-{name}"}, headers={"X-Session": ""})
        self.assertEqual((status, "disabled" in data["error"]), (403, True))
        self.assertTrue(any(row["name"] == name and row["disabled"] for row in call(self.port, "GET", "/ops/accounts")[1]["accounts"]))
        self.assertEqual(call(self.port, "POST", "/ops/accounts/enable", {"username": name})[0], 200)
        self.assertEqual(call(self.port, "POST", "/login", {"username": name, "password": f"password-{name}"},
                              headers={"X-Session": ""})[0], 200)
        self.assertEqual(call(self.port, "POST", "/ops/accounts/disable", {"username": "no-such-person"})[0], 404)
        self.assertEqual(call(self.port, "POST", "/ops/accounts/disable", {})[0], 400)

    def test_a_temporary_password_forces_a_change(self) -> None:
        name, session = self.user()
        status, data = call(self.port, "POST", "/ops/accounts/reset-password", {"username": name})
        self.assertEqual(status, 200)
        temporary = data["temporary_password"]
        self.assertGreaterEqual(len(temporary), 8)
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 401)  # the old password and its sessions are gone
        status, login = call(self.port, "POST", "/login", {"username": name, "password": temporary}, headers={"X-Session": ""})
        self.assertEqual((status, login.get("must_change_password")), (200, True))
        self.assertTrue(self.as_user(login["session"], "GET", "/me")[1]["must_change_password"])
        status, changed = self.as_user(login["session"], "POST", "/account/change-password", {"current": temporary, "new": "chosen-by-the-owner"})
        self.assertEqual(status, 200)
        self.assertFalse(self.as_user(changed["session"], "GET", "/me")[1]["must_change_password"])
        status, again = call(self.port, "POST", "/login", {"username": name, "password": "chosen-by-the-owner"}, headers={"X-Session": ""})
        self.assertNotIn("must_change_password", again)

    def test_deleting_an_account_from_the_page(self) -> None:
        name, session = self.user()
        status, data = call(self.port, "POST", "/ops/accounts/delete", {"username": name})
        self.assertEqual(status, 200)
        self.assertIn("saves_deleted", data["deleted"])
        self.assertEqual(self.as_user(session, "GET", "/me")[0], 401)
        self.assertEqual(call(self.port, "POST", "/ops/accounts/delete", {"username": name})[0], 404)

    def test_administrators_cannot_be_changed_from_the_page(self) -> None:
        boss_name, _ = self.user()
        self.server.accounts.set_admin(boss_name, True)
        self.addCleanup(self.server.accounts.set_admin, boss_name, False)
        for action in ("disable", "delete", "reset-password"):
            status, data = call(self.port, "POST", f"/ops/accounts/{action}", {"username": boss_name})
            self.assertEqual(status, 403, action)
            self.assertIn("accounts_admin.py", data["error"])
        self.assertEqual(call(self.port, "POST", "/ops/accounts/nonsense", {"username": boss_name})[0], 404)
        self.assertTrue(self.server.accounts.is_admin(boss_name))  # untouched

    def test_invite_codes_from_the_page(self) -> None:
        self.assertEqual(call(self.port, "GET", "/ops/invites")[1], {"enabled": False, "codes": []})
        self.assertEqual(call(self.port, "POST", "/ops/invites/create", {"count": 2})[0], 409)  # not switched on
        with tempfile.TemporaryDirectory() as tmp:
            self.server.invites = InviteStore(Path(tmp) / "invites.json")
            self.addCleanup(setattr, self.server, "invites", None)
            status, data = call(self.port, "POST", "/ops/invites/create", {"count": 2, "note": "friends"})
            self.assertEqual(status, 200)
            self.assertEqual(len(data["codes"]), 2)
            listing = call(self.port, "GET", "/ops/invites")[1]
            self.assertTrue(listing["enabled"])
            self.assertEqual([c["note"] for c in listing["codes"]], ["friends", "friends"])
            for code in data["codes"]:
                self.assertNotIn(code.replace("-", ""), json.dumps(listing))  # only hashes are kept: codes show once
            self.assertEqual(call(self.port, "POST", "/ops/invites/revoke", {"id": listing["codes"][0]["id"]})[1], {"revoked": 1})
            self.assertEqual(call(self.port, "POST", "/ops/invites/create", {"count": "many"})[0], 400)

    def test_ending_a_game_from_the_page(self) -> None:
        name, session = self.user()
        status, game = self.as_user(session, "POST", "/games", {"name": f"{name} room"})
        self.assertEqual(status, 200)
        listed = [g for g in call(self.port, "GET", "/ops/games")[1]["games"] if g["id"] == game["id"]]
        self.assertEqual((len(listed), listed[0]["kind"], listed[0]["private"]), (1, "game", False))
        self.assertEqual(call(self.port, "POST", "/ops/games/end", {"id": game["id"]})[0], 200)
        self.assertEqual(self.server.registry.get(game["id"]), None)
        self.assertEqual(call(self.port, "POST", "/ops/games/end", {"id": game["id"]})[0], 404)
        self.assertEqual(call(self.port, "POST", "/ops/games/end", {})[0], 400)

    def test_counters_move(self) -> None:
        before = call(self.port, "GET", "/ops/status")[1]["since_start"]
        call(self.port, "POST", "/login", {"username": "nobody-at-all", "password": "wrong-password"}, headers={"X-Session": ""})
        name, _ = self.user()
        call(self.port, "POST", "/login", {"username": name, "password": f"password-{name}"}, headers={"X-Session": ""})
        after = call(self.port, "GET", "/ops/status")[1]["since_start"]
        self.assertEqual(after.get("login_failures", 0), before.get("login_failures", 0) + 1)
        self.assertGreater(after.get("logins", 0), before.get("logins", 0))
        self.assertGreater(after.get("registrations", 0), before.get("registrations", 0))


if __name__ == "__main__":
    unittest.main()

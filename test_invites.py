"""Single-use invite codes: the store, the signup flow over HTTP and the admin command."""

from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path

import accounts_admin
from games import GameRegistry
from invites import InviteStore, normalize
from state import GameError
from test_coop import RECIPES, call, load_recipes, serve


class InviteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "invites.json"
        self.store = InviteStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_codes_work_once_and_ignore_case_and_hyphens(self) -> None:
        (code,) = self.store.create()
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        handle = self.store.claim(code.lower().replace("-", " "))
        self.store.finish(handle, "ann")
        with self.assertRaises(GameError) as ctx:
            self.store.claim(code)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual([row["used_by"] for row in self.store.listing()], ["ann"])

    def test_a_failed_signup_gives_the_code_back(self) -> None:
        (code,) = self.store.create()
        handle = self.store.claim(code)
        with self.assertRaises(GameError):
            self.store.claim(code)  # held by the signup in progress
        self.store.release(handle)
        self.store.finish(self.store.claim(code), "bob")

    def test_unknown_malformed_and_non_text_codes_are_refused(self) -> None:
        self.store.create()
        for bad in ("ABCD-EFGH-JKLM", "short", "", None, 12345, ["x"], "A" * 200):
            with self.assertRaises(GameError) as ctx:
                self.store.claim(bad)
            self.assertEqual(ctx.exception.status, 403, bad)
        self.assertEqual(normalize(None), "")

    def test_only_hashes_are_stored(self) -> None:
        codes = self.store.create(3, note="for the club")
        text = self.path.read_text(encoding="utf-8")
        for code in codes:
            self.assertNotIn(code.replace("-", ""), text)
            self.assertNotIn(code, text)
        self.assertIn("for the club", text)

    def test_revoke_removes_unused_codes_only(self) -> None:
        keep, drop = self.store.create(2)
        self.store.finish(self.store.claim(keep), "cy")
        used_id = next(r["id"] for r in self.store.listing() if r["used_by"])
        unused_id = next(r["id"] for r in self.store.listing() if not r["used_by"])
        self.assertEqual(self.store.revoke(used_id), 0)
        self.assertEqual(self.store.revoke(unused_id), 1)
        with self.assertRaises(GameError):
            self.store.claim(drop)
        with self.assertRaises(GameError):
            self.store.revoke("ab")  # too short to be a safe handle

    def test_two_signups_racing_for_one_code_only_one_wins(self) -> None:
        (code,) = self.store.create()
        wins: list[str] = []

        def attempt() -> None:
            try:
                wins.append(self.store.claim(code))
            except GameError:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(wins), 1)

    def test_a_second_store_sees_codes_made_while_the_server_runs(self) -> None:
        (code,) = InviteStore(self.path).create()  # e.g. accounts_admin.py in another process
        self.store.finish(self.store.claim(code), "dee")


class SignupOverHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=5))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.invites = InviteStore(Path(cls.tmp.name) / "invites.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.server.invites = self.invites

    def tearDown(self) -> None:
        self.server.invites = None

    def register(self, name: str, invite: object = None) -> tuple[int, dict]:
        body = {"username": name, "password": "a-long-enough-pass"}
        if invite is not None:
            body["invite"] = invite
        status, data = call(self.port, "POST", "/register", body)
        return status, data

    def test_the_page_can_ask_whether_a_code_is_needed(self) -> None:
        self.assertTrue(call(self.port, "GET", "/auth-config")[1]["invite_required"])
        self.server.invites = None
        self.assertFalse(call(self.port, "GET", "/auth-config")[1]["invite_required"])

    def test_signup_needs_a_valid_unused_code(self) -> None:
        (code,) = self.invites.create()
        self.assertEqual(self.register("nocode")[0], 403)
        self.assertEqual(self.register("wrongcode", "AAAA-BBBB-CCCC")[0], 403)
        status, data = self.register("goodcode", code)
        self.assertEqual(status, 200)
        self.assertEqual(data["username"], "goodcode")
        status, data = self.register("reuser", code)
        self.assertEqual((status, "invite" in data["error"]), (403, True))

    def test_a_taken_username_does_not_burn_the_code(self) -> None:
        first, second = self.invites.create(2)
        self.assertEqual(self.register("sameone", first)[0], 200)
        self.assertEqual(self.register("sameone", second)[0], 409)  # name taken
        self.assertEqual(self.register("othername", second)[0], 200)  # the code still works

    def test_a_weak_password_does_not_burn_the_code(self) -> None:
        (code,) = self.invites.create()
        status, _ = call(self.port, "POST", "/register", {"username": "weakling", "password": "x", "invite": code})
        self.assertEqual(status, 400)
        self.assertEqual(self.register("strongling", code)[0], 200)

    def test_login_never_asks_for_a_code(self) -> None:
        (code,) = self.invites.create()
        self.register("returning", code)
        status, _ = call(self.port, "POST", "/login", {"username": "returning", "password": "a-long-enough-pass"})
        self.assertEqual(status, 200)

    def test_without_the_option_signup_is_open_as_before(self) -> None:
        self.server.invites = None
        self.assertEqual(self.register("openuser")[0], 200)


class AdminCommandTests(unittest.TestCase):
    def run_cli(self, tmp: str, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        argv = ["accounts_admin.py", "--file", f"{tmp}/accounts.json", "--invites-file", f"{tmp}/invites.json", *args]
        with contextlib.redirect_stdout(out):
            code = accounts_admin.main(argv)
        return code, out.getvalue()

    def test_create_list_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(tmp, "invite", "create", "--count", "2", "--note", "beta")
            self.assertEqual(code, 0)
            self.assertEqual(len([line for line in out.splitlines() if line.startswith("  ")]), 2)
            code, out = self.run_cli(tmp, "invite", "list")
            self.assertEqual(out.count("unused"), 2)
            self.assertIn("(beta)", out)
            handle = out.split()[0]
            self.assertEqual(self.run_cli(tmp, "invite", "revoke", handle)[0], 0)
            self.assertEqual(self.run_cli(tmp, "invite", "list")[1].count("unused"), 1)
            self.assertEqual(self.run_cli(tmp, "invite", "revoke", "zzzz")[0], 1)


if __name__ == "__main__":
    unittest.main()

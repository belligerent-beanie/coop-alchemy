"""The in-memory view of accounts.json: fast on the hot path, never stale, never corrupted by a failed write."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from accounts import AccountStore
from state import GameError

PASSWORD = "a-long-enough-pass"


class AccountCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "accounts.json"
        self.store = AccountStore(self.path, iterations=1000)
        for name in ("ann", "bob"):
            self.store.register(name, PASSWORD)
        self.token = self.store.login("ann", PASSWORD)[0]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def loads_during(self, action) -> int:  # type: ignore[no-untyped-def]
        """How many times the file was parsed while `action` ran."""
        real = self.store._load
        calls = []

        def counting():  # type: ignore[no-untyped-def]
            calls.append(1)
            return real()

        with mock.patch.object(self.store, "_load", counting):
            action()
        return len(calls)

    def test_repeated_lookups_do_not_reread_the_file(self) -> None:
        self.store.username(self.token)  # the first one may parse
        def hot_path() -> None:
            for _ in range(200):
                self.assertEqual(self.store.username(self.token), "ann")
                self.assertFalse(self.store.is_admin("ann"))
                self.assertFalse(self.store.is_bot("bob"))
                self.assertEqual(self.store.usernames(), ["ann", "bob"])
        self.assertEqual(self.loads_during(hot_path), 0)

    def test_our_own_writes_are_seen_at_once(self) -> None:
        self.store.username(self.token)
        self.store.register("cyd", PASSWORD)
        self.assertEqual(self.store.usernames(), ["ann", "bob", "cyd"])
        self.store.set_admin("cyd", True)
        self.assertTrue(self.store.is_admin("cyd"))
        self.store.set_bot("bob", True)
        self.assertEqual(self.store.bots(), ["bob"])

    def test_our_own_writes_do_not_depend_on_the_file_timestamp(self) -> None:
        """On filesystems with coarse timestamps a same-size rewrite can look unchanged: our own saves must not rely on that."""
        self.assertEqual(self.store.username(self.token), "ann")
        with mock.patch.object(self.store, "_stamp", lambda: (1, 1)):  # a file that never seems to change
            self.assertEqual(self.store.username(self.token), "ann")  # warms the cache under the frozen stamp
            self.store.set_password("ann", "a-brand-new-password")  # same-length hash and salt: the same file size
            self.assertIsNone(self.store.username(self.token))  # the old session must die at once
            self.store.set_admin("bob", True)
            self.assertTrue(self.store.is_admin("bob"))

    def test_changes_made_by_another_process_are_seen_without_a_restart(self) -> None:
        self.assertFalse(self.store.is_admin("bob"))  # warms the cache
        admin_tool = AccountStore(self.path, iterations=1000)  # what accounts_admin.py does
        admin_tool.set_admin("bob", True)
        self.assertTrue(self.store.is_admin("bob"))
        admin_tool.set_bot("ann", True)
        self.assertEqual(self.store.bots(), ["ann"])

    def test_deleting_or_resetting_an_account_elsewhere_ends_its_sessions(self) -> None:
        self.assertEqual(self.store.username(self.token), "ann")
        admin_tool = AccountStore(self.path, iterations=1000)
        admin_tool.set_password("ann", "a-brand-new-password")
        self.assertIsNone(self.store.username(self.token))  # the salt changed, so the old session is void
        bob = self.store.login("bob", PASSWORD)[0]
        self.assertEqual(self.store.username(bob), "bob")
        admin_tool.delete("bob")
        self.assertIsNone(self.store.username(bob))

    def test_a_failed_write_never_shows_up_for_readers(self) -> None:
        self.store.usernames()
        with mock.patch("accounts.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(GameError):
                self.store.register("ghost", PASSWORD)
            with self.assertRaises(GameError):
                self.store.set_admin("ann", True)
        self.assertEqual(self.store.usernames(), ["ann", "bob"])
        self.assertFalse(self.store.is_admin("ann"))
        self.assertEqual(AccountStore(self.path, iterations=1000).usernames(), ["ann", "bob"])  # nor on disk

    def test_a_rejected_signup_leaves_the_view_alone(self) -> None:
        self.store.usernames()
        for _ in range(3):
            with self.assertRaises(GameError):
                self.store.register("ANN", PASSWORD)  # name taken
        self.assertEqual(self.store.usernames(), ["ann", "bob"])

    def test_a_deleted_or_damaged_file_is_noticed(self) -> None:
        self.assertEqual(self.store.usernames(), ["ann", "bob"])
        os.remove(self.path)
        self.assertEqual(self.store.usernames(), [])
        self.assertIsNone(self.store.username(self.token))
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(GameError) as ctx:
            self.store.usernames()
        self.assertEqual(ctx.exception.status, 500)  # refuses to carry on rather than guess

    def test_login_of_unconfirmed_accounts_still_cleans_up_without_extra_reads(self) -> None:
        now = [1000.0]
        store = AccountStore(Path(self.tmp.name) / "pending.json", clock=lambda: now[0], iterations=1000)
        store.register("new", PASSWORD, "new@example.org")
        store.register("okay", PASSWORD)
        self.assertEqual(store.login("okay", PASSWORD)[1], "okay")  # nothing stale: a plain login
        now[0] += 25 * 3600
        with self.assertRaises(GameError):
            store.login("new", PASSWORD)
        self.assertEqual(store.usernames(), ["okay"])  # the stale pending account is gone


if __name__ == "__main__":
    unittest.main()

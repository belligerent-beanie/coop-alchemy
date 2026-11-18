"""Accounts nobody has logged in to for 90 days are deleted (accounts.py last logins, inactive.py, the --inactive-days option)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accounts import AccountActive, AccountStore
from games import GameRegistry
from inactive import CHECK_SECONDS, FIRST_CHECK_SECONDS, inactive_loop, purge_inactive
from server import erase_inactive
from state import GameError
from test_coop import RECIPES, load_recipes, serve

PASSWORD = "correct horse battery"
DAY = 86400.0
NINETY = 90 * DAY


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class InactiveAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "accounts.json"
        self.clock = Clock()
        self.store = AccountStore(self.path, clock=self.clock, iterations=1000)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def record(self, name: str) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))["accounts"][name.casefold()]

    def test_a_new_account_has_a_last_login(self) -> None:
        self.store.register("ann", PASSWORD)
        self.assertEqual(self.record("ann")["last_login"], int(self.clock.now))

    def test_logging_in_updates_it_at_most_once_a_day(self) -> None:
        self.store.register("ann", PASSWORD)
        first = self.record("ann")["last_login"]
        self.clock.now += 3600
        self.store.login("ann", PASSWORD)
        self.assertEqual(self.record("ann")["last_login"], first)   # an hour later: not rewritten
        self.clock.now += 2 * DAY
        self.store.login("ann", PASSWORD)
        self.assertEqual(self.record("ann")["last_login"], int(self.clock.now))

    def test_idle_for_more_than_the_period_is_listed(self) -> None:
        self.store.register("ann", PASSWORD)
        self.clock.now += NINETY - DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), [])
        self.clock.now += 2 * DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), ["ann"])

    def test_logging_in_starts_the_period_again(self) -> None:
        self.store.register("ann", PASSWORD)
        self.clock.now += 80 * DAY
        self.store.login("ann", PASSWORD)
        self.clock.now += 80 * DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), [])

    def test_admins_bots_disabled_and_unverified_accounts_are_never_listed(self) -> None:
        for name in ("ann", "bot", "ban", "dee", "unverified"):
            self.store.register(name, PASSWORD)
        self.store.set_admin("ann", True)
        self.store.set_bot("bot", True)
        self.store.set_disabled("ban", True)
        accounts = json.loads(self.path.read_text(encoding="utf-8"))
        accounts["accounts"]["unverified"]["verified"] = False
        self.path.write_text(json.dumps(accounts), encoding="utf-8")
        self.clock.now += 400 * DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), ["dee"])
        for exempt in ("ann", "bot", "ban", "unverified"):
            with self.assertRaises(AccountActive, msg=exempt):
                self.store.delete_if_inactive(exempt, NINETY)
        with self.assertRaises(GameError) as missing:
            self.store.delete_if_inactive("nobody", NINETY)
        self.assertEqual(missing.exception.status, 404)

    def test_an_account_from_before_last_logins_were_recorded_gets_the_whole_period(self) -> None:
        self.store.register("old", PASSWORD)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        del data["accounts"]["old"]["last_login"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.clock.now += 500 * DAY   # created long ago, but nothing says it was idle
        self.assertEqual(self.store.inactive_accounts(NINETY), [])
        self.assertEqual(self.record("old")["last_login"], int(self.clock.now))   # stamped now
        self.clock.now += NINETY + DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), ["old"])

    def test_purge_erases_idle_accounts(self) -> None:
        for name in ("ann", "bob", "cat"):
            self.store.register(name, PASSWORD)
        self.clock.now += NINETY + DAY
        self.store.login("bob", PASSWORD)   # bob is back, so only ann and cat are idle
        erased: list[str] = []

        def erase(name: str) -> None:
            self.store.delete_if_inactive(name, NINETY)
            erased.append(name)

        self.assertEqual(purge_inactive(self.store, erase, NINETY), ["ann", "cat"])
        self.assertEqual(erased, ["ann", "cat"])
        self.assertEqual(self.store.login("bob", PASSWORD)[1], "bob")

    def test_someone_who_comes_back_after_the_list_was_made_keeps_their_account(self) -> None:
        for name in ("ann", "cat"):
            self.store.register(name, PASSWORD)
        self.clock.now += NINETY + DAY
        stale_list = self.store.inactive_accounts(NINETY)
        self.assertEqual(stale_list, ["ann", "cat"])
        self.store.login("ann", PASSWORD)   # ann returns after the list was made

        class Stale(AccountStore):
            def inactive_accounts(self, max_idle_seconds: float) -> list[str]:
                return stale_list

        store = Stale(self.path, clock=self.clock, iterations=1000)
        erased: list[str] = []

        def erase(name: str) -> None:
            store.delete_if_inactive(name, NINETY)   # what erasure.erase_account does first: check and delete under one lock
            erased.append(name)

        self.assertEqual(purge_inactive(store, erase, NINETY), ["cat"])
        self.assertEqual(erased, ["cat"])
        self.assertEqual(store.login("ann", PASSWORD)[1], "ann")

    def test_a_malformed_last_login_is_treated_as_missing_not_as_ancient(self) -> None:
        for name, bad in (("ann", None), ("bob", "yesterday"), ("cat", True)):
            self.store.register(name, PASSWORD)
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data["accounts"][name]["last_login"] = bad
            self.path.write_text(json.dumps(data), encoding="utf-8")
        self.clock.now += 400 * DAY
        for name in ("ann", "bob", "cat"):
            with self.assertRaises(AccountActive):
                self.store.delete_if_inactive(name, NINETY)   # unknown idle time is never grounds for deleting
        self.assertEqual(self.store.inactive_accounts(NINETY), [])   # the check stamps them with the present
        self.assertEqual(self.record("ann")["last_login"], int(self.clock.now))
        self.clock.now += NINETY + DAY
        self.assertEqual(self.store.inactive_accounts(NINETY), ["ann", "bob", "cat"])

    def test_a_failing_erase_does_not_stop_the_others(self) -> None:
        for name in ("ann", "bob"):
            self.store.register(name, PASSWORD)
        self.clock.now += NINETY + DAY

        def erase(name: str) -> None:
            if name == "ann":
                raise GameError(500, "disk full")
            self.store.delete(name)

        self.assertEqual(purge_inactive(self.store, erase, NINETY), ["bob"])

    def test_the_loop_waits_then_checks_every_few_hours_and_survives_a_failure(self) -> None:
        waits: list[float] = []
        calls: list[str] = []

        class Stop(Exception):
            pass

        def sleep(seconds: float) -> None:
            waits.append(seconds)
            if len(waits) == 3:
                raise Stop

        def erase(name: str) -> None:
            calls.append(name)

        class Broken(AccountStore):
            def inactive_accounts(self, max_idle_seconds: float) -> list[str]:
                raise RuntimeError("boom")

        with self.assertRaises(Stop):
            inactive_loop(Broken(self.path, iterations=1000), erase, NINETY, sleep=sleep)
        self.assertEqual(waits, [FIRST_CHECK_SECONDS, CHECK_SECONDS, CHECK_SECONDS])


class EraseInactiveWiringTests(unittest.TestCase):
    """server.erase_inactive(): the routine the background job calls, against a real server and registry."""

    def setUp(self) -> None:
        self.registry = GameRegistry(load_recipes(RECIPES))
        self.server, self.port = serve(self.registry)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.clock = Clock()
        self.server.accounts._clock = self.clock   # noqa: SLF001 (the test controls time)

    def test_an_idle_account_is_erased_everywhere_and_counted(self) -> None:
        self.server.accounts.register("Idle", PASSWORD)
        self.clock.now += NINETY + DAY
        erase_inactive(self.server, "Idle", NINETY)
        self.assertFalse(self.server.accounts.is_active("Idle"))
        self.assertEqual(self.server.stats["accounts_purged"], 1)

    def test_an_account_that_came_back_is_not_erased(self) -> None:
        self.server.accounts.register("Back", PASSWORD)
        self.clock.now += NINETY + DAY
        self.server.accounts.login("Back", PASSWORD)
        with self.assertRaises(AccountActive):
            erase_inactive(self.server, "Back", NINETY)
        self.assertTrue(self.server.accounts.is_active("Back"))
        self.assertEqual(self.server.stats["accounts_purged"], 0)


if __name__ == "__main__":
    unittest.main()

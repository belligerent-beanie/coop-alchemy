"""Accounts nobody has logged in to for 90 days are deleted (accounts.py last logins, inactive.py, the --inactive-days option)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accounts import AccountStore
from inactive import CHECK_SECONDS, FIRST_CHECK_SECONDS, inactive_loop, purge_inactive
from state import GameError

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
        self.assertFalse(self.store.still_inactive("ann", NINETY))
        self.assertFalse(self.store.still_inactive("nobody", NINETY))

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
            erased.append(name)
            self.store.delete(name)

        self.assertEqual(purge_inactive(self.store, erase, NINETY), ["ann", "cat"])
        self.assertEqual(erased, ["ann", "cat"])
        self.assertEqual(self.store.login("bob", PASSWORD)[1], "bob")

    def test_someone_who_comes_back_after_the_list_was_made_keeps_their_account(self) -> None:
        for name in ("ann", "cat"):
            self.store.register(name, PASSWORD)
        self.clock.now += NINETY + DAY
        stale_list = self.store.inactive_accounts(NINETY)
        self.assertEqual(stale_list, ["ann", "cat"])
        self.store.login("ann", PASSWORD)   # ann returns after the list, before her turn

        class Stale(AccountStore):
            def inactive_accounts(self, max_idle_seconds: float) -> list[str]:
                return stale_list

        store = Stale(self.path, clock=self.clock, iterations=1000)
        erased: list[str] = []
        self.assertEqual(purge_inactive(store, lambda name: (erased.append(name), store.delete(name)) and None, NINETY), ["cat"])
        self.assertEqual(erased, ["cat"])

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


if __name__ == "__main__":
    unittest.main()

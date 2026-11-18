"""The most accounts a server holds (--max-accounts): the default is 5000, and signup is refused beyond the limit."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accounts import MAX_ACCOUNTS, AccountStore, account_limit
from state import GameError

PASSWORD = "correct horse battery"


class AccountCapTests(unittest.TestCase):
    def test_default_limit(self) -> None:
        self.assertEqual(MAX_ACCOUNTS, 5000)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(AccountStore(Path(tmp) / "accounts.json").max_accounts, 5000)

    def test_a_zero_or_negative_option_cannot_lock_everybody_out(self) -> None:
        self.assertEqual(account_limit(0), 1)
        self.assertEqual(account_limit(-7), 1)
        self.assertEqual(account_limit(250), 250)

    def test_signup_is_refused_at_the_limit_and_works_again_below_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "accounts.json", iterations=1000, max_accounts=2)
            store.register("ann", PASSWORD)
            store.register("bob", PASSWORD)
            with self.assertRaises(GameError) as refused:
                store.register("cat", PASSWORD)
            self.assertEqual(refused.exception.status, 409)
            self.assertIn("limit on accounts", refused.exception.message)
            store.delete("bob")
            self.assertEqual(store.register("cat", PASSWORD), "cat")


if __name__ == "__main__":
    unittest.main()

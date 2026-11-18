"""Deleting accounts nobody has logged in to for a long time (the rule the terms, the privacy page and the login page announce).

The accounts file records each account's last login (accounts.py). Every few hours the server asks for the accounts idle for longer
than --inactive-days (default 90) and erases each one the same way "delete my account" does (erasure.py). Administrators, bots,
disabled and unverified accounts are never listed (see AccountStore.inactive_accounts).
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from accounts import AccountActive, AccountStore
from state import GameError

log = logging.getLogger("alchemy.inactive")

CHECK_SECONDS = 6 * 3600.0
FIRST_CHECK_SECONDS = 60.0   # soon after a start, so accounts from before last logins were recorded are stamped at once


def purge_inactive(accounts: AccountStore, erase: Callable[[str], None], max_idle_seconds: float) -> list[str]:
    """Erase every account idle for longer than `max_idle_seconds`; returns the usernames deleted.

    `erase(username)` must delete the account only if it is still idle, atomically (erasure.erase_account with idle_seconds), and
    raise AccountActive if its owner has just come back: that account is kept. One account failing to erase is logged and
    does not stop the rest.
    """
    deleted: list[str] = []
    for username in accounts.inactive_accounts(max_idle_seconds):
        try:
            erase(username)
        except AccountActive:
            continue   # logged in after the list was made
        except GameError as exc:
            log.warning("inactive account %s could not be deleted: %s", username, exc.message)
            continue
        deleted.append(username)
        log.info("inactive account deleted: user %s", username)
    return deleted


def inactive_loop(accounts: AccountStore, erase: Callable[[str], None], max_idle_seconds: float,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """Check for idle accounts soon after the start and then every few hours, forever. A failure here must never stop the server."""
    wait = FIRST_CHECK_SECONDS
    while True:
        sleep(wait)
        wait = CHECK_SECONDS
        try:
            purge_inactive(accounts, erase, max_idle_seconds)
        except Exception:
            log.exception("checking for inactive accounts failed")

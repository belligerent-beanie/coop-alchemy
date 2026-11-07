"""Deleting an account and everything the server keeps under its username.

One routine, used by the player's own "delete my account", the administrator's page and `accounts_admin.py delete`, so
they can never disagree about what deletion means:

  - the account itself (its password hash, email if any, and all its login sessions)
  - its recipe drafts and promotion proposals
  - its daily-challenge scores and attempt counts
  - its solo-mode progress (which elements it found, and whether it finished)
  - saved games: ones nobody else played are deleted; in shared ones the name is replaced by "deleted player" and the game
    stays for the others
  - its name on the invite code it used (replaced by a placeholder)
  - any reports about it

Not touched: the server's own log file, which records addresses and usernames and is deleted by age (see DEPLOYMENT.md), and
games running right now, which end on their own and are not saved under the name. Running games and race rooms keep the
name until they close.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from accounts import AccountStore
from daily import DailyStore
from drafts import DraftStore
from invites import InviteStore
from reports import ReportStore
from saves import SaveStore
from solo import SoloStore


@dataclass
class ErasureSummary:
    saves_deleted: int
    saves_kept_anonymised: int
    drafts: int
    daily_entries: int
    invite_codes: int
    reports: int = 0
    solo: int = 0

    def as_json(self) -> dict[str, int]:
        return asdict(self)


def erase_account(username: str, *, accounts: AccountStore, saves: SaveStore, drafts: DraftStore, daily: DailyStore,
                  invites: InviteStore | None = None, reports: ReportStore | None = None,
                  solo: SoloStore | None = None) -> ErasureSummary:
    """Delete `username` everywhere. Raises GameError(404) first, touching nothing, if there is no such account."""
    accounts.delete(username)  # the 404 for an unknown account happens here, before anything else is changed
    deleted, rewritten = saves.anonymize_player(username)
    return ErasureSummary(
        saves_deleted=len(deleted),
        saves_kept_anonymised=len(rewritten),
        drafts=drafts.delete_user(username),
        daily_entries=daily.delete_user(username),
        invite_codes=invites.forget_user(username) if invites is not None else 0,
        reports=reports.delete_user(username) if reports is not None else 0,
        solo=solo.delete_user(username) if solo is not None else 0,
    )

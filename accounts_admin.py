"""Manage accounts from the command line (the server may keep running).

Usage:
    python3 accounts_admin.py list
    python3 accounts_admin.py reset <username>     asks for a new password
    python3 accounts_admin.py delete <username>    removes the account and what it left behind (see erasure.py)
    python3 accounts_admin.py disable <username>   locks the account out without deleting anything
    python3 accounts_admin.py enable <username>
    python3 accounts_admin.py make-admin <username>    lets the account review recipe promotion requests
    python3 accounts_admin.py remove-admin <username>
    python3 accounts_admin.py make-bot <username>      an automated proposer: no limit on drafts or waiting proposals
    python3 accounts_admin.py remove-bot <username>
    python3 accounts_admin.py invite create [--count N] [--note TEXT]   new single-use signup codes (shown once)
    python3 accounts_admin.py invite list
    python3 accounts_admin.py invite revoke <id>       deletes an unused code (ids are in `invite list`)
    python3 accounts_admin.py verify <username>        confirm an account's email by hand (e.g. if mail is not arriving)
Add --file path/to/accounts.json and --saves-dir path/to/saves if the server uses different ones.

Resetting a password or deleting an account logs that account out of the server.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from accounts import AccountStore
from daily import DailyStore
from drafts import DraftStore
from erasure import erase_account
from invites import InviteStore
from reports import ReportStore
from saves import SaveStore
from state import GameError

BASE_DIR = Path(__file__).resolve().parent


def invite_command(invites: InviteStore, args: argparse.Namespace) -> int:
    if args.action == "create":
        codes = invites.create(args.count, args.note)
        print("Give each code to one person. They are shown only now (only hashes are stored):")
        print("\n".join(f"  {code}" for code in codes))
    elif args.action == "revoke":
        removed = invites.revoke(args.id)
        print(f"Revoked {removed} unused code(s)." if removed else "No unused code has an id starting with that.")
        return 0 if removed else 1
    else:
        rows = invites.listing()
        for row in sorted(rows, key=lambda r: r["created"]):
            state = f"used by {row['used_by']}" if row["used_by"] else "unused"
            note = f"  ({row['note']})" if row["note"] else ""
            print(f"{row['id']}  {state}{note}")
        if not rows:
            print("(no invite codes)")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage Co-op Alchemy accounts")
    parser.add_argument("--file", default=str(BASE_DIR / "accounts.json"))
    parser.add_argument("--saves-dir", default=str(BASE_DIR / "saves"))
    parser.add_argument("--drafts-file", default=str(BASE_DIR / "drafts.json"))
    parser.add_argument("--invites-file", default=str(BASE_DIR / "invites.json"))
    parser.add_argument("--daily-file", default=str(BASE_DIR / "daily.json"))
    parser.add_argument("--reports-file", default=str(BASE_DIR / "reports.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("reset").add_argument("username")
    sub.add_parser("delete").add_argument("username")
    sub.add_parser("make-admin").add_argument("username")
    sub.add_parser("remove-admin").add_argument("username")
    sub.add_parser("make-bot").add_argument("username")
    sub.add_parser("remove-bot").add_argument("username")
    sub.add_parser("verify").add_argument("username")
    sub.add_parser("disable").add_argument("username")
    sub.add_parser("enable").add_argument("username")
    invite = sub.add_parser("invite").add_subparsers(dest="action", required=True)
    create = invite.add_parser("create")
    create.add_argument("--count", type=int, default=1)
    create.add_argument("--note", default="")
    invite.add_parser("list")
    invite.add_parser("revoke").add_argument("id")
    args = parser.parse_args(argv[1:])
    store = AccountStore(args.file)
    try:
        if args.command == "invite":
            return invite_command(InviteStore(args.invites_file), args)
        if args.command == "verify":
            store.set_verified(args.username)
            print(f"{args.username!r} can now log in.")
        elif args.command in ("disable", "enable"):
            store.set_disabled(args.username, args.command == "disable")
            print(f"{args.username!r} is {'now locked out' if args.command == 'disable' else 'allowed in again'}.")
        elif args.command == "list":
            names = store.usernames()
            admins = {n.casefold() for n in store.admins()}
            bots = {n.casefold() for n in store.bots()}
            pending = {n.casefold() for n in store.unverified()}
            print("\n".join(n + ("  (admin)" if n.casefold() in admins else "")
                            + ("  (bot)" if n.casefold() in bots else "")
                            + ("  (email not confirmed)" if n.casefold() in pending else "") for n in names)
                  if names else "(no accounts)")
        elif args.command in ("make-bot", "remove-bot"):
            store.set_bot(args.username, args.command == "make-bot")
            print(f"{args.username!r} is {'now' if args.command == 'make-bot' else 'no longer'} a bot account.")
        elif args.command in ("make-admin", "remove-admin"):
            store.set_admin(args.username, args.command == "make-admin")
            print(f"{args.username!r} is {'now' if args.command == 'make-admin' else 'no longer'} an admin.")
        elif args.command in ("reset", "delete") and args.username.casefold() not in {
                n.casefold() for n in store.usernames()}:
            print(f"There is no account called {args.username!r}. Accounts: "
                  + (", ".join(store.usernames()) or "(none)"), file=sys.stderr)
            return 1
        elif args.command == "reset":
            password = getpass.getpass("New password: ")
            if getpass.getpass("Again: ") != password:
                print("The two passwords differ; nothing was changed.", file=sys.stderr)
                return 1
            store.set_password(args.username, password)
            print(f"Password for {args.username!r} changed.")
        else:
            saves = SaveStore(args.saves_dir)
            games = saves.for_player(args.username)
            if games:
                print("Saved games this account played in:")
                for game in games:
                    others = [p for p in game["players"] if p.casefold() != args.username.casefold()]
                    print(f"  - {game['name']!r}" + (f" (also played by {', '.join(others)}: it stays for them, "
                                                     "with this name removed)" if others else " (nobody else: it will be deleted)"))
            if input(f"Delete the account {args.username!r} and its drafts, daily-challenge scores and saved games? "
                     "Type yes to confirm: ").strip().lower() != "yes":
                print("Nothing was deleted.")
                return 1
            # the daily store only needs its file here, not the recipes
            summary = erase_account(args.username, accounts=store, saves=saves, drafts=DraftStore(args.drafts_file),
                                    daily=DailyStore(args.daily_file, None),  # type: ignore[arg-type]
                                    invites=InviteStore(args.invites_file), reports=ReportStore(args.reports_file))
            print(f"Deleted {args.username!r}: {summary.saves_deleted} saved game(s) deleted, "
                  f"{summary.saves_kept_anonymised} kept for the others, {summary.drafts} draft recipe(s), "
                  f"{summary.daily_entries} daily-challenge entries.")
    except GameError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; nothing was changed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

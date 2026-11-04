"""Manage accounts from the command line (the server may keep running).

Usage:
    python3 accounts_admin.py list
    python3 accounts_admin.py reset <username>     asks for a new password
    python3 accounts_admin.py delete <username>    also deletes the saved games the account played in
    python3 accounts_admin.py make-admin <username>    lets the account review recipe promotion requests
    python3 accounts_admin.py remove-admin <username>
    python3 accounts_admin.py make-bot <username>      an automated proposer: no limit on drafts or waiting proposals
    python3 accounts_admin.py remove-bot <username>
Add --file path/to/accounts.json and --saves-dir path/to/saves if the server uses different ones.

Resetting a password or deleting an account logs that account out of the server.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from accounts import AccountStore
from drafts import DraftStore
from saves import SaveStore
from state import GameError

BASE_DIR = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage Co-op Alchemy accounts")
    parser.add_argument("--file", default=str(BASE_DIR / "accounts.json"))
    parser.add_argument("--saves-dir", default=str(BASE_DIR / "saves"))
    parser.add_argument("--drafts-file", default=str(BASE_DIR / "drafts.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("reset").add_argument("username")
    sub.add_parser("delete").add_argument("username")
    sub.add_parser("make-admin").add_argument("username")
    sub.add_parser("remove-admin").add_argument("username")
    sub.add_parser("make-bot").add_argument("username")
    sub.add_parser("remove-bot").add_argument("username")
    args = parser.parse_args(argv[1:])
    store = AccountStore(args.file)
    try:
        if args.command == "list":
            names = store.usernames()
            admins = {n.casefold() for n in store.admins()}
            bots = {n.casefold() for n in store.bots()}
            print("\n".join(n + ("  (admin)" if n.casefold() in admins else "")
                            + ("  (bot)" if n.casefold() in bots else "") for n in names)
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
                print("These saved games will be deleted too:")
                for game in games:
                    others = [p for p in game["players"] if p.casefold() != args.username.casefold()]
                    print(f"  - {game['name']!r}" + (f" (also played by {', '.join(others)})" if others else ""))
            if input(f"Delete the account {args.username!r}"
                     f"{' and ' + str(len(games)) + ' saved game(s)' if games else ''}? "
                     "Type yes to confirm: ").strip().lower() != "yes":
                print("Nothing was deleted.")
                return 1
            store.delete(args.username)
            removed = saves.delete_for_player(args.username)
            drafts = DraftStore(args.drafts_file).delete_user(args.username)
            print(f"Deleted {args.username!r}" + (f" and {len(removed)} saved game(s)" if removed else "")
                  + (f", and {drafts} draft recipe(s)" if drafts else "") + ".")
    except GameError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; nothing was changed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

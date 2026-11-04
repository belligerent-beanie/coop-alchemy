"""Patch the server without dropping games: snapshot, restart in place, restore.

`python3 reload.py` (see reload.py) asks the running server to reload. The server then

1. stops accepting new connections (they wait in the operating system's queue),
2. writes every running game (tiles, discoveries, players and their tokens, Djinn state)
   and every login session to a snapshot file,
3. replaces itself with a fresh copy of the code (same process id, same listening socket), and
4. loads the snapshot, so browsers just reconnect and carry on.

Players see a pause of a second or two. This module holds the snapshot code; the
signal and restart live in server.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from accounts import AccountStore
from games import GameRegistry

SNAPSHOT_VERSION = 1
MAX_SNAPSHOT_AGE = 600.0  # a snapshot older than this (seconds) is ignored at startup


def write_snapshot(path: str | Path, registry: GameRegistry, accounts: AccountStore) -> None:
    """Write the snapshot atomically (readable only by you: it holds login tokens)."""
    path = Path(path)
    data = {
        "version": SNAPSHOT_VERSION,
        "written": time.time(),
        "games": registry.live_export(),
        "sessions": accounts.export_sessions(),
    }
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def restore_snapshot(path: str | Path, registry: GameRegistry, accounts: AccountStore) -> tuple[int, int] | None:
    """Load a recent snapshot into the registry and accounts; returns (games, sessions), or None if there is none.

    A used snapshot is renamed to <name>.last (kept as a backup until the next reload); one that
    cannot be read is renamed to <name>.bad so it can be looked at, and the server starts empty.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
            raise ValueError("unknown snapshot format")
        written = data.get("written")
        if not isinstance(written, (int, float)):
            raise ValueError("no timestamp")
        if time.time() - written > MAX_SNAPSHOT_AGE:
            print(f"ignoring an old snapshot ({path.name}): it is more than "
                  f"{round(MAX_SNAPSHOT_AGE / 60)} minutes old", file=sys.stderr)
            os.replace(path, path.with_name(path.name + ".old"))
            return None
        result = (registry.live_restore(data.get("games")), accounts.import_sessions(data.get("sessions")))
    except (OSError, ValueError) as exc:
        print(f"could not use the reload snapshot ({exc}); starting empty", file=sys.stderr)
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        return None
    os.replace(path, path.with_name(path.name + ".last"))
    return result

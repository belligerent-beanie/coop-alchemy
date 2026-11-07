"""A few counters per account, and the account-wide achievements they earn (see achievements.py, BADGES).

What is kept: for each account, how many elements it discovered in shared games, how many shared games it played with others, a few
daily-challenge and race results, and the achievements earned and when. Nothing about who played with whom, and no game content.

Counters change often (every discovery), so they are kept in memory and written to profiles.json at most every few seconds (flush(),
called by a loop in server.py and before a reload or exit), not on every change. A server that is killed loses a few seconds of counting at
worst. A damaged file is never overwritten: the store then works for this run without saving, and says so.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from accounts import key_of
from achievements import COUNTERS, earned_badges

MAX_COUNT = 1_000_000


class ProfileStore:
    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] | None = None
        self._dirty = False
        self._broken = False   # the file exists but cannot be read: never write over it

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._users is not None:
            return self._users
        users: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                found = data.get("users") if isinstance(data, dict) else None
                if not isinstance(found, dict):
                    raise ValueError("not in the expected format")
                users = found
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                self._broken = True
                print(f"warning: the profile file ({self.path.name}) cannot be read ({exc}); achievements work for this run but are not saved",
                      flush=True)
        self._users = users
        return users

    def flush(self) -> bool:
        """Write the counters if anything changed since the last write. Returns True if it wrote."""
        with self._lock:
            if self.path is None or not self._dirty or self._broken or self._users is None:
                return False
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"version": 1, "users": self._users}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                print(f"warning: could not save profiles: {exc}", flush=True)
                return False
            self._dirty = False
            return True

    @staticmethod
    def _user(users: dict[str, dict[str, Any]], username: str) -> dict[str, Any]:
        record = users.setdefault(key_of(username), {"counters": {}, "achievements": {}})
        record.setdefault("counters", {})
        record.setdefault("achievements", {})
        return record

    # ---- counting ------------------------------------------------------

    def bump(self, username: str, add: dict[str, int] | None = None, best: dict[str, int] | None = None) -> list[str]:
        """Add to counters (`add`) and raise counters to at least a value (`best`, for streaks). Returns the achievements that are new.

        Unknown counter names are ignored, so a bug elsewhere cannot fill the file with junk.
        """
        with self._lock:
            record = self._user(self._load(), username)
            counters: dict[str, int] = record["counters"]
            for key, amount in (add or {}).items():
                if key in COUNTERS and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
                    counters[key] = min(MAX_COUNT, counters.get(key, 0) + amount)
            for key, value in (best or {}).items():
                if key in COUNTERS and isinstance(value, int) and not isinstance(value, bool) and value > counters.get(key, 0):
                    counters[key] = min(MAX_COUNT, value)
            earned: dict[str, int] = record["achievements"]
            new = sorted(a for a in earned_badges(counters) if a not in earned)
            for a in new:
                earned[a] = int(self._clock())
            self._dirty = True
            return new

    def counters(self, username: str) -> dict[str, int]:
        with self._lock:
            record = self._load().get(key_of(username)) or {}
            return dict(record.get("counters") or {})

    def achievements(self, username: str) -> dict[str, int]:
        """{achievement id: unix time it was earned}."""
        with self._lock:
            record = self._load().get(key_of(username)) or {}
            have = record.get("achievements")
            return {k: int(v) for k, v in have.items() if isinstance(v, (int, float))} if isinstance(have, dict) else {}

    def delete_user(self, username: str) -> int:
        """Remove an account's counters and achievements (used when the account is deleted); returns 1 if there were any."""
        with self._lock:
            removed = self._load().pop(key_of(username), None)
            if removed is not None:
                self._dirty = True
                self.flush()
            return 1 if removed is not None else 0


def profile_loop(store: ProfileStore, seconds: float = 5.0) -> None:
    """Write the counters every few seconds, forever. A failure here must never stop the server."""
    while True:
        time.sleep(seconds)
        try:
            store.flush()
        except Exception as exc:
            print(f"warning: profile save failed: {exc}", flush=True)

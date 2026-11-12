"""Solo mode: one account, one private game that remembers what it has found.

A solo game is an ordinary game with a single player. Every discovery is written here at once, so closing the tab (or the
server restarting) loses nothing, and a game picks up where the account left off. When the account has found every
element that can be found, `complete` is set for good: that is what unlocks proposing recipes (see server.py).

Everything lives in one JSON file (solo.json), written atomically. A damaged file is never overwritten.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from accounts import key_of
from hints import HINT_REWARD_MIN_STREAK, MAX_HINTS, STARTING_HINTS
from state import GameError

KEEP_REWARD_KEYS = 120   # daily-challenge rewards remembered per account (so one challenge cannot pay twice)


class SoloStore:
    """Per-account solo progress. `path=None` keeps everything in memory (tests)."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"version": 1, "users": {}}

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "users": {}}
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the solo file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise GameError(500, f"the solo file ({self.path.name}) is not in the expected format")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "solo progress could not be saved (see the server console)") from exc

    def load(self, username: str) -> dict[str, Any]:
        """What GameState.export() wrote for this account last time (empty for a new account)."""
        with self._lock:
            user = self._load()["users"].get(key_of(username))
            return dict(user["game"]) if user and isinstance(user.get("game"), dict) else {}

    def record(self, username: str, game: dict[str, Any], found: int, total: int, complete: bool,
               earned: Iterable[str] = ()) -> list[str]:
        """Keep the game's progress and the achievements it has earned; returns the achievements that are new.

        Once `complete` is set it stays set, whatever the recipes do later, and an achievement is never taken back.
        """
        with self._lock:
            data = self._load()
            user = data["users"].setdefault(key_of(username), {"complete": False})
            was = bool(user.get("complete"))
            user["game"] = {"discovered": game.get("discovered", {}), "genie": game.get("genie", {}),
                            "hints": game.get("hints", {})}   # the balance itself lives beside the game (see hints())
            user["found"], user["total"] = found, total
            if complete and not was:
                user["complete"] = True
                user["completed_at"] = int(self._clock())
            have = user.setdefault("achievements", {})
            new = [a for a in earned if a not in have]
            for a in new:
                have[a] = int(self._clock())
            self._save(data)
            return new

    def status(self, username: str) -> dict[str, Any]:
        """{"found", "total", "complete"} as last recorded; zeros for an account that never played solo."""
        with self._lock:
            user = self._load()["users"].get(key_of(username)) or {}
            return {"found": int(user.get("found", 0)), "total": int(user.get("total", 0)),
                    "complete": bool(user.get("complete"))}

    def achievements(self, username: str) -> dict[str, int]:
        """{achievement id: unix time it was earned}."""
        with self._lock:
            user = self._load()["users"].get(key_of(username)) or {}
            have = user.get("achievements")
            return {k: int(v) for k, v in have.items() if isinstance(v, (int, float))} if isinstance(have, dict) else {}

    def is_complete(self, username: str) -> bool:
        return self.status(username)["complete"]

    # ---- hints: a balance per account, shared by every solo game and earned in the daily challenge ----------------------------

    def hints(self, username: str) -> int:
        """Hints this account has left. An account that never spent or earned one has STARTING_HINTS."""
        with self._lock:
            user = self._load()["users"].get(key_of(username)) or {}
            balance = user.get("hints")
            return balance if isinstance(balance, int) and not isinstance(balance, bool) and balance >= 0 else STARTING_HINTS

    def spend_hint(self, username: str) -> int:
        """Use one hint; returns what is left. 429 if there are none."""
        with self._lock:
            data = self._load()
            user = data["users"].setdefault(key_of(username), {"complete": False})
            left = self.hints(username)
            if left <= 0:
                raise GameError(429, "you are out of hints: finish a daily challenge on a streak of two days or more to earn another")
            user["hints"] = left - 1
            self._save(data)
            return left - 1

    def reward_hint(self, username: str, key: str, streak: int) -> int | None:
        """Pay one hint for finishing the daily challenge `key` (e.g. "2026-11-12:easy") while on a streak of at least
        HINT_REWARD_MIN_STREAK days. Each key pays once. Returns the new balance if a hint was paid, otherwise None."""
        if streak < HINT_REWARD_MIN_STREAK:
            return None
        with self._lock:
            data = self._load()
            user = data["users"].setdefault(key_of(username), {"complete": False})
            paid = user.get("hint_rewards")
            paid = [k for k in paid if isinstance(k, str)] if isinstance(paid, list) else []
            if key in paid:
                return None
            balance = min(MAX_HINTS, self.hints(username) + 1)
            user["hints"] = balance
            user["hint_rewards"] = (paid + [key])[-KEEP_REWARD_KEYS:]
            self._save(data)
            return balance

    def wallet(self, username: str) -> "StoreWallet":
        return StoreWallet(self, username)

    def delete_user(self, username: str) -> int:
        """Remove an account's solo progress (used when the account is deleted); returns 1 if there was any."""
        with self._lock:
            data = self._load()
            removed = data["users"].pop(key_of(username), None)
            if removed is not None:
                self._save(data)
            return 1 if removed is not None else 0


class StoreWallet:
    """One account's hint balance in a SoloStore, in the shape GameState uses (see hints.LocalWallet)."""

    def __init__(self, store: SoloStore, username: str) -> None:
        self._store = store
        self._username = username

    def balance(self) -> int:
        return self._store.hints(self._username)

    def spend(self) -> int:
        return self._store.spend_hint(self._username)

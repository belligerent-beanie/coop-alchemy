"""Saved games: one small JSON file per game, kept for a few days.

A game is saved only when a player ends it with "save"; a game that closes by
itself after sitting empty is not saved. Only discoveries, the Djinn's state and the
usernames of the players are kept; the workspace tiles are not. A saved game belongs
to the accounts that played in it: only they can find and resume it, and resuming puts
it back among the running games.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from state import GameError, GameState

SAVE_TTL_SECONDS = 3 * 24 * 3600
SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


class SaveStore:
    def __init__(self, directory: str | Path | None, *, ttl: float = SAVE_TTL_SECONDS,
                 now: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory) if directory else None
        self.ttl = ttl
        self._now = now

    # ---- files ---------------------------------------------------------

    def _path(self, slug: str) -> Path | None:
        if self.directory is None or not SLUG_RE.match(slug):
            return None
        return self.directory / f"{slug}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        """The saved data, or None if the file is missing, unreadable or expired (expired files are deleted)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("saved_at"), (int, float)):
            return None
        if self._now() - data["saved_at"] >= self.ttl:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data

    def _all(self) -> list[dict[str, Any]]:
        if self.directory is None or not self.directory.is_dir():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            data = self._read(path)
            if data is not None and isinstance(data.get("slug"), str) and path.stem == data["slug"]:
                found.append(data)
        return found

    # ---- public --------------------------------------------------------

    def save(self, game: GameState) -> None:
        path = self._path(game.slug)
        if path is None:
            raise GameError(409, "saving is switched off on this server")
        data = game.export()
        data["saved_at"] = self._now()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            print(f"warning: could not save game {game.slug!r}: {exc}", file=sys.stderr)
            raise GameError(500, "the game could not be saved (see the server console)") from exc

    def load(self, slug: str) -> dict[str, Any] | None:
        path = self._path(slug)
        return self._read(path) if path else None

    def exists(self, slug: str) -> bool:
        return self.load(slug) is not None

    def has_player(self, data: dict[str, Any], username: str) -> bool:
        wanted = normalise(username)
        return bool(wanted) and any(isinstance(p, str) and normalise(p) == wanted
                                    for p in data.get("players", []))

    def for_player(self, username: str) -> list[dict[str, Any]]:
        """Saved games this account played in, newest first."""
        matches = []
        for data in self._all():
            if not self.has_player(data, username):
                continue
            players = [p for p in data["players"] if isinstance(p, str)]
            discovered = data.get("discovered")
            matches.append({
                "id": data["slug"],
                "name": data.get("name", data["slug"]),
                "players": players,
                "found": len(discovered) if isinstance(discovered, dict) else 0,
                "expires_in": max(0, round(data["saved_at"] + self.ttl - self._now())),
                "saved_at": data["saved_at"],
            })
        return sorted(matches, key=lambda m: -m["saved_at"])

    def delete_for_player(self, username: str) -> list[str]:
        """Delete every saved game this account played in (used when the account is deleted).

        Returns the ids of the games removed. Players who shared a game lose it too.
        """
        removed = []
        for data in self._all():
            if self.has_player(data, username):
                path = self._path(data["slug"])
                if path is not None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise GameError(500, f"could not delete the saved game {data['slug']!r}: {exc}") from exc
                    removed.append(data["slug"])
        return removed

    def purge(self) -> int:
        """Delete expired saves; returns how many saved games remain."""
        return len(self._all())

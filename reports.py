"""Player reports: "this player's name or behaviour is a problem", for the administrators to look at.

Designed to hold as little as possible:
  - It does NOT record who sent a report, so nobody can be traced or retaliated against. The reporter's account is used only in
    memory, to ignore the same person reporting the same player again within a day, and is never written to the file.
  - Reports about one player for one reason are merged into a single entry with a count, the latest note and the latest game name.
  - Entries are deleted after RETENTION_SECONDS (14 days) whether or not anyone looked, or at once when an administrator dismisses
    them, or when the reported account is deleted (erasure.py).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from accounts import key_of
from state import GameError

RETENTION_SECONDS = 14 * 24 * 3600.0
DEDUPE_SECONDS = 24 * 3600.0
MAX_REPORTS = 500
MAX_NOTE = 140
REASONS = {"name": "inappropriate name", "behaviour": "bad behaviour", "other": "something else"}


def clean_note(raw: object) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise GameError(400, "the note must be text")
    return "".join(ch for ch in " ".join(raw.split()) if ch.isprintable())[:MAX_NOTE].strip()


class ReportStore:
    def __init__(self, path: str | Path | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.Lock()
        self._memory: list[dict[str, Any]] = []
        self._seen: dict[tuple[str, str], float] = {}  # (reporter, reported) -> when: memory only, never saved

    # ---- file ----------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the reports file ({self.path.name}) cannot be read; fix or move it") from exc
        reports = data.get("reports") if isinstance(data, dict) else None
        if not isinstance(reports, list):
            raise GameError(500, f"the reports file ({self.path.name}) is not in the expected format")
        return [r for r in reports if isinstance(r, dict)]

    def _save(self, reports: list[dict[str, Any]]) -> None:
        if self.path is None:
            self._memory = reports
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "reports": reports}, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the report could not be saved") from exc

    def _fresh(self) -> list[dict[str, Any]]:
        """The stored reports without the ones past their retention (call with the lock held)."""
        cutoff = self._clock() - RETENTION_SECONDS
        reports = self._load()
        kept = [r for r in reports if isinstance(r.get("at"), (int, float)) and r["at"] >= cutoff]
        if len(kept) != len(reports):
            self._save(kept)
        return kept

    # ---- use -----------------------------------------------------------

    def add(self, user: str, reason: str, note: str, game: str, reporter: str) -> bool:
        """Record a report about `user`. False (and nothing changes) if `reporter` already reported them within a day."""
        if reason not in REASONS:
            raise GameError(400, "say what is wrong: name, behaviour or other")
        now = self._clock()
        with self._lock:
            pair = (key_of(reporter), key_of(user))
            if now - self._seen.get(pair, -DEDUPE_SECONDS) < DEDUPE_SECONDS:
                return False
            self._seen[pair] = now
            if len(self._seen) > 5000:
                self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_SECONDS}
            reports = self._fresh()
            entry = next((r for r in reports if key_of(str(r.get("user"))) == key_of(user) and r.get("reason") == reason), None)
            if entry is None:
                if len(reports) >= MAX_REPORTS:
                    reports.sort(key=lambda r: r["at"])
                    del reports[0]
                entry = {"id": secrets.token_hex(4), "user": user, "reason": reason, "note": "", "game": "", "count": 0, "first": now}
                reports.append(entry)
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["at"] = now
            entry["game"] = game[:40]
            if note:
                entry["note"] = note
            self._save(reports)
            return True

    def listing(self) -> list[dict[str, Any]]:
        """Open reports, most reported first. No reporter appears anywhere in them."""
        with self._lock:
            return sorted(self._fresh(), key=lambda r: (-int(r.get("count", 0)), -float(r["at"])))

    def count(self) -> int:
        return len(self.listing())

    def dismiss(self, report_id: object) -> bool:
        with self._lock:
            reports = self._fresh()
            kept = [r for r in reports if r.get("id") != report_id]
            if len(kept) == len(reports):
                return False
            self._save(kept)
            return True

    def delete_user(self, username: str) -> int:
        """Remove every report about an account (used when it is deleted); returns how many entries went."""
        with self._lock:
            reports = self._fresh()
            kept = [r for r in reports if key_of(str(r.get("user"))) != key_of(username)]
            if len(kept) != len(reports):
                self._save(kept)
            return len(reports) - len(kept)

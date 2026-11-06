"""Single-use invite codes for signup.

Codes are random (60 bits), shown once when created, and stored only as SHA-256 hashes, so a leaked
invites.json does not give away unused codes. Someone with the file can only see how many were made
and which were used. The file is re-read on every check, so `accounts_admin.py invite ...` can add or
revoke codes while the server keeps running.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from state import GameError

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, so codes survive being read aloud
CODE_LEN = 12
MAX_CODES = 1000


def normalize(raw: object) -> str:
    """Upper-case, without spaces or hyphens; '' if `raw` is not text. 'abcd-efgh-jklm' and 'ABCDEFGHJKLM' match."""
    if not isinstance(raw, str) or len(raw) > 64:
        return ""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii", "ignore")).hexdigest()


def pretty(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


class InviteStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: set[str] = set()  # digests being used by a signup in progress

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the invites file ({self.path.name}) cannot be read; fix or move it") from exc
        codes = data.get("codes") if isinstance(data, dict) else None
        if not isinstance(codes, dict):
            raise GameError(500, f"the invites file ({self.path.name}) is not in the expected format")
        return codes

    def _save(self, codes: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "codes": codes}, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the invite codes could not be saved") from exc

    # ---- administration ------------------------------------------------

    def create(self, count: int = 1, note: str = "") -> list[str]:
        """Make `count` new codes and return them (the only time they are visible)."""
        if not 1 <= count <= 100:
            raise GameError(400, "make between 1 and 100 codes at a time")
        with self._lock:
            codes = self._load()
            if len(codes) + count > MAX_CODES:
                raise GameError(409, f"at most {MAX_CODES} codes are kept; revoke unused ones first")
            made = []
            for _ in range(count):
                code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
                codes[digest(code)] = {"note": note[:60], "created": self._clock(), "used_by": None, "used_at": None}
                made.append(pretty(code))
            self._save(codes)
        return made

    def listing(self) -> list[dict[str, Any]]:
        """Every code as {id, note, created, used_by, used_at}; `id` is a short handle for revoking."""
        with self._lock:
            return [{"id": key[:8], **info} for key, info in self._load().items()]

    def revoke(self, handle: str) -> int:
        """Delete unused codes whose id starts with `handle`; returns how many (used ones stay as a record)."""
        if len(handle) < 4:
            raise GameError(400, "give at least 4 characters of the code's id")
        with self._lock:
            codes = self._load()
            gone = [key for key, info in codes.items() if key.startswith(handle) and not info.get("used_by")]
            for key in gone:
                del codes[key]
            if gone:
                self._save(codes)
            return len(gone)

    # ---- use at signup -------------------------------------------------

    def claim(self, raw: object) -> str:
        """Reserve a code for one signup attempt; returns a handle for finish() or release().

        Raises 403 if the code is missing, wrong, already used or being used right now. The reservation is held in
        memory only, so a signup that fails (username taken, bad password) gives the code back with release().
        """
        code = normalize(raw)
        if len(code) != CODE_LEN:
            raise GameError(403, "a valid invite code is needed to create an account")
        key = digest(code)
        with self._lock:
            info = self._load().get(key)
            if info is None or info.get("used_by") or key in self._pending:
                raise GameError(403, "that invite code is not valid (it may have been used already)")
            self._pending.add(key)
        return key

    def release(self, handle: str) -> None:
        with self._lock:
            self._pending.discard(handle)

    def finish(self, handle: str, username: str) -> None:
        """Mark a claimed code as used by `username`."""
        with self._lock:
            try:
                codes = self._load()
                if handle in codes:
                    codes[handle]["used_by"] = username
                    codes[handle]["used_at"] = self._clock()
                    self._save(codes)
            finally:
                self._pending.discard(handle)

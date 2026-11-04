"""Accounts: unique usernames with passwords, and login sessions.

Accounts live in one JSON file (accounts.json). Passwords are never stored, only a
salted PBKDF2-SHA256 hash. A username is also the player's name in games, and names
are unique ignoring capitals. A login gives the browser a random session token,
which it sends with every request; sessions are kept in memory, so restarting the
server logs everyone out. Resetting a password or deleting an account (see
accounts_admin.py) also ends that account's sessions.

Passwords travel over plain HTTP unless the server was started with --tls-cert.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from state import MAX_NAME_LEN, GameError

MIN_USERNAME_LEN = 3
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
MAX_ACCOUNTS = 500
PBKDF2_ITERATIONS = 600_000
SESSION_SECONDS = 24 * 3600.0   # a session ends after this long without being used
MAX_FAILURES = 5                # wrong passwords in a row before the account is locked for a while
LOCKOUT_SECONDS = 60.0
MAX_SESSIONS = 2000


def clean_username(raw: object) -> str:
    """Validate and tidy a username: letters, digits, spaces and _ - . only."""
    if not isinstance(raw, str):
        raise GameError(400, "the username must be text")
    name = " ".join(raw.split())
    if not MIN_USERNAME_LEN <= len(name) <= MAX_NAME_LEN:
        raise GameError(400, f"the username must be {MIN_USERNAME_LEN} to {MAX_NAME_LEN} characters")
    if not all(ch.isalnum() or ch in "_-. " for ch in name):
        raise GameError(400, "the username may only use letters, digits, spaces and _ - .")
    return name


def check_password(password: object, username: str = "") -> str:
    if not isinstance(password, str):
        raise GameError(400, "the password must be text")
    if not MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN:
        raise GameError(400, f"the password must be {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} characters")
    if username and password.casefold() == username.casefold():
        raise GameError(400, "the password must not be the same as the username")
    return password


def key_of(username: str) -> str:
    return " ".join(username.split()).casefold()


class AccountStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 iterations: int = PBKDF2_ITERATIONS) -> None:
        self.path = Path(path)
        self._clock = clock
        self._iterations = iterations
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, str, float]] = {}   # token -> (key, salt, last used)
        self._failures: dict[str, tuple[int, float]] = {}        # key -> (count, locked until)

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Refuse to carry on rather than overwrite a damaged file and lose every account.
            raise GameError(500, f"the accounts file ({self.path.name}) cannot be read; "
                                 "fix or move it and try again") from exc
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, dict):
            raise GameError(500, f"the accounts file ({self.path.name}) is not in the expected format")
        return accounts

    def _save(self, accounts: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "accounts": accounts}, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the account could not be saved (see the server console)") from exc

    def _hash(self, password: str, salt: str, iterations: int) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()

    # ---- sign up and log in --------------------------------------------

    def register(self, raw_username: object, raw_password: object) -> str:
        """Create an account; returns its username. 409 if the name is taken (ignoring case)."""
        username = clean_username(raw_username)
        password = check_password(raw_password, username)
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if key in accounts:
                raise GameError(409, "that username is taken")
            if len(accounts) >= MAX_ACCOUNTS:
                raise GameError(409, "this server has reached its limit on accounts")
        salt = secrets.token_hex(16)
        digest = self._hash(password, salt, self._iterations)  # slow, so done outside the lock
        with self._lock:
            accounts = self._load()
            if key in accounts:
                raise GameError(409, "that username is taken")
            accounts[key] = {"name": username, "salt": salt, "hash": digest,
                             "iterations": self._iterations, "created": self._clock()}
            self._save(accounts)
        return username

    def login(self, raw_username: object, raw_password: object) -> tuple[str, str]:
        """Check a username and password; returns (session token, username)."""
        if not isinstance(raw_username, str) or not isinstance(raw_password, str) \
                or len(raw_password) > MAX_PASSWORD_LEN or len(raw_username) > 200:
            raise GameError(401, "wrong username or password")
        key = key_of(raw_username)
        now = self._clock()
        with self._lock:
            count, until = self._failures.get(key, (0, 0.0))
            if until > now:
                raise GameError(429, f"too many wrong passwords; try again in {int(until - now) + 1} seconds")
            record = self._load().get(key)
        salt = record["salt"] if record else "00" * 16
        iterations = record.get("iterations", self._iterations) if record else self._iterations
        digest = self._hash(raw_password, salt, iterations)  # unknown names cost the same time as wrong passwords
        good = record is not None and hmac.compare_digest(digest, record["hash"])
        with self._lock:
            if not good:
                count = self._failures.get(key, (0, 0.0))[0] + 1
                locked = now + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0.0
                self._failures[key] = (0 if locked else count, locked)
                if len(self._failures) > 5000:
                    self._failures = {k: v for k, v in self._failures.items() if v[1] > now}
                raise GameError(401, "wrong username or password")
            self._failures.pop(key, None)
            return self._new_session(key, record["salt"]), record["name"]

    def _new_session(self, key: str, salt: str) -> str:
        now = self._clock()
        if len(self._sessions) >= MAX_SESSIONS:
            self._sessions = {t: s for t, s in self._sessions.items() if now - s[2] < SESSION_SECONDS}
            while len(self._sessions) >= MAX_SESSIONS:  # still full: drop the least recently used
                del self._sessions[min(self._sessions, key=lambda t: self._sessions[t][2])]
        token = secrets.token_urlsafe(24)
        self._sessions[token] = (key, salt, now)
        return token

    def session_for(self, username: str) -> str:
        """A new session for an account that was just created."""
        with self._lock:
            record = self._load().get(key_of(username))
            if record is None:
                raise GameError(404, "no such account")
            return self._new_session(key_of(username), record["salt"])

    def username(self, token: object) -> str | None:
        """The username a session belongs to, or None if it is unknown, expired or no longer valid."""
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            key, salt, last = entry
            now = self._clock()
            record = self._load().get(key)
            if now - last >= SESSION_SECONDS or record is None or record["salt"] != salt:
                del self._sessions[token]  # expired, deleted account, or password reset
                return None
            self._sessions[token] = (key, salt, now)
            return str(record["name"])

    def export_sessions(self) -> list[dict[str, Any]]:
        """Logged-in sessions as ages, so a restarted server (see hotreload.py) can keep everyone logged in."""
        with self._lock:
            now = self._clock()
            return [{"token": t, "key": key, "salt": salt, "age": max(0.0, now - last)}
                    for t, (key, salt, last) in self._sessions.items() if now - last < SESSION_SECONDS]

    def import_sessions(self, items: object) -> int:
        """Load export_sessions() output; returns how many were kept. Each is still checked against the account's salt."""
        if not isinstance(items, list):
            return 0
        kept = 0
        with self._lock:
            now = self._clock()
            for item in items[:MAX_SESSIONS]:
                try:
                    token, key, salt, age = item["token"], item["key"], item["salt"], float(item["age"])
                except (KeyError, TypeError, ValueError):
                    continue
                if isinstance(token, str) and isinstance(key, str) and isinstance(salt, str) \
                        and 0 <= age < SESSION_SECONDS:
                    self._sessions[token] = (key, salt, now - age)
                    kept += 1
        return kept

    def logout(self, token: object) -> None:
        if isinstance(token, str):
            with self._lock:
                self._sessions.pop(token, None)

    # ---- administration (accounts_admin.py) ----------------------------

    def usernames(self) -> list[str]:
        with self._lock:
            return sorted((r["name"] for r in self._load().values()), key=str.casefold)

    def set_password(self, username: str, password: str) -> None:
        key = key_of(username)
        with self._lock:
            if key not in self._load():
                raise GameError(404, f"there is no account called {username!r}")
        password = check_password(password, username)
        salt = secrets.token_hex(16)
        digest = self._hash(password, salt, self._iterations)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            accounts[key].update(salt=salt, hash=digest, iterations=self._iterations)
            self._save(accounts)
            self._failures.pop(key, None)

    def is_admin(self, username: str) -> bool:
        """True for accounts an administrator has marked as admins (see accounts_admin.py make-admin)."""
        with self._lock:
            record = self._load().get(key_of(username))
            return bool(record and record.get("admin") is True)

    def set_admin(self, username: str, admin: bool) -> None:
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            if admin:
                accounts[key]["admin"] = True
            else:
                accounts[key].pop("admin", None)
            self._save(accounts)

    def admins(self) -> list[str]:
        with self._lock:
            return sorted((r["name"] for r in self._load().values() if r.get("admin") is True), key=str.casefold)

    def is_bot(self, username: str) -> bool:
        """True for accounts marked as bots (see accounts_admin.py make-bot): automated proposers with no draft or proposal limits."""
        with self._lock:
            record = self._load().get(key_of(username))
            return bool(record and record.get("bot") is True)

    def set_bot(self, username: str, bot: bool) -> None:
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            if bot:
                accounts[key]["bot"] = True
            else:
                accounts[key].pop("bot", None)
            self._save(accounts)

    def bots(self) -> list[str]:
        with self._lock:
            return sorted((r["name"] for r in self._load().values() if r.get("bot") is True), key=str.casefold)

    def delete(self, username: str) -> None:
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if accounts.pop(key, None) is None:
                raise GameError(404, f"there is no account called {username!r}")
            self._save(accounts)

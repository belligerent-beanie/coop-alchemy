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
import re
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
VERIFY_SECONDS = 24 * 3600.0    # an emailed link works this long; an account still unverified then is deleted
MAX_EMAIL_LEN = 254
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


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


def clean_email(raw: object) -> tuple[str, str]:
    """Validate an email address; returns (the address as typed, a key that treats aliases as the same mailbox).

    The key lower-cases everything and drops a "+tag" from the name part, and for Gmail also ignores dots, so
    a.b+x@gmail.com and ab@googlemail.com cannot be used to open several accounts on one mailbox.
    """
    if not isinstance(raw, str):
        raise GameError(400, "the email address must be text")
    email = raw.strip()
    if not email.isascii() or len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        raise GameError(400, "please enter a valid email address")
    local, domain = email.rsplit("@", 1)
    local, domain = local.lower().split("+")[0], domain.lower()
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    if not local:
        raise GameError(400, "please enter a valid email address")
    return email, f"{local}@{domain}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()


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
        self._cache: tuple[tuple[int, int] | None, dict[str, dict[str, Any]]] | None = None  # see _peek()

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

    def _stamp(self) -> tuple[int, int] | None:
        """Changes whenever the file is rewritten (by us, or by accounts_admin.py); None if there is no file."""
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GameError(500, f"the accounts file ({self.path.name}) cannot be read") from exc
        return info.st_mtime_ns, info.st_size

    def _peek(self) -> dict[str, dict[str, Any]]:
        """A READ-ONLY view of the accounts, for the lookups made on every request (call with the lock held).

        The file is parsed only when it has changed since the last time, which is what makes a logged-in request cheap
        (parsing took about 1.4 ms with 500 accounts and 15 ms with 5000, on every request, one at a time). Never change
        what this returns: writers use _load(), which always gives them a private copy, so a failed or half-done
        change can never leak into what other requests see.
        """
        stamp = self._stamp()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        accounts = self._load()
        self._cache = (stamp, accounts)
        return accounts

    def _save(self, accounts: dict[str, dict[str, Any]]) -> None:
        self._cache = None  # whatever happens next, readers must look at the file again
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

    def register(self, raw_username: object, raw_password: object, raw_email: object = None) -> str:
        """Create an account; returns its username. 409 if the name is taken (ignoring case).

        With an email address the account starts unverified: it cannot log in until verify_email() succeeds, and it is
        deleted after VERIFY_SECONDS if that never happens (so nobody can hold a name or an address for long).
        """
        username = clean_username(raw_username)
        password = check_password(raw_password, username)
        key = key_of(username)
        email, email_key = clean_email(raw_email) if raw_email is not None else ("", "")
        with self._lock:
            accounts = self._load()
            self._claim_free(accounts, key, email_key)
        salt = secrets.token_hex(16)
        digest = self._hash(password, salt, self._iterations)  # slow, so done outside the lock
        with self._lock:
            accounts = self._load()
            self._claim_free(accounts, key, email_key)
            accounts[key] = {"name": username, "salt": salt, "hash": digest,
                             "iterations": self._iterations, "created": self._clock()}
            if email:
                accounts[key].update(email=email, email_key=email_key, verified=False)
            self._save(accounts)
        return username

    def _claim_free(self, accounts: dict[str, dict[str, Any]], key: str, email_key: str) -> None:
        """Make sure a new account may use this username and email (call with the lock held; may drop stale pending ones).

        A pending (unverified) account holding the same email is replaced, so someone cannot lock a victim out of
        signing up by registering the victim's address first. A verified account is never replaced.
        """
        now = self._clock()
        stale = [k for k, r in accounts.items() if r.get("verified") is False and now - r.get("created", 0) >= VERIFY_SECONDS]
        if email_key:
            stale += [k for k, r in accounts.items()
                      if r.get("email_key") == email_key and r.get("verified") is False and k not in stale]
        if stale:
            for k in stale:
                del accounts[k]
            self._save(accounts)
        if key in accounts:
            raise GameError(409, "that username is taken")
        if email_key and any(r.get("email_key") == email_key for r in accounts.values()):
            raise GameError(409, "that email address already has an account")
        if len(accounts) >= MAX_ACCOUNTS:
            raise GameError(409, "this server has reached its limit on accounts")

    # ---- email verification --------------------------------------------

    def begin_verification(self, username: str) -> tuple[str, str] | None:
        """A fresh single-use link token for a pending account: (email address, token); None if nothing is pending."""
        return self._issue_token(lambda key, record: key == key_of(username))

    def begin_verification_for_email(self, raw_email: object) -> tuple[str, str] | None:
        """The same, found by address (for 'resend the email'); None if no pending account uses it."""
        try:
            _, email_key = clean_email(raw_email)
        except GameError:
            return None
        return self._issue_token(lambda key, record: record.get("email_key") == email_key)

    def _issue_token(self, wanted: Callable[[str, dict[str, Any]], bool]) -> tuple[str, str] | None:
        with self._lock:
            accounts = self._load()
            now = self._clock()
            for key, record in accounts.items():
                if record.get("verified") is False and wanted(key, record) and now - record.get("created", 0) < VERIFY_SECONDS:
                    token = secrets.token_urlsafe(24)
                    record["verify"] = {"hash": token_digest(token), "expires": now + VERIFY_SECONDS}
                    self._save(accounts)
                    return str(record["email"]), token
        return None

    def verify_email(self, token: object) -> str:
        """Activate the account a link token belongs to; returns its username, or raises 400 if it is wrong or too old."""
        invalid = GameError(400, "this link is not valid any more; log in to ask for a new one")
        if not isinstance(token, str) or not 16 <= len(token) <= 200:
            raise invalid
        digest = token_digest(token)
        with self._lock:
            accounts = self._load()
            now = self._clock()
            for record in accounts.values():
                pending = record.get("verify")
                if record.get("verified") is False and isinstance(pending, dict) \
                        and hmac.compare_digest(str(pending.get("hash")), digest):
                    if pending.get("expires", 0) <= now:
                        raise invalid
                    record["verified"] = True
                    del record["verify"]
                    self._save(accounts)
                    return str(record["name"])
        raise invalid

    def set_verified(self, username: str) -> None:
        """Mark an account's email as confirmed without a link (accounts_admin.py verify)."""
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            accounts[key]["verified"] = True
            accounts[key].pop("verify", None)
            self._save(accounts)

    def unverified(self) -> list[str]:
        with self._lock:
            return sorted((r["name"] for r in self._peek().values() if r.get("verified") is False), key=str.casefold)

    def login(self, raw_username: object, raw_password: object, client: str = "") -> tuple[str, str]:
        """Check a username and password; returns (session token, username).

        Wrong-password lockouts are per (account, client address), so a stranger cannot lock you out."""
        if not isinstance(raw_username, str) or not isinstance(raw_password, str) \
                or len(raw_password) > MAX_PASSWORD_LEN or len(raw_username) > 200:
            raise GameError(401, "wrong username or password")
        key = key_of(raw_username)
        fail_key = f"{key}|{client}"
        now = self._clock()
        with self._lock:
            self._drop_stale_pending()
            count, until = self._failures.get(fail_key, (0, 0.0))
            if until > now:
                raise GameError(429, f"too many wrong passwords; try again in {int(until - now) + 1} seconds")
            record = self._peek().get(key)
        salt = record["salt"] if record else "00" * 16
        iterations = record.get("iterations", self._iterations) if record else self._iterations
        digest = self._hash(raw_password, salt, iterations)  # unknown names cost the same time as wrong passwords
        good = record is not None and hmac.compare_digest(digest, record["hash"])
        with self._lock:
            if not good:
                count = self._failures.get(fail_key, (0, 0.0))[0] + 1
                locked = now + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0.0
                self._failures[fail_key] = (0 if locked else count, locked)
                if len(self._failures) > 5000:
                    self._failures = {k: v for k, v in self._failures.items() if v[1] > now}
                raise GameError(401, "wrong username or password")
            self._failures.pop(fail_key, None)
            if record.get("verified") is False:  # told only once the password is right
                raise GameError(403, "please confirm your email address first: open the link we sent you, "
                                     "or ask for a new one below")
            if record.get("disabled") is True:
                raise GameError(403, "this account has been disabled by an administrator")
            return self._new_session(key, record["salt"]), record["name"]

    def _drop_stale_pending(self) -> None:
        """Delete accounts that never confirmed their email within VERIFY_SECONDS (call with the lock held)."""
        now = self._clock()
        if not any(r.get("verified") is False and now - r.get("created", 0) >= VERIFY_SECONDS
                   for r in self._peek().values()):
            return  # nothing to do: the common case costs no file read
        accounts = self._load()
        stale = [k for k, r in accounts.items() if r.get("verified") is False and now - r.get("created", 0) >= VERIFY_SECONDS]
        if stale:
            for k in stale:
                del accounts[k]
            self._save(accounts)

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
            record = self._peek().get(key_of(username))
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
            record = self._peek().get(key)
            if now - last >= SESSION_SECONDS or record is None or record["salt"] != salt or record.get("disabled") is True:
                del self._sessions[token]  # expired, deleted account, password reset, or disabled by an administrator
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
            return sorted((r["name"] for r in self._peek().values()), key=str.casefold)

    def listing(self) -> list[dict[str, object]]:
        """Every account as {"name", "admin", "bot", "pending", "disabled", "created"} (created is a date), by name.

        Nothing private: no password hashes, and no email addresses.
        """
        with self._lock:
            rows = [{"name": r["name"], "admin": r.get("admin") is True, "bot": r.get("bot") is True,
                     "pending": r.get("verified") is False, "disabled": r.get("disabled") is True,
                     "created": time.strftime("%Y-%m-%d", time.localtime(r["created"])) if isinstance(r.get("created"), (int, float)) else ""}
                    for r in self._peek().values()]
        return sorted(rows, key=lambda row: str(row["name"]).casefold())

    def counts(self) -> dict[str, int]:
        rows = self.listing()
        return {"total": len(rows), "pending": sum(1 for r in rows if r["pending"]),
                "disabled": sum(1 for r in rows if r["disabled"]), "admins": sum(1 for r in rows if r["admin"])}

    def session_count(self) -> int:
        """How many logins are active right now."""
        with self._lock:
            now = self._clock()
            return sum(1 for _, _, last in self._sessions.values() if now - last < SESSION_SECONDS)

    def set_password(self, username: str, password: str, must_change: bool = False) -> None:
        """Replace an account's password (this ends all its sessions). With must_change the owner has to pick their own
        at next login: used for the temporary password an administrator hands out."""
        key = key_of(username)
        with self._lock:
            if key not in self._peek():
                raise GameError(404, f"there is no account called {username!r}")
        password = check_password(password, username)
        salt = secrets.token_hex(16)
        digest = self._hash(password, salt, self._iterations)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            accounts[key].update(salt=salt, hash=digest, iterations=self._iterations)
            if must_change:
                accounts[key]["must_change"] = True
            else:
                accounts[key].pop("must_change", None)
            self._save(accounts)
            self._failures = {k: v for k, v in self._failures.items() if not k.startswith(f"{key}|")}  # unlock it for everyone

    def change_password(self, username: str, current: object, new: object, client: str = "") -> str:
        """Check the current password (with the same lockout as logging in), set the new one, and return a fresh session.

        Every other session of the account ends, because the password's salt changes.
        """
        token, name = self.login(username, current, client)  # 401/403/429 exactly as at login
        self.logout(token)
        check_password(new, name)
        self.set_password(name, str(new))
        return self.session_for(name)

    def must_change_password(self, username: str) -> bool:
        """True if an administrator reset this password and the owner has not chosen a new one yet."""
        with self._lock:
            record = self._peek().get(key_of(username))
            return bool(record and record.get("must_change") is True)

    def set_disabled(self, username: str, disabled: bool) -> None:
        """Lock an account out (its sessions stop working at once) or let it back in. Nothing is deleted."""
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if key not in accounts:
                raise GameError(404, f"there is no account called {username!r}")
            if disabled:
                accounts[key]["disabled"] = True
            else:
                accounts[key].pop("disabled", None)
            self._save(accounts)

    def is_disabled(self, username: str) -> bool:
        with self._lock:
            record = self._peek().get(key_of(username))
            return bool(record and record.get("disabled") is True)

    def is_admin(self, username: str) -> bool:
        """True for accounts an administrator has marked as admins (see accounts_admin.py make-admin)."""
        with self._lock:
            record = self._peek().get(key_of(username))
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
            return sorted((r["name"] for r in self._peek().values() if r.get("admin") is True), key=str.casefold)

    def is_bot(self, username: str) -> bool:
        """True for accounts marked as bots (see accounts_admin.py make-bot): automated proposers with no draft or proposal limits."""
        with self._lock:
            record = self._peek().get(key_of(username))
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
            return sorted((r["name"] for r in self._peek().values() if r.get("bot") is True), key=str.casefold)

    def delete(self, username: str) -> None:
        key = key_of(username)
        with self._lock:
            accounts = self._load()
            if accounts.pop(key, None) is None:
                raise GameError(404, f"there is no account called {username!r}")
            self._save(accounts)

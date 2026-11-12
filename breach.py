"""Ask Have I Been Pwned whether a password has appeared in a known data breach, without telling it the password.

Only the first five characters of the password's SHA-1 hash leave this machine (the "k-anonymity" range API); the service
sends back every hash suffix that starts that way and the comparison happens here. Requests ask for padded answers so their
size does not hint at the result. Nothing else is sent: no username, no address of ours beyond the connection itself.

Any failure (no network, a timeout, an odd answer) gives None, meaning "unknown", and callers carry on: the check is an extra
safety net, so an outage at the service must never stop anyone signing up or logging in.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

import alchemylog   # also gives the "alchemy" loggers a quiet default handler

API = "https://api.pwnedpasswords.com/range/"
TIMEOUT_SECONDS = 3.0
DEADLINE_SECONDS = 4.0     # the whole lookup, name resolution included (urlopen's timeout does not cover that, so a stuck lookup could hang)
PAUSE_SECONDS = 60.0       # after a failed or late lookup, skip the service this long so sign-ups do not each wait for it again
CACHE_SECONDS = 24 * 3600.0
MAX_CACHED = 2000
MAX_RESPONSE_BYTES = 2_000_000    # a real answer is about 40 KB

Fetch = Callable[[str], str]
SLOW_SECONDS = 1.0   # a lookup slower than this is logged as a warning
log = logging.getLogger("alchemy.breach")


def fetch_range(prefix: str) -> str:
    """The service's answer for a five-character hash prefix (raises OSError-like errors on failure)."""
    request = urllib.request.Request(API + prefix, headers={"Add-Padding": "true", "User-Agent": "coop-alchemy-password-check"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:   # https only: API is a fixed https URL
        return response.read(MAX_RESPONSE_BYTES).decode("ascii", "replace")


class BreachChecker:
    def __init__(self, fetch: Fetch = fetch_range, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, int]]] = {}   # prefix -> (when fetched, suffix -> count); oldest first
        self._lock = threading.Lock()
        self._paused_until = 0.0

    def _fetch_within_deadline(self, prefix: str) -> str:
        """The service's answer, or OSError if it takes longer than DEADLINE_SECONDS (the stuck call is left to finish on its own thread)."""
        result: list[str] = []
        failure: list[BaseException] = []

        def work() -> None:
            try:
                result.append(self._fetch(prefix))
            except BaseException as exc:   # handed to the caller below, not lost on this thread
                failure.append(exc)
        worker = threading.Thread(target=work, daemon=True, name="breach-lookup")
        worker.start()
        worker.join(DEADLINE_SECONDS)
        if worker.is_alive():
            raise TimeoutError(f"no answer within {DEADLINE_SECONDS:g} seconds")
        if failure:
            raise failure[0]
        return result[0]

    def _range(self, prefix: str) -> dict[str, int] | None:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(prefix)
            if hit and now - hit[0] < CACHE_SECONDS:
                log.info("breach lookup: answered from the cache")
                return hit[1]
        if now < self._paused_until:
            log.info("breach lookup skipped: the service failed lately, trying again in %.0f s", self._paused_until - now)
            return None
        began = time.monotonic()
        try:
            text = self._fetch_within_deadline(prefix)
        except (OSError, ValueError, urllib.error.URLError) as exc:   # TimeoutError is an OSError
            log.warning("breach lookup failed after %.0f ms (%s: %s): treated as unknown, and not tried again for %.0f s",
                        (time.monotonic() - began) * 1000, type(exc).__name__, exc, PAUSE_SECONDS)
            self._paused_until = now + PAUSE_SECONDS
            return None
        elapsed = time.monotonic() - began
        found: dict[str, int] = {}
        for line in text.splitlines():
            suffix, _, count = line.strip().partition(":")
            if len(suffix) == 35 and count.isdigit():
                found[suffix.upper()] = int(count)
        if not found:   # a real answer always has hundreds of lines: treat an empty or garbled one as unknown
            log.warning("breach lookup gave an empty or garbled answer after %.0f ms: treated as unknown", elapsed * 1000)
            return None
        (log.warning if elapsed > SLOW_SECONDS else log.info)(
            "breach lookup ok in %.0f ms (%d suffixes)", elapsed * 1000, len(found))
        with self._lock:
            self._cache.pop(prefix, None)
            self._cache[prefix] = (now, found)
            while len(self._cache) > MAX_CACHED:
                del self._cache[next(iter(self._cache))]
        return found

    def count(self, password: str) -> int | None:
        """How many times the password appears in known breaches (0 if never), or None if that could not be found out."""
        digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        found = self._range(digest[:5])
        return None if found is None else found.get(digest[5:], 0)

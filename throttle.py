"""A small sliding-window rate limiter, used to slow down password guessing and account spam per client address."""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
from collections import deque
from typing import Callable

MAX_TRACKED = 5000


class Throttle:
    """Allows `limit` hits per `window` seconds for each key (a client address)."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> float:
        """0.0 if this hit is allowed (and counted); otherwise the seconds until it would be."""
        now = self._clock()
        with self._lock:
            if len(self._hits) > MAX_TRACKED:
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window}
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return self.window - (now - hits[0])
            hits.append(now)
            return 0.0


class TicketStore:
    """One-time, short-lived tickets that let an event stream connect without putting a player token in the URL."""

    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic, limit: int = 5000) -> None:
        self.ttl, self.limit, self._clock = ttl, limit, clock
        self._tickets: dict[str, tuple[str, str, float]] = {}  # ticket -> (game slug, player token, expires)
        self._lock = threading.Lock()

    def issue(self, slug: str, token: str) -> str:
        now = self._clock()
        with self._lock:
            if len(self._tickets) >= self.limit:
                self._tickets = {t: v for t, v in self._tickets.items() if v[2] > now}
                while len(self._tickets) >= self.limit:  # still full: drop the oldest
                    del self._tickets[min(self._tickets, key=lambda t: self._tickets[t][2])]
            ticket = secrets.token_urlsafe(16)
            self._tickets[ticket] = (slug, token, now + self.ttl)
            return ticket

    def redeem(self, ticket: object, slug: str) -> str | None:
        """The player token a ticket was issued for (the ticket is used up), or None if it is unknown, old or for another game."""
        if not isinstance(ticket, str):
            return None
        with self._lock:
            entry = self._tickets.pop(ticket, None)
        if entry is None or entry[0] != slug or entry[2] <= self._clock():
            return None
        return entry[1]


def parse_networks(items: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Turn '10.0.0.1' / '10.0.0.0/8' strings into networks (ValueError if one is malformed)."""
    return [ipaddress.ip_network(item.strip(), strict=False) for item in items if item.strip()]


def throttle_key(address: str) -> str:
    """The key that rate limits and lockouts use for a client address.

    IPv4 addresses are used as they are. An IPv4-mapped IPv6 address (::ffff:1.2.3.4) counts as that IPv4 address. Any other
    IPv6 address counts as its whole /64 network, because one subscriber is normally given a /64 or more and can switch
    between billions of addresses in it. Anything that is not an address is returned unchanged.
    """
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip(peer: str, forwarded: str | None, trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
              direct: str | None = None) -> str:
    """The real client address. Proxy headers are believed only when the peer is a trusted proxy.

    `direct` is the value of a header that holds exactly one address and that the proxy sets itself (Cloudflare's
    CF-Connecting-IP, see --client-ip-header): if it is a valid address it wins. Otherwise X-Forwarded-For is walked from the
    right (the entry our own proxy appended), skipping further trusted proxies, so a client cannot choose its address by
    sending its own X-Forwarded-For.
    """
    def trusted_addr(text: str) -> bool:
        try:
            return any(ipaddress.ip_address(text) in net for net in trusted)
        except ValueError:
            return False

    if trusted and direct and trusted_addr(peer):
        try:
            return str(ipaddress.ip_address(direct.strip()))
        except ValueError:
            pass  # a malformed value is not trusted: fall back to the other header
    if not trusted or not forwarded or not trusted_addr(peer):
        return peer
    for entry in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            return peer  # malformed header: do not trust any of it
        if not trusted_addr(entry):
            return entry
    return peer

"""Token buckets: a limit on how fast one player may send game requests, with room for short bursts.

The page sends a cursor position at most 20 times a second and a tile move about as often (see CURSOR_TICK_MS and MIN_GAP_MS
in static/app.js), and everything else only when a person clicks or drags. The limits below are about twice the page's own
peak, so honest play never meets them, while a script that floods the server does.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

MOTION_RATE, MOTION_BURST = 50.0, 100   # cursor and tile moves, per second, and the largest burst
ACTION_RATE, ACTION_BURST = 10.0, 30    # every other request a player makes in a game
MAX_TRACKED = 5000


class TokenBuckets:
    """`rate` requests a second for each key, saving up at most `burst` of them while the key is quiet."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic, limit: int = MAX_TRACKED) -> None:
        self.rate, self.burst, self.limit, self._clock = rate, float(burst), limit, clock
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (requests left, when that was worked out); oldest first
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """True if this request may go ahead (and is counted)."""
        now = self._clock()
        with self._lock:
            left, then = self._buckets.pop(key, (self.burst, now))
            left = min(self.burst, left + (now - then) * self.rate)
            allowed = left >= 1.0
            self._buckets[key] = (left - 1.0 if allowed else left, now)   # put back last, so the dict stays oldest first
            while len(self._buckets) > self.limit:
                del self._buckets[next(iter(self._buckets))]
            return allowed

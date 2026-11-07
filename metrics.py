"""Load figures for the Operations page: how busy the server is now, how busy it got, and whether it is near the point to scale.

Every 30 seconds the server records how many players are in games, how many public rooms are running, and how much of one core the process
used since the last sample. The last 24 hours are kept in memory (they start again when the server does). The page shows the current values and
the 24-hour peaks next to the limits in DEPLOYMENT.md ("Capacity (measured)"), so the moment to do something about capacity is visible before
players feel it.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

SAMPLE_SECONDS = 30.0
KEEP_SECONDS = 24 * 3600.0
CPU_WARN = 60.0      # percent of one core; DEPLOYMENT.md: above this, think about scaling
PLAYERS_WARN = 50    # players in games; about 10 rooms is where one process is still pleasant
ROOMS_WARN = 10


class Metrics:
    def __init__(self, *, clock: Callable[[], float] = time.time, cpu: Callable[[], float] = time.process_time) -> None:
        self._clock = clock
        self._cpu = cpu
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, int, int, float]] = deque()   # (when, players, rooms, cpu percent of one core)
        self._last: tuple[float, float] | None = None                    # (when, cpu seconds) at the last sample
        self.started = clock()

    def sample(self, players: int, rooms: int) -> None:
        """Record one sample. CPU is the share of one core used since the previous sample (the first sample has none, so it reads 0)."""
        now, used = self._clock(), self._cpu()
        with self._lock:
            percent = 0.0
            if self._last is not None and now > self._last[0]:
                percent = max(0.0, (used - self._last[1]) / (now - self._last[0]) * 100.0)
            self._last = (now, used)
            self._samples.append((now, int(players), int(rooms), percent))
            while self._samples and now - self._samples[0][0] > KEEP_SECONDS:
                self._samples.popleft()

    def summary(self) -> dict[str, Any]:
        """What the Operations page shows: now, the peaks over the kept hours, and which limits have been reached."""
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"samples": 0, "hours": 0.0, "now": None, "peak": None, "watch": []}
        now = samples[-1]
        peak_players = max(samples, key=lambda s: s[1])
        peak_rooms = max(samples, key=lambda s: s[2])
        peak_cpu = max(samples, key=lambda s: s[3])
        peak = {"players": peak_players[1], "players_at": int(peak_players[0]), "rooms": peak_rooms[2], "rooms_at": int(peak_rooms[0]),
                "cpu": round(peak_cpu[3], 1), "cpu_at": int(peak_cpu[0])}
        watch = [
            {"id": "cpu", "label": "CPU (percent of one core)", "now": round(now[3], 1), "peak": peak["cpu"], "limit": CPU_WARN,
             "hit": peak["cpu"] >= CPU_WARN},
            {"id": "players", "label": "Players in games", "now": now[1], "peak": peak["players"], "limit": PLAYERS_WARN,
             "hit": peak["players"] >= PLAYERS_WARN},
            {"id": "rooms", "label": "Public rooms", "now": now[2], "peak": peak["rooms"], "limit": ROOMS_WARN,
             "hit": peak["rooms"] >= ROOMS_WARN},
        ]
        return {"samples": len(samples), "hours": round((now[0] - samples[0][0]) / 3600.0, 2),
                "now": {"players": now[1], "rooms": now[2], "cpu": round(now[3], 1)}, "peak": peak, "watch": watch}


def metrics_loop(metrics: Metrics, registry: Any) -> None:
    """Take a sample every 30 seconds, forever. A failure here must never stop the server."""
    while True:
        try:
            games = registry.overview()
            metrics.sample(sum(int(g["players"]) for g in games), sum(1 for g in games if not g["private"]))
        except Exception as exc:
            print(f"warning: could not take a load sample: {exc}")
        time.sleep(SAMPLE_SECONDS)

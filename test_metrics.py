"""The Operations page's load figures: players, rooms and CPU, their peaks, and the limits they are compared with."""

from __future__ import annotations

import unittest
from typing import Any

from games import GameRegistry
from metrics import CPU_WARN, KEEP_SECONDS, PLAYERS_WARN, ROOMS_WARN, SAMPLE_SECONDS, Metrics
from test_coop import RECIPES, call, load_recipes, login_as, serve


class Clock:
    def __init__(self) -> None:
        self.wall = 1_000_000.0
        self.cpu = 0.0

    def tick(self, seconds: float, cpu_used: float = 0.0) -> None:
        self.wall += seconds
        self.cpu += cpu_used


def metrics() -> tuple[Metrics, Clock]:
    clock = Clock()
    return Metrics(clock=lambda: clock.wall, cpu=lambda: clock.cpu), clock


class MetricsTests(unittest.TestCase):
    def test_before_any_sample_there_is_nothing_to_show(self) -> None:
        m, _ = metrics()
        self.assertEqual(m.summary(), {"samples": 0, "hours": 0.0, "now": None, "peak": None, "watch": []})

    def test_cpu_is_the_share_of_one_core_used_since_the_last_sample(self) -> None:
        m, clock = metrics()
        m.sample(0, 0)
        clock.tick(30, cpu_used=15)    # 15 s of CPU in 30 s of time: half a core
        m.sample(5, 1)
        self.assertEqual(m.summary()["now"]["cpu"], 50.0)
        clock.tick(30, cpu_used=0)
        m.sample(5, 1)
        self.assertEqual(m.summary()["now"]["cpu"], 0.0)
        self.assertEqual(m.summary()["peak"]["cpu"], 50.0)   # the peak remembers

    def test_the_first_sample_reads_zero_cpu_rather_than_a_guess(self) -> None:
        m, clock = metrics()
        clock.cpu = 999.0
        m.sample(1, 1)
        self.assertEqual(m.summary()["now"]["cpu"], 0.0)

    def test_peaks_cover_players_rooms_and_cpu_with_when_they_happened(self) -> None:
        m, clock = metrics()
        m.sample(2, 1)
        clock.tick(30, 3)
        m.sample(12, 4)
        at_peak = int(clock.wall)
        clock.tick(30, 6)
        m.sample(3, 2)
        peak = m.summary()["peak"]
        self.assertEqual((peak["players"], peak["rooms"]), (12, 4))
        self.assertEqual((peak["players_at"], peak["rooms_at"]), (at_peak, at_peak))
        self.assertEqual(m.summary()["now"]["players"], 3)

    def test_samples_older_than_a_day_are_forgotten(self) -> None:
        m, clock = metrics()
        m.sample(40, 8)
        clock.tick(KEEP_SECONDS + 60)
        m.sample(2, 1)
        summary = m.summary()
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["peak"]["players"], 2)

    def test_hours_covered_are_reported(self) -> None:
        m, clock = metrics()
        for _ in range(121):
            m.sample(1, 1)
            clock.tick(SAMPLE_SECONDS)
        self.assertEqual(m.summary()["hours"], 1.0)

    def test_the_limits_are_flagged_when_a_peak_reaches_them(self) -> None:
        m, clock = metrics()
        m.sample(1, 1)
        clock.tick(30, 30 * CPU_WARN / 100)           # exactly at the CPU limit
        m.sample(PLAYERS_WARN - 1, ROOMS_WARN)         # just under the players limit, at the rooms limit
        watch = {row["id"]: row for row in m.summary()["watch"]}
        self.assertTrue(watch["cpu"]["hit"])
        self.assertFalse(watch["players"]["hit"])
        self.assertTrue(watch["rooms"]["hit"])
        self.assertEqual(watch["cpu"]["limit"], CPU_WARN)

    def test_nothing_is_flagged_for_a_quiet_server(self) -> None:
        m, clock = metrics()
        for _ in range(5):
            m.sample(3, 1)
            clock.tick(30, 1)
        self.assertFalse(any(row["hit"] for row in m.summary()["watch"]))


class OpsStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = GameRegistry(load_recipes(RECIPES), max_games=1)
        cls.server, cls.port = serve(cls.registry)
        cls.admin = login_as(cls.port, "Rooty")
        cls.server.accounts.set_admin("Rooty", True)
        cls.ann = login_as(cls.port, "Annie")
        cls.bob = login_as(cls.port, "Bobby")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method: str, path: str, body: Any, session: str) -> tuple[int, Any]:
        return call(self.port, method, path, body, headers={"X-Session": session})

    def test_status_carries_the_load_for_administrators_only(self) -> None:
        self.assertEqual(self.req("GET", "/ops/status", None, self.ann)[0], 403)
        self.server.metrics.sample(0, 0)
        self.server.metrics.sample(3, 1)
        status, data = self.req("GET", "/ops/status", None, self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(data["load"]["now"]["players"], 3)
        self.assertEqual({row["id"] for row in data["load"]["watch"]}, {"cpu", "players", "rooms"})

    def test_a_refused_game_because_the_lobby_is_full_is_counted_as_demand(self) -> None:
        self.assertEqual(self.req("POST", "/games", {"name": "First room"}, self.ann)[0], 200)
        before = self.req("GET", "/ops/status", None, self.admin)[1]["since_start"].get("lobby_full", 0)
        status, body = self.req("POST", "/games", {"name": "Second room"}, self.bob)
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])
        after = self.req("GET", "/ops/status", None, self.admin)[1]["since_start"].get("lobby_full", 0)
        self.assertEqual(after, before + 1)

    def test_other_refusals_are_not_counted_as_the_lobby_being_full(self) -> None:
        before = self.req("GET", "/ops/status", None, self.admin)[1]["since_start"].get("lobby_full", 0)
        self.req("POST", "/games", {"name": "bitch club"}, self.bob)   # refused for its name, not for room
        after = self.req("GET", "/ops/status", None, self.admin)[1]["since_start"].get("lobby_full", 0)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

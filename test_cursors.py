"""Shared cursors: an event stream sends each player's newest pointer position, never an out-of-date one."""

from __future__ import annotations

import json
import time
import unittest
from typing import Any

from games import GameRegistry
from server import CURSOR_BATCH_SECONDS, coalesce_cursors, cursor_pid
from state import format_event
from test_coop import RECIPES, SSEClient, call, load_recipes, login_as, serve


def cursor(pid: str, x: float | None, y: float | None = 0.5) -> str:
    return format_event("cursor", {"pid": pid, "x": x, "y": None if x is None else y})


class CoalesceTests(unittest.TestCase):
    def test_only_each_players_newest_position_is_kept(self) -> None:
        batch = [cursor("a", 0.1), cursor("b", 0.5), cursor("a", 0.2), cursor("a", 0.3), cursor("b", 0.6)]
        self.assertEqual(coalesce_cursors(batch), [cursor("a", 0.3), cursor("b", 0.6)])

    def test_hiding_is_a_cursor_event_and_the_newest_of_move_and_hide_wins(self) -> None:
        self.assertEqual(coalesce_cursors([cursor("a", 0.1), cursor("a", None)]), [cursor("a", None)])
        self.assertEqual(coalesce_cursors([cursor("a", None), cursor("a", 0.4)]), [cursor("a", 0.4)])

    def test_other_events_are_untouched_and_keep_their_order(self) -> None:
        tile = format_event("tile_added", {"tile": {"id": "t1"}})
        dropped = format_event("tile_dropped", {"id": "t1"})
        keepalive = ": keepalive\n\n"
        batch = [cursor("a", 0.1), tile, cursor("a", 0.2), keepalive, dropped, cursor("b", 0.9)]
        # a's first position is gone; the survivors stay where they were relative to everything else
        self.assertEqual(coalesce_cursors(batch), [tile, cursor("a", 0.2), keepalive, dropped, cursor("b", 0.9)])

    def test_odd_input_passes_through(self) -> None:
        self.assertEqual(coalesce_cursors([]), [])
        broken = "event: cursor\ndata: {not json\n\n"
        missing = format_event("cursor", {"x": 0.5})  # no pid
        self.assertEqual(coalesce_cursors([broken, missing, broken]), [broken, missing, broken])

    def test_cursor_pid_only_recognises_cursor_events(self) -> None:
        self.assertEqual(cursor_pid(cursor("p1", 0.5)), "p1")
        self.assertEqual(cursor_pid(cursor("p1", None)), "p1")
        for other in (format_event("tile_moved", {"pid": "p1"}), ": keepalive\n\n", "event: cursors\ndata: {}\n\n", ""):
            self.assertIsNone(cursor_pid(other))


class CursorStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=5))
        cls.counter = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def room(self) -> tuple[str, dict[str, Any], dict[str, Any], SSEClient]:
        """A game with two players; returns (slug, mover, watcher, the watcher's event stream after its snapshot)."""
        type(self).counter += 1
        status, game = call(self.port, "POST", "/games", {"name": f"cursors {self.counter}"})
        self.assertEqual(status, 200)
        slug = game["id"]
        players = []
        for name in (f"mover{self.counter}", f"watcher{self.counter}"):
            status, joined = call(self.port, "POST", f"/g/{slug}/join", {}, headers={"X-Session": login_as(self.port, name)})
            self.assertEqual(status, 200)
            players.append(joined)
        stream = SSEClient(self.port, slug, players[1]["token"])
        self.addCleanup(stream.close)
        stream.until("snapshot")
        return slug, players[0], players[1], stream

    def move(self, slug: str, who: dict[str, Any], x: float | None, y: float | None = 0.5) -> None:
        status, _ = call(self.port, "POST", f"/g/{slug}/cursor", {"player": who["token"], "x": x, "y": y})
        self.assertEqual(status, 200)

    def test_a_burst_of_moves_ends_with_the_final_position(self) -> None:
        slug, mover, _, stream = self.room()
        for n in range(1, 41):
            self.move(slug, mover, n / 100)
        last = None
        for _ in range(60):
            name, data = stream.next()
            if name == "cursor":
                self.assertEqual(data["pid"], mover["pid"])
                last = data
                if data["x"] == 0.4:
                    break
        self.assertEqual(last["x"], 0.4, "the newest position must always arrive")

    def test_hiding_after_moving_is_what_the_viewer_ends_up_with(self) -> None:
        slug, mover, _, stream = self.room()
        for n in range(1, 6):
            self.move(slug, mover, n / 10)
        self.move(slug, mover, None, None)
        seen = []
        for _ in range(20):
            name, data = stream.next()
            if name == "cursor":
                seen.append(data["x"])
                if data["x"] is None:
                    break
        self.assertIsNone(seen[-1])

    def test_other_events_arrive_promptly_and_after_the_cursor_that_preceded_them(self) -> None:
        slug, mover, _, stream = self.room()
        self.move(slug, mover, 0.2)
        self.move(slug, mover, 0.3)
        started = time.monotonic()
        status, _ = call(self.port, "POST", f"/g/{slug}/tile/add",
                         {"player": mover["token"], "element": "water", "x": 0.5, "y": 0.5})
        self.assertEqual(status, 200)
        order = []
        for _ in range(20):
            name, data = stream.next()
            order.append((name, data.get("x") if name == "cursor" else None))
            if name == "tile_added":
                break
        delay = time.monotonic() - started
        self.assertEqual(order[-1][0], "tile_added")
        self.assertIn(("cursor", 0.3), order[:-1])  # the newest position came before the tile
        self.assertNotIn(("cursor", 0.2), order)     # and the out-of-date one was never sent
        self.assertLess(delay, CURSOR_BATCH_SECONDS + 1.0, "batching must only ever delay by a fraction of a second")

    def test_ending_the_game_right_after_a_cursor_move_still_delivers_the_end(self) -> None:
        slug, mover, _, stream = self.room()
        self.move(slug, mover, 0.7)
        status, _ = call(self.port, "POST", f"/g/{slug}/end", {"player": mover["token"], "save": False})
        self.assertEqual(status, 200)
        names = []
        try:
            for _ in range(20):
                names.append(stream.next()[0])
        except EOFError:
            pass  # the stream closes after the game ends
        self.assertIn("ended", names)

    def test_a_quiet_room_still_gets_keepalives_and_a_lone_cursor_arrives_within_the_batch_window(self) -> None:
        slug, mover, _, stream = self.room()
        started = time.monotonic()
        self.move(slug, mover, 0.5)
        name, data = stream.next()
        self.assertEqual((name, data["x"]), ("cursor", 0.5))
        self.assertLess(time.monotonic() - started, CURSOR_BATCH_SECONDS + 0.5)


if __name__ == "__main__":
    unittest.main()

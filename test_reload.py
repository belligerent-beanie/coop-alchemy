"""Tests for reloading the server without dropping games (hotreload.py, reload.py, server.run)."""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from accounts import AccountStore
from games import GameRegistry
from hotreload import restore_snapshot, write_snapshot
from recipes import load_recipes
from state import GameState

BASE_DIR = Path(__file__).resolve().parent
BOOK = load_recipes(BASE_DIR / "test_recipes.json")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SnapshotTests(unittest.TestCase):
    def make(self) -> tuple[GameRegistry, AccountStore, tempfile.TemporaryDirectory[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        accounts = AccountStore(Path(tmp.name) / "accounts.json", iterations=1000)
        return GameRegistry(BOOK, clock=FakeClock()), accounts, tmp

    def test_game_survives_with_tiles_players_and_discoveries(self) -> None:
        registry, accounts, tmp = self.make()
        game = registry.create("Friday", creator="Ann")
        ann = game.join("Ann")
        sub = game.subscribe(ann.token)
        game.add_tile(ann.token, "water", 0.25, 0.5)
        game.add_tile(ann.token, "fire", 0.75, 0.5)
        game.grab_tile(ann.token, "t1")
        game.combine(ann.token, "t2", tile="t1")  # water + fire -> steam
        before = game.export()
        tiles_before = game.tiles()
        write_snapshot(Path(tmp.name) / "snap.json", registry, accounts)
        game.unsubscribe(sub)

        fresh, fresh_accounts, _ = self.make()
        self.assertEqual(restore_snapshot(Path(tmp.name) / "snap.json", fresh, fresh_accounts), (1, 0))
        again = fresh.get("friday")
        assert again is not None
        self.assertEqual(again.export()["discovered"], before["discovered"])
        self.assertEqual(again.creator, "Ann")
        self.assertEqual(again.tiles(), tiles_before)
        # The player's token still works, and a new tile does not reuse an old id.
        self.assertEqual(again.authorize(ann.token).name, "Ann")
        new = again.add_tile(ann.token, "water", 0.1, 0.1)
        self.assertNotIn(new["tile"]["id"] if "tile" in new else new.get("id"), {t["id"] for t in tiles_before})
        self.assertIsNotNone(again.subscribe(ann.token))  # reconnecting inside the grace period works
        self.assertTrue((Path(tmp.name) / "snap.json.last").exists())
        self.assertFalse((Path(tmp.name) / "snap.json").exists())

    def test_players_are_forgotten_if_they_do_not_come_back(self) -> None:
        clock = FakeClock()
        game = GameState(BOOK, clock=clock)
        ann = game.join("Ann")
        game.subscribe(ann.token)
        data = game.live_export()
        fresh = GameState(BOOK, clock=clock)
        fresh.live_restore(data)
        clock.now += 60
        with self.assertRaises(Exception):
            fresh.authorize(ann.token)

    def test_sessions_carry_over_and_respect_password_resets(self) -> None:
        registry, accounts, tmp = self.make()
        accounts.register("Annie", "password-Annie")
        token = accounts.session_for("Annie")
        path = Path(tmp.name) / "snap.json"
        write_snapshot(path, registry, accounts)
        accounts2 = AccountStore(Path(tmp.name) / "accounts.json", iterations=1000)
        self.assertEqual(restore_snapshot(path, registry, accounts2), (0, 1))
        self.assertEqual(accounts2.username(token), "Annie")
        # A session for an account whose password changed in between is not honoured.
        write_snapshot(path, registry, accounts2)
        accounts2.set_password("Annie", "another-password")
        accounts3 = AccountStore(Path(tmp.name) / "accounts.json", iterations=1000)
        restore_snapshot(path, registry, accounts3)
        self.assertIsNone(accounts3.username(token))

    def test_bad_old_or_missing_snapshots_are_harmless(self) -> None:
        registry, accounts, tmp = self.make()
        path = Path(tmp.name) / "snap.json"
        self.assertIsNone(restore_snapshot(path, registry, accounts))
        path.write_text("{not json")
        self.assertIsNone(restore_snapshot(path, registry, accounts))
        self.assertTrue((Path(tmp.name) / "snap.json.bad").exists())
        path.write_text(json.dumps({"version": 1, "written": time.time() - 3600, "games": [], "sessions": []}))
        self.assertIsNone(restore_snapshot(path, registry, accounts))
        self.assertTrue((Path(tmp.name) / "snap.json.old").exists())
        path.write_text(json.dumps({"version": 1, "written": time.time(),
                                    "games": [5, {"slug": "../x"}, {"slug": "ok", "name": "Ok", "live": {
                                        "tiles": [{"element": "nope", "x": 0, "y": 0, "id": "t1"}, 3],
                                        "players": [{"token": 1}], "tile_seq": "x"}}],
                                    "sessions": ["bad", {"token": "t"}]}))
        self.assertEqual(restore_snapshot(path, registry, accounts), (1, 0))
        game = registry.get("ok")
        assert game is not None
        self.assertEqual(game.tiles(), [])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@unittest.skipUnless(hasattr(signal, "SIGUSR1"), "needs Unix signals")
class RealReloadTests(unittest.TestCase):
    """Starts a real server process, reloads it, and checks the game is still there."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.port = free_port()
        self.pid_file = self.dir / "alchemy.pid"
        self.proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "server.py"), "--host", "127.0.0.1", "--port", str(self.port),
             "--recipes", str(BASE_DIR / "test_recipes.json"), "--saves-dir", str(self.dir / "saves"),
             "--accounts-file", str(self.dir / "accounts.json"), "--snapshot-file", str(self.dir / "snap.json"),
             "--pid-file", str(self.pid_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            if self.pid_file.exists():
                break
            time.sleep(0.1)
        else:
            self.fail("the server did not start")

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, body: Any = None, session: str = "") -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json", "X-Session": session}
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            res = conn.getresponse()
            raw = res.read()
            return res.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
        finally:
            conn.close()

    def first_snapshot(self, slug: str, token: str) -> dict[str, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            status, ticket = self.call("POST", f"/g/{slug}/ticket", {"player": token})
            self.assertEqual(status, 200)
            conn.request("GET", f"/g/{slug}/events?ticket={ticket['ticket']}")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            event = ""
            while True:
                line = res.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event == "snapshot":
                    return json.loads(line[6:])
        finally:
            conn.close()

    def test_reload_keeps_games_tiles_players_and_logins(self) -> None:
        body = {"username": "Annie", "password": "password-Annie"}
        session = self.call("POST", "/register", body)[1]["session"]
        status, game = self.call("POST", "/games", {"name": "Live"}, session)
        self.assertEqual(status, 200)
        player = self.call("POST", "/g/live/join", {}, session)[1]
        for x in (0.2, 0.8):
            status, _ = self.call("POST", "/g/live/tile/add",
                                  {"player": player["token"], "element": "water", "x": x, "y": 0.5}, session)
            self.assertEqual(status, 200)
        version = self.call("GET", "/version")[1]["version"]

        result = subprocess.run([sys.executable, str(BASE_DIR / "reload.py"), "--pid-file", str(self.pid_file)],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reloaded", result.stdout)

        self.assertEqual(self.proc.poll(), None)  # same process, still running
        self.assertEqual(json.loads(self.pid_file.read_text())["pid"], self.proc.pid)
        self.assertEqual(self.call("GET", "/me", session=session)[1], {"username": "Annie", "admin": False, "must_change_password": False})
        self.assertEqual([g["id"] for g in self.call("GET", "/games", session=session)[1]["games"]], ["live"])
        snap = self.first_snapshot("live", player["token"])
        self.assertEqual(snap["you"], player["pid"])
        self.assertEqual(sorted(t["x"] for t in snap["tiles"]), [0.2, 0.8])
        self.assertEqual(self.call("GET", "/version")[1]["version"], version)
        # And it can be reloaded again.
        again = subprocess.run([sys.executable, str(BASE_DIR / "reload.py"), "--pid-file", str(self.pid_file)],
                               capture_output=True, text=True, timeout=40)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_reload_refuses_when_the_new_code_is_broken(self) -> None:
        info = json.loads(self.pid_file.read_text())
        info["recipes"] = str(self.dir / "missing.json")  # stands in for any startup problem
        self.pid_file.write_text(json.dumps(info))
        result = subprocess.run([sys.executable, str(BASE_DIR / "reload.py"), "--pid-file", str(self.pid_file)],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT reloaded", result.stderr)
        self.assertIsNone(self.proc.poll())
        self.assertFalse((self.dir / "snap.json").exists())


class VersionTests(unittest.TestCase):
    def test_version_changes_when_a_page_file_changes(self) -> None:
        from server import AlchemyServer, Handler
        tmp = tempfile.TemporaryDirectory()
        static = Path(tmp.name)
        (static / "a.js").write_text("one")
        server = AlchemyServer(("127.0.0.1", 0), GameRegistry(BOOK), static,
                               AccountStore(static / "x" / "accounts.json"))
        try:
            handler = Handler.__new__(Handler)
            handler.server = server
            first = handler._static_version()
            (static / "elements").mkdir()
            (static / "elements" / "water.png").write_bytes(b"x")
            self.assertEqual(handler._static_version(), first)  # pictures do not count
            (static / "a.js").write_text("two!")
            self.assertNotEqual(handler._static_version(), first)
        finally:
            server.server_close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

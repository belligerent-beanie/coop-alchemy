"""Log rotation, the daily summary, the games limit, and that tracebacks never carry client addresses."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import logging
import logging.handlers
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bench_cursor import free_port, wait_up
from games import GameRegistry
from ops import daily_summary
from recipes import load_recipes
from server import configure_logging
from test_coop import RECIPES, serve

ROOT = Path(__file__).resolve().parent


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self.saved_handlers, self.saved_level = list(root.handlers), root.level
        self.addCleanup(self.restore)

    def restore(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in self.saved_handlers:
            root.addHandler(handler)
        root.setLevel(self.saved_level)

    def test_a_log_file_rotates_at_midnight_and_keeps_the_requested_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = configure_logging(str(Path(tmp) / "live.log"), 9)
            self.assertIsInstance(handler, logging.handlers.TimedRotatingFileHandler)
            assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
            self.assertEqual((handler.backupCount, handler.when), (9, "MIDNIGHT"))
            logging.getLogger("alchemy.auth").info("login ok client=1.2.3.4 user=ann")
            handler.flush()
            self.assertIn("login ok", (Path(tmp) / "live.log").read_text(encoding="utf-8"))
            self.restore()  # release the file before the folder is deleted (Windows)

    def test_without_a_file_it_logs_to_the_console_and_replaces_earlier_handlers(self) -> None:
        configure_logging(None)
        configure_logging(None)
        self.assertEqual(len(logging.getLogger().handlers), 1)
        self.assertIsInstance(logging.getLogger().handlers[0], logging.StreamHandler)

    def test_a_silly_number_of_days_still_keeps_at_least_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = configure_logging(str(Path(tmp) / "x.log"), 0)
            assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
            self.assertEqual(handler.backupCount, 1)
            self.restore()


class ErrorHandlerTests(unittest.TestCase):
    def test_a_crash_is_logged_with_its_traceback_but_without_the_client_address(self) -> None:
        server, _ = serve(GameRegistry(load_recipes(RECIPES), max_games=1))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        console = io.StringIO()
        with self.assertLogs("alchemy.server", "ERROR") as logged, contextlib.redirect_stderr(console):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                server.handle_error(None, ("203.0.113.9", 51234))
        text = "\n".join(logged.output) + console.getvalue()
        self.assertIn("boom", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("203.0.113.9", text)
        self.assertEqual(server.stats["server_errors"], 1)

    def test_a_browser_closing_a_tab_is_not_an_error(self) -> None:
        server, _ = serve(GameRegistry(load_recipes(RECIPES), max_games=1))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        try:
            raise ConnectionResetError()
        except ConnectionResetError:
            server.handle_error(None, ("203.0.113.9", 1))
        self.assertEqual(server.stats["server_errors"], 0)


class RealServerOptionTests(unittest.TestCase):
    """Starts the real server process with the new options."""

    def start(self, *extra: str) -> tuple[int, Path, subprocess.Popen]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lambda: self.cleanup_dir(tmp))
        folder, port = Path(tmp.name), free_port()
        server = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--host", "127.0.0.1", "--port", str(port),
             "--recipes", str(ROOT / "test_recipes.json"), "--accounts-file", str(folder / "a.json"),
             "--saves-dir", str(folder / "saves"), "--drafts-file", str(folder / "d.json"),
             "--daily-file", str(folder / "daily.json"), "--snapshot-file", str(folder / "s.json"),
             "--pid-file", str(folder / "p.pid"), "--reports-file", str(folder / "r.json"), *extra],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, server)
        wait_up(port)
        return port, folder, server

    @staticmethod
    def stop(server: subprocess.Popen) -> None:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()

    @staticmethod
    def cleanup_dir(tmp: tempfile.TemporaryDirectory) -> None:
        try:
            tmp.cleanup()
        except OSError:
            pass

    @staticmethod
    def post(port: int, path: str, body: dict, session: str = "") -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Session": session})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_max_games_limits_running_games(self) -> None:
        port, _, _ = self.start("--max-games", "1")
        status, data = self.post(port, "/register", {"username": "limiter", "password": "limiter-pass-123"})
        self.assertEqual(status, 200)
        session = data["session"]
        self.assertEqual(self.post(port, "/games", {"name": "first"}, session)[0], 200)
        status, data = self.post(port, "/games", {"name": "second"}, session)
        self.assertEqual(status, 409)
        self.assertIn("1 games are already running", data["error"])

    def test_the_log_file_records_activity_and_nothing_goes_to_the_console_log(self) -> None:
        log = Path(tempfile.mkdtemp()) / "live.log"
        port, _, server = self.start("--log-file", str(log), "--log-days", "3")
        self.post(port, "/register", {"username": "logged", "password": "logged-pass-1234"})
        self.post(port, "/login", {"username": "logged", "password": "wrong-password"})
        deadline = time.time() + 5
        while time.time() < deadline and not (log.exists() and "login failed" in log.read_text(encoding="utf-8")):
            time.sleep(0.1)
        text = log.read_text(encoding="utf-8")
        self.assertIn("register ok", text)
        self.assertIn("login failed", text)
        self.stop(server)  # release the file (Windows) before the folder goes


class DailySummaryTests(unittest.TestCase):
    SAMPLE = """\
2026-11-05 09:00:01,000 alchemy.auth register ok client=203.0.113.5 user=secretname1
2026-11-05 09:05:00,000 alchemy.auth login ok client=203.0.113.5 user=secretname1
2026-11-05 09:06:00,000 alchemy.auth login failed status=401 client=198.51.100.7 user=secretname2
2026-11-05 09:06:05,000 alchemy.auth login failed status=401 client=198.51.100.7 user=secretname2
2026-11-05 09:06:09,000 alchemy.auth login throttled client=198.51.100.7
2026-11-05 10:00:00,000 alchemy.auth admin opsboss: disable secretname2
2026-11-05 11:00:00,000 alchemy.auth account deleted client=203.0.113.5 user=secretname1
2026-11-05 12:00:00,000 alchemy.server unexpected error while handling a request
2026-11-04 23:59:59,000 alchemy.auth register ok client=192.0.2.1 user=yesterdays
2026-11-06 00:00:01,000 alchemy.auth register ok client=192.0.2.2 user=tomorrows
"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.live = Path(self.tmp.name)
        (self.live / "data").mkdir()
        (self.live / "data" / "live.log").write_text(self.SAMPLE, encoding="utf-8")
        (self.live / "data" / "live.log.2026-11-04").write_text("2026-11-05 01:00:00,000 alchemy.auth login ok client=203.0.113.9 user=x\n",
                                                                 encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_counts_only_the_requested_day_across_rotated_files(self) -> None:
        counts = daily_summary.summarise(sorted((self.live / "data").glob("live.log*")), "2026-11-05")
        self.assertEqual(counts["sign-ups"], 1)
        self.assertEqual(counts["logins"], 2)  # one in each file
        self.assertEqual(counts["failed logins"], 2)
        self.assertEqual(counts["rate-limited requests"], 1)
        self.assertEqual(counts["administrator actions"], 1)
        self.assertEqual(counts["accounts deleted"], 1)
        self.assertEqual(counts["server errors"], 1)
        self.assertEqual(counts["different network addresses"], 3)

    def test_the_summary_has_no_names_or_addresses(self) -> None:
        text = daily_summary.render("2026-11-05", daily_summary.summarise(sorted((self.live / "data").glob("live.log*")), "2026-11-05"), True)
        for private in ("secretname", "203.0.113", "198.51.100", "opsboss", "client="):
            self.assertNotIn(private, text)
        self.assertIn("sign-ups", text)

    def test_the_command_writes_a_file_and_copes_with_no_log(self) -> None:
        self.assertEqual(daily_summary.main(["--live-dir", str(self.live), "--date", "2026-11-05"]), 0)
        written = (self.live / "summaries" / "2026-11-05.txt").read_text(encoding="utf-8")
        self.assertIn("failed logins", written)
        empty = Path(tempfile.mkdtemp())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(daily_summary.main(["--live-dir", str(empty), "--date", "2026-11-05"]), 0)
        self.assertIn("no log file found", out.getvalue())


if __name__ == "__main__":
    unittest.main()

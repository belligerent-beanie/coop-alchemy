"""The supervisor (ops/supervise.py): restarts after a crash or a hang, never restart-loops, never starts a second server, keeps
running games across a restart, runs the daily jobs, and is stopped and restarted through its request file."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from bench_cursor import free_port
from ops import supervise
from ops.supervise import Supervisor

ROOT = Path(__file__).resolve().parent


class FakeServer:
    """Stands in for the server process."""
    count = 0

    def __init__(self, data_dir: Path | None = None) -> None:
        FakeServer.count += 1
        self.pid = 1000 + FakeServer.count
        self.returncode: int | None = None
        self.stopped_politely = False
        self.data_dir = data_dir

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.stopped_politely = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        # A polite stop is the stop file the server looks for (the only way on Windows) and, elsewhere, SIGTERM as well.
        if self.data_dir is not None and (self.data_dir / "alchemy.stop").exists():
            self.stopped_politely = True
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.now = [1000.0]
        self.wall = [time.mktime((2026, 11, 13, 10, 0, 0, 0, 0, -1))]
        self.answering = [True]
        self.started: list[FakeServer] = []
        self.port_taken = [False]

        def popen(command: list[str], **kwargs: object) -> FakeServer:
            server = FakeServer(self.data)
            self.started.append(server)
            return server

        def healthy(port: int) -> bool:   # before our server starts: is someone else on the port?
            if self.port_taken[0]:
                return True
            return bool(self.started) and self.started[-1].poll() is None and self.answering[0]

        self.lines: list[str] = []
        self.sup = Supervisor(command=["server"], cwd=self.data, data_dir=self.data, port=8000,
                              backups_dir=self.data / "backups", summaries_dir=self.data / "summaries",
                              popen=popen, healthy=healthy, mono=lambda: self.now[0], wall=lambda: self.wall[0],
                              echo=self.lines.append)

    def advance(self, seconds: float, ticks: int = 1) -> None:
        for _ in range(ticks):
            self.now[0] += seconds / ticks
            self.wall[0] += seconds / ticks
            self.sup.tick()

    def test_it_starts_the_server(self) -> None:
        self.sup.tick()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.sup.status(), "running")

    def test_a_crash_is_restarted_after_a_growing_pause_and_a_crash_loop_pauses(self) -> None:
        self.sup.tick()
        delays = []
        for _ in range(supervise.MAX_RESTARTS):
            self.started[-1].returncode = 1   # it crashes
            count = len(self.started)
            self.sup.tick()
            waited = 0
            while len(self.started) == count:
                self.advance(1)
                waited += 1
            delays.append(waited)
        self.assertEqual(delays, [2, 4, 8, 16, 32])
        self.started[-1].returncode = 1       # one more within ten minutes: it gives up for a while
        self.sup.tick()
        self.assertIn("paused", self.sup.status())
        self.assertTrue(any("GIVING UP" in line for line in self.lines))
        count = len(self.started)
        self.advance(supervise.PAUSE - 5, ticks=20)
        self.assertEqual(len(self.started), count)   # nothing while paused
        self.advance(10, ticks=10)
        self.assertEqual(len(self.started), count + 1)

    def test_a_hung_server_is_restarted_after_three_missed_checks(self) -> None:
        self.sup.tick()
        self.advance(supervise.STARTUP_GRACE)
        self.answering[0] = False
        self.advance(supervise.HEALTH_EVERY * (supervise.FAILURES_NEEDED - 1), ticks=supervise.FAILURES_NEEDED - 1)
        self.assertEqual(len(self.started), 1)        # two misses: still patient
        self.advance(supervise.HEALTH_EVERY)
        self.assertTrue(self.started[0].stopped_politely)
        self.answering[0] = True
        self.advance(3, ticks=3)
        self.assertEqual(len(self.started), 2)
        self.assertTrue(any("not answering" in line for line in self.lines))

    def test_it_never_starts_a_second_server_on_a_busy_port(self) -> None:
        self.port_taken[0] = True
        self.sup.tick()
        self.advance(60, ticks=6)
        self.assertEqual(self.started, [])
        self.assertIn("waiting", self.sup.status())
        self.port_taken[0] = False
        self.advance(30, ticks=3)
        self.assertEqual(len(self.started), 1)

    def test_requests_restart_and_stop(self) -> None:
        self.sup.tick()
        supervise.request(self.data, "restart")
        self.sup.tick()
        self.assertTrue(self.started[0].stopped_politely)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.sup.restarts, [])      # asked for: not a failure
        self.assertFalse((self.data / supervise.REQUEST_FILE).exists())
        supervise.request(self.data, "stop")
        self.assertFalse(self.sup.tick())
        self.assertIsNotNone(self.started[1].poll())
        self.assertEqual(json.loads((self.data / supervise.STATE_FILE).read_text())["status"], "stopped")

    def test_the_daily_backup_and_summary_run_once_a_day_at_their_time(self) -> None:
        (self.data / "accounts.json").write_text('{"accounts": {}}', encoding="utf-8")
        self.wall[0] = time.mktime((2026, 11, 14, 3, 59, 0, 0, 0, -1))
        self.sup.tick()
        self.assertFalse((self.data / "backups").exists())
        self.advance(120, ticks=4)                  # 04:01
        self.assertEqual(len(list((self.data / "backups").iterdir())), 1)
        self.advance(3600, ticks=10)                # 05:01: the summary too, and still one backup
        self.assertEqual(len(list((self.data / "backups").iterdir())), 1)
        self.assertTrue((self.data / "summaries" / "2026-11-13.txt").is_file())
        again = Supervisor(command=["server"], cwd=self.data, data_dir=self.data, port=8000, backups_dir=self.data / "backups",
                           mono=lambda: self.now[0], wall=lambda: self.wall[0], echo=self.lines.append,
                           popen=lambda *a, **k: FakeServer(), healthy=lambda port: False)
        again.run_jobs()                            # a restarted supervisor remembers it already ran today
        self.assertEqual(len(list((self.data / "backups").iterdir())), 1)

    def test_the_server_gets_every_data_file_in_the_data_folder(self) -> None:
        command = supervise.server_command("py", Path("/app"), Path("/data"), 8000, "0.0.0.0", ["--require-invite"])
        self.assertEqual(command[:2], ["py", str(Path("/app") / "server.py")])
        for option, name in supervise.DATA_OPTIONS:
            self.assertEqual(command[command.index(option) + 1], str(Path("/data") / name))
        self.assertEqual(command[-1], "--require-invite")

    def test_one_supervisor_per_data_folder(self) -> None:
        first = supervise.Lock(self.data / supervise.LOCK_FILE)
        self.assertTrue(first.acquire())
        self.assertTrue(supervise.supervisor_running(self.data))
        self.assertFalse(supervise.Lock(self.data / supervise.LOCK_FILE).acquire())
        first.handle.close()
        self.assertFalse(supervise.supervisor_running(self.data))

    def test_pid_alive(self) -> None:
        self.assertTrue(supervise.pid_alive(os.getpid()))
        self.assertFalse(supervise.pid_alive(0))


class InstallerTests(unittest.TestCase):
    """What install_supervisor.py registers (the registering itself needs a real Mac login or Windows PC)."""

    def test_the_windows_task_never_times_out_restarts_on_failure_and_quotes_paths(self) -> None:
        import xml.etree.ElementTree as ET
        from ops import install_supervisor as inst
        command = [r"C:\Program Files\Python313\pythonw.exe", r"C:\games\coop alchemy\ops\supervise.py", "run",
                   "--data-dir", r"C:\games\coop alchemy\data", "--", "--contact", "a&b <me>"]
        tree = ET.fromstring(inst.task_xml(command).split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(tree.find("t:Settings/t:ExecutionTimeLimit", ns).text, "PT0S")   # the default would end it after 72 hours
        self.assertEqual(tree.find("t:Settings/t:RestartOnFailure/t:Interval", ns).text, "PT1M")
        self.assertIsNotNone(tree.find("t:Triggers/t:LogonTrigger", ns))
        self.assertEqual(tree.find("t:Actions/t:Exec/t:Command", ns).text, command[0])
        arguments = tree.find("t:Actions/t:Exec/t:Arguments", ns).text
        self.assertIn('"C:\\games\\coop alchemy\\data"', arguments)      # a path with a space stays one argument
        self.assertIn("a&b <me>", arguments)                                  # XML-escaped in the file, intact when read

    def test_the_mac_launch_agent_starts_at_login_and_after_a_crash_only(self) -> None:
        import plistlib
        from ops import install_supervisor as inst
        agent = plistlib.loads(inst.plist_for(["python3", "ops/supervise.py", "run"], Path("/tmp/data")))
        self.assertEqual(agent["Label"], inst.LABEL)
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})      # not again after a deliberate stop
        self.assertEqual(agent["ProgramArguments"], ["python3", "ops/supervise.py", "run"])


def kill_outright(pid: int) -> None:
    """End a process with no chance to clean up, as a crash does (Windows has no SIGKILL; taskkill /F is its equivalent)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=True)
    else:
        os.kill(pid, signal.SIGKILL)


class RealSupervisorTest(unittest.TestCase):
    """The real thing: a supervisor process running a real server on a scratch data folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.port = free_port()

    def get(self, path: str, session: str = "", body: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Session": session})
        with urllib.request.urlopen(req, timeout=5) as reply:
            return json.loads(reply.read())

    def wait_for(self, check, timeout: float = 30.0) -> object:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                value = check()
                if value:
                    return value
            except Exception:
                pass
            time.sleep(0.3)
        self.fail("timed out waiting")

    def server_pid(self) -> int | None:
        return (supervise.read_state(self.data) or {}).get("server_pid")

    def test_crash_restart_keeps_games_and_stop(self) -> None:
        sup = subprocess.Popen([sys.executable, str(ROOT / "ops" / "supervise.py"), "run", "--data-dir", str(self.data),
                                "--port", str(self.port), "--host", "127.0.0.1", "--no-keep-awake", "--backup-at", "off",
                                "--summary-at", "off", "--", "--recipes", str(ROOT / "test_recipes.json"), "--no-breach-check"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: sup.poll() is None and sup.kill())
        self.wait_for(lambda: supervise.probe(self.port))
        first = self.wait_for(self.server_pid)

        # a game is running when a restart is asked for: it is still there afterwards
        session = self.get("/register", body={"username": "keeper", "password": "a long enough password"})["session"]
        subprocess.run([sys.executable, str(ROOT / "accounts_admin.py"), "--file", str(self.data / "accounts.json"),
                        "make-admin", "keeper"], capture_output=True, check=True)   # admins may start shared games at once
        game = self.get("/games", session, {"name": "Kept across restarts"})
        self.assertEqual(subprocess.run([sys.executable, str(ROOT / "ops" / "supervise.py"), "restart", "--data-dir", str(self.data)],
                                        capture_output=True).returncode, 0)
        second = self.wait_for(lambda: (pid := self.server_pid()) and pid != first and supervise.probe(self.port) and pid)
        names = [g["name"] for g in self.get("/games", session)["games"]]
        self.assertIn("Kept across restarts", names, "the running game was lost in the restart")
        self.assertIn(game["id"], [g["id"] for g in self.get("/games", session)["games"]])

        # the server is killed outright (a crash): it comes back by itself
        kill_outright(second)
        self.wait_for(lambda: (pid := self.server_pid()) and pid != second and supervise.probe(self.port))

        # stop: the supervisor ends cleanly, and the server with it
        self.assertEqual(subprocess.run([sys.executable, str(ROOT / "ops" / "supervise.py"), "stop", "--data-dir", str(self.data)],
                                        capture_output=True).returncode, 0)
        self.assertEqual(sup.wait(timeout=30), 0)
        self.assertFalse(supervise.probe(self.port))
        self.assertFalse(supervise.supervisor_running(self.data))


if __name__ == "__main__":
    unittest.main()

"""Keep the game server running: the same program on macOS, Windows, Linux and inside the Docker container.

    python ops/supervise.py run [--data-dir DIR] [--port 8000] [--host 0.0.0.0] [...] [-- extra server options]
    python ops/supervise.py status  [--data-dir DIR]     is it running, since when, how many restarts, the last backup
    python ops/supervise.py restart [--data-dir DIR]     restart the server; running games are kept (see below)
    python ops/supervise.py stop    [--data-dir DIR]     stop the server and the supervisor (until the next login)

`run` starts server.py with every data file in the data folder (and the extra options given after `--`), then watches it:

  - it exited (a crash): it is started again, after 2 s, then 4, 8, … up to a minute when it keeps failing;
  - it stopped answering (hung): /healthz is asked every 20 s, and after 3 misses in a row it is stopped and started again;
  - it cannot stay up (5 restarts within 10 minutes): the supervisor pauses for 10 minutes and says so in its log, because
    something needs a person (a broken recipe file, a port another program holds), then tries again;
  - another server already answers on the port (one started by hand): nothing is started until it goes away.

Every day it backs up the data at --backup-at (ops/backup.py, keeping the newest 14) and writes yesterday's summary at
--summary-at (ops/daily_summary.py). While it runs it keeps the computer from going to sleep (macOS and Windows), since a
sleeping computer serves nobody. Its own messages go to <data>/supervisor.log (trimmed when large) and to its console.

Restarts keep running games: the server is asked to stop politely (a stop file next to its pid file, which it looks for
every second, the same on every system; plus SIGTERM where there is one), which saves every running game to its snapshot first, and the new server picks the snapshot up when it starts (as reload.py's in-place
reload does). Players see a pause of a few seconds while their browsers reconnect.

`restart` and `stop` work by leaving a request file in the data folder, which the supervisor picks up within a second: the
same on every system, and usable by reload.py and ops/deploy.py. Only one supervisor runs per data folder (a lock file).

To have it start by itself when you log in, see ops/install_supervisor.py. In the container it is the entry point (Dockerfile).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import backup as backup_tools  # noqa: E402
from ops import daily_summary  # noqa: E402

HEALTH_EVERY = 20.0          # seconds between health checks
STARTUP_GRACE = 30.0         # a server just started gets this long before it is judged
FAILURES_NEEDED = 3          # missed health checks in a row before a running server counts as hung
CRASH_WINDOW = 600.0         # restarts counted within this many seconds…
MAX_RESTARTS = 5             # …beyond which the supervisor pauses instead of restart-looping
PAUSE = 600.0
STOP_TIMEOUT = 20.0          # a polite stop gets this long (saving the games) before the server is killed
LOG_LIMIT = 1_000_000
STATE_EVERY = 10.0
WINDOWS = os.name == "nt"

# The server's data files, all in the data folder: (option, file name). Mirrors what the Docker image and the old restart
# script passed, plus the newer stores.
DATA_OPTIONS = (
    ("--accounts-file", "accounts.json"), ("--daily-file", "daily.json"), ("--drafts-file", "drafts.json"),
    ("--invites-file", "invites.json"), ("--reports-file", "reports.json"), ("--solo-file", "solo.json"),
    ("--profile-file", "profiles.json"), ("--saves-dir", "saves"), ("--snapshot-file", "reload_snapshot.json"),
    ("--pid-file", "alchemy.pid"), ("--icons-file", "icons.json"), ("--icon-files-dir", "icon_files"),
    ("--patches-state-file", "patches_state.json"), ("--log-file", "live.log"),
)
REQUEST_FILE = "supervisor.request"
STATE_FILE = "supervisor.state.json"
LOCK_FILE = "supervisor.lock"
LOG_FILE = "supervisor.log"


# ---- small cross-platform helpers ----

def probe(port: int, timeout: float = 5.0) -> bool:
    """True if a server answers /healthz properly on this port (or, for a version without /healthz, its login page)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as reply:
            return reply.status == 200 and json.loads(reply.read()).get("ok") is True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return False
    except (urllib.error.URLError, OSError, ValueError):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=timeout) as reply:
            return reply.status == 200
    except (urllib.error.URLError, OSError):
        return False


def pid_alive(pid: int) -> bool:
    """Whether a process is running. (On Windows os.kill(pid, 0) would END the process, so it is never used there.)"""
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """One supervisor per data folder: an exclusive lock on a file, released when the process ends (even if it crashes)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            if WINDOWS:
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True


def keep_awake(log: Callable[[str], None]) -> None:
    """Stop the computer sleeping for as long as this process runs (nothing to do on Linux servers and in containers)."""
    if sys.platform == "darwin":
        try:   # caffeinate ends by itself when this process does (-w)
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log(f"could not keep the Mac awake ({exc}): set it never to sleep while plugged in")
    elif WINDOWS:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: holds until this thread ends or changes it
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001):  # type: ignore[attr-defined]
            log("could not keep the PC awake: set it never to sleep while plugged in")


def server_command(python: str, live_dir: Path, data_dir: Path, port: int, host: str, extra: list[str]) -> list[str]:
    command = [python, str(live_dir / "server.py"), "--host", host, "--port", str(port), "--log-days", "14"]
    for option, name in DATA_OPTIONS:
        command += [option, str(data_dir / name)]
    return command + extra


def read_state(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def supervisor_running(data_dir: Path) -> bool:
    """A supervisor is running for this data folder: its lock is held (checked by trying to take it)."""
    lock_path = data_dir / LOCK_FILE
    if not lock_path.exists():
        return False
    probe_lock = Lock(lock_path)
    if probe_lock.acquire():
        probe_lock.handle.close()   # type: ignore[union-attr]
        return False
    return True


def request(data_dir: Path, what: str) -> None:
    """Ask the running supervisor to restart or stop (it looks for the request every second)."""
    tmp = data_dir / (REQUEST_FILE + ".tmp")
    tmp.write_text(what, encoding="utf-8")
    os.replace(tmp, data_dir / REQUEST_FILE)


# ---- the supervisor ----

@dataclass
class Supervisor:
    command: list[str]
    cwd: Path
    data_dir: Path
    port: int
    backups_dir: Path | None = None
    summaries_dir: Path | None = None
    backup_at: tuple[int, int] | None = (4, 0)
    summary_at: tuple[int, int] | None = (4, 30)
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    healthy: Callable[[int], bool] = probe
    mono: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    echo: Callable[[str], None] = print
    child: subprocess.Popen | None = None
    started: float = 0.0
    failures: int = 0
    last_health: float = 0.0
    restarts: list[float] = field(default_factory=list)
    next_start: float = 0.0
    paused_until: float = 0.0
    total_restarts: int = 0
    last_reason: str = ""
    waiting_for_port: bool = False
    stopped: bool = False
    last_state: float = -1e9
    jobs: dict[str, str] = field(default_factory=dict)   # job -> the day it last ran ("YYYY-MM-DD")
    since: float = 0.0

    def __post_init__(self) -> None:
        self.since = self.wall()
        old = read_state(self.data_dir) or {}
        self.jobs = {k: v for k, v in (old.get("jobs") or {}).items() if isinstance(v, str)}

    # ---- logging and state ----

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.wall()))} {message}"
        self.echo(line)
        path = self.data_dir / LOG_FILE
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if path.stat().st_size > LOG_LIMIT:
                kept = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except OSError:
            pass

    def status(self) -> str:
        if self.stopped:
            return "stopped"
        if self.paused_until > self.mono():
            return "paused: the server keeps failing, see supervisor.log"
        if self.waiting_for_port:
            return f"waiting: another server is already answering on port {self.port}"
        if self.child is None or self.child.poll() is not None:
            return "starting"
        return "running" if self.failures == 0 else f"running, {self.failures} missed health check(s)"

    def save_state(self, force: bool = False) -> None:
        now = self.mono()
        if not force and now - self.last_state < STATE_EVERY:
            return
        self.last_state = now
        alive = self.child is not None and self.child.poll() is None
        state = {"supervisor_pid": os.getpid(), "server_pid": self.child.pid if alive else None, "status": self.status(),
                 "since": self.since, "server_started": self.wall() - (now - self.started) if alive else None,
                 "restarts": self.total_restarts, "last_restart_reason": self.last_reason, "jobs": self.jobs,
                 "port": self.port, "updated": self.wall()}
        try:
            tmp = self.data_dir / (STATE_FILE + ".tmp")
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            os.replace(tmp, self.data_dir / STATE_FILE)
        except OSError:
            pass

    # ---- the server process ----

    def start(self) -> None:
        if self.healthy(self.port):   # someone else's server: never start a second one on the same port
            if not self.waiting_for_port:
                self.log(f"another server is already answering on port {self.port}: waiting until it stops")
            self.waiting_for_port = True
            self.next_start = self.mono() + 30
            return
        if self.waiting_for_port:
            self.log(f"port {self.port} is free now")
        self.waiting_for_port = False
        out = open(self.data_dir / "live.out.log", "ab")
        try:
            kwargs: dict = {"cwd": str(self.cwd), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
            if WINDOWS:
                kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW: no console window pops up at login
            self.child = self.popen(self.command, **kwargs)
        except OSError as exc:
            self.log(f"could not start the server: {exc}")
            self.child = None
            self._failed("could not start")
            return
        finally:
            out.close()
        self.started = self.last_health = self.mono()
        self.failures = 0
        self.log(f"server started (process {self.child.pid}) on port {self.port}")
        self.save_state(force=True)

    def stop_server(self) -> None:
        """Ask politely (the server saves running games), then insist after STOP_TIMEOUT."""
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        stop_file = self.data_dir / "alchemy.stop"   # server.py looks for <pid file name>.stop every second
        try:
            stop_file.write_text("stop", encoding="utf-8")
            if not WINDOWS:
                child.terminate()   # SIGTERM: the same polite stop, sooner
            child.wait(timeout=STOP_TIMEOUT)
            self.log(f"server stopped (it saved the running games)")
        except subprocess.TimeoutExpired:
            self.log(f"the server did not stop within {int(STOP_TIMEOUT)} s: ending it")
            child.kill()
            child.wait(timeout=10)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                stop_file.unlink()

    def _failed(self, reason: str) -> None:
        """The server died or hung: start it again after a pause that grows, or stop trying for a while."""
        now = self.mono()
        self.restarts = [t for t in self.restarts if now - t < CRASH_WINDOW] + [now]
        self.total_restarts += 1
        self.last_reason = reason
        if len(self.restarts) > MAX_RESTARTS:
            self.log(f"GIVING UP for {int(PAUSE // 60)} minutes: the server failed {len(self.restarts)} times in "
                     f"{int(CRASH_WINDOW // 60)} minutes ({reason}). Something needs a person: see live.out.log and live.log.")
            self.paused_until = now + PAUSE
            self.restarts = []
            self.next_start = self.paused_until
        else:
            delay = min(60.0, 2.0 ** len(self.restarts))
            self.next_start = now + delay
            self.log(f"starting it again in {int(delay)} s")
        self.save_state(force=True)

    def restart(self, reason: str) -> None:
        """A restart someone asked for (a deploy, a reload): running games are kept; not counted as a failure."""
        self.log(f"restarting the server: {reason}")
        self.last_reason = reason
        self.total_restarts += 1
        self.stop_server()
        self.next_start = 0.0
        self.start()

    # ---- the loop ----

    def check_request(self) -> str | None:
        path = self.data_dir / REQUEST_FILE
        try:
            what = path.read_text(encoding="utf-8").strip()
            path.unlink()
        except OSError:
            return None
        return what

    def tick(self) -> bool:
        """One look at everything. Returns False when the supervisor should end (a stop was asked for)."""
        now = self.mono()
        what = self.check_request()
        if what == "stop":
            self.log("stop requested")
            self.stop_server()
            self.stopped = True
            self.save_state(force=True)
            return False
        if what == "restart":
            self.paused_until = 0.0
            self.restarts = []
            self.restart("restart requested")
        elif what:
            self.log(f"ignored an unknown request: {what[:40]!r}")
        self.run_jobs()
        if now < self.paused_until:
            self.save_state()
            return True
        if self.child is None:
            if now >= self.next_start:
                self.start()
            self.save_state()
            return True
        code = self.child.poll()
        if code is not None:
            self.child = None
            self.log(f"the server stopped by itself (exit code {code})")
            self._failed(f"exited with code {code}")
            return True
        if now - self.started >= STARTUP_GRACE and now - self.last_health >= HEALTH_EVERY:
            self.last_health = now
            if self.healthy(self.port):
                if self.failures:
                    self.log(f"answering again after {self.failures} missed health check(s)")
                self.failures = 0
            else:
                self.failures += 1
                self.log(f"no healthy answer ({self.failures}/{FAILURES_NEEDED})")
                if self.failures >= FAILURES_NEEDED:
                    self.log("the server is running but not answering: restarting it")
                    self.stop_server()
                    self._failed("stopped answering")
        self.save_state()
        return True

    def run_jobs(self) -> None:
        local = time.localtime(self.wall())
        today = time.strftime("%Y-%m-%d", local)
        for job, at in (("backup", self.backup_at), ("summary", self.summary_at)):
            if at is None or (local.tm_hour, local.tm_min) < at or self.jobs.get(job) == today:
                continue
            self.jobs[job] = today
            try:
                if job == "backup" and self.backups_dir is not None:
                    made = backup_tools.make_backup(self.data_dir, self.backups_dir)
                    backup_tools.prune(self.backups_dir)
                    self.log(f"daily backup made: {made.name}")
                elif job == "summary" and self.summaries_dir is not None:
                    day = time.strftime("%Y-%m-%d", time.localtime(self.wall() - 86400))
                    logs = sorted(self.data_dir.glob("live.log*"))
                    self.summaries_dir.mkdir(parents=True, exist_ok=True)
                    (self.summaries_dir / f"{day}.txt").write_text(
                        daily_summary.render(day, daily_summary.summarise(logs, day), bool(logs)), encoding="utf-8")
                    self.log(f"summary of {day} written")
            except Exception as exc:   # a failed job is reported and tried again tomorrow; it never stops the supervisor
                self.log(f"the daily {job} FAILED: {exc}")
            self.save_state(force=True)


def run(args: argparse.Namespace, extra: list[str]) -> int:
    live, data = Path(args.live_dir).resolve(), Path(args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    lock = Lock(data / LOCK_FILE)
    if not lock.acquire():
        print(f"A supervisor is already running for {data}.")
        return 0   # not a failure: nothing for a login item or a container to retry
    stopping = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, name), lambda *_: stopping.set())
    supervisor = Supervisor(
        command=server_command(args.python or sys.executable, live, data, args.port, args.host, extra),
        cwd=live, data_dir=data, port=args.port,
        backups_dir=Path(args.backups_dir).resolve() if args.backups_dir else live / "backups",
        summaries_dir=Path(args.summaries_dir).resolve() if args.summaries_dir else live / "summaries",
        backup_at=parse_time(args.backup_at), summary_at=parse_time(args.summary_at))
    supervisor.log(f"supervisor started (process {os.getpid()}) for {live}, data in {data}")
    if not args.no_keep_awake:
        keep_awake(supervisor.log)
    with contextlib.suppress(OSError):
        (data / REQUEST_FILE).unlink()   # a request left over from before means nothing now
    try:
        while not stopping.is_set():
            if not supervisor.tick():
                return 0
            stopping.wait(1.0)
    finally:
        if not supervisor.stopped:
            supervisor.log("supervisor stopping: stopping the server")
            supervisor.stop_server()
            supervisor.stopped = True
            supervisor.save_state(force=True)
    return 0


def parse_time(text: str) -> tuple[int, int] | None:
    if text in ("", "off"):
        return None
    hours, _, minutes = text.partition(":")
    return int(hours), int(minutes or 0)


def show_status(data: Path) -> int:
    state = read_state(data)
    if not supervisor_running(data) or state is None:
        print(f"No supervisor is running for {data}." + (f" Last known: {state.get('status')}." if state else ""))
        return 1
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"   # noqa: E731
    print(f"Supervisor:  running since {stamp(state.get('since'))} (process {state.get('supervisor_pid')})")
    print(f"Server:      {state.get('status')} on port {state.get('port')}"
          + (f", process {state['server_pid']}, up since {stamp(state.get('server_started'))}" if state.get("server_pid") else ""))
    print(f"Restarts:    {state.get('restarts', 0)}" + (f" (last: {state['last_restart_reason']})" if state.get("last_restart_reason") else ""))
    jobs = state.get("jobs") or {}
    print(f"Daily jobs:  backup {jobs.get('backup', 'not yet')}, summary {jobs.get('summary', 'not yet')}")
    print(f"Log:         {data / LOG_FILE}")
    return 0


def ask(data: Path, what: str, wait: float = 40.0) -> int:
    if not supervisor_running(data):
        print(f"No supervisor is running for {data}.", file=sys.stderr)
        return 1
    request(data, what)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        if not (data / REQUEST_FILE).exists():
            break
    else:
        print("The supervisor did not pick the request up.", file=sys.stderr)
        return 1
    if what == "stop":
        print("Stopped. It starts again at the next login (or: python ops/install_supervisor.py start).")
    else:
        print("Restart requested: running games are kept; players reconnect by themselves.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(description="Keep the Co-op Alchemy server running")
    parser.add_argument("command", choices=("run", "status", "restart", "stop"))
    parser.add_argument("--live-dir", default=str(ROOT), help="the code folder (server.py); default: this checkout")
    parser.add_argument("--data-dir", help="where the data files live; default: <live-dir>/data")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--python", help="the Python to run the server with; default: this one")
    parser.add_argument("--backups-dir", help="default: <live-dir>/backups")
    parser.add_argument("--summaries-dir", help="default: <live-dir>/summaries")
    parser.add_argument("--backup-at", default="04:00", help="daily backup time, HH:MM, or off")
    parser.add_argument("--summary-at", default="04:30", help="daily summary time, HH:MM, or off")
    parser.add_argument("--no-keep-awake", action="store_true", help="let the computer sleep (servers and containers)")
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir or str(Path(args.live_dir) / "data")
    data = Path(args.data_dir).resolve()
    if args.command == "run":
        return run(args, extra)
    if args.command == "status":
        return show_status(data)
    return ask(data, args.command)


if __name__ == "__main__":
    sys.exit(main())

"""Restart the live server if it stops answering. Meant to be run once a minute by a scheduled task (ops/install_tasks.ps1).

    python ops/watchdog.py [--port 8000] [--live-dir DIR]

Each run asks the server whether it is healthy. After FAILURES_NEEDED misses in a row (so a short hiccup is ignored) it
restarts the server with restart-live.ps1. To avoid a restart loop it gives up, and only logs, after MAX_RESTARTS restarts
within RESTART_WINDOW seconds: a server that cannot stay up needs a person. Everything but "all fine" is appended to
<live folder>\\data\\watchdog.log, which is trimmed when it grows large.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import backup as backup_tools  # noqa: E402
from ops.deploy import restart_with_script  # noqa: E402

FAILURES_NEEDED = 3
MAX_RESTARTS = 3
RESTART_WINDOW = 30 * 60.0
LOG_LIMIT = 200_000


def probe(port: int, timeout: float = 5.0) -> bool:
    """True if the server answers /healthz properly (or, on a version that predates /healthz, answers its login page)."""
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


def check_once(state_path: Path, healthy: Callable[[], bool], restart: Callable[[], None],
               clock: Callable[[], float] = time.time, log: Callable[[str], None] = print) -> str:
    """One watchdog run. Returns "ok", "failing", "restarted", "gave up" or "restart failed"."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        failures, restarts = int(state.get("failures", 0)), [float(t) for t in state.get("restarts", [])]
    except (OSError, ValueError, TypeError):
        failures, restarts = 0, []
    now = clock()
    restarts = [t for t in restarts if now - t < RESTART_WINDOW]

    def save() -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"failures": failures, "restarts": restarts}), encoding="utf-8")

    if healthy():
        if failures:
            log(f"recovered after {failures} missed check(s)")
        failures = 0
        save()
        return "ok"
    failures += 1
    if failures < FAILURES_NEEDED:
        log(f"no healthy answer ({failures}/{FAILURES_NEEDED})")
        save()
        return "failing"
    if len(restarts) >= MAX_RESTARTS:
        log(f"GIVING UP: restarted {len(restarts)} times in {int(RESTART_WINDOW // 60)} minutes and it is still down")
        save()
        return "gave up"
    log(f"no healthy answer {failures} times in a row: restarting the server")
    restarts.append(now)
    failures = 0
    try:
        restart()
    except Exception as exc:
        log(f"restart failed: {exc}")
        save()
        return "restart failed"
    save()
    return "restarted"


def trim_log(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_LIMIT:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart the live server if it stops answering")
    parser.add_argument("--live-dir", default=str(backup_tools.default_live_dir()))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    live = Path(args.live_dir)
    log_path = live / "data" / "watchdog.log"

    def log(message: str) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    outcome = check_once(live / "data" / "watchdog.json", lambda: probe(args.port),
                         lambda: restart_with_script(live / "restart-live.ps1", args.port), log=log)
    trim_log(log_path)
    return 0 if outcome in ("ok", "failing", "restarted") else 1


if __name__ == "__main__":
    sys.exit(main())

"""Measure what shared cursors cost the server: a busy room of players all moving their pointers.

Usage:
    python3 bench_cursor.py [--seconds 10] [--players 5] [--runs 3] [--json]

Starts a real server process (on a temporary data folder, so nothing of yours is touched), logs in `players` accounts,
puts them in one game, and has each one send cursor updates the way the browser does (one POST, then a 40 ms pause,
repeated) while holding an event stream open to receive the others' cursors. Reports, per run:

  connections    TCP connections the clients had to open (a browser reuses one when the server allows keep-alive)
  rate           cursor updates per second per player that actually got through
  latency        round trip of a cursor POST (median / 95th percentile / worst), in milliseconds
  delivered      cursor events that arrived on the event streams, as a share of those that should have
  server cpu     CPU seconds the server process used while the load ran
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
GAP = 0.04  # the page waits this long between cursor updates (MIN_GAP_MS in static/app.js)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def cpu_seconds(pid: int) -> float:
    """CPU time (user + kernel) used so far by a process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        times = [wintypes.FILETIME() for _ in range(4)]
        kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
        kernel.CloseHandle(handle)
        return sum(((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7 for t in times[2:])
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


class Browser:
    """One browser tab's connection to the server: reuses its connection whenever the server permits."""

    def __init__(self, port: int) -> None:
        self.connects = 0
        outer = self

        class Conn(http.client.HTTPConnection):
            def connect(self) -> None:
                outer.connects += 1
                super().connect()

        self.conn = Conn("127.0.0.1", port, timeout=10)
        self.session = ""

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.session:
            headers["X-Session"] = self.session
        self.conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = self.conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)


def wait_up(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the server did not start")


def run_once(seconds: float, players: int) -> dict[str, Any]:
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "server.py"), "--host", "127.0.0.1", "--port", str(port),
         "--recipes", str(BASE_DIR / "recipes.json"),
         "--accounts-file", str(folder / "a.json"), "--saves-dir", str(folder / "saves"),
         "--drafts-file", str(folder / "d.json"), "--daily-file", str(folder / "daily.json"),
         "--snapshot-file", str(folder / "s.json"), "--pid-file", str(folder / "p.pid"),
         "--invites-file", str(folder / "i.json")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_up(port)
        tabs = [Browser(port) for _ in range(players)]
        for i, tab in enumerate(tabs):
            status, data = tab.call("POST", "/register", {"username": f"bench{i}", "password": "bench-password-1"})
            assert status == 200, data
            tab.session = data["session"]
        status, game = tabs[0].call("POST", "/games", {"name": "benchroom"})
        assert status == 200, game
        tokens = []
        for tab in tabs:
            status, joined = tab.call("POST", f"/g/{game['id']}/join", {})
            assert status == 200, joined
            tokens.append(joined["token"])

        received = [0] * players
        streams_ready = threading.Barrier(players + 1)

        def listen(i: int) -> None:
            # the page's event stream is its own connection, outside the request connection above
            stream = Browser(port)
            stream.session = tabs[i].session
            status, data = stream.call("POST", f"/g/{game['id']}/ticket", {"player": tokens[i]})
            assert status == 200, data
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds + 15)
            conn.request("GET", f"/g/{game['id']}/events?ticket={data['ticket']}")
            resp = conn.getresponse()
            streams_ready.wait()
            try:
                for raw in resp:
                    if raw.startswith(b"event: cursor"):
                        received[i] += 1
            except (OSError, http.client.HTTPException):
                pass  # the server was stopped at the end of the run
            finally:
                conn.close()

        listeners = [threading.Thread(target=listen, args=(i,), daemon=True) for i in range(players)]
        for thread in listeners:
            thread.start()
        streams_ready.wait()

        latencies: list[list[float]] = [[] for _ in range(players)]
        sent = [0] * players
        connects_before = [tab.connects for tab in tabs]
        stop_at = time.perf_counter() + seconds

        def pointer(i: int) -> None:
            n = 0
            while time.perf_counter() < stop_at:
                n += 1
                started = time.perf_counter()
                status, _ = tabs[i].call("POST", f"/g/{game['id']}/cursor",
                                         {"player": tokens[i], "x": (n % 100) / 100, "y": 0.5})
                if status == 200:
                    latencies[i].append((time.perf_counter() - started) * 1000)
                    sent[i] += 1
                time.sleep(GAP)

        cpu_start = cpu_seconds(server.pid)
        wall_start = time.perf_counter()
        movers = [threading.Thread(target=pointer, args=(i,)) for i in range(players)]
        for thread in movers:
            thread.start()
        for thread in movers:
            thread.join()
        wall = time.perf_counter() - wall_start
        cpu = cpu_seconds(server.pid) - cpu_start
        time.sleep(0.5)  # let the last events arrive

        flat = sorted(ms for per in latencies for ms in per)
        expected = sum(sent) * (players - 1)
        return {
            "connections": sum(tab.connects - before for tab, before in zip(tabs, connects_before)),
            "updates_sent": sum(sent),
            "rate_per_player": sum(sent) / players / wall,
            "latency_p50_ms": statistics.median(flat),
            "latency_p95_ms": flat[int(len(flat) * 0.95) - 1],
            "latency_max_ms": flat[-1],
            "delivered_pct": 100.0 * sum(received) / expected if expected else 0.0,
            "server_cpu_s": cpu,
            "server_cpu_pct_of_one_core": 100.0 * cpu / wall,
        }
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        try:
            tmp.cleanup()
        except OSError:
            pass  # Windows may still hold a file for a moment; the temp folder is harmless


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark shared-cursor traffic")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--players", type=int, default=5)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = [run_once(args.seconds, args.players) for _ in range(args.runs)]
    if args.json:
        print(json.dumps(results))
        return 0
    print(f"{args.players} players, {args.seconds:g} s per run, {args.runs} run(s); medians:")
    for key, label in (("connections", "connections opened"), ("updates_sent", "cursor updates sent"),
                       ("rate_per_player", "updates/s per player"), ("latency_p50_ms", "latency p50 (ms)"),
                       ("latency_p95_ms", "latency p95 (ms)"), ("latency_max_ms", "latency worst (ms)"),
                       ("delivered_pct", "events delivered (%)"), ("server_cpu_s", "server cpu (s)"),
                       ("server_cpu_pct_of_one_core", "server cpu (% of one core)")):
        print(f"  {label:<28}{statistics.median(r[key] for r in results):>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

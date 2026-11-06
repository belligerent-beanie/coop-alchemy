"""Measure what shared cursors cost the server: a busy room of players all moving their pointers.

Usage:
    python3 bench_cursor.py [--seconds 10] [--players 5] [--runs 3] [--client old|new] [--server-dir DIR] [--batch SEC] [--json]

Starts a real server process (on a temporary data folder, so nothing of yours is touched), logs in `players` accounts,
puts them in one game, and has each one send cursor updates while holding an event stream open to receive the others'.
Every update carries a sequence number, so the receivers can measure the true delay from "player moved" to "other
player's screen got it", and check that each player's final position always arrives.

  --client old   the original page: send, wait for the reply, pause 40 ms, repeat
  --client new   the current page: a fixed 20 Hz timer; a tick is skipped while the previous send is still in flight,
                 and nothing is sent at all while the player is alone in the room
  --server-dir   run the server code from another checkout (to compare against an older commit)
  --batch SEC    run the server with this cursor batching window (0 = send at once), to tune CURSOR_BATCH_SECONDS

Reports, per run (the median of the runs is printed):
  connections        new TCP connections the senders opened during the run (a kept-alive one is reused)
  updates sent       cursor POSTs, and per player per second
  post latency       round trip of a cursor POST (median / 95th percentile)
  end-to-end delay   from the POST being made to the event arriving on another player's stream (median / 95th / worst)
  events delivered   cursor events received as a share of those that could have been (intermediate positions may be
                     legitimately skipped when the server batches; the *final* position must never be)
  final position     how many receivers ended up with each sender's last position
  bytes per second   what each player's event stream received
  server cpu         CPU seconds the server process used while the load ran, and per cursor update
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import random
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
OLD_GAP = 0.04   # the original page waited this long after each reply (MIN_GAP_MS)
NEW_TICK = 0.05  # the current page sends at most 20 times a second
SEQ_SCALE = 100_000  # y = sequence / SEQ_SCALE, so the receiver can recover which update it is looking at


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


def percentile(sorted_values: list[float], fraction: float) -> float:
    return sorted_values[max(0, min(len(sorted_values) - 1, int(len(sorted_values) * fraction) - 1))] if sorted_values else 0.0


def server_command(server_dir: Path, port: int, folder: Path, batch: float | None) -> list[str]:
    arguments = ["--host", "127.0.0.1", "--port", str(port), "--recipes", str(server_dir / "recipes.json"),
                 "--accounts-file", str(folder / "a.json"), "--saves-dir", str(folder / "saves"),
                 "--drafts-file", str(folder / "d.json"), "--daily-file", str(folder / "daily.json"),
                 "--snapshot-file", str(folder / "s.json"), "--pid-file", str(folder / "p.pid"),
                 "--invites-file", str(folder / "i.json")]
    if batch is None:
        return [sys.executable, str(server_dir / "server.py"), *arguments]
    # Same server, but with the cursor batching window set from outside (the constant is not a command-line option).
    code = ("import sys; sys.argv = ['server.py'] + sys.argv[1:]; import server; "
            f"server.CURSOR_BATCH_SECONDS = {batch!r}; sys.exit(server.main())")
    return [sys.executable, "-c", code, *arguments]


def run_once(seconds: float, players: int, client: str, server_dir: Path, batch: float | None = None) -> dict[str, Any]:
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    port = free_port()
    server = subprocess.Popen(server_command(server_dir, port, folder, batch),
                              cwd=str(server_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_up(port)
        tabs = [Browser(port) for _ in range(players)]
        for i, tab in enumerate(tabs):
            status, data = tab.call("POST", "/register", {"username": f"bench{i}", "password": "bench-password-1"})
            assert status == 200, data
            tab.session = data["session"]
        status, game = tabs[0].call("POST", "/games", {"name": "benchroom"})
        assert status == 200, game
        tokens, pids = [], []
        for tab in tabs:
            status, joined = tab.call("POST", f"/g/{game['id']}/join", {})
            assert status == 200, joined
            tokens.append(joined["token"])
            pids.append(joined["pid"])
        index_of = {pid: i for i, pid in enumerate(pids)}

        sent_at: list[dict[int, float]] = [{} for _ in range(players)]  # sender -> sequence -> time it was sent
        delays: list[float] = []
        last_seen: list[dict[int, int]] = [{} for _ in range(players)]  # receiver -> sender -> last sequence received
        received = [0] * players
        received_bytes = [0] * players
        streams_ready = threading.Barrier(players + 1)

        def listen(i: int) -> None:
            stream = Browser(port)  # the page's event stream is its own connection
            stream.session = tabs[i].session
            status, data = stream.call("POST", f"/g/{game['id']}/ticket", {"player": tokens[i]})
            assert status == 200, data
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds + 15)
            conn.request("GET", f"/g/{game['id']}/events?ticket={data['ticket']}")
            resp = conn.getresponse()
            streams_ready.wait()
            try:
                is_cursor = False
                for raw in resp:
                    received_bytes[i] += len(raw)
                    if raw.startswith(b"event: "):
                        is_cursor = raw.startswith(b"event: cursor")
                    elif is_cursor and raw.startswith(b"data: "):
                        arrived = time.perf_counter()
                        event = json.loads(raw[6:])
                        received[i] += 1
                        if event.get("y") is not None and event["pid"] in index_of:
                            sender, seq = index_of[event["pid"]], round(event["y"] * SEQ_SCALE)
                            last_seen[i][sender] = seq
                            started = sent_at[sender].get(seq)
                            if started is not None:
                                delays.append((arrived - started) * 1000)
            except (OSError, http.client.HTTPException, ValueError):
                pass  # the server was stopped at the end of the run
            finally:
                conn.close()

        listeners = [threading.Thread(target=listen, args=(i,), daemon=True) for i in range(players)]
        for thread in listeners:
            thread.start()
        streams_ready.wait()

        post_ms: list[float] = []
        sent = [0] * players
        last_sent = [0] * players
        connects_before = [tab.connects for tab in tabs]
        stop_at = time.perf_counter() + seconds

        def pointer(i: int) -> None:
            n = 0
            if client == "new":
                time.sleep(random.random() * NEW_TICK)  # real pages' timers are not in step with each other
            next_tick = time.perf_counter()
            while time.perf_counter() < stop_at:
                if client == "new" and players == 1:
                    time.sleep(NEW_TICK)  # alone in the room: the current page sends nothing
                    continue
                n += 1
                sent_at[i][n] = started = time.perf_counter()
                status, _ = tabs[i].call("POST", f"/g/{game['id']}/cursor",
                                         {"player": tokens[i], "x": (n % 100) / 100, "y": n / SEQ_SCALE})
                if status == 200:
                    post_ms.append((time.perf_counter() - started) * 1000)
                    sent[i] += 1
                    last_sent[i] = n
                if client == "old":
                    time.sleep(OLD_GAP)
                else:  # a fixed timer: if this send took longer than a tick, skip the ticks that already passed
                    next_tick += NEW_TICK
                    now = time.perf_counter()
                    if next_tick < now:
                        next_tick = now
                    time.sleep(max(0.0, next_tick - now))

        cpu_start = cpu_seconds(server.pid)
        wall_start = time.perf_counter()
        movers = [threading.Thread(target=pointer, args=(i,)) for i in range(players)]
        for thread in movers:
            thread.start()
        for thread in movers:
            thread.join()
        wall = time.perf_counter() - wall_start
        cpu = cpu_seconds(server.pid) - cpu_start
        time.sleep(0.6)  # let the last events arrive

        receivers_pairs = [(r, s) for r in range(players) for s in range(players) if r != s and sent[s]]
        final_ok = sum(1 for r, s in receivers_pairs if last_seen[r].get(s) == last_sent[s])
        expected = sum(sent) * (players - 1)
        delays.sort()
        post_ms.sort()
        total_sent = sum(sent)
        return {
            "connections": sum(tab.connects - before for tab, before in zip(tabs, connects_before)),
            "updates_sent": total_sent,
            "rate_per_player": total_sent / players / wall,
            "post_p50_ms": statistics.median(post_ms) if post_ms else 0.0,
            "post_p95_ms": percentile(post_ms, 0.95),
            "e2e_p50_ms": statistics.median(delays) if delays else 0.0,
            "e2e_p95_ms": percentile(delays, 0.95),
            "e2e_max_ms": delays[-1] if delays else 0.0,
            "delivered_pct": 100.0 * sum(received) / expected if expected else 0.0,
            "final_position_ok": final_ok,
            "final_position_of": len(receivers_pairs),
            "bytes_per_s_per_player": sum(received_bytes) / players / wall,
            "server_cpu_s": cpu,
            "server_cpu_per_update_ms": 1000.0 * cpu / total_sent if total_sent else 0.0,
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


ROWS = (("connections", "new connections", 0), ("updates_sent", "cursor updates sent", 0),
        ("rate_per_player", "updates/s per player", 1), ("post_p50_ms", "POST latency p50 (ms)", 2),
        ("post_p95_ms", "POST latency p95 (ms)", 2), ("e2e_p50_ms", "end-to-end delay p50 (ms)", 1),
        ("e2e_p95_ms", "end-to-end delay p95 (ms)", 1), ("e2e_max_ms", "end-to-end delay worst (ms)", 1),
        ("delivered_pct", "events delivered (%)", 1), ("bytes_per_s_per_player", "stream bytes/s per player", 0),
        ("server_cpu_s", "server cpu (s)", 2), ("server_cpu_per_update_ms", "server cpu per update (ms)", 2),
        ("server_cpu_pct_of_one_core", "server cpu (% of one core)", 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark shared-cursor traffic")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--players", type=int, default=5)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--client", choices=("old", "new"), default="new")
    parser.add_argument("--server-dir", default=str(BASE_DIR))
    parser.add_argument("--batch", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = [run_once(args.seconds, args.players, args.client, Path(args.server_dir).resolve(), args.batch)
               for _ in range(args.runs)]
    if args.json:
        print(json.dumps(results))
        return 0
    print(f"{args.players} players, {args.seconds:g} s per run, {args.runs} run(s), {args.client} client; medians:")
    for key, label, digits in ROWS:
        print(f"  {label:<30}{statistics.median(r[key] for r in results):>10.{digits}f}")
    ok = min(r["final_position_ok"] for r in results)
    print(f"  {'final positions delivered':<30}{ok:>6} of {results[0]['final_position_of']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

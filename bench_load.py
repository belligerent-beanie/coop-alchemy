"""How many players can one server carry? Runs rooms of players and single-player daily challenges against a real server.

Usage:
    python3 bench_load.py [--scenarios 1x5,3x5,10x5,20x5,solo50] [--seconds 12] [--json]

A scenario is ROOMSxPLAYERS (for example 10x5: ten rooms of five players) or soloN (N players, each in their own daily-challenge
world). Every player logs in, joins, holds an event stream, and then behaves like a person: in a room the cursor moves for 3 s and
rests for 2 s (20 updates a second while moving) and a tile is placed every 2 s; alone, only the tile. The server runs as its own
process on throwaway data, with the rate limits lifted so thousands of logins are possible.

Reported per scenario: server CPU (percent of one core), memory and threads at the end, the delay from a cursor being sent to
another player receiving it, request latency, errors, and how many cursor updates were skipped because THIS PROGRAM could not
keep up (if that is high, the numbers describe the load generator, not the server).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from accounts import AccountStore
from bench_cursor import SEQ_SCALE, Browser, cpu_seconds, free_port, percentile, wait_up

BASE_DIR = Path(__file__).resolve().parent
TICK = 0.05          # cursor sends at most 20 times a second
MOVE_SECONDS, REST_SECONDS = 3.0, 2.0
TILE_EVERY = 2.0
CLEAR_EVERY = 25     # tile placements between clearing the workspace


def process_stats(pid: int) -> tuple[float, int]:
    """(working-set memory in MB, number of threads) of a process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                        ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)  # type: ignore[attr-defined]
        kernel.CloseHandle(handle)
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        threads = 0
        more = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            threads += entry.th32OwnerProcessID == pid
            more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        kernel.CloseHandle(snapshot)
        return counters.WorkingSetSize / 1e6, threads
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
    return int(status["VmRSS"].split()[0]) / 1024, int(status["Threads"])


def parse_scenario(text: str) -> tuple[int, int, int]:
    """(rooms, players per room, solo players)."""
    if text.startswith("solo"):
        return 0, 0, int(text[4:])
    rooms, players = text.split("x")
    return int(rooms), int(players), 0


def run_scenario(spec: str, seconds: float, server_dir: Path = BASE_DIR) -> dict[str, Any]:
    rooms, per_room, solo = parse_scenario(spec)
    total = rooms * per_room + solo
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    accounts = AccountStore(folder / "a.json", iterations=1000)
    for i in range(total):
        accounts.register(f"load{i}", f"load-password-{i}")
    port = free_port()
    code = ("import sys; sys.argv = ['server.py'] + sys.argv[1:]; import server, throttle; original = throttle.Throttle; "
            "server.Throttle = lambda limit, window, **kw: original(10**9, window, **kw); sys.exit(server.main())")
    server = subprocess.Popen(
        [sys.executable, "-c", code, "--host", "127.0.0.1", "--port", str(port), "--recipes", str(server_dir / "recipes.json"),
         "--accounts-file", str(folder / "a.json"), "--saves-dir", str(folder / "saves"), "--drafts-file", str(folder / "d.json"),
         "--daily-file", str(folder / "daily.json"), "--snapshot-file", str(folder / "s.json"), "--pid-file", str(folder / "p.pid"),
         "--reports-file", str(folder / "r.json"), "--max-games", str(max(rooms, 1))],
        cwd=str(server_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_up(port)
        tabs = [Browser(port) for _ in range(total)]
        for i, tab in enumerate(tabs):
            status, data = tab.call("POST", "/login", {"username": f"load{i}", "password": f"load-password-{i}"})
            assert status == 200, data
            tab.session = data["session"]
        slugs: list[str] = []
        tokens: list[str] = []
        pids: list[str] = []
        rooms_of: list[int] = []
        for r in range(rooms):
            status, game = tabs[r * per_room].call("POST", "/games", {"name": f"load room {r}"})
            assert status == 200, game
            for p in range(per_room):
                tab = tabs[r * per_room + p]
                status, joined = tab.call("POST", f"/g/{game['id']}/join", {})
                assert status == 200, joined
                slugs.append(game["id"]); tokens.append(joined["token"]); pids.append(joined["pid"]); rooms_of.append(r)
        for s in range(solo):
            tab = tabs[rooms * per_room + s]
            status, game = tab.call("POST", "/daily/start", {"tier": "easy"})
            assert status == 200, game
            status, joined = tab.call("POST", f"/g/{game['id']}/join", {})
            assert status == 200, joined
            slugs.append(game["id"]); tokens.append(joined["token"]); pids.append(joined["pid"]); rooms_of.append(-1 - s)

        index_of = {pid: i for i, pid in enumerate(pids)}
        sent_at: list[dict[int, float]] = [{} for _ in range(total)]
        delays: list[float] = []
        events = [0] * total
        errors: list[str] = []
        ready = threading.Barrier(total + 1)

        def listen(i: int) -> None:
            stream = Browser(port)
            stream.session = tabs[i].session
            status, data = stream.call("POST", f"/g/{slugs[i]}/ticket", {"player": tokens[i]})
            if status != 200:
                errors.append(f"ticket {status}")
                ready.wait()
                return
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds + 30)
            conn.request("GET", f"/g/{slugs[i]}/events?ticket={data['ticket']}")
            resp = conn.getresponse()
            ready.wait()
            try:
                is_cursor = False
                for raw in resp:
                    if raw.startswith(b"event: "):
                        events[i] += 1
                        is_cursor = raw.startswith(b"event: cursor")
                    elif is_cursor and raw.startswith(b"data: "):
                        arrived = time.perf_counter()
                        event = json.loads(raw[6:])
                        if event.get("y") is not None and event["pid"] in index_of:
                            sender = index_of[event["pid"]]
                            started = sent_at[sender].get(round(event["y"] * SEQ_SCALE))
                            if started is not None:
                                delays.append((arrived - started) * 1000)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            finally:
                conn.close()

        for i in range(total):
            threading.Thread(target=listen, args=(i,), daemon=True).start()
        ready.wait()

        cursor_ms: list[float] = []
        tile_ms: list[float] = []
        cursor_sends = [0]
        skipped = [0]
        stop_at = time.perf_counter() + seconds

        def post(i: int, path: str, body: dict[str, Any], sink: list[float]) -> None:
            started = time.perf_counter()
            try:
                status, _ = tabs[i].call("POST", f"/g/{slugs[i]}{path}", {"player": tokens[i], **body})
            except (OSError, http.client.HTTPException) as exc:
                errors.append(type(exc).__name__)
                return
            if status == 200:
                sink.append((time.perf_counter() - started) * 1000)
            else:
                errors.append(f"{path} {status}")

        def person(i: int) -> None:
            rng = random.Random(i)
            in_room = rooms_of[i] >= 0
            cycle_start = time.perf_counter() + rng.random() * (MOVE_SECONDS + REST_SECONDS)
            next_tile = time.perf_counter() + rng.random() * TILE_EVERY
            next_tick = time.perf_counter()
            seq = tiles = 0
            time.sleep(rng.random() * TICK)
            while time.perf_counter() < stop_at:
                now = time.perf_counter()
                if in_room and ((now - cycle_start) % (MOVE_SECONDS + REST_SECONDS)) < MOVE_SECONDS:
                    seq += 1
                    sent_at[i][seq] = time.perf_counter()
                    post(i, "/cursor", {"x": rng.random(), "y": seq / SEQ_SCALE}, cursor_ms)
                    cursor_sends[0] += 1
                if time.perf_counter() >= next_tile:
                    tiles += 1
                    post(i, "/tile/add", {"element": "water", "x": rng.random(), "y": rng.random()}, tile_ms)
                    if tiles % CLEAR_EVERY == 0:
                        post(i, "/tiles/clear", {}, [])
                    next_tile += TILE_EVERY
                next_tick += TICK
                lag = time.perf_counter() - next_tick
                if lag > 0:
                    skipped[0] += int(lag / TICK) + 1  # this program fell behind its own 20 Hz timer
                    next_tick = time.perf_counter()
                time.sleep(max(0.0, next_tick - time.perf_counter()))

        cpu_start, wall_start = cpu_seconds(server.pid), time.perf_counter()
        people = [threading.Thread(target=person, args=(i,)) for i in range(total)]
        for thread in people:
            thread.start()
        for thread in people:
            thread.join()
        wall = time.perf_counter() - wall_start
        cpu = cpu_seconds(server.pid) - cpu_start
        memory, threads = process_stats(server.pid)
        time.sleep(0.5)
        delays.sort(); cursor_ms.sort(); tile_ms.sort()
        return {
            "scenario": spec, "players": total, "seconds": round(wall, 1),
            "server_cpu_pct": 100.0 * cpu / wall, "server_memory_mb": memory, "server_threads": threads,
            "cursor_updates_per_s": cursor_sends[0] / wall,
            "e2e_p50_ms": statistics.median(delays) if delays else 0.0, "e2e_p95_ms": percentile(delays, 0.95),
            "cursor_post_p95_ms": percentile(cursor_ms, 0.95), "tile_post_p50_ms": statistics.median(tile_ms) if tile_ms else 0.0,
            "tile_post_p95_ms": percentile(tile_ms, 0.95), "errors": len(errors), "error_kinds": sorted(set(errors))[:5],
            "generator_skipped_ticks_pct": 100.0 * skipped[0] / max(1, total * wall / TICK),
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
            pass


ROWS = (("players", "players", 0), ("server_cpu_pct", "server cpu (% of one core)", 1), ("server_memory_mb", "server memory (MB)", 0),
        ("server_threads", "server threads", 0), ("cursor_updates_per_s", "cursor updates/s (all)", 0),
        ("e2e_p50_ms", "cursor delay p50 (ms)", 1), ("e2e_p95_ms", "cursor delay p95 (ms)", 1),
        ("tile_post_p50_ms", "tile request p50 (ms)", 1), ("tile_post_p95_ms", "tile request p95 (ms)", 1),
        ("errors", "errors", 0), ("generator_skipped_ticks_pct", "load generator behind (%)", 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Load-test a server with rooms of players")
    parser.add_argument("--scenarios", default="1x5,3x5,10x5,20x5,solo50")
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = [run_scenario(spec, args.seconds) for spec in args.scenarios.split(",")]
    if args.json:
        print(json.dumps(results))
        return 0
    print(f"{'':<30}" + "".join(f"{r['scenario']:>10}" for r in results))
    for key, label, digits in ROWS:
        print(f"{label:<30}" + "".join(f"{r[key]:>10.{digits}f}" for r in results))
    for r in results:
        if r["errors"]:
            print(f"errors in {r['scenario']}: {r['error_kinds']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

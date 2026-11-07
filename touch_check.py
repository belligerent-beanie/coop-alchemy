"""Check the game on an emulated phone, tablet and desktop, in a real Chrome, with real touch input.

Usage:
    python touch_check.py [--out DIR] [--chrome PATH]

Starts the real server on a throwaway data folder, launches Chrome (headless, with a throwaway profile) and drives it over the
DevTools Protocol: it sets the screen size and device type, switches on touch emulation (so the page sees a touch-screen
pointer, and fingers go through Chrome's own gesture handling: a swipe scrolls, a tap taps), and then plays: taps an element
to add it, swipes the element list, drags one tile onto another, turns the phone sideways, and loads the other pages. Screenshots
go to --out (default: touch-check-output). The exit status is 0 only if every check passes.

What this does and does not prove: it is Chromium's engine, so it covers touch handling, layout at each size and the
`pointer: coarse` rules. It is not iOS Safari (which has its own quirks: toolbar height, input zoom, notch spacing), so a quick
look on a real iPhone and a real Android phone is still worth doing before announcing phone play.

Nothing here needs more than the Python standard library and an installed Chrome (or Edge).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from accounts import AccountStore  # noqa: E402
from bench_cursor import free_port, server_command, wait_up  # noqa: E402
from recipes import load_recipes  # noqa: E402

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Mobile/15E148 Safari/604.1")
CHROME_PATHS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def find_chrome(explicit: str | None = None) -> str | None:
    for candidate in ([explicit] if explicit else []) + list(CHROME_PATHS) + [shutil.which("chrome") or "", shutil.which("chromium") or ""]:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


# ---- a minimal WebSocket client (RFC 6455), just enough for the DevTools Protocol -----------------------------------------

class WebSocket:
    def __init__(self, host: str, port: int, path: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the browser closed the connection during the WebSocket handshake")
            head += chunk
        head, _, self.buffer = head.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake refused: {head[:80]!r}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.encode() not in head:
            raise ConnectionError("WebSocket handshake answer did not match")

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the browser closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x8:
                raise ConnectionError("the browser closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)  # answer a ping
                continue
            if opcode in (0x1, 0x0, 0x2):
                message += payload
                if first & 0x80:
                    return message.decode("utf-8")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Chrome:
    """A headless Chrome with a throwaway profile, controlled over the DevTools Protocol."""

    def __init__(self, executable: str) -> None:
        self.profile = tempfile.mkdtemp(prefix="touch-check-profile-")
        self.port = free_port()
        self.process = subprocess.Popen(
            [executable, "--headless=new", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
             "--hide-scrollbars", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as reply:
                    targets = [t for t in json.loads(reply.read()) if t.get("type") == "page"]
                    if targets:
                        target = targets[0]
                        break
            except OSError:
                pass
            time.sleep(0.2)
        if target is None:
            self.close()
            raise RuntimeError("Chrome did not start")
        path = "/" + target["webSocketDebuggerUrl"].split("/", 3)[3]
        self.ws = WebSocket("127.0.0.1", self.port, path)
        self.next_id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.next_id += 1
        self.ws.send_text(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv_text())
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"page script failed: {result['exceptionDetails'].get('exception', {}).get('description', result['exceptionDetails'])}")
        return result["result"].get("value")

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", url=url)
        for _ in range(100):
            time.sleep(0.15)
            if self.evaluate("document.readyState") == "complete":
                return
        raise RuntimeError(f"{url} did not finish loading")

    def screenshot(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))

    def emulate(self, width: int, height: int, *, phone: bool) -> None:
        """A phone (mobile screen, touch, iPhone user agent) or a plain desktop window."""
        if phone:
            self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=2, mobile=True,
                      screenOrientation={"type": "portraitPrimary" if height >= width else "landscapePrimary", "angle": 0})
            self.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
            self.call("Emulation.setUserAgentOverride", userAgent=IPHONE_UA)
        else:
            self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
            self.call("Emulation.setTouchEmulationEnabled", enabled=False)
            self.call("Emulation.setUserAgentOverride", userAgent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36")

    # real touch input: Chrome turns these into touch events, pointer events and scrolling, as a finger would
    def touch(self, kind: str, x: float | None = None, y: float | None = None) -> None:
        points = [] if x is None else [{"x": x, "y": y, "id": 1}]
        self.call("Input.dispatchTouchEvent", type=kind, touchPoints=points)

    def tap(self, x: float, y: float) -> None:
        self.touch("touchStart", x, y)
        time.sleep(0.06)
        self.touch("touchEnd")

    def swipe(self, x1: float, y1: float, x2: float, y2: float, steps: int = 12) -> None:
        self.touch("touchStart", x1, y1)
        for step in range(1, steps + 1):
            time.sleep(0.02)
            self.touch("touchMove", x1 + (x2 - x1) * step / steps, y1 + (y2 - y1) * step / steps)
        time.sleep(0.05)
        self.touch("touchEnd")

    def long_press(self, x: float, y: float, hold: float = 0.8) -> None:
        self.touch("touchStart", x, y)
        time.sleep(hold)
        self.touch("touchEnd")

    def hold_drag(self, x1: float, y1: float, x2: float, y2: float, hold: float = 0.7, steps: int = 12) -> None:
        """Hold still long enough to lift the element, then drag it."""
        self.touch("touchStart", x1, y1)
        time.sleep(hold)
        for step in range(1, steps + 1):
            time.sleep(0.02)
            self.touch("touchMove", x1 + (x2 - x1) * step / steps, y1 + (y2 - y1) * step / steps)
        time.sleep(0.05)
        self.touch("touchEnd")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        if self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)], capture_output=True)
            else:
                self.process.kill()
        time.sleep(0.5)
        shutil.rmtree(self.profile, ignore_errors=True)


class Api:
    """Plain HTTP calls to the game server for setting the scene (the page under test is a different player)."""

    def __init__(self, port: int) -> None:
        self.port = port

    def call(self, method: str, path: str, body: Any = None, session: str = "") -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json", "X-Session": session})
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
        finally:
            conn.close()


# ---- the checks ---------------------------------------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.count = 0

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        self.count += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}" + (f"   [{detail}]" if detail and not ok else ""))
        if not ok:
            self.failures.append(what + (f" ({detail})" if detail else ""))
        return ok


def center_of(browser: Chrome, selector: str, index: int = 0) -> tuple[float, float]:
    box = browser.evaluate(f"(() => {{ const e = document.querySelectorAll({json.dumps(selector)})[{index}]; if (!e) return null; "
                           f"e.scrollIntoView({{block: 'nearest'}}); const r = e.getBoundingClientRect(); "
                           f"return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
    if box is None:
        raise RuntimeError(f"nothing matches {selector!r}")
    return box[0], box[1]


def layout(browser: Chrome) -> dict[str, Any]:
    return browser.evaluate("""(() => {
      const rect = (id) => { const e = document.getElementById(id); if (!e) return null; const r = e.getBoundingClientRect();
        return {left: r.left, top: r.top, width: r.width, height: r.height}; };
      const items = [...document.querySelectorAll('#items .item')].slice(0, 12);
      return {
        width: innerWidth, height: innerHeight,
        overflowX: document.documentElement.scrollWidth - innerWidth,
        coarse: matchMedia('(pointer: coarse)').matches, hoverNone: matchMedia('(hover: none)').matches,
        header: rect('app') && document.querySelector('header').getBoundingClientRect().height,
        sidebar: rect('sidebar'), workspace: rect('workspace'),
        itemsPerRow: new Set(items.map(i => Math.round(i.getBoundingClientRect().left))).size,
        hint: (document.getElementById('hint') || {}).textContent || '',
        tiles: document.querySelectorAll('#workspace .tile').length,
      };
    })()""")


def discover_elements(api: Api, slug: str, token: str, starters: list[str], want: int) -> list[str]:
    """Make the room discover about `want` elements, so the element list is long enough to need scrolling."""
    known = list(starters)
    tried: set[tuple[str, str]] = set()
    progress = True
    while len(known) < want and progress:
        progress = False
        for a in list(known):
            for b in list(known):
                if (a, b) in tried:
                    continue
                tried.add((a, b))
                status, tile = api.call("POST", f"/g/{slug}/tile/add", {"player": token, "element": a, "x": 0.5, "y": 0.5})
                if status != 200:
                    continue
                status, result = api.call("POST", f"/g/{slug}/combine", {"player": token, "element": b, "b": tile["id"], "x": 0.5, "y": 0.5})
                if status == 200:
                    for element in result.get("new", []):
                        if element not in known:
                            known.append(element)
                            progress = True
                api.call("POST", f"/g/{slug}/tiles/clear", {"player": token})
                if len(known) >= want:
                    return known
    return known


def run(out: Path, chrome_path: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    report = Report()
    tmp = tempfile.TemporaryDirectory()
    port = free_port()
    # "phonea" has already finished Solo mode, so the Create Recipes checks below see the real page
    (Path(tmp.name) / "solo.json").write_text(json.dumps({"version": 1, "users": {"phonea": {"complete": True, "found": 1, "total": 1}}}), encoding="utf-8")
    server = subprocess.Popen(server_command(ROOT, port, Path(tmp.name), None), cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser: Chrome | None = None
    try:
        wait_up(port)
        api = Api(port)
        sessions = {}
        for name in ("phonea", "phoneb"):
            status, data = api.call("POST", "/register", {"username": name, "password": "touch-check-pass-1"})
            assert status == 200, data
            sessions[name] = data["session"]
        status, game = api.call("POST", "/games", {"name": "touch check"}, sessions["phoneb"])
        slug = game["id"]
        status, joined = api.call("POST", f"/g/{slug}/join", {}, sessions["phoneb"])
        token_b = joined["token"]
        # phoneb stays connected (an open event stream), so the phone shows them in the player list
        status, ticket = api.call("POST", f"/g/{slug}/ticket", {"player": token_b}, sessions["phoneb"])
        stream_b = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
        stream_b.request("GET", f"/g/{slug}/events?ticket={ticket['ticket']}")
        response_b = stream_b.getresponse()
        threading.Thread(target=lambda: [None for _ in response_b], daemon=True).start()
        book = load_recipes(ROOT / "recipes.json")
        starters = [getattr(s, "id", s) for s in book.starters]
        known = discover_elements(api, slug, token_b, starters, 36)
        print(f"scene: game {slug!r}, {len(known)} elements discovered so the list needs scrolling")

        browser = Chrome(chrome_path)
        base = f"http://127.0.0.1:{port}"

        def login_and_join(label: str) -> None:
            browser.navigate(base + "/login")
            browser.evaluate(f"localStorage.setItem('alchemy-session', {json.dumps(sessions['phonea'])}); "
                             "localStorage.setItem('alchemy-user', 'phonea');")  # sessionStorage stays: a reload keeps its seat
            browser.navigate(f"{base}/g/{slug}")

        def join_by_touch(touch: bool = True) -> None:
            """Tap Join if the page asks (a reloaded page that already has a seat goes straight in), then wait for the connection."""
            if browser.evaluate("!document.getElementById('join').hidden"):
                if touch:
                    x, y = center_of(browser, "#join-form button[type=submit]")
                    browser.tap(x, y)
                else:
                    browser.evaluate("document.getElementById('join-form').requestSubmit()")
            for _ in range(60):
                time.sleep(0.15)
                if browser.evaluate("!document.getElementById('app').hidden && document.getElementById('status').classList.contains('on')"):
                    return
            raise RuntimeError("the game page never connected after tapping Join")

        # ---- phone, portrait ----
        print("\nphone, portrait (390 x 844, touch)")
        browser.emulate(390, 844, phone=True)
        login_and_join("phone")
        join_by_touch()
        time.sleep(0.8)
        info = layout(browser)
        report.check(info["coarse"], "the page sees a touch screen (pointer: coarse)", json.dumps({"coarse": info["coarse"], "hover_none": info["hoverNone"]}))
        report.check(info["overflowX"] <= 1, "nothing makes the page wider than the screen", f"{info['overflowX']}px too wide")
        report.check(info["header"] <= 0.2 * info["height"], "the header leaves most of the screen for the board", f"header {info['header']:.0f}px of {info['height']}")
        report.check(0.3 <= info["sidebar"]["height"] / info["height"] <= 0.5, "the element list is a band under the board",
                     f"{info['sidebar']['height'] / info['height']:.2f} of the height")
        report.check(info["sidebar"]["top"] > info["workspace"]["top"], "the list sits below the board")
        report.check(info["itemsPerRow"] >= 3, "at least three elements per row", f"{info['itemsPerRow']} per row")
        report.check("Tap" in info["hint"], "the board explains tapping", info["hint"][:50])
        browser.screenshot(out / "phone-portrait-game.png")

        tiles_before = layout(browser)["tiles"]
        x, y = center_of(browser, "#items .item", 0)
        browser.tap(x, y)
        time.sleep(0.8)
        report.check(layout(browser)["tiles"] == tiles_before + 1, "tapping an element adds it to the board",
                     f"{tiles_before} tiles before, {layout(browser)['tiles']} after")

        scrollable = browser.evaluate("(() => { const l = document.getElementById('items'); return l.scrollHeight - l.clientHeight; })()")
        report.check(scrollable > 80, "the element list is long enough to scroll", f"{scrollable}px of overflow")
        tiles_before = layout(browser)["tiles"]
        scroll_before = browser.evaluate("document.getElementById('items').scrollTop")
        x, y = center_of(browser, "#items .item", 3)
        browser.swipe(x, y, x, y - 220)
        time.sleep(0.6)
        scroll_after = browser.evaluate("document.getElementById('items').scrollTop")
        report.check(scroll_after > scroll_before + 40, "swiping up on the element list scrolls it", f"{scroll_before} -> {scroll_after}")
        report.check(layout(browser)["tiles"] == tiles_before, "...and does not add an element by accident",
                     f"{tiles_before} -> {layout(browser)['tiles']} tiles")

        # ---- reporting another player with a finger ----
        button = browser.evaluate("(() => { const b = document.querySelector('#key .report'); if (!b) return null;"
                                  " const r = b.getBoundingClientRect(); return {w: r.width, h: r.height}; })()")
        if report.check(button is not None, "the other player in the list has a report button"):
            report.check(button["w"] >= 36 and button["h"] >= 36, "the report button is big enough for a finger", f"{button['w']:.0f} x {button['h']:.0f}")
            x, y = center_of(browser, "#key .report")
            browser.tap(x, y)
            time.sleep(0.6)
            dialog = browser.evaluate("""(() => { const d = document.getElementById('report-dialog'); const r = d.getBoundingClientRect();
              return {open: d.open, left: r.left, right: r.right, bottom: r.bottom, w: innerWidth, h: innerHeight,
                      font: parseFloat(getComputedStyle(document.getElementById('report-reason')).fontSize)}; })()""")
            report.check(dialog["open"], "tapping it opens the report dialog")
            report.check(dialog["left"] >= 0 and dialog["right"] <= dialog["w"] and dialog["bottom"] <= dialog["h"],
                         "the dialog fits the screen", json.dumps(dialog))
            report.check(dialog["font"] >= 16, "its fields are big enough that iOS will not zoom in", f"{dialog['font']}px")
            small = browser.evaluate("[...document.querySelectorAll('input:not([type=checkbox]):not([type=radio]), select, textarea')]"
                                     ".filter(e => parseFloat(getComputedStyle(e).fontSize) < 16 && e.id).map(e => e.id)")
            report.check(not small, "no text field on the game page is small enough to make iOS zoom", str(small))
            browser.screenshot(out / "phone-report-dialog.png")
            x, y = center_of(browser, "#report-form button[type=submit]")
            browser.tap(x, y)
            time.sleep(1.0)
            report.check(not browser.evaluate("document.getElementById('report-dialog').open"), "sending closes the dialog")
            AccountStore(Path(tmp.name) / "a.json", iterations=1000).set_admin("phoneb", True)
            status, listing = api.call("GET", "/ops/reports", None, sessions["phoneb"])
            mine = [r for r in listing.get("reports", []) if r["user"] == "phoneb"] if status == 200 else []
            report.check(len(mine) == 1 and mine[0]["count"] == 1, "the administrator sees the report", f"{status} {json.dumps(listing)}")
            report.check("phonea" not in json.dumps(listing), "...and nothing in it says who sent it")

        # drag one tile onto another with a finger: water + fire should make steam
        api.call("POST", f"/g/{slug}/tiles/clear", {"player": token_b})
        for _ in range(60):  # the cat sweeps the board for a couple of seconds; tiles added during that would be swept too
            time.sleep(0.2)
            if browser.evaluate("!document.querySelector('.smoke') && document.querySelectorAll('#workspace .tile').length === 0"):
                break
        time.sleep(0.4)
        browser.evaluate("document.getElementById('items').scrollTop = 0")
        for element, tx in (("water", 0.25), ("fire", 0.75)):
            api.call("POST", f"/g/{slug}/tile/add", {"player": token_b, "element": element, "x": tx, "y": 0.35})
        time.sleep(1.0)
        have = browser.evaluate("[...document.querySelectorAll('#workspace .tile')].map(t => t.dataset.element)")
        report.check(set(have) >= {"water", "fire"}, "tiles added by another player appear on the phone", str(have))
        x1, y1 = center_of(browser, '#workspace .tile[data-element="water"]')
        x2, y2 = center_of(browser, '#workspace .tile[data-element="fire"]')
        browser.swipe(x1, y1, x2, y2, steps=14)
        time.sleep(1.2)
        made = browser.evaluate("[...document.querySelectorAll('#workspace .tile')].map(t => t.dataset.element)")
        report.check("steam" in made and "water" not in made and "fire" not in made,
                     "dragging a tile onto another with a finger combines them (water + fire = steam)", str(made))

        print("\nlong press and hold-to-drag on a phone")
        dialog_open = "document.getElementById('journal-dialog').open"
        shown = "(document.querySelector('#journal-detail h2') || {}).textContent || ''"
        x, y = center_of(browser, '#workspace .tile[data-element="steam"]')
        under = browser.evaluate(f"document.elementFromPoint({x}, {y}).closest('.tile').dataset.element")   # tiles may overlap
        under_label = browser.evaluate(f"document.querySelector('#items .item[data-element=\"{under}\"]').dataset.search")
        browser.long_press(x, y)
        time.sleep(0.8)
        report.check(browser.evaluate(dialog_open), "holding a tile opens the journal")
        report.check(under_label.lower() in browser.evaluate(shown).lower(), "...on that tile's element", f"{under_label!r} vs {browser.evaluate(shown)!r}")
        browser.evaluate("document.getElementById('journal-close').click()")
        time.sleep(0.3)
        tiles_before = browser.evaluate("document.querySelectorAll('#workspace .tile').length")
        report.check(tiles_before >= 1 and not browser.evaluate(dialog_open), "closing it leaves the board as it was")
        browser.evaluate("document.getElementById('items').scrollTop = 0")
        time.sleep(0.3)
        item_name = browser.evaluate("document.querySelector('#items .item:not([hidden])').dataset.element")
        x, y = center_of(browser, "#items .item:not([hidden])", 0)
        browser.long_press(x, y)
        time.sleep(0.8)
        report.check(browser.evaluate(dialog_open), "holding an element in the list and letting go opens its page")
        report.check(browser.evaluate("document.querySelectorAll('#workspace .tile').length") == tiles_before,
                     "...without adding a tile")
        report.check(item_name.lower() in browser.evaluate(shown).lower() or browser.evaluate(shown) != "",
                     "...showing an element", browser.evaluate(shown))
        browser.evaluate("document.getElementById('journal-close').click()")
        time.sleep(0.3)
        box = browser.evaluate("(() => { const r = document.getElementById('workspace').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
        x, y = center_of(browser, "#items .item:not([hidden])", 1)
        browser.hold_drag(x, y, box[0] + box[2] * 0.5, box[1] + box[3] * 0.8)
        time.sleep(0.8)
        report.check(browser.evaluate("document.querySelectorAll('#workspace .tile').length") == tiles_before + 1,
                     "holding an element and dragging it puts it on the board")
        report.check(not browser.evaluate(dialog_open), "...without opening its page")
        scroll_before = browser.evaluate("document.getElementById('items').scrollTop")
        x, y = center_of(browser, "#items .item:not([hidden])", 3)
        browser.swipe(x, y, x, y - 160)
        time.sleep(0.5)
        report.check(browser.evaluate("document.getElementById('items').scrollTop") > scroll_before and not browser.evaluate(dialog_open),
                     "a quick swipe on the list still just scrolls it")
        browser.evaluate("document.getElementById('items').scrollTop = 0")

        print("\nelement pictures")
        pics = browser.evaluate("[...document.querySelectorAll('#items .item img.pic')].map(i => i.getAttribute('src'))")
        report.check(len(pics) > 5 and all(p.startswith("/pic/") for p in pics), "pictures show at their secret addresses", str(pics[:3]))
        report.check(browser.evaluate("[...document.querySelectorAll('#items .item img.pic')].every(i => i.complete && i.naturalWidth > 0)"),
                     "...and they load")
        report.check(api.call("GET", "/static/elements/air.png", None, sessions["phonea"])[0] == 404, "the picture folder is not served by file name")
        report.check(api.call("GET", "/images", None, sessions["phonea"])[0] == 403, "the picture list is closed to players")
        report.check("/static/elements/" not in browser.evaluate("document.documentElement.outerHTML"), "no page mentions the picture folder")
        print("\nMr Alchemy's visit on a phone")
        subprocess.run([sys.executable, str(ROOT / "accounts_admin.py"), "--file", str(Path(tmp.name) / "a.json"), "make-admin", "phonea"],
                       capture_output=True, check=True)
        before = browser.evaluate("document.querySelectorAll('#items .item').length")
        status, sent = api.call("POST", "/ops/games/visit", {"id": slug}, sessions["phonea"])
        report.check(status == 200 and 5 <= len(sent["gave"]) <= 10, "an administrator can send Mr Alchemy to the game", str(sent))
        time.sleep(1.6)
        report.check(browser.evaluate("!document.getElementById('visitor').hidden"), "the cutscene starts")
        report.check("struck by Mr Alchemy" in browser.evaluate("document.querySelector('.visitor-title').textContent"), "...with his line")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and does not widen the page")
        time.sleep(2.6)
        browser.screenshot(out / "phone-visitor.png")
        report.check(browser.evaluate("document.querySelectorAll('.visitor-chip').length") >= 1, "the elements appear one at a time")
        x, y = center_of(browser, ".visitor-skip")
        browser.tap(x, y)
        time.sleep(0.5)
        report.check(browser.evaluate("document.getElementById('visitor').hidden"), "Skip ends the cutscene at once")
        after = browser.evaluate("document.querySelectorAll('#items .item').length")
        report.check(after - before == len(sent["gave"]), "...with every gifted element in the list", f"{before} -> {after}")
        report.check(browser.evaluate("!!document.querySelector('#items .item.new')"), "...and they are marked new")
        api.call("POST", "/ops/games/visit", {"id": slug}, sessions["phonea"])
        time.sleep(13)
        report.check(browser.evaluate("document.getElementById('visitor').hidden"), "left alone, the cutscene ends by itself")
        print("\na companion on a phone")
        names_before = browser.evaluate("[...document.querySelectorAll('#key li')].map(li => li.querySelector('span:nth-child(2)').textContent)")
        status, added = api.call("POST", "/ops/bots/add", {"id": slug, "level": 1}, sessions["phonea"])
        report.check(status == 200 and added.get("name"), "an administrator can put a companion in the game", str(added))
        time.sleep(2.0)
        names_now = browser.evaluate("[...document.querySelectorAll('#key li')].map(li => li.querySelector('span:nth-child(2)').textContent)")
        report.check(len(names_now) == len(names_before) + 1 and added["name"] in names_now, "it appears in the player list like anyone else", str(names_now))
        row = browser.evaluate("(() => { const li = [...document.querySelectorAll('#key li')].find(l => l.textContent.includes(%s)); "
                               "return li ? [li.children.length, !!li.querySelector('.report'), li.textContent] : null; })()" % json.dumps(added["name"]))
        report.check(row is not None and row[1] and "bot" not in row[2].lower(), "...with the same report button and nothing marking it", str(row))
        seen_cursor = False
        for _ in range(60):
            time.sleep(0.5)
            if browser.evaluate("[...document.querySelectorAll('#cursor-layer .cursor-name')].some(n => n.textContent === %s)" % json.dumps(added["name"])):
                seen_cursor = True
                break
        report.check(seen_cursor, "its cursor shows on the board")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and the page still fits the screen")
        api.call("POST", "/ops/bots/remove", {"name": added["name"]}, sessions["phonea"])
        time.sleep(1.5)
        report.check(browser.evaluate("![...document.querySelectorAll('#key li')].some(l => l.textContent.includes(%s))" % json.dumps(added["name"])),
                     "when it is removed it leaves the list")
        print("\nemotes, sound, discovery moments and awards on a phone")
        report.check(browser.evaluate("!document.getElementById('emote-open').hidden"), "a shared game has an emote button")
        x, y = center_of(browser, "#emote-open")
        browser.tap(x, y)
        time.sleep(0.4)
        report.check(browser.evaluate("document.querySelectorAll('#emote-picker button').length") >= 6, "tapping it lists the emotes")
        x, y = center_of(browser, "#emote-picker button", 0)
        browser.tap(x, y)
        time.sleep(1.0)
        report.check(browser.evaluate("document.getElementById('emote-picker').hidden"), "choosing one closes the list")
        report.check(browser.evaluate("!!document.querySelector('#key li .emote')"), "the emote shows next to your name")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "the header with the new buttons fits the phone")
        x, y = center_of(browser, "#sound")
        browser.tap(x, y)
        time.sleep(0.3)
        report.check(browser.evaluate("document.getElementById('sound').textContent") == "🔇"
                     and browser.evaluate("localStorage.getItem('alchemy-sound')") == "off", "the sound button turns sound off and remembers it")
        browser.tap(x, y)
        time.sleep(0.3)
        report.check(browser.evaluate("document.getElementById('sound').textContent") == "🔊", "...and back on")
        browser.evaluate("discoveryMoment({emoji: '✨', label: 'Test element'}, 'epic', true)")
        time.sleep(0.3)
        report.check(browser.evaluate("!!document.querySelector('#workspace .moment.epic')"), "an epic find shows a banner over the board")
        browser.screenshot(out / "phone-moment.png")
        time.sleep(2.6)
        report.check(browser.evaluate("!document.querySelector('#workspace .moment')"), "...which goes away by itself")
        browser.screenshot(out / "phone-portrait-after-combine.png")

        # ---- phone, landscape ----
        print("\nphone, landscape (844 x 390, touch)")
        browser.emulate(844, 390, phone=True)
        login_and_join("landscape")
        join_by_touch()
        time.sleep(0.8)
        info = layout(browser)
        report.check(info["overflowX"] <= 1, "nothing makes the page wider than the screen", f"{info['overflowX']}px too wide")
        report.check(info["sidebar"]["left"] > 0.45 * info["width"], "the element list moves to the side", f"starts at {info['sidebar']['left']:.0f} of {info['width']}")
        report.check(info["workspace"]["height"] > 0.6 * info["height"], "the board keeps most of the height", f"{info['workspace']['height']:.0f} of {info['height']}")
        browser.screenshot(out / "phone-landscape-game.png")

        # ---- tablet, portrait ----
        print("\ntablet, portrait (768 x 1024, touch)")
        browser.emulate(768, 1024, phone=True)
        login_and_join("tablet")
        join_by_touch()
        time.sleep(0.8)
        info = layout(browser)
        report.check(info["overflowX"] <= 1, "nothing makes the page wider than the screen", f"{info['overflowX']}px too wide")
        report.check(abs(info["sidebar"]["width"] - 300) <= 3, "a tablet gets the desktop layout (list 300px at the side)", f"{info['sidebar']['width']:.0f}px")
        browser.screenshot(out / "tablet-portrait-game.png")

        # ---- desktop, to be sure nothing changed there ----
        print("\ndesktop (1280 x 800, mouse)")
        browser.emulate(1280, 800, phone=False)
        login_and_join("desktop")
        join_by_touch(touch=False)
        time.sleep(0.8)
        info = layout(browser)
        report.check(not info["coarse"], "a desktop is not mistaken for a touch screen")
        report.check(abs(info["sidebar"]["width"] - 300) <= 3, "the element list is 300px at the side", f"{info['sidebar']['width']:.0f}px")
        report.check(info["header"] <= 70, "the header is a single row", f"{info['header']:.0f}px")
        report.check(info["overflowX"] <= 1, "nothing makes the page wider than the screen", f"{info['overflowX']}px too wide")
        report.check("Drag" in info["hint"], "the board still says drag, not tap", info["hint"][:50])
        report.check(browser.evaluate("!!document.querySelector('#key .report')"), "the report button is there on a desktop too")
        # right-click on an element opens its journal page (and never the browser's own menu)
        wanted = browser.evaluate("document.querySelector('#items .item:not([hidden])').dataset.search")
        prevented = browser.evaluate("""(() => {
          const item = document.querySelector('#items .item:not([hidden])');
          const r = item.getBoundingClientRect();
          const event = new MouseEvent('contextmenu', {bubbles: true, cancelable: true, button: 2, clientX: r.left + 5, clientY: r.top + 5});
          item.dispatchEvent(event);
          return event.defaultPrevented;
        })()""")
        time.sleep(0.8)
        report.check(prevented, "right-clicking an element does not show the browser's menu")
        report.check(browser.evaluate("document.getElementById('journal-dialog').open"), "...it opens the element's journal page")
        report.check(wanted.lower() in browser.evaluate("(document.querySelector('#journal-detail h2') || {}).textContent || ''").lower(),
                     "...for that element", wanted)
        browser.evaluate("document.getElementById('journal-close').click()")
        time.sleep(0.3)
        browser.screenshot(out / "desktop-game.png")

        # ---- the other pages, on a phone ----
        print("\nother pages on a phone (390 x 844)")
        browser.emulate(390, 844, phone=True)
        for name, path in (("home", "/"), ("create", "/create"), ("privacy", "/privacy"), ("terms", "/terms"), ("login", "/login")):
            browser.navigate(base + path)
            time.sleep(0.8)
            wide = browser.evaluate("document.documentElement.scrollWidth - innerWidth")
            report.check(wide <= 1, f"{name} page fits the screen", f"{wide}px too wide")
            browser.screenshot(out / f"phone-{name}.png")

        print("\nthe Operations page's load table on a phone")
        browser.navigate(base + "/ops")
        time.sleep(2.0)
        report.check(not browser.evaluate("document.getElementById('load-table').hidden"), "the load table shows once a sample exists")
        report.check(browser.evaluate("document.querySelectorAll('#load-table tbody tr').length") == 3, "...with CPU, players and rooms")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and the Operations page fits the screen")
        report.check("full" in browser.evaluate("document.getElementById('load-note').textContent"), "...and says whether the lobby has been full")
        browser.evaluate("document.getElementById('load-table').scrollIntoView({block: 'center'})")
        time.sleep(0.4)
        browser.screenshot(out / "phone-ops-load.png")
        print("\nHome page tabs and Solo mode on a phone")
        browser.navigate(base + "/#solo")
        time.sleep(1.0)
        time.sleep(1.0)
        report.check(not browser.evaluate("document.getElementById('profile').hidden"), "the home page shows the player's profile card")
        report.check(browser.evaluate("document.querySelectorAll('#profile-stats .stat').length") == 6, "...with six numbers")
        report.check("with others" in browser.evaluate("document.getElementById('profile-stats').textContent"), "...about playing with others")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...that fits the phone screen")
        report.check(not browser.evaluate("document.getElementById('profile').open"), "...collapsed to one line on a phone")
        browser.evaluate("document.querySelector('#profile summary').click()")
        time.sleep(0.4)
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and still fits when opened")
        browser.screenshot(out / "phone-profile.png")
        browser.evaluate("document.querySelector('#profile summary').click()")
        report.check(browser.evaluate("[...document.querySelectorAll('.home-tab')].map(t => t.textContent).join()") == "Solo,Multiplayer,Challenges",
                     "the home page has Solo, Multiplayer and Challenges tabs")
        report.check(browser.evaluate("!document.getElementById('panel-solo').hidden && document.getElementById('panel-multiplayer').hidden"),
                     "the address #solo opens the Solo tab", browser.evaluate("location.href + ' ' + [...document.querySelectorAll('.home-panel')].map(p => p.id + ':' + p.hidden).join()"))
        x, y = center_of(browser, "#home-tab-challenges")
        browser.tap(x, y)
        time.sleep(0.4)
        report.check(browser.evaluate("!document.getElementById('panel-challenges').hidden && document.getElementById('panel-solo').hidden"),
                     "tapping a tab switches to it")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "the Challenges tab fits the screen")
        browser.screenshot(out / "phone-home-challenges.png")
        x, y = center_of(browser, "#home-tab-solo")
        browser.tap(x, y)
        time.sleep(0.4)
        report.check(browser.evaluate("document.querySelectorAll('#achievements-multiplayer .achievement').length") == 11
                     and browser.evaluate("document.querySelectorAll('#achievements-challenges .achievement').length") == 8,
                     "the Multiplayer and Challenges tabs list their achievements")
        report.check(browser.evaluate("document.querySelectorAll('#achievements-multiplayer .achievement:not(.locked)').length") >= 1,
                     "...and playing with another person has already earned Good company")
        browser.evaluate("document.getElementById('home-tab-multiplayer').click()")
        time.sleep(0.4)
        browser.evaluate("document.getElementById('achievements-multiplayer').scrollIntoView({block: 'start'})")
        time.sleep(0.3)
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and the Multiplayer tab still fits the screen")
        browser.screenshot(out / "phone-achievements.png")
        browser.evaluate("document.getElementById('home-tab-solo').click()")
        time.sleep(0.3)
        report.check(browser.evaluate("document.querySelectorAll('#achievements .achievement').length") >= 10,
                     "the Solo tab lists the achievements")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and fits the phone screen")
        browser.screenshot(out / "phone-home-solo.png")
        x, y = center_of(browser, "#solo-start")
        browser.tap(x, y)
        for _ in range(40):
            time.sleep(0.25)
            if "/g/solo-" in browser.evaluate("location.pathname"):
                break
        report.check("/g/solo-" in browser.evaluate("location.pathname"), "Play solo opens a private solo game")
        join_by_touch()
        report.check(browser.evaluate("document.getElementById('invite').hidden"), "a solo game has no invite button")
        report.check(browser.evaluate("document.getElementById('end').textContent") == "Leave", "a solo game says Leave, not End")
        report.check(browser.evaluate("document.getElementById('emote-open').hidden"), "a solo game has no emote button")
        report.check("1 hint" in browser.evaluate("document.getElementById('hints-left').textContent"), "a solo game says you have 1 hint today")
        browser.evaluate("document.querySelector('#hints summary').click()")
        time.sleep(0.3)
        x, y = center_of(browser, '[data-hint="can_make"]')
        browser.tap(x, y)
        time.sleep(1.0)
        report.check("used today's hint" in browser.evaluate("document.getElementById('hints-left').textContent"), "using it says the hint is used up")
        report.check(browser.evaluate("document.querySelector('[data-hint=can_make]').disabled"), "...and the hint buttons are switched off")
        # the end-of-game screen (this is the solo page, so closing its stream disturbs nothing that follows)
        browser.evaluate("handlers.ended({by: 'phoneb', saved: false, awards: [{icon: '🥇', title: 'Most elements found', player: 'phonea', detail: '12 elements'}, "
                         "{icon: '💎', title: 'Rarest find', player: 'phoneb', detail: 'a rare thing, only 1 way to make it'}]})")
        time.sleep(0.4)
        report.check(browser.evaluate("!document.getElementById('awards').hidden && document.querySelectorAll('#awards-list li').length === 2"),
                     "the end-of-game screen lists the awards")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...and fits the phone screen")
        browser.screenshot(out / "phone-awards.png")
        browser.evaluate("document.getElementById('journal-open').click()")
        time.sleep(0.8)
        browser.evaluate("document.getElementById('journal-tab-grid').click()")
        time.sleep(0.5)
        report.check(browser.evaluate("document.querySelectorAll('#journal-grid .collection-tile.locked').length") > 100,
                     "the Collection tab shows silhouettes for what is not found yet")
        report.check(browser.evaluate("document.getElementById('journal-dialog').scrollWidth <= innerWidth"), "the Collection fits the phone screen")
        browser.screenshot(out / "phone-collection.png")
        browser.evaluate("document.getElementById('journal-close').click()")

        print("\nCreate Recipes page is locked for an account that has not finished Solo")
        status, other = api.call("POST", "/register", {"username": "phonec", "password": "touch-check-pass-1"})
        browser.navigate(base + "/login")
        browser.evaluate(f"localStorage.setItem('alchemy-session', {json.dumps(other['session'])}); localStorage.setItem('alchemy-user', 'phonec');")
        browser.navigate(base + "/create")
        time.sleep(1.0)
        report.check(browser.evaluate("document.querySelectorAll('#items .item').length") == 0, "a locked Create page lists no elements")
        report.check("Solo" in browser.evaluate("document.getElementById('hint').textContent"), "...and says to finish Solo mode")
        browser.navigate(base + "/login")
        browser.evaluate(f"localStorage.setItem('alchemy-session', {json.dumps(sessions['phonea'])}); localStorage.setItem('alchemy-user', 'phonea');")

        print("\nCreate Recipes page with a finger")
        browser.navigate(base + "/create")
        time.sleep(1.0)
        tiles_before = browser.evaluate("document.querySelectorAll('#workspace .tile').length")
        x, y = center_of(browser, "#items .item", 0)
        browser.tap(x, y)
        time.sleep(0.6)
        report.check(browser.evaluate("document.querySelectorAll('#workspace .tile').length") == tiles_before + 1,
                     "tapping an element adds it on the Create page too")
        tiles_before = browser.evaluate("document.querySelectorAll('#workspace .tile').length")
        scroll_before = browser.evaluate("document.getElementById('items').scrollTop")
        x, y = center_of(browser, "#items .item", 3)
        browser.swipe(x, y, x, y - 220)
        time.sleep(0.6)
        report.check(browser.evaluate("document.getElementById('items').scrollTop") > scroll_before + 40,
                     "swiping the list scrolls it on the Create page")
        report.check(browser.evaluate("document.querySelectorAll('#workspace .tile').length") == tiles_before,
                     "...without adding an element by accident")
        report.check("Tap" in browser.evaluate("document.getElementById('hint').textContent"), "the Create page explains tapping")

        print("\na room opened by a companion, on a phone")
        status, hosted = api.call("POST", "/ops/bots/host", {}, sessions["phonea"])
        report.check(status == 200 and hosted.get("id"), "an administrator can have a companion open a room", str(hosted))
        status, listing = api.call("GET", "/games", None, sessions["phonea"])
        entry = next((g for g in listing["games"] if g["id"] == hosted["id"]), None)
        report.check(entry is not None and entry["players"] == 1, "the lobby lists it with someone in it", str(entry))
        browser.navigate(f"{base}/g/{hosted['id']}")
        join_by_touch()
        time.sleep(1.0)
        keys = browser.evaluate("[...document.querySelectorAll('#key li')].map(li => li.querySelector('span:nth-child(2)').textContent)")
        report.check(hosted["name"] in keys and len(keys) == 2, "joining it, the host is in the player list next to you", str(keys))
        seen = False
        for _ in range(40):
            time.sleep(0.5)
            if browser.evaluate("[...document.querySelectorAll('#cursor-layer .cursor-name')].some(n => n.textContent === %s)" % json.dumps(hosted["name"])):
                seen = True
                break
        report.check(seen, "...and its cursor wanders the board")
        report.check(browser.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1, "...on a page that fits the screen")
        browser.screenshot(out / "phone-hosted-room.png")
        api.call("POST", "/ops/bots/remove", {"id": hosted["id"]}, sessions["phonea"])
    finally:
        try:
            stream_b.close()
        except Exception:
            pass
        if browser is not None:
            browser.close()
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        try:
            tmp.cleanup()
        except OSError:
            pass

    print(f"\n{report.count - len(report.failures)} of {report.count} checks passed; screenshots are in {out}")
    for failure in report.failures:
        print(f"  FAILED: {failure}")
    return 1 if report.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the game on emulated phones, tablets and desktops")
    parser.add_argument("--out", default="touch-check-output")
    parser.add_argument("--chrome", help="path to chrome or msedge (found automatically if it is installed normally)")
    args = parser.parse_args()
    chrome = find_chrome(args.chrome)
    if chrome is None:
        print("No Chrome or Edge found: install one, or pass --chrome PATH", file=sys.stderr)
        return 2
    return run(Path(args.out), chrome)


if __name__ == "__main__":
    sys.exit(main())

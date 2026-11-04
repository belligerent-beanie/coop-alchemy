"""Co-op Alchemy server (standard library only).

Run:
    python3 server.py [--host 0.0.0.0] [--port 8000]
                      [--recipes recipes.json] [--idle-minutes 10] [--saves-dir saves]
                      [--accounts-file accounts.json] [--tls-cert cert.pem --tls-key key.pem]

Players open http://<this-machine's-LAN-ip>:8000 and log in (or register: a unique
username, which is also their name in games, and a password). Then they can start a new game
(at most 3 run at once) or join a running one. Each game has its own address,
http://<ip>:8000/g/<game-id>, which can be shared directly.

HTTP API
    Everything except the pages, /register, /login and /laptop needs the header
    X-Session: <session token> from /register or /login (401 otherwise).
    POST /register {"username", "password"}  -> {"session", "username"}   (409 if the name is taken)
    POST /login    {"username", "password"}  -> {"session", "username"}   (401 wrong; 429 after 5 wrong in a row)
    POST /logout   {}                        -> {}
    GET  /me                                 -> {"username"}
    GET  /lan                                -> {"base"}   the address other players use (for invite links)
    GET  /games                         -> {"games": [...], "max_games"}   (a game has "recent": true if the
                                           caller left it within the last 30 minutes and it is still running;
                                           the caller's own daily attempt is listed with "private": true)
    POST /games         {"name"}        -> {"id", "name", "url"}      (409 if 3 games are running)
    GET  /saved                         -> {"games": [...]}  saved games this account played in
    GET  /daily                         -> today's challenge: {"date", "target", "reference", "board", "best",
                                           "running"}   (see daily.py)
    POST /daily/start   {"fresh"?}      -> {"id", "url"}   the caller's attempt at today's challenge: the one in
                                           progress, or a new one (always a new one with "fresh": true)
    POST /games/resume  {"id"}          -> {"id", "name", "url"}      (works even with 3 games running)
    GET  /laptop                        the page every phone is sent to; phones get nothing else

Per game, under /g/<game-id> (every POST except /join also carries {"player": <token>}):
    GET  /g/<id>                                          the game page
    POST /g/<id>/join          {}                         -> {"token", "pid", "name"}   (name = the username)
                        (409 if 5 players are already in, or this account is already in the game)
    POST /g/<id>/cursor        {"x", "y"}  (null/null hides)  -> {}
    POST /g/<id>/tile/add      {"element", "x", "y"}      -> tile
    POST /g/<id>/tile/grab     {"tile"}                   -> {}   (409 if someone else holds it)
    POST /g/<id>/tile/move     {"tile", "x", "y", "cx", "cy"} -> {}
    POST /g/<id>/tile/drop     {"tile", "x", "y"}         -> {}
    POST /g/<id>/tile/remove   {"tile"}                   -> {}
    POST /g/<id>/tiles/clear   {}                         -> {"cleared"}
    POST /g/<id>/end           {"save": true | false}     -> {"saved"}   ends the game for everyone
                        (saving: 403 unless the caller is the account that created the game)
    POST /g/<id>/combine       {"b": target tile, and "a": held tile or "element",
                                "x", "y"}                 -> {"results", "new", "tiles", "removed"}
    POST /g/<id>/hint          {"kind": "can_make" | "try" | "find", "target"}  -> a private hint
    POST /g/<id>/journal       {}                         -> {"elements": [...]}   every discovered element with the
                                           recipes that make it and use it (only fully discovered ones)
    POST /g/<id>/genie         {"element"}                -> {"results", "new", "tiles", "genie"}
    GET  /g/<id>/events?player=<token>                    -> server-sent events:
        snapshot, players, discovered, genie, cursor, tile_added, tile_grabbed,
        tile_moved, tile_dropped, tile_removed, tiles_cleared ({"pid", "name"} of whoever sent the cat), ended
Positions are fractions (0..1) of the workspace size.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import queue
import re
import signal
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from accounts import AccountStore
from daily import DailyStore
from drafts import DraftStore
from games import IDLE_CLOSE_SECONDS, GameRegistry
from hints import find_element
from hotreload import restore_snapshot, write_snapshot
from recipes import RecipeError, load_recipes
from saves import SaveStore
from state import GameError, GameState

BASE_DIR = Path(__file__).resolve().parent
MAX_BODY = 4096
KEEPALIVE_SECONDS = 3  # a closed browser is noticed on the second failed write, so about 6 s

ACTIONS = ("/end", "/combine", "/cursor", "/genie", "/hint", "/journal", "/tile/add", "/tile/grab", "/tile/move",
           "/tile/drop", "/tile/remove", "/tiles/clear")
# Phones are not supported: they are sent to /laptop and get nothing else.
PHONE_RE = re.compile(r"iPhone|iPod|Windows Phone|Android.+Mobile|Mobile.+Android|\bMobi", re.I)
GAME_PATH = re.compile(r"^/g/([a-z0-9-]+)(/[a-z/]*)?$")

# Some systems (notably Windows registries) map .js to text/plain; be explicit.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def run_action(state: GameState, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    if path == "/combine":
        return state.combine(token, body.get("b"), tile=body.get("a"), element=body.get("element"),
                             x=body.get("x"), y=body.get("y"))
    if path == "/cursor":
        return state.set_cursor(token, body.get("x"), body.get("y"))
    if path == "/tile/add":
        return state.add_tile(token, body.get("element"), body.get("x"), body.get("y"))
    if path == "/tile/grab":
        return state.grab_tile(token, body.get("tile"))
    if path == "/tile/move":
        return state.move_tile(token, body.get("tile"), body.get("x"), body.get("y"),
                               body.get("cx"), body.get("cy"))
    if path == "/tile/drop":
        return state.drop_tile(token, body.get("tile"), body.get("x"), body.get("y"))
    if path == "/tile/remove":
        return state.remove_tile(token, body.get("tile"))
    if path == "/tiles/clear":
        return state.clear_tiles(token)
    if path == "/hint":
        return state.hint(token, body.get("kind"), body.get("target"))
    if path == "/journal":
        return state.journal(token)
    if path == "/genie":
        return state.genie_use(token, body.get("element"))
    raise GameError(404, "not found")


class AlchemyServer(ThreadingHTTPServer):
    daemon_threads = True      # SSE handler threads must not block shutdown
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], registry: GameRegistry, static_dir: str | Path,
                 accounts: AccountStore, listen_fd: int | None = None, drafts: DraftStore | None = None) -> None:
        if listen_fd is None:
            super().__init__(address, Handler)
        else:
            # A reload (see hotreload.py): carry on with the socket the previous copy was listening on.
            super().__init__(address, Handler, bind_and_activate=False)
            self.socket.close()
            self.socket = socket.socket(fileno=listen_fd)
            self.server_address = self.socket.getsockname()
        self.registry = registry
        self.accounts = accounts
        self.drafts = drafts or DraftStore(None)  # draft recipes and promotion requests (memory only if no file)
        self.public_base = ""  # how other players reach this server, e.g. http://192.168.1.20:8000 (set by main)
        self.static_dir = Path(static_dir).resolve()

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            return  # a browser closing a tab is not worth a traceback
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "CoopAlchemy/1.0"
    server: AlchemyServer

    def log_message(self, format: str, *args: Any) -> None:  # keep the console quiet
        pass

    # ---- routing ------------------------------------------------------

    def _block_phone(self, path: str) -> bool:
        """Answer phones with the 'use a laptop' page. Returns True if the request was handled."""
        if not PHONE_RE.search(self.headers.get("User-Agent", "")) or path == "/laptop":
            return False
        if self.command == "GET":
            self.send_response(302)
            self.send_header("Location", "/laptop")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(403, {"error": "please join from a laptop"})
        return True

    def _user(self) -> str:
        """The username of the logged-in caller (401 if there is no valid session)."""
        name = self.server.accounts.username(self.headers.get("X-Session", ""))
        if name is None:
            raise GameError(401, "please log in")
        return name

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if self._block_phone(path):
            return
        try:
            if path == "/laptop":
                self._static("laptop.html")
            elif path == "/login":
                self._static("login.html")
            elif path == "/me":
                username = self._user()
                self._json(200, {"username": username, "admin": self.server.accounts.is_admin(username)})
            elif path == "/create":
                self._static("create.html")
            elif path == "/create/data":
                self._json(200, self._create_data())
            elif path == "/admin":
                self._static("admin.html")
            elif path == "/admin/queue":
                self._admin()
                self._json(200, {"proposals": self.server.drafts.queue(self.server.registry.book)})
            elif path == "/admin/approved":
                self._admin()
                self._json(200, self.server.drafts.approved(self.server.registry.book))
            elif path == "/lan":
                self._user()
                self._json(200, {"base": self.server.public_base})
            elif path in ("/", "/index.html"):
                self._static("home.html")
            elif path == "/games":
                self._user()
                self._json(200, self._game_list())
            elif path == "/saved":
                self._json(200, {"games": self.server.registry.find_saved(self._user())})
            elif path == "/daily":
                self._json(200, self.server.registry.daily_status(self._user()))
            elif path == "/version":
                self._json(200, {"version": self._static_version()})
            elif path == "/images":
                self._json(200, {"images": self._image_names()})
            elif path.startswith("/static/"):
                self._static(unquote(path[len("/static/"):]))
            else:
                match = GAME_PATH.match(path)
                if not match:
                    raise GameError(404, "not found")
                slug, rest = match.group(1), (match.group(2) or "").rstrip("/")
                game = self.server.registry.get(slug)
                if rest == "":
                    if game is None:
                        self._missing_game_page()
                    else:
                        self._static("index.html")
                elif rest == "/events":
                    if game is None:
                        raise GameError(404, "that game does not exist (it may have ended)")
                    self._events(game, parse_qs(parts.query))
                else:
                    raise GameError(404, "not found")
        except GameError as exc:
            self._json(exc.status, {"error": exc.message})

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if self._block_phone(path):
                return
            if path in ("/register", "/login", "/logout"):
                self._account_action(path, self._read_json())
                return
            if path == "/games":
                game = self.server.registry.create(self._read_json().get("name"), self._user())
                self._json(200, {"id": game.slug, "name": game.name, "url": f"/g/{game.slug}"})
                return
            if path == "/daily/start":
                game = self.server.registry.start_daily(self._user(), self._read_json().get("fresh") is True)
                self._json(200, {"id": game.slug, "url": f"/g/{game.slug}"})
                return
            if path == "/games/resume":
                game = self.server.registry.resume(self._read_json().get("id"), self._user())
                self._json(200, {"id": game.slug, "name": game.name, "url": f"/g/{game.slug}"})
                return
            if path.startswith(("/create/", "/admin/")):
                self._json(200, self._drafts_action(path, self._read_json()))
                return
            match = GAME_PATH.match(path)
            rest = match.group(2) if match else None
            if not match or (rest != "/join" and rest not in ACTIONS):
                raise GameError(404, "not found")
            body = self._read_json()
            username = self._user() if rest == "/join" else ""
            if rest == "/end":
                save = body.get("save")
                if not isinstance(save, bool):
                    raise GameError(400, "say whether to save the game")
                self._json(200, self.server.registry.end(match.group(1), body.get("player"), save))
                return
            game = self.server.registry.get(match.group(1))
            if game is None:
                raise GameError(404, "that game does not exist (it may have ended)")
            if rest == "/join":
                player = game.join(username)
                self._json(200, {"token": player.token, "pid": player.pid, "name": player.name})
            else:
                token = body.get("player")
                if not isinstance(token, str):
                    raise GameError(400, "missing player token")
                self._json(200, run_action(game, rest, token, body))
        except GameError as exc:
            self._json(exc.status, {"error": exc.message})

    # ---- helpers ------------------------------------------------------

    def _account_action(self, path: str, body: dict[str, Any]) -> None:
        accounts = self.server.accounts
        if path == "/logout":
            accounts.logout(self.headers.get("X-Session", ""))
            self._json(200, {})
            return
        if path == "/register":
            username = accounts.register(body.get("username"), body.get("password"))
            self._json(200, {"session": accounts.session_for(username), "username": username})
        else:
            session, username = accounts.login(body.get("username"), body.get("password"))
            self._json(200, {"session": session, "username": username})

    def _admin(self) -> str:
        """The username of the logged-in caller, who must be an admin (403 otherwise)."""
        name = self._user()
        if not self.server.accounts.is_admin(name):
            raise GameError(403, "only admins can do that")
        return name

    def _create_data(self) -> dict[str, Any]:
        """Everything the Create Recipes page needs: every real element, plus this account's drafts."""
        username = self._user()
        book = self.server.registry.book
        mine = self.server.drafts.view(username, book)
        return {
            "username": username,
            "elements": [{"id": e.id, "label": e.label, "emoji": e.emoji, "draft": False}
                         for e in book.elements.values()],
            "drafts": mine,
        }

    def _drafts_action(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        drafts, book = self.server.drafts, self.server.registry.book
        if path == "/admin/handle":
            admin = self._admin()
            return drafts.handle(admin, body.get("id"), body.get("action"), body.get("reply"))
        if path == "/admin/reopen":
            return drafts.reopen(self._admin(), body.get("id"))
        username = self._user()
        if path == "/create/combine":
            return {"results": drafts.combine(username, book, body.get("a"), body.get("b"))}
        if path == "/create/recipe":
            return drafts.create(username, book, body.get("a"), body.get("b"), body.get("name"))
        if path == "/create/delete":
            return drafts.delete(username, book, body.get("id"))
        if path == "/create/promote":
            return drafts.promote(username, book, body.get("id"), body.get("note"))
        raise GameError(404, "not found")

    def _game_list(self) -> dict[str, Any]:
        registry = self.server.registry
        return {"games": registry.list(self._user()), "max_games": registry.max_games}

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise GameError(400, "bad Content-Length") from None
        if length <= 0 or length > MAX_BODY:
            raise GameError(400, "request body missing or too large")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GameError(400, "body must be valid JSON") from None
        if not isinstance(data, dict):
            raise GameError(400, "body must be a JSON object")
        return data

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _missing_game_page(self) -> None:
        body = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Game not found</title>"
            "<body style='font:16px system-ui;max-width:30rem;margin:4rem auto;padding:0 1rem'>"
            "<h1>That game isn't running</h1>"
            "<p>It may have been ended, or it closed after sitting empty for a while. "
            "If it was saved, you can bring it back from the home page.</p>"
            f"<p><a href='/'>{html.escape('← Back to all games')}</a></p>"
        ).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static_version(self) -> str:
        """Changes whenever a page, script or stylesheet in static/ changes (pictures are not counted)."""
        digest = hashlib.sha1()
        try:
            for path in sorted(self.server.static_dir.iterdir()):
                if path.is_file():
                    info = path.stat()
                    digest.update(f"{path.name}:{info.st_mtime_ns}:{info.st_size};".encode())
        except OSError:
            pass
        return digest.hexdigest()[:12]

    def _image_names(self) -> list[str]:
        """File names (without .png) in static/elements, so the page only asks for pictures that exist."""
        folder = self.server.static_dir / "elements"
        try:
            return sorted(p.stem for p in folder.glob("*.png") if p.is_file())
        except OSError:
            return []

    def _static(self, relative: str) -> None:
        root = self.server.static_dir
        try:
            target = (root / relative).resolve()
            ok = target.is_relative_to(root) and target.is_file()
        except (OSError, ValueError):
            ok = False
        if not ok:
            raise GameError(404, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Element pictures rarely change; everything else is always re-checked.
        self.send_header("Cache-Control", "max-age=3600" if relative.startswith("elements/") else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _events(self, game: GameState, query: dict[str, list[str]]) -> None:
        sub = game.subscribe(query.get("player", [""])[0])
        if sub is None:
            raise GameError(403, "unknown player")
        self.close_connection = True
        tune_socket(self.connection)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            while not sub.dead:
                try:
                    message = sub.messages.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    message = ": keepalive\n\n"
                if message is None:  # the game was ended; the "ended" event has already gone out
                    break
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
        except OSError:
            pass  # client went away
        finally:
            game.unsubscribe(sub)


def tune_socket(sock: socket.socket) -> None:
    """Make the OS notice vanished clients (Wi-Fi dropped, laptop closed) within ~20 s.

    Without this, writes to a dead connection just sit in a buffer and the player
    would look connected for many minutes. Options a platform lacks are skipped.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)  # macOS name
        for option, value in ((idle, 8), (getattr(socket, "TCP_KEEPINTVL", None), 4),
                              (getattr(socket, "TCP_KEEPCNT", None), 3),
                              (getattr(socket, "TCP_USER_TIMEOUT", None), 15000)):
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError:
        pass


def lan_ip() -> str:
    """Best-effort LAN address (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Co-op Alchemy server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--recipes", default=str(BASE_DIR / "recipes.json"))
    parser.add_argument("--saves-dir", default=str(BASE_DIR / "saves"),
                        help="where saved games are kept for 3 days (default: ./saves)")
    parser.add_argument("--accounts-file", default=str(BASE_DIR / "accounts.json"),
                        help="where usernames and password hashes are kept (default: ./accounts.json)")
    parser.add_argument("--tls-cert", help="certificate file (PEM): serve over HTTPS so passwords are encrypted")
    parser.add_argument("--tls-key", help="private key file (PEM) for --tls-cert")
    parser.add_argument("--drafts-file", default=str(BASE_DIR / "drafts.json"),
                        help="where draft recipes and promotion requests are kept (default: ./drafts.json)")
    parser.add_argument("--daily-file", default=str(BASE_DIR / "daily.json"),
                        help="where the daily challenge's targets and scores are kept (default: ./daily.json)")
    parser.add_argument("--snapshot-file", default=str(BASE_DIR / "reload_snapshot.json"),
                        help="where running games are parked during a reload (default: ./reload_snapshot.json)")
    parser.add_argument("--pid-file", default=str(BASE_DIR / "alchemy.pid"),
                        help="records this server's process id so reload.py can find it (default: ./alchemy.pid)")
    parser.add_argument("--idle-minutes", type=float, default=IDLE_CLOSE_SECONDS / 60,
                        help="close a game this long after its last player leaves (default 10)")
    args = parser.parse_args()

    try:
        book = load_recipes(args.recipes)
    except RecipeError as exc:
        print(exc, file=sys.stderr)
        return 1

    saves = SaveStore(args.saves_dir)
    registry = GameRegistry(book, idle_seconds=args.idle_minutes * 60, saves=saves,
                            daily=DailyStore(args.daily_file, book))
    accounts = AccountStore(args.accounts_file)
    listen_fd = int(os.environ.pop("ALCHEMY_LISTEN_FD")) if "ALCHEMY_LISTEN_FD" in os.environ else None
    try:
        server = AlchemyServer((args.host, args.port), registry, BASE_DIR / "static", accounts, listen_fd,
                               DraftStore(args.drafts_file, is_bot=accounts.is_bot))
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    scheme = "http"
    if bool(args.tls_cert) != bool(args.tls_key):
        print("--tls-cert and --tls-key must be given together", file=sys.stderr)
        return 1
    if args.tls_cert:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            # The handshake then happens in each connection's own thread, not in the accept loop.
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        except (OSError, ssl.SSLError) as exc:
            print(f"cannot use the TLS certificate: {exc}", file=sys.stderr)
            return 1
        scheme = "https"
    else:
        print("note: passwords are sent unencrypted over this network (use --tls-cert/--tls-key for HTTPS).")

    print(f"{len(book.elements)} elements, {len(book.lookup)} recipes loaded.")
    if book.notes:
        print(f"{len(book.notes)} repeated recipe(s) were merged or ignored "
              "(run validate_recipes.py to see which).")
    if find_element(book, "djinn") is None:
        print("note: the recipe file has no 'Djinn' element, so the Djinn feature is switched off.")
    print(f"{saves.purge()} saved game(s) in {args.saves_dir} (kept for 3 days).")
    server.public_base = f"{scheme}://{lan_ip()}:{args.port}"
    restored = restore_snapshot(args.snapshot_file, registry, accounts)
    if restored is not None:
        print(f"Reloaded: {restored[0]} running game(s) and {restored[1]} login(s) carried over.")
    print(f"This machine:  {scheme}://localhost:{args.port}")
    print(f"Other players: {server.public_base}   (same network)")
    print("Press Ctrl+C to stop." + ("  To patch without stopping games: python3 reload.py"
                                      if hasattr(signal, "SIGUSR1") else ""))
    write_pid_file(args.pid_file, args)
    return run(server, registry, accounts, args)


def write_pid_file(path: str, args: argparse.Namespace) -> None:
    """Tell reload.py which process to signal and which files it will need to check."""
    info = {"pid": os.getpid(), "python": sys.executable, "recipes": args.recipes,
            "snapshot": args.snapshot_file}
    try:
        Path(path).write_text(json.dumps(info), encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write {path}: {exc} (reload.py will not work)", file=sys.stderr)


def run(server: AlchemyServer, registry: GameRegistry, accounts: AccountStore, args: argparse.Namespace) -> int:
    """Serve until Ctrl+C. On SIGUSR1 (sent by reload.py), restart in place with the games intact."""
    wanted = threading.Event()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: wanted.set())
    try:
        while True:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            thread.start()
            while not wanted.wait(1.0):
                pass
            wanted.clear()
            server.shutdown()  # stop accepting; new connections wait in the system's queue
            thread.join()
            time.sleep(0.3)    # let requests already under way finish
            try:
                reload_in_place(server, registry, accounts, args)
            except Exception as exc:  # the old code keeps running, nothing is lost
                print(f"reload failed, carrying on with the current code: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
        try:
            info = json.loads(Path(args.pid_file).read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                Path(args.pid_file).unlink()
        except (OSError, ValueError):
            pass
    return 0


def reload_in_place(server: AlchemyServer, registry: GameRegistry, accounts: AccountStore,
                    args: argparse.Namespace) -> None:
    """Park the running games in a snapshot file, then become a fresh copy of this program.

    Same process id and same listening socket; the new copy restores the snapshot at startup.
    Only returns (by raising) if the snapshot could not be written.
    """
    write_snapshot(args.snapshot_file, registry, accounts)
    fd = server.socket.fileno()
    os.set_inheritable(fd, True)
    os.environ["ALCHEMY_LISTEN_FD"] = str(fd)
    print("Reloading...")
    os.execv(sys.executable, [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())

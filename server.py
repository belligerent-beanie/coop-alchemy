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
    GET  /daily                         -> today's three challenges (easy, medium, hard) with the caller's results,
                                           their streak and the weekly board   (see daily.py)
    GET  /daily/history                 -> {"days": [...]}   the latest days: targets, the caller's best, the winner
    POST /daily/start   {"tier", "fresh"?} -> {"id", "url"}   the caller's attempt at today's challenge in that tier:
                                           the one in progress (free), or a new one that uses up one of the 3
                                           attempts a day in that tier (always a new one with "fresh": true; 409
                                           when none are left)
    POST /daily/reveal  {"tier"}        -> {"tier", "route"}   gives up the rest of today's attempts in that tier
                                           and shows the reference route
    GET  /races                         -> {"races": [open lobbies], "mine": id or null, "max_races"}
    POST /races         {"goal"}        -> a new race room (goal "target": first to discover a named element;
                                           goal "count": first to have 100 elements); the caller is its host
    GET  /races/<id>                    -> the room: state (lobby, countdown, running, finished), racers and their
                                           counts, the target, winner, and "game" (the caller's own world)
    POST /races/<id>/join | /leave | /start     (only the host can start, with at least 2 racers)
    GET  /race/<id>                     -> the lobby / results page
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
import ipaddress
import json
import logging
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

from accounts import AccountStore, clean_email
from captcha import ORIGIN as TURNSTILE_ORIGIN
from captcha import Turnstile
from daily import DailyStore
from drafts import DraftStore
from games import IDLE_CLOSE_SECONDS, GameRegistry
from hints import find_element
from hotreload import restore_snapshot, write_snapshot
from invites import InviteStore
from mailer import MailConfigError, Mailer, verification_message
from recipes import RecipeError, load_recipes
from saves import SaveStore
from state import GameError, GameState
from throttle import Throttle, TicketStore, client_ip, parse_networks

BASE_DIR = Path(__file__).resolve().parent
MAX_BODY = 4096
MAX_DRAIN = 65536  # an unread request body up to this size is read and thrown away before the connection closes
KEEPALIVE_SECONDS = 3  # a closed browser is noticed on the second failed write, so about 6 s
CURSOR_BATCH_SECONDS = 0.03  # after a cursor update, the stream waits this long to send the room's pointer moves together

ACTIONS = ("/end", "/combine", "/cursor", "/genie", "/hint", "/journal", "/tile/add", "/tile/grab", "/tile/move",
           "/tile/drop", "/tile/remove", "/tiles/clear")
# Phones are not supported: they are sent to /laptop and get nothing else.
PHONE_RE = re.compile(r"iPhone|iPod|Windows Phone|Android.+Mobile|Mobile.+Android|\bMobi", re.I)
GAME_PATH = re.compile(r"^/g/([a-z0-9-]+)(/[a-z/]*)?$")
RACE_PATH = re.compile(r"^/races/([a-z0-9-]+)(/(?:join|leave|start))?$")

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


log = logging.getLogger("alchemy.auth")


def cursor_pid(message: str) -> str | None:
    """The player a cursor event is about, or None if `message` is any other kind of event."""
    if not message.startswith("event: cursor\n"):
        return None
    try:
        return str(json.loads(message.split("data: ", 1)[1])["pid"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def coalesce_cursors(messages: list[str]) -> list[str]:
    """Keep only each player's newest cursor event in a batch; every other event stays, in order.

    A pointer position is out of date the moment the next one exists, so a stream that is behind should send the latest
    one, not all of them. Hiding a cursor is a cursor event too, so the newest of "moved" and "hidden" always wins.
    """
    pids = [cursor_pid(message) for message in messages]
    newest = {pid: i for i, pid in enumerate(pids) if pid is not None}
    return [message for i, (message, pid) in enumerate(zip(messages, pids)) if pid is None or newest[pid] == i]


def build_csp(turnstile: bool) -> str:
    """The Content-Security-Policy: our own files only, plus Cloudflare's origin when the Turnstile widget is on."""
    extra = f" {TURNSTILE_ORIGIN}" if turnstile else ""
    return (f"default-src 'self'; script-src 'self'{extra}; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; connect-src 'self'{extra}; frame-src 'self'{extra}; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


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
        self.login_throttle = Throttle(20, 60.0)    # password guesses per client address
        self.register_throttle = Throttle(5, 600.0)  # new accounts per client address
        self.invites: InviteStore | None = None      # set by --require-invite: signup then needs a single-use code
        self.captcha: Turnstile | None = None        # set by --turnstile-site-key: signup needs a Turnstile answer
        self.mailer: Mailer | None = None            # set by --require-email-verification: signup needs a confirmed email
        self.public_url = ""                         # how links in emails reach us, e.g. https://alchemy.example.com
        self.resend_throttle = Throttle(10, 3600.0)  # "send the email again" requests per client address
        self.email_throttle = Throttle(3, 3600.0)    # verification emails per mailbox
        self.csp = build_csp(False)
        self.tickets = TicketStore()                 # one-time passes for event streams
        self.trusted_proxies: list = []              # addresses whose X-Forwarded-* headers are believed (--trusted-proxy)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            return  # a browser closing a tab is not worth a traceback
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "CoopAlchemy/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive: a browser reuses one connection for its many small requests (cursors!)
    server: AlchemyServer
    # Seconds a client may stall while sending a request, and also how long an idle kept-alive connection is held open
    # (the event stream only writes, so it is not affected).
    timeout = 30

    SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    )

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", self.server.csp)
        for name, value in self.SECURITY_HEADERS:
            self.send_header(name, value)
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if self.close_connection:
            self.send_header("Connection", "close")  # so the browser does not try to reuse a connection we are about to drop
        super().end_headers()

    def _plan_for_request_body(self) -> None:
        """Decide, before replying, whether this connection may carry another request.

        Keep-alive treats whatever follows a request as the next request, so a body we never read (or cannot read, such
        as a chunked one) would be misread as one. Bodies we do not expect, or that are too big to throw away, end the
        connection instead; small unread POST bodies are discarded after the reply (see _discard_unread_body).
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length < 0 or (length > 0 and self.command != "POST") or length > MAX_DRAIN:
            self.close_connection = True

    def _same_origin(self) -> bool:
        """False if a browser says this request comes from a page on another site."""
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host", "")

    def _client(self) -> str:
        """The caller's address, looking through a trusted reverse proxy (see --trusted-proxy)."""
        peer = str(self.client_address[0])
        return client_ip(peer, self.headers.get("X-Forwarded-For"), self.server.trusted_proxies)

    def _https(self) -> bool:
        """True if the browser reached us over HTTPS: directly, or via a trusted proxy that says so."""
        if self.server.public_base.startswith("https://"):
            return True
        peer = ipaddress.ip_address(self.client_address[0])
        return (any(peer in net for net in self.server.trusted_proxies)
                and self.headers.get("X-Forwarded-Proto", "").lower() == "https")

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
        self._plan_for_request_body()
        parts = urlsplit(self.path)
        path = parts.path
        if self._block_phone(path):
            return
        try:
            if path == "/laptop":
                self._static("laptop.html")
            elif path == "/login":
                self._static("login.html")
            elif path == "/auth-config":
                server = self.server
                self._json(200, {"invite_required": server.invites is not None,
                                 "email_required": server.mailer is not None,
                                 "turnstile_site_key": server.captcha.site_key if server.captcha else None})
            elif path == "/verify":
                self._static("verify.html")
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
            elif path == "/daily/history":
                self._json(200, self.server.registry.daily_history(self._user()))
            elif path == "/races":
                self._json(200, self.server.registry.list_races(self._user()))
            elif path.startswith("/races/"):
                match = RACE_PATH.match(path)
                if not match or match.group(2):
                    raise GameError(404, "not found")
                self._json(200, self.server.registry.race_status(match.group(1), self._user()))
            elif path.startswith("/race/") and re.fullmatch(r"/race/[a-z0-9-]+", path):
                self._static("race.html")
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
                    self._events(game, slug, parse_qs(parts.query))
                else:
                    raise GameError(404, "not found")
        except GameError as exc:
            self._json(exc.status, {"error": exc.message})

    def do_POST(self) -> None:
        self._body_read = False
        self._plan_for_request_body()
        try:
            self._handle_post()
        finally:
            self._discard_unread_body()

    def _discard_unread_body(self) -> None:
        """Read a request body nobody looked at (e.g. we answered 403 or 404 first) so closing does not reset the connection.

        Closing a socket that still holds unread incoming data makes Windows (and sometimes other systems) send a
        reset, which can destroy the reply we just wrote: the browser sees "connection aborted" instead of the error.
        """
        if self._body_read:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_DRAIN:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass  # a bad header or a client that went away: nothing to tidy up

    def _handle_post(self) -> None:
        try:
            path = urlsplit(self.path).path
            if self._block_phone(path):
                return
            if not self._same_origin():
                raise GameError(403, "cross-site request refused")
            if path in ("/register", "/login", "/logout", "/verify-email", "/resend-verification"):
                self._account_action(path, self._read_json())
                return
            if path == "/games":
                game = self.server.registry.create(self._read_json().get("name"), self._user())
                self._json(200, {"id": game.slug, "name": game.name, "url": f"/g/{game.slug}"})
                return
            if path == "/daily/start":
                body = self._read_json()
                game = self.server.registry.start_daily(self._user(), body.get("tier"), body.get("fresh") is True)
                self._json(200, {"id": game.slug, "url": f"/g/{game.slug}"})
                return
            if path == "/daily/reveal":
                self._json(200, self.server.registry.reveal_daily(self._user(), self._read_json().get("tier")))
                return
            if path == "/races":
                body = self._read_json()
                self._json(200, self.server.registry.create_race(self._user(), body.get("goal")))
                return
            race = RACE_PATH.match(path)
            if race and race.group(2):
                registry, user = self.server.registry, self._user()
                self._read_json()
                action = {"/join": registry.join_race, "/leave": registry.leave_race,
                          "/start": registry.start_race}[race.group(2)]
                self._json(200, action(race.group(1), user))
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
            if not match or (rest not in ("/join", "/ticket") and rest not in ACTIONS):
                raise GameError(404, "not found")
            body = self._read_json()
            if rest == "/ticket":
                self._json(200, {"ticket": self._event_ticket(match.group(1), body.get("player"))})
                return
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

    def _event_ticket(self, slug: str, token: object) -> str:
        """A one-time pass for GET /g/<id>/events, so the player token itself never appears in a URL."""
        game = self.server.registry.get(slug)
        if game is None:
            raise GameError(404, "that game does not exist (it may have ended)")
        player = game.authorize(token)
        return self.server.tickets.issue(slug, player.token)

    def _register(self, body: dict[str, Any]) -> str:
        """Create an account, spending an invite code first if signup requires one."""
        accounts, invites = self.server.accounts, self.server.invites
        email = None
        if self.server.mailer is not None:
            if body.get("email") is None:
                raise GameError(400, "an email address is needed to create an account")
            email = body.get("email")
        if invites is None:
            return accounts.register(body.get("username"), body.get("password"), email)
        handle = invites.claim(body.get("invite"))  # 403 unless it is a valid, unused code
        try:
            username = accounts.register(body.get("username"), body.get("password"), email)
        except GameError:
            invites.release(handle)  # a typo or a taken name should not burn the code
            raise
        invites.finish(handle, username)
        return username

    def _account_action(self, path: str, body: dict[str, Any]) -> None:
        accounts = self.server.accounts
        if path == "/logout":
            accounts.logout(self.headers.get("X-Session", ""))
            self._json(200, {})
            return
        client = self._client()
        if path == "/resend-verification":
            self._resend_verification(body, client)
            return
        if path == "/verify-email":
            if self.server.login_throttle.allow(client):
                raise GameError(429, "too many attempts from this address; try again in a minute")
            username = accounts.verify_email(body.get("token"))
            log.info("email verified client=%s user=%s", client, username)
            self._json(200, {"username": username})
            return
        if path in ("/register", "/login"):
            throttle = self.server.register_throttle if path == "/register" else self.server.login_throttle
            wait = throttle.allow(client)
            if wait:
                log.warning("%s throttled client=%s", path[1:], client)
                raise GameError(429, f"too many attempts from this address; try again in {int(wait) + 1} seconds")
        if path == "/register" and self.server.captcha is not None:
            self.server.captcha.verify(body.get("captcha"), client)  # before anything else: bots learn nothing
        try:
            if path == "/register" and self.server.mailer is not None:
                username = self._register(body)
                self._send_verification(username)
                self._json(200, {"verify_email": True, "username": username})
            elif path == "/register":
                username = self._register(body)
                self._json(200, {"session": accounts.session_for(username), "username": username})
            else:
                session, username = accounts.login(body.get("username"), body.get("password"), client)
                self._json(200, {"session": session, "username": username})
        except GameError as exc:
            if path == "/login":
                log.warning("login failed status=%s client=%s user=%s", exc.status, client,
                            str(body.get("username"))[:40].encode("ascii", "replace").decode())
            raise
        log.info("%s ok client=%s user=%s", path[1:], client, username)

    def _send_verification(self, username: str) -> None:
        """Email a fresh confirmation link to a pending account (limited per mailbox)."""
        found = self.server.accounts.begin_verification(username)
        self._mail_link(found)

    def _mail_link(self, found: tuple[str, str] | None) -> None:
        if found is None:
            return
        email, token = found
        if self.server.email_throttle.allow(clean_email(email)[1]):
            return  # this mailbox already got its quota; stay quiet rather than reveal anything
        # The token sits after the # so it is never sent to a server, logged by a proxy or leaked in a Referer header.
        subject, text = verification_message(f"{self.server.public_url}/verify#{token}")
        assert self.server.mailer is not None
        self.server.mailer.send(email, subject, text)

    def _resend_verification(self, body: dict[str, Any], client: str) -> None:
        """'Send me the email again'. Always answers the same, so it cannot be used to find out who has an account."""
        if self.server.mailer is None:
            raise GameError(404, "not found")
        wait = self.server.resend_throttle.allow(client)
        if wait:
            raise GameError(429, f"too many requests from this address; try again in {int(wait) + 1} seconds")
        self._mail_link(self.server.accounts.begin_verification_for_email(body.get("email")))
        self._json(200, {"ok": True})

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
        self._body_read = True
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

    def _events(self, game: GameState, slug: str, query: dict[str, list[str]]) -> None:
        token = self.server.tickets.redeem(query.get("ticket", [""])[0], slug)
        sub = game.subscribe(token) if token else None
        if sub is None:
            raise GameError(403, "unknown player")
        self.close_connection = True
        tune_socket(self.connection)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")  # no Content-Length: the stream ends when the connection does
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            while not sub.dead:
                try:
                    message = sub.messages.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    message = ": keepalive\n\n"
                if message is None:  # the game was ended; the "ended" event has already gone out
                    break
                batch = [message]
                if cursor_pid(message) is not None:
                    time.sleep(CURSOR_BATCH_SECONDS)  # pointer moves are frequent: gather the room's moves into one write
                ended = False
                while True:  # whatever else is already waiting goes out in the same write
                    try:
                        more = sub.messages.get_nowait()
                    except queue.Empty:
                        break
                    if more is None:
                        ended = True
                        break
                    batch.append(more)
                self.wfile.write("".join(coalesce_cursors(batch)).encode("utf-8"))
                self.wfile.flush()
                if ended:
                    break
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
    parser.add_argument("--trusted-proxy", action="append", default=[], metavar="IP_OR_CIDR",
                        help="a reverse proxy in front of this server (repeatable, e.g. 127.0.0.1): its "
                             "X-Forwarded-For / X-Forwarded-Proto headers are believed, so rate limits see real client addresses")
    parser.add_argument("--require-invite", action="store_true",
                        help="signup needs a single-use invite code (make them with accounts_admin.py invite create)")
    parser.add_argument("--invites-file", default=str(BASE_DIR / "invites.json"),
                        help="where invite codes are kept, hashed (default: ./invites.json)")
    parser.add_argument("--turnstile-site-key", default=os.environ.get("TURNSTILE_SITE_KEY", ""),
                        help="Cloudflare Turnstile site key: signup then needs the human check "
                             "(the secret key goes in the TURNSTILE_SECRET_KEY environment variable)")
    parser.add_argument("--require-email-verification", action="store_true",
                        help="signup asks for an email address and the account works only after its link is opened "
                             "(needs --public-url and the SMTP_* environment variables; see mailer.py)")
    parser.add_argument("--public-url", default="",
                        help="the site's public address, used in emailed links (e.g. https://alchemy.example.com)")
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
    if args.require_invite:
        server.invites = InviteStore(args.invites_file)
    if args.turnstile_site_key:
        secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
        if not secret:
            print("--turnstile-site-key needs the secret key in the TURNSTILE_SECRET_KEY environment variable",
                  file=sys.stderr)
            return 1
        server.captcha = Turnstile(args.turnstile_site_key, secret)
        server.csp = build_csp(True)
    if args.require_email_verification:
        if not args.public_url.startswith(("http://", "https://")):
            print("--require-email-verification needs --public-url (e.g. https://alchemy.example.com)", file=sys.stderr)
            return 1
        try:
            server.mailer = Mailer.from_env(os.environ)
        except MailConfigError as exc:
            print(f"--require-email-verification: {exc}", file=sys.stderr)
            return 1
        server.public_url = args.public_url.rstrip("/")
    try:
        server.trusted_proxies = parse_networks(args.trusted_proxy)
    except ValueError as exc:
        print(f"--trusted-proxy: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
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
    elif not server.trusted_proxies:
        print("note: passwords are sent unencrypted over this network (use --tls-cert/--tls-key for HTTPS).")

    if server.captcha is not None:
        print("Signup needs the Turnstile human check.")
    if server.mailer is not None:
        print(f"Signup needs a confirmed email address (links point to {server.public_url}).")
    if server.invites is not None:
        print("Signup needs an invite code. Make some with: python3 accounts_admin.py invite create --count 5")
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

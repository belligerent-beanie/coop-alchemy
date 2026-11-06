"""Hardening: rate limits, per-client lockout, security headers and the cross-site check."""

from __future__ import annotations

import email.message
import http.client
import io
import tempfile
import unittest
from pathlib import Path

from accounts import AccountStore
from state import GameError
from games import GameRegistry
from test_coop import RECIPES, call, load_recipes, serve
from server import MAX_DRAIN, Handler
from throttle import Throttle, TicketStore, client_ip, parse_networks


class ThrottleTests(unittest.TestCase):
    def test_blocks_after_the_limit_and_recovers(self) -> None:
        now = [0.0]
        throttle = Throttle(2, 10.0, clock=lambda: now[0])
        self.assertEqual([throttle.allow("a"), throttle.allow("a")], [0.0, 0.0])
        self.assertGreater(throttle.allow("a"), 0)
        self.assertEqual(throttle.allow("b"), 0.0)  # other clients are unaffected
        now[0] = 10.0
        self.assertEqual(throttle.allow("a"), 0.0)


class TicketTests(unittest.TestCase):
    def test_tickets_work_once_for_their_game_and_expire(self) -> None:
        now = [0.0]
        store = TicketStore(ttl=30.0, clock=lambda: now[0])
        ticket = store.issue("game-a", "tok")
        self.assertIsNone(store.redeem(ticket, "game-b"))  # wrong game (and now used up)
        ticket = store.issue("game-a", "tok")
        self.assertEqual(store.redeem(ticket, "game-a"), "tok")
        self.assertIsNone(store.redeem(ticket, "game-a"))  # one use only
        old = store.issue("game-a", "tok")
        now[0] = 31.0
        self.assertIsNone(store.redeem(old, "game-a"))
        self.assertIsNone(store.redeem(None, "game-a"))


class ClientIpTests(unittest.TestCase):
    PROXY = parse_networks(["127.0.0.1", "10.0.0.0/8"])

    def test_forwarded_header_is_ignored_unless_the_peer_is_a_trusted_proxy(self) -> None:
        self.assertEqual(client_ip("203.0.113.9", "1.2.3.4", self.PROXY), "203.0.113.9")
        self.assertEqual(client_ip("127.0.0.1", "198.51.100.7", self.PROXY), "198.51.100.7")
        self.assertEqual(client_ip("127.0.0.1", None, self.PROXY), "127.0.0.1")
        self.assertEqual(client_ip("127.0.0.1", "198.51.100.7", []), "127.0.0.1")

    def test_a_single_address_header_set_by_the_proxy_wins_over_x_forwarded_for(self) -> None:
        self.assertEqual(client_ip("127.0.0.1", "6.6.6.6, 9.9.9.9", self.PROXY, "198.51.100.7"), "198.51.100.7")
        self.assertEqual(client_ip("127.0.0.1", None, self.PROXY, " 2001:db8::5 "), "2001:db8::5")

    def test_that_header_is_ignored_unless_the_peer_is_a_trusted_proxy(self) -> None:
        self.assertEqual(client_ip("203.0.113.9", None, self.PROXY, "198.51.100.7"), "203.0.113.9")
        self.assertEqual(client_ip("127.0.0.1", None, [], "198.51.100.7"), "127.0.0.1")

    def test_a_malformed_or_multiple_value_falls_back_to_the_other_rules(self) -> None:
        self.assertEqual(client_ip("127.0.0.1", "198.51.100.7", self.PROXY, "not-an-ip"), "198.51.100.7")
        self.assertEqual(client_ip("127.0.0.1", None, self.PROXY, "1.2.3.4, 5.6.7.8"), "127.0.0.1")
        self.assertEqual(client_ip("127.0.0.1", None, self.PROXY, ""), "127.0.0.1")

    def test_a_client_cannot_choose_its_address_by_sending_its_own_header(self) -> None:
        # our proxy appended the real address on the right; the left entry is whatever the client claimed
        self.assertEqual(client_ip("127.0.0.1", "6.6.6.6, 198.51.100.7", self.PROXY), "198.51.100.7")
        self.assertEqual(client_ip("127.0.0.1", "198.51.100.7, 10.1.2.3", self.PROXY), "198.51.100.7")
        self.assertEqual(client_ip("127.0.0.1", "not-an-ip", self.PROXY), "127.0.0.1")


class LockoutTests(unittest.TestCase):
    def test_a_stranger_cannot_lock_you_out(self) -> None:
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000)
        accounts.register("alice", "correct horse")
        for _ in range(5):
            with self.assertRaises(GameError):
                accounts.login("alice", "wrong guess", client="10.0.0.9")
        with self.assertRaises(GameError) as ctx:
            accounts.login("alice", "correct horse", client="10.0.0.9")
        self.assertEqual(ctx.exception.status, 429)
        self.assertTrue(accounts.login("alice", "correct horse", client="10.0.0.5")[0])


class UnreadBodyTests(unittest.TestCase):
    """Closing a connection with an unread request body can reset it on Windows and lose our error reply."""

    @staticmethod
    def handler(body: bytes, length: object, read: bool) -> tuple[Handler, io.BytesIO]:
        handler = Handler.__new__(Handler)  # no socket: only the pieces the method touches
        handler.rfile = io.BytesIO(body)
        handler.headers = email.message.Message()
        if length is not None:
            handler.headers["Content-Length"] = str(length)
        handler._body_read = read
        return handler, handler.rfile

    def test_an_unread_body_is_read_and_thrown_away(self) -> None:
        handler, rfile = self.handler(b"x" * 500, 500, read=False)
        handler._discard_unread_body()
        self.assertEqual(rfile.tell(), 500)

    def test_a_body_the_handler_already_read_is_left_alone(self) -> None:
        handler, rfile = self.handler(b"x" * 500, 500, read=True)
        handler._discard_unread_body()
        self.assertEqual(rfile.tell(), 0)

    def test_huge_or_nonsense_lengths_are_not_waited_for(self) -> None:
        for length in (MAX_DRAIN + 1, 10**9, "abc", -5, 0, None):
            handler, rfile = self.handler(b"x" * 100, length, read=False)
            handler._discard_unread_body()  # must neither raise nor block
            self.assertEqual(rfile.tell(), 0, length)

    def test_a_client_that_vanished_mid_body_is_tolerated(self) -> None:
        handler, _ = self.handler(b"", 100, read=False)
        handler.rfile = type("Broken", (), {"read": lambda self, n: (_ for _ in ()).throw(ConnectionResetError())})()
        handler._discard_unread_body()


class ServerHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=5))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_console_stays_quiet(self) -> None:
        from http.server import BaseHTTPRequestHandler
        from server import Handler
        self.assertIsNot(Handler.log_message, BaseHTTPRequestHandler.log_message)  # no per-request access lines

    def test_error_replies_to_unread_posts_are_never_aborted(self) -> None:
        """Before the fix about 2% of these died with 'connection aborted' on Windows (a flaky test_coop)."""
        phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}
        for _ in range(300):
            self.assertEqual(call(self.port, "POST", "/games", {"name": "x" * 40, "player": "t" * 30}, headers=phone)[0], 403)
            self.assertEqual(call(self.port, "POST", "/g/nope/elsewhere", {"a": "b" * 60})[0], 404)

    def test_responses_carry_security_headers(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/login")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")

    def test_cross_site_posts_are_refused(self) -> None:
        status, _ = call(self.port, "POST", "/login", {"username": "x", "password": "y"},
                         headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = call(self.port, "POST", "/login", {"username": "x", "password": "y"},
                         headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 401)

    def test_event_streams_need_a_ticket_and_it_works_once(self) -> None:
        status, _ = call(self.port, "POST", "/games", {"name": "ticketed"})
        self.assertEqual(status, 200)
        status, joined = call(self.port, "POST", "/g/ticketed/join", {})
        self.assertEqual(status, 200)
        token = joined["token"]
        self.assertEqual(call(self.port, "POST", "/g/ticketed/ticket", {"player": "bogus"})[0], 403)
        status, data = call(self.port, "POST", "/g/ticketed/ticket", {"player": token})
        self.assertEqual(status, 200)
        for path, expected in ((f"/g/ticketed/events?player={token}", 403),   # a bare token is no longer accepted
                               (f"/g/ticketed/events?ticket={data['ticket']}", 200),
                               (f"/g/ticketed/events?ticket={data['ticket']}", 403)):  # already used
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            self.assertEqual(resp.status, expected, path)
            conn.close()

    def test_behind_a_cloudflare_style_proxy_each_visitor_has_their_own_login_limit(self) -> None:
        server = self.server
        saved = (server.trusted_proxies, server.client_ip_header, server.login_throttle)
        self.addCleanup(lambda: (setattr(server, "trusted_proxies", saved[0]), setattr(server, "client_ip_header", saved[1]),
                                 setattr(server, "login_throttle", saved[2])))
        server.trusted_proxies = parse_networks(["127.0.0.1"])  # the tunnel's connection arrives from here
        server.client_ip_header = "CF-Connecting-IP"
        server.login_throttle = Throttle(2, 60.0)

        def attempt(visitor: str) -> int:
            return call(self.port, "POST", "/login", {"username": "x", "password": "y"},
                        headers={"CF-Connecting-IP": visitor, "X-Session": ""})[0]

        self.assertEqual([attempt("198.51.100.1") for _ in range(3)], [401, 401, 429])  # one visitor hits their limit
        self.assertEqual(attempt("198.51.100.2"), 401)  # a different visitor is unaffected
        self.assertEqual(call(self.port, "POST", "/login", {"username": "x", "password": "y"},
                              headers={"X-Session": ""})[0], 401)  # without the header the proxy's own address is used

    def test_login_is_rate_limited_per_address(self) -> None:
        statuses = {call(self.port, "POST", "/login", {"username": f"u{i}", "password": "badbadbad"})[0]
                    for i in range(25)}
        self.assertIn(429, statuses)


if __name__ == "__main__":
    unittest.main()

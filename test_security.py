"""Hardening: rate limits, per-client lockout, security headers and the cross-site check."""

from __future__ import annotations

import email.message
import http.client
import io
import socket
import tempfile
import time
import unittest
from pathlib import Path

import accounts as accounts_module
import hotreload
from accounts import AccountStore
from commonpasswords import is_common, load_common
from state import GameError
from games import GameRegistry
from test_coop import RECIPES, call, load_recipes, login_as, serve
from server import MAX_DRAIN, Handler, broad_proxy_trust, log_safe
from ratelimit import ACTION_BURST, ACTION_RATE, MOTION_BURST, MOTION_RATE, TokenBuckets
from throttle import Throttle, TicketStore, client_ip, parse_networks, throttle_key


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


class ProxyTrustWarningTests(unittest.TestCase):
    def test_only_whole_ranges_trigger_the_note(self) -> None:
        self.assertFalse(broad_proxy_trust(parse_networks(["127.0.0.1", "::1", "10.1.2.3"])))
        self.assertFalse(broad_proxy_trust([]))
        self.assertTrue(broad_proxy_trust(parse_networks(["172.16.0.0/12"])))
        self.assertTrue(broad_proxy_trust(parse_networks(["127.0.0.1", "192.168.0.0/16"])))


class LogSafeTests(unittest.TestCase):
    def test_line_breaks_and_odd_characters_cannot_make_a_new_log_line(self) -> None:
        forged = "x\n2026-11-12 alchemy.auth login failed\r\x1b[2Jé\u2028"
        safe = log_safe(forged)
        self.assertEqual(len(safe.splitlines()), 1)
        self.assertTrue(safe.isascii() and safe.isprintable())
        self.assertLessEqual(len(log_safe("a" * 500)), 40)
        self.assertEqual(log_safe("Alice 2_b"), "Alice 2_b")
        self.assertEqual(log_safe(None), "None")


class TokenBucketTests(unittest.TestCase):
    def test_a_burst_is_allowed_then_refused_then_refills(self) -> None:
        now = [0.0]
        buckets = TokenBuckets(2.0, 3, clock=lambda: now[0])
        self.assertEqual([buckets.allow("a") for _ in range(5)], [True, True, True, False, False])
        now[0] = 0.5   # one request's worth has come back
        self.assertEqual([buckets.allow("a"), buckets.allow("a")], [True, False])
        now[0] = 100.0   # a long rest saves up only the burst
        self.assertEqual([buckets.allow("a") for _ in range(4)], [True, True, True, False])

    def test_keys_are_independent_and_the_table_stays_bounded(self) -> None:
        now = [0.0]
        buckets = TokenBuckets(1.0, 1, clock=lambda: now[0], limit=10)
        self.assertTrue(buckets.allow("a"))
        self.assertFalse(buckets.allow("a"))
        self.assertTrue(buckets.allow("b"))
        for i in range(100):
            buckets.allow(f"k{i}")
        self.assertLessEqual(len(buckets._buckets), 10)

    def test_honest_play_never_meets_the_limits(self) -> None:
        """What the page really does, replayed on a clock: 20 cursor updates a second all the time, tile moves every 40 ms
        for the first second of every 5, a click every half second, and a burst of ten quick actions every 20 seconds."""
        motion_times = [i * 0.05 for i in range(20 * 60)]
        motion_times += [t + i * 0.04 for t in range(0, 60, 5) for i in range(25)]
        action_times = [i * 0.5 for i in range(120)] + [t + i * 0.01 for t in range(0, 60, 20) for i in range(10)]
        now = [0.0]
        motion = TokenBuckets(MOTION_RATE, MOTION_BURST, clock=lambda: now[0])
        action = TokenBuckets(ACTION_RATE, ACTION_BURST, clock=lambda: now[0])
        for bucket, times in ((motion, motion_times), (action, action_times)):
            now[0] = 0.0
            for moment in sorted(times):
                now[0] = moment
                self.assertTrue(bucket.allow("p"), moment)

    def test_a_flood_is_cut_off(self) -> None:
        now = [0.0]
        action = TokenBuckets(ACTION_RATE, ACTION_BURST, clock=lambda: now[0])
        allowed = sum(action.allow("p") for _ in range(1000))   # 1000 requests in one instant
        self.assertEqual(allowed, ACTION_BURST)


class ThrottleKeyTests(unittest.TestCase):
    def test_ipv4_is_used_as_it_is(self) -> None:
        self.assertEqual(throttle_key("203.0.113.9"), "203.0.113.9")

    def test_an_ipv4_mapped_ipv6_address_counts_as_the_ipv4_address(self) -> None:
        self.assertEqual(throttle_key("::ffff:203.0.113.9"), "203.0.113.9")

    def test_every_address_in_one_ipv6_64_shares_a_key(self) -> None:
        self.assertEqual(throttle_key("2001:db8:1:2::1"), throttle_key("2001:db8:1:2:ffff:ffff:ffff:ffff"))
        self.assertNotEqual(throttle_key("2001:db8:1:2::1"), throttle_key("2001:db8:1:3::1"))

    def test_text_that_is_not_an_address_is_left_alone(self) -> None:
        self.assertEqual(throttle_key("not-an-ip"), "not-an-ip")

    def test_a_visitor_cannot_get_a_fresh_limit_by_changing_the_end_of_an_ipv6_address(self) -> None:
        throttle = Throttle(3, 60.0)
        waits = [throttle.allow(throttle_key(f"2001:db8:aa:bb::{i:x}")) for i in range(1, 6)]
        self.assertEqual(waits[:3], [0.0, 0.0, 0.0])
        self.assertTrue(all(wait > 0 for wait in waits[3:]))


class AccountFailureCapTests(unittest.TestCase):
    def make(self) -> tuple[AccountStore, list[float]]:
        now = [1000.0]
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000, clock=lambda: now[0])
        accounts.register("alice", "correct horse battery")
        return accounts, now

    def fail(self, accounts: AccountStore, client: str) -> int:
        try:
            accounts.login("alice", "wrong guess", client=client)
        except GameError as exc:
            return exc.status
        return 200

    def test_guessing_spread_over_many_visitors_is_stopped_for_the_account(self) -> None:
        accounts, now = self.make()
        limit = accounts_module.ACCOUNT_MAX_FAILURES
        for i in range(limit):   # every guess from a different visitor: no single visitor is ever locked
            self.assertEqual(self.fail(accounts, f"10.0.{i // 250}.{i % 250}"), 401)
        self.assertEqual(self.fail(accounts, "10.9.9.9"), 429)
        with self.assertRaises(GameError) as ctx:   # even the right password waits
            accounts.login("alice", "correct horse battery", client="10.8.8.8")
        self.assertEqual(ctx.exception.status, 429)
        now[0] += accounts_module.ACCOUNT_PAUSE_SECONDS + 1
        self.assertTrue(accounts.login("alice", "correct horse battery", client="10.8.8.8")[0])

    def test_other_accounts_are_not_paused(self) -> None:
        accounts, _ = self.make()
        accounts.register("bobby", "another long password")
        for i in range(accounts_module.ACCOUNT_MAX_FAILURES):
            self.fail(accounts, f"10.1.0.{i}")
        self.assertTrue(accounts.login("bobby", "another long password", client="10.8.8.8")[0])

    def test_old_failures_stop_counting(self) -> None:
        accounts, now = self.make()
        for i in range(accounts_module.ACCOUNT_MAX_FAILURES - 1):
            self.fail(accounts, f"10.2.0.{i}")
        now[0] += accounts_module.ACCOUNT_WINDOW_SECONDS + 1
        self.assertEqual(self.fail(accounts, "10.2.9.9"), 401)

    def test_flooding_the_failure_table_does_not_reset_a_live_lockout(self) -> None:
        accounts, _ = self.make()
        for _ in range(accounts_module.MAX_FAILURES):
            self.fail(accounts, "10.3.0.1")
        self.assertEqual(self.fail(accounts, "10.3.0.1"), 429)   # locked for this visitor
        for i in range(accounts_module.MAX_TRACKED_FAILURES + 50):
            try:
                accounts.login(f"nobody{i}", "wrong guess", client="10.4.0.1")
            except GameError:
                pass
        self.assertEqual(self.fail(accounts, "10.3.0.1"), 429)   # still locked


class SessionLifetimeTests(unittest.TestCase):
    def make(self) -> tuple[AccountStore, list[float]]:
        now = [1000.0]
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000, clock=lambda: now[0])
        accounts.register("alice", "correct horse battery")
        return accounts, now

    @staticmethod
    def use_until(accounts: AccountStore, now: list[float], token: str, seconds: float, check: bool = True) -> None:
        """Move the clock forward by `seconds`, using the session at least every 23 hours on the way so it never goes idle."""
        end = now[0] + seconds
        while end - now[0] > 23 * 3600:
            now[0] += 23 * 3600
            if check:
                assert accounts.username(token) == "alice"
            else:
                accounts.username(token)
        now[0] = end

    def test_a_session_used_every_day_still_ends_after_the_maximum_age(self) -> None:
        accounts, now = self.make()
        token = accounts.login("alice", "correct horse battery", client="10.0.0.1")[0]
        self.use_until(accounts, now, token, accounts_module.SESSION_MAX_SECONDS - 60)
        self.assertEqual(accounts.username(token), "alice")      # just under the maximum
        now[0] += 120
        self.assertIsNone(accounts.username(token))              # just over it, though it was used a moment ago
        self.assertEqual(accounts.session_count(), 0)
        self.assertTrue(accounts.login("alice", "correct horse battery", client="10.0.0.1")[0])   # logging in again works

    def test_the_idle_rule_still_applies(self) -> None:
        accounts, now = self.make()
        token = accounts.login("alice", "correct horse battery", client="10.0.0.1")[0]
        now[0] += accounts_module.SESSION_SECONDS + 1
        self.assertIsNone(accounts.username(token))

    def test_a_reload_keeps_when_the_session_was_made(self) -> None:
        accounts, now = self.make()
        token = accounts.login("alice", "correct horse battery", client="10.0.0.1")[0]
        self.use_until(accounts, now, token, 13 * 86400)
        self.assertEqual(accounts.username(token), "alice")      # used on day 13
        exported = accounts.export_sessions()
        self.assertEqual(exported[0]["made"], 13 * 86400)
        fresh = AccountStore(accounts.path, iterations=1000, clock=lambda: now[0])
        self.assertEqual(fresh.import_sessions(exported), 1)
        self.assertEqual(fresh.username(token), "alice")
        self.use_until(fresh, now, token, 2 * 86400, check=False)   # day 15, with the session used on the way
        self.assertIsNone(fresh.username(token))

    def test_a_snapshot_from_before_the_maximum_age_existed_still_loads(self) -> None:
        accounts, now = self.make()
        token = accounts.login("alice", "correct horse battery", client="10.0.0.1")[0]
        old_style = [{k: v for k, v in item.items() if k != "made"} for item in accounts.export_sessions()]
        fresh = AccountStore(accounts.path, iterations=1000, clock=lambda: now[0])
        self.assertEqual(fresh.import_sessions(old_style), 1)
        self.assertEqual(fresh.username(token), "alice")

    def test_nonsense_in_a_snapshot_is_skipped(self) -> None:
        accounts, _ = self.make()
        bad = [{"token": "t", "key": "alice", "salt": "s", "age": 1.0, "made": "soon"},
               {"token": "u", "key": "alice", "salt": "s", "age": 100.0, "made": 50.0},   # made after it was last used: impossible
               {"token": "v", "key": "alice", "salt": "s", "age": 1.0, "made": 10**9}]
        self.assertEqual(accounts.import_sessions(bad), 0)


class CommonPasswordTests(unittest.TestCase):
    def test_the_list_is_there_and_catches_the_usual_suspects(self) -> None:
        self.assertGreater(len(load_common()), 10_000)
        for weak in ("password123456", "Password123456", "PASSWORD123456", "1234567890", "qwertyuiop", "aaaaaaaaaaaa", "abababababab",
                     "abcdefghijkl", "9876543210"):
            self.assertTrue(is_common(weak), weak)

    def test_ordinary_good_passwords_pass(self) -> None:
        for fine in ("correct horse battery", "my-own-fresh-passphrase", "k7#Qv!2mRt9z", "purple giraffes eat 4 pies"):
            self.assertFalse(is_common(fine), fine)

    def test_a_missing_list_file_switches_the_list_check_off_but_not_the_pattern_check(self) -> None:
        empty = frozenset()
        self.assertFalse(is_common("password123456", empty))
        self.assertTrue(is_common("aaaaaaaaaaaa", empty))

    def test_new_passwords_on_the_list_are_refused_everywhere_a_password_is_chosen(self) -> None:
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000)
        with self.assertRaises(GameError) as ctx:
            accounts.register("weakling", "Password123456")
        self.assertEqual(ctx.exception.status, 400)
        accounts.register("sturdy", "k7#Qv!2mRt9z")
        with self.assertRaises(GameError):
            accounts.change_password("sturdy", "k7#Qv!2mRt9z", "1234567890")
        with self.assertRaises(GameError):
            accounts.set_password("sturdy", "qwertyuiop")
        self.assertTrue(accounts.login("sturdy", "k7#Qv!2mRt9z", client="10.0.0.1")[0])   # unchanged

    def test_an_old_account_whose_password_is_on_the_list_can_still_log_in_and_must_pick_a_better_one_to_change(self) -> None:
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000)
        records = accounts._load()
        records["oldtimer"] = {"name": "oldtimer", "salt": "cd" * 16, "hash": accounts._hash("Password123456", "cd" * 16, 1000),
                               "iterations": 1000, "created": 1.0}
        accounts._save(records)
        self.assertTrue(accounts.login("oldtimer", "Password123456", client="10.0.0.1")[0])
        with self.assertRaises(GameError):
            accounts.change_password("oldtimer", "Password123456", "iloveyou1234")
        self.assertTrue(accounts.change_password("oldtimer", "Password123456", "a much better one 42"))


class PasswordLengthTests(unittest.TestCase):
    def test_new_passwords_need_the_new_minimum_but_old_short_ones_still_log_in(self) -> None:
        accounts = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json", iterations=1000)
        with self.assertRaises(GameError):
            accounts.register("shorty", "123456789")   # 9 characters
        accounts.register("longer", "lemon-ocean")
        # an account made when 8 characters were enough: written straight into the file
        records = accounts._load()
        old = accounts._hash("12345678", "ab" * 16, 1000)
        records["oldie"] = {"name": "oldie", "salt": "ab" * 16, "hash": old, "iterations": 1000, "created": 1.0}
        accounts._save(records)
        self.assertTrue(accounts.login("oldie", "12345678", client="10.0.0.1")[0])
        with self.assertRaises(GameError):
            accounts.change_password("oldie", "12345678", "87654321", client="10.0.0.1")


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
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=60))

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
        cross_site = {"Origin": "http://evil.example"}  # refused with a 403 before the body is read, like any early error
        for _ in range(300):
            self.assertEqual(call(self.port, "POST", "/games", {"name": "x" * 40, "player": "t" * 30}, headers=cross_site)[0], 403)
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
        self.assertEqual(resp.getheader("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertIn("camera=()", resp.getheader("Permissions-Policy"))

    def test_the_extra_headers_are_on_json_replies_and_the_event_stream_too(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Cross-Origin-Opener-Policy"), "same-origin")
        token = self.join("headers-stream")
        stream = self.open_stream("headers-stream", self.ticket("headers-stream", token))
        head = stream.recv(4096).decode("latin-1").lower()
        self.assertIn("cross-origin-opener-policy: same-origin", head)
        self.assertIn("permissions-policy:", head)

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

    # ---- event stream limits -------------------------------------------------------------------

    def join(self, name: str) -> str:
        self.assertEqual(call(self.port, "POST", "/games", {"name": name})[0], 200)
        status, joined = call(self.port, "POST", f"/g/{name}/join", {})
        self.assertEqual(status, 200)
        return joined["token"]

    def ticket(self, name: str, token: str) -> str:
        status, data = call(self.port, "POST", f"/g/{name}/ticket", {"player": token})
        self.assertEqual(status, 200)
        return data["ticket"]

    def open_stream(self, name: str, ticket: str) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=8)
        sock.sendall(f"GET /g/{name}/events?ticket={ticket} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        self.addCleanup(sock.close)
        return sock

    @staticmethod
    def until_closed(sock: socket.socket, seconds: float) -> bool:
        """True if the server closes the connection within `seconds` (anything it still sends meanwhile is read and dropped)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sock.settimeout(max(0.1, end - time.monotonic()))
            try:
                if sock.recv(65536) == b"":
                    return True
            except socket.timeout:
                return False
            except OSError:
                return True
        return False

    def test_a_player_cannot_ask_for_tickets_without_limit(self) -> None:
        saved = self.server.ticket_throttle
        self.addCleanup(lambda: setattr(self.server, "ticket_throttle", saved))
        self.server.ticket_throttle = Throttle(3, 60.0)
        token = self.join("ticketlimit")
        statuses = [call(self.port, "POST", "/g/ticketlimit/ticket", {"player": token})[0] for _ in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])
        other = call(self.port, "POST", "/g/ticketlimit/join", {}, headers={"X-Session": login_as(self.port, "Second")})[1]["token"]
        self.assertEqual(call(self.port, "POST", "/g/ticketlimit/ticket", {"player": other})[0], 200)  # another player is unaffected

    def test_a_second_event_stream_replaces_the_first(self) -> None:
        token = self.join("onestream")
        first = self.open_stream("onestream", self.ticket("onestream", token))
        self.assertIn(b"200", first.recv(4096)[:20])
        second = self.open_stream("onestream", self.ticket("onestream", token))
        self.assertIn(b"200", second.recv(4096)[:20])
        self.assertTrue(self.until_closed(first, 8), "the older stream should be closed by the server")
        self.assertFalse(self.until_closed(second, 1), "the newer stream stays open")
        game = self.server.registry.get("onestream")
        self.assertEqual(len(game._subs), 1)

    def test_one_player_opening_many_streams_holds_one_thread(self) -> None:
        import threading
        token = self.join("manystreams")
        before = threading.active_count()
        for _ in range(25):
            sock = self.open_stream("manystreams", self.ticket("manystreams", token))
            sock.recv(1024)
        time.sleep(4)   # the replaced streams notice within a keepalive
        self.assertLessEqual(threading.active_count() - before, 3)
        self.assertEqual(len(self.server.registry.get("manystreams")._subs), 1)

    def test_a_reconnect_keeps_the_players_tiles_and_seat(self) -> None:
        token = self.join("reconnecting")
        self.open_stream("reconnecting", self.ticket("reconnecting", token)).recv(1024)
        game = self.server.registry.get("reconnecting")
        first_elements = [e["id"] for e in call(self.port, "POST", "/g/reconnecting/journal", {"player": token})[1]["elements"]][:1]
        status, tile = call(self.port, "POST", "/g/reconnecting/tile/add", {"player": token, "element": first_elements[0], "x": 0.3, "y": 0.3}) \
            if first_elements else (200, {})
        self.assertEqual(status, 200)
        self.open_stream("reconnecting", self.ticket("reconnecting", token)).recv(1024)
        time.sleep(1)
        self.assertEqual(len(game._subs), 1)
        self.assertEqual(len(game._tiles), 1 if first_elements else 0)
        self.assertEqual(len(game._online_players()), 1)

    def test_connections_beyond_the_limit_are_turned_away_and_the_slot_comes_back(self) -> None:
        server = self.server
        saved = server.max_connections
        self.addCleanup(lambda: setattr(server, "max_connections", saved))
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(2)]  # half-sent requests hold a slot
        time.sleep(0.3)
        server.max_connections = server.open_connections
        refused = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        refused.request("GET", "/healthz")
        self.assertEqual(refused.getresponse().status, 503)
        refused.close()
        for sock in idle:
            sock.close()
        for _ in range(30):   # the slots free up as the server notices the closed sockets
            time.sleep(0.2)
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                conn.close()
                break
            conn.close()
        else:
            self.fail("connections were still being refused after the idle ones closed")
        self.assertGreaterEqual(server.stats["connections_refused"], 1)

    # ---- disabled and deleted accounts leave running games ---------------------------------------------

    def seat(self, game: str, name: str) -> tuple[str, str]:
        """Log `name` in, have them join `game`, and return (their session, their player token)."""
        session = login_as(self.port, name)
        status, joined = call(self.port, "POST", f"/g/{game}/join", {}, headers={"X-Session": session})
        self.assertEqual(status, 200)
        return session, joined["token"]

    def names_in(self, game: str) -> set[str]:
        return {p.name for p in self.server.registry.get(game)._players.values()}

    def act(self, game: str, token: str) -> int:
        return call(self.port, "POST", f"/g/{game}/cursor", {"player": token, "x": 0.5, "y": 0.5})[0]

    def test_disabling_an_account_removes_it_from_a_running_game(self) -> None:
        self.server.accounts.set_admin("Host", True)
        self.assertEqual(call(self.port, "POST", "/games", {"name": "banned"})[0], 200)
        _, host_token = self.seat("banned", "Host")
        _, troll_token = self.seat("banned", "Troll")
        stream = self.open_stream("banned", self.ticket("banned", troll_token))
        stream.recv(4096)
        self.assertEqual(self.act("banned", troll_token), 200)
        self.assertEqual(call(self.port, "POST", "/ops/accounts/disable", {"username": "Troll"})[0], 200)
        self.assertEqual(self.act("banned", troll_token), 403)
        self.assertTrue(self.until_closed(stream, 8), "their event stream should be closed")
        self.assertEqual(self.names_in("banned"), {"Host"})
        self.assertEqual(self.act("banned", host_token), 200)
        # enabling lets them back in, with a new login and a new seat
        self.assertEqual(call(self.port, "POST", "/ops/accounts/enable", {"username": "Troll"})[0], 200)
        _, again = self.seat("banned", "Troll")
        self.assertEqual(self.act("banned", again), 200)

    def test_deleting_an_account_removes_it_from_a_running_game(self) -> None:
        self.assertEqual(call(self.port, "POST", "/games", {"name": "deleted"})[0], 200)
        self.seat("deleted", "Host")
        session, token = self.seat("deleted", "Leaver")
        status, _ = call(self.port, "POST", "/account/delete", {"password": "password-Leaver"}, headers={"X-Session": session})
        self.assertEqual(status, 200)
        self.assertEqual(self.act("deleted", token), 403)
        self.assertEqual(self.names_in("deleted"), {"Host"})

    def test_an_account_disabled_by_the_command_line_tool_is_refused_at_the_next_action(self) -> None:
        self.assertEqual(call(self.port, "POST", "/games", {"name": "offline-ban"})[0], 200)
        _, token = self.seat("offline-ban", "Cli Victim")
        self.assertEqual(self.act("offline-ban", token), 200)
        AccountStore(self.server.accounts.path).set_disabled("Cli Victim", True)   # another process, as accounts_admin.py does
        self.assertEqual(self.act("offline-ban", token), 403)
        self.assertEqual(call(self.port, "POST", "/g/offline-ban/ticket", {"player": token})[0], 403)
        self.assertNotIn("Cli Victim", self.names_in("offline-ban"))

    def test_a_player_held_tile_is_put_down_when_they_are_removed(self) -> None:
        self.assertEqual(call(self.port, "POST", "/games", {"name": "held"})[0], 200)
        self.seat("held", "Host")
        _, token = self.seat("held", "Holder")
        game = self.server.registry.get("held")
        element = next(iter(game._discovered))
        status, tile = call(self.port, "POST", "/g/held/tile/add", {"player": token, "element": element, "x": 0.2, "y": 0.2})
        self.assertEqual(status, 200)
        self.assertEqual(call(self.port, "POST", "/g/held/tile/grab", {"player": token, "tile": tile["id"]})[0], 200)
        self.assertEqual(game.kick("Holder"), 1)
        self.assertIsNone(game._tiles[tile["id"]].held_by)
        self.assertEqual(game.kick("Holder"), 0)

    def test_a_flood_of_game_requests_is_limited_and_the_game_stays_sound(self) -> None:
        token = self.join("flooded")
        game = self.server.registry.get("flooded")
        element = next(iter(game._discovered))
        statuses = [call(self.port, "POST", "/g/flooded/tile/add", {"player": token, "element": element, "x": 0.1, "y": 0.1})[0]
                    for _ in range(80)]
        self.assertIn(429, statuses)
        self.assertIn(200, statuses)
        self.assertLessEqual(len(game._tiles), 300)
        self.assertEqual(len(game._tiles), statuses.count(200))
        cursor = [call(self.port, "POST", "/g/flooded/cursor", {"player": token, "x": 0.5, "y": 0.5})[0] for _ in range(150)]
        self.assertEqual(set(cursor), {200})   # excess pointer updates are dropped without an error
        self.assertGreaterEqual(self.server.stats["actions_limited"], 1)
        other = self.seat("flooded", "Flood Neighbour")[1]   # another player's budget is separate
        self.assertEqual(call(self.port, "POST", "/g/flooded/tile/add", {"player": other, "element": element, "x": 0.2, "y": 0.2})[0], 200)

    def test_a_temporary_password_only_lets_the_owner_choose_a_new_one(self) -> None:
        self.server.accounts.set_admin("Host", True)
        login_as(self.port, "Forgetful")
        status, reset = call(self.port, "POST", "/ops/accounts/reset-password", {"username": "Forgetful"})
        self.assertEqual(status, 200)
        temporary = reset["temporary_password"]
        status, data = call(self.port, "POST", "/login", {"username": "Forgetful", "password": temporary}, headers={"X-Session": ""})
        self.assertEqual((status, data.get("must_change_password")), (200, True))
        session = {"X-Session": data["session"]}
        self.assertEqual(call(self.port, "GET", "/me", headers=session)[1]["must_change_password"], True)
        for method, path, body in (("GET", "/games", None), ("GET", "/saved", None), ("GET", "/profile", None),
                                   ("POST", "/games", {"name": "sneaky"}), ("POST", "/solo/start", {}),
                                   ("POST", "/races", {"goal": "count"}), ("GET", "/ops/status", None),
                                   ("POST", "/account/delete", {"password": temporary})):
            self.assertEqual(call(self.port, method, path, body, headers=session)[0], 403, path)
        self.assertEqual(call(self.port, "POST", "/account/change-password", {"current": temporary, "new": "x"}, headers=session)[0], 400)
        status, changed = call(self.port, "POST", "/account/change-password",
                               {"current": temporary, "new": "my own new password"}, headers=session)
        self.assertEqual(status, 200)
        session = {"X-Session": changed["session"]}
        self.assertEqual(call(self.port, "GET", "/me", headers=session)[1]["must_change_password"], False)
        self.assertEqual(call(self.port, "GET", "/games", headers=session)[0], 200)

    def test_a_username_with_a_line_break_writes_one_log_line(self) -> None:
        with self.assertLogs("alchemy.auth", level="WARNING") as captured:
            status, _ = call(self.port, "POST", "/login",
                             {"username": "x\n2026-11-12 alchemy.auth login failed status=401 client=1.2.3.4", "password": "whatever"},
                             headers={"X-Session": "", "CF-Connecting-IP": "198.51.100.200"})
        self.assertIn(status, (401, 429))
        failed = [r.getMessage() for r in captured.records if r.getMessage().startswith("login failed")]
        self.assertTrue(failed)
        for line in failed:
            self.assertEqual(len(line.splitlines()), 1, line)

    def test_a_visitor_on_one_ipv6_64_shares_one_login_limit(self) -> None:
        server = self.server
        saved = (server.trusted_proxies, server.client_ip_header, server.login_throttle)
        self.addCleanup(lambda: (setattr(server, "trusted_proxies", saved[0]), setattr(server, "client_ip_header", saved[1]),
                                 setattr(server, "login_throttle", saved[2])))
        server.trusted_proxies = parse_networks(["127.0.0.1"])
        server.client_ip_header = "CF-Connecting-IP"
        server.login_throttle = Throttle(2, 60.0)

        def attempt(visitor: str) -> int:
            return call(self.port, "POST", "/login", {"username": "x", "password": "y"},
                        headers={"CF-Connecting-IP": visitor, "X-Session": ""})[0]

        statuses = [attempt(f"2001:db8:77:88::{i:x}") for i in range(1, 6)]   # five addresses, one /64
        self.assertEqual(statuses, [401, 401, 429, 429, 429])
        self.assertEqual(attempt("2001:db8:77:99::1"), 401)   # a different /64 is a different visitor

    def test_login_is_rate_limited_per_address(self) -> None:
        statuses = {call(self.port, "POST", "/login", {"username": f"u{i}", "password": "badbadbad"})[0]
                    for i in range(25)}
        self.assertIn(429, statuses)


if __name__ == "__main__":
    unittest.main()

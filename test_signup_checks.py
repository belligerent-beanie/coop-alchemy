"""Email verification and the Turnstile human check: account rules, the mailer, and the whole flow over HTTP."""

from __future__ import annotations

import http.client
import re
import socketserver
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import accounts_admin
from accounts import VERIFY_SECONDS, AccountStore, clean_email
from captcha import Turnstile
from games import GameRegistry
from invites import InviteStore
from mailer import MailConfigError, Mailer
from server import build_csp
from state import GameError
from test_coop import RECIPES, call, load_recipes, serve
from throttle import Throttle

PASSWORD = "a-long-enough-pass"


class CleanEmailTests(unittest.TestCase):
    def test_aliases_of_one_mailbox_share_a_key(self) -> None:
        keys = {clean_email(e)[1] for e in ("a.b+x@gmail.com", "AB@googlemail.com", "a.b@Gmail.com")}
        self.assertEqual(keys, {"ab@gmail.com"})
        self.assertEqual(clean_email("Bob+news@Example.org")[1], "bob@example.org")
        self.assertNotEqual(clean_email("a.b@example.org")[1], clean_email("ab@example.org")[1])  # dots count elsewhere

    def test_bad_addresses_are_refused(self) -> None:
        for bad in ("", "nobody", "a@b", "a b@c.de", "x@y.z\r\nBcc: evil@e.com", "é@example.org", None, 5, "@x.org",
                    "+tag@example.org", "a" * 300 + "@example.org"):
            with self.assertRaises(GameError, msg=repr(bad)):
                clean_email(bad)


class AccountEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.store = AccountStore(Path(self.tmp.name) / "accounts.json", clock=lambda: self.now[0], iterations=1000)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def signup(self, name: str = "ann", email: str = "ann@example.org") -> str:
        self.store.register(name, PASSWORD, email)
        found = self.store.begin_verification(name)
        assert found is not None
        return found[1]

    def test_unverified_accounts_cannot_log_in_until_the_link_is_used(self) -> None:
        token = self.signup()
        with self.assertRaises(GameError) as wrong:
            self.store.login("ann", "not the password")
        self.assertEqual(wrong.exception.status, 401)  # a wrong password reveals nothing about verification
        with self.assertRaises(GameError) as pending:
            self.store.login("ann", PASSWORD)
        self.assertEqual(pending.exception.status, 403)
        self.assertEqual(self.store.verify_email(token), "ann")
        self.assertTrue(self.store.login("ann", PASSWORD)[0])

    def test_a_link_works_once_and_a_new_link_replaces_the_old_one(self) -> None:
        first = self.signup()
        second = self.store.begin_verification_for_email("ANN@example.org")[1]  # type: ignore[index]
        with self.assertRaises(GameError):
            self.store.verify_email(first)
        self.assertEqual(self.store.verify_email(second), "ann")
        with self.assertRaises(GameError):
            self.store.verify_email(second)
        self.assertIsNone(self.store.begin_verification_for_email("ann@example.org"))  # nothing pending any more

    def test_garbage_and_expired_links_are_refused(self) -> None:
        token = self.signup()
        for bad in (None, 5, "", "short", "x" * 500, "A" * 32):
            with self.assertRaises(GameError):
                self.store.verify_email(bad)
        self.now[0] += VERIFY_SECONDS + 1
        with self.assertRaises(GameError):
            self.store.verify_email(token)

    def test_unconfirmed_accounts_disappear_and_free_their_name(self) -> None:
        self.signup()
        self.now[0] += VERIFY_SECONDS + 1
        with self.assertRaises(GameError):
            self.store.login("ann", PASSWORD)  # also clears it out
        self.assertEqual(self.store.usernames(), [])
        self.store.register("ann", PASSWORD, "someone-else@example.org")

    def test_a_pending_signup_cannot_block_the_real_owner_of_an_address(self) -> None:
        self.store.register("squatter", PASSWORD, "victim@example.org")
        self.store.register("victim", PASSWORD, "Victim+shop@Example.org")  # same mailbox: replaces the pending one
        self.assertEqual(self.store.usernames(), ["victim"])

    def test_a_verified_address_cannot_be_used_twice(self) -> None:
        self.store.verify_email(self.signup())
        with self.assertRaises(GameError) as ctx:
            self.store.register("ann2", PASSWORD, "ann+two@example.org")
        self.assertEqual(ctx.exception.status, 409)

    def test_taken_usernames_are_still_refused(self) -> None:
        self.signup()
        with self.assertRaises(GameError):
            self.store.register("ANN", PASSWORD, "other@example.org")

    def test_accounts_without_an_email_are_unaffected(self) -> None:
        self.store.register("old", PASSWORD)  # made before email verification existed
        self.assertTrue(self.store.login("old", PASSWORD)[0])
        self.assertEqual(self.store.unverified(), [])

    def test_admin_can_confirm_by_hand_and_the_listing_shows_pending_accounts(self) -> None:
        self.signup()
        self.assertEqual(self.store.unverified(), ["ann"])
        self.store.set_verified("ann")
        self.assertTrue(self.store.login("ann", PASSWORD)[0])
        with self.assertRaises(GameError):
            self.store.set_verified("nobody")

    def test_the_admin_command_verifies(self) -> None:
        self.signup()
        argv = ["accounts_admin.py", "--file", str(self.store.path), "verify", "ann"]
        self.assertEqual(accounts_admin.main(argv), 0)
        self.assertEqual(self.store.unverified(), [])


class TurnstileTests(unittest.TestCase):
    def make(self, reply: Any) -> tuple[Turnstile, list[dict[str, str]]]:
        sent: list[dict[str, str]] = []

        def post(fields: dict[str, str]) -> dict[str, Any]:
            sent.append(fields)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return Turnstile("site", "secret", post), sent

    def test_a_good_answer_passes_and_sends_the_secret_and_address(self) -> None:
        check, sent = self.make({"success": True})
        check.verify("token", "203.0.113.9")
        self.assertEqual(sent, [{"secret": "secret", "response": "token", "remoteip": "203.0.113.9"}])

    def test_a_missing_or_silly_token_never_reaches_cloudflare(self) -> None:
        check, sent = self.make({"success": True})
        for bad in (None, "", 5, "x" * 5000):
            with self.assertRaises(GameError) as ctx:
                check.verify(bad, "1.2.3.4")
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(sent, [])

    def test_a_rejected_answer_is_a_400(self) -> None:
        check, _ = self.make({"success": False, "error-codes": ["invalid-input-response"]})
        with self.assertRaises(GameError) as ctx:
            check.verify("token", "1.2.3.4")
        self.assertEqual(ctx.exception.status, 400)

    def test_an_outage_fails_closed(self) -> None:
        check, _ = self.make(OSError("network down"))
        with self.assertRaises(GameError) as ctx:
            check.verify("token", "1.2.3.4")
        self.assertEqual(ctx.exception.status, 503)

    def test_an_unknown_address_is_left_out(self) -> None:
        check, sent = self.make({"success": True})
        check.verify("token", "not-an-ip")
        self.assertNotIn("remoteip", sent[0])

    def test_success_must_be_exactly_true(self) -> None:
        for odd in ({"success": "true"}, {"success": 1}, {}, {"error-codes": []}):
            check, _ = self.make(odd)
            with self.assertRaises(GameError):
                check.verify("token", "1.2.3.4")

    def test_the_csp_allows_cloudflare_only_when_the_check_is_on(self) -> None:
        self.assertNotIn("cloudflare", build_csp(False))
        csp = build_csp(True)
        for directive in ("script-src 'self' https://challenges.cloudflare.com",
                          "frame-src 'self' https://challenges.cloudflare.com",
                          "connect-src 'self' https://challenges.cloudflare.com"):
            self.assertIn(directive, csp)
        self.assertIn("default-src 'self'", csp)


class FakeSmtp(socketserver.StreamRequestHandler):
    """Just enough SMTP for smtplib to deliver one message, so the real send path is exercised."""

    def handle(self) -> None:
        def reply(text: str) -> None:
            self.wfile.write(text.encode() + b"\r\n")

        reply("220 fake ready")
        data: list[str] = []
        in_data = False
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if line == ".":
                    in_data = False
                    self.server.messages.append("\n".join(data))  # type: ignore[attr-defined]
                    reply("250 queued")
                else:
                    data.append(line)
                continue
            command = line.split(" ")[0].upper()
            if command == "EHLO":
                reply("250 fake")
            elif command in ("MAIL", "RCPT", "RSET", "NOOP"):
                reply("250 ok")
            elif command == "DATA":
                in_data = True
                reply("354 go")
            elif command == "QUIT":
                reply("221 bye")
                return
            else:
                reply("502 not supported")


class MailerTests(unittest.TestCase):
    def test_settings_are_checked(self) -> None:
        for env in ({}, {"SMTP_HOST": "x"}, {"SMTP_HOST": "x", "MAIL_FROM": "nobody"},
                    {"SMTP_HOST": "x", "MAIL_FROM": "a@b.co", "SMTP_PORT": "abc"},
                    {"SMTP_HOST": "x", "MAIL_FROM": "a@b.co", "SMTP_SECURITY": "weird"}):
            with self.assertRaises(MailConfigError, msg=str(env)):
                Mailer.from_env(env)
        mailer = Mailer.from_env({"SMTP_HOST": "smtp.example.org", "SMTP_USER": "me@example.org", "SMTP_PORT": "465"})
        self.assertEqual((mailer.sender, mailer._security), ("me@example.org", "ssl"))

    def test_console_mode_prints_instead_of_sending(self) -> None:
        mailer = Mailer.from_env({"SMTP_HOST": "console", "MAIL_FROM": "noreply@example.org"})
        mailer._background = False
        with self.assertLogs("alchemy.mail", "WARNING") as logs:
            mailer.send("ann@example.org", "Hi", "the link")
        self.assertIn("the link", logs.output[0])

    def test_a_message_really_goes_over_smtp(self) -> None:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSmtp)
        server.messages = []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            mailer = Mailer("127.0.0.1", server.server_address[1], "Alchemy <noreply@example.org>", security="none",
                            background=False)
            mailer.send("ann@example.org", "Confirm", "open https://example.org/verify#abc")
        finally:
            server.shutdown()
            server.server_close()
        (sent,) = server.messages  # type: ignore[attr-defined]
        self.assertIn("Subject: Confirm", sent)
        self.assertIn("To: ann@example.org", sent)
        self.assertIn("open https://example.org/verify#abc", sent)

    def test_a_dead_mail_server_does_not_raise(self) -> None:
        mailer = Mailer("127.0.0.1", 1, "a@b.co", security="none", background=False)  # nothing listens on port 1
        with self.assertLogs("alchemy.mail", "ERROR"):
            mailer.send("ann@example.org", "Hi", "text")


class SignupFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=5))
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.outbox: list[EmailMessage] = []
        self.server.mailer = Mailer("console", 25, "noreply@example.org", deliver=self.outbox.append, background=False)
        self.server.public_url = "https://alchemy.example.org"
        self.server.captcha = None
        self.server.invites = None
        self.server.email_throttle = Throttle(3, 3600.0)
        self.server.resend_throttle = Throttle(10, 3600.0)
        self.counter = getattr(self.__class__, "counter", 0) + 1
        self.__class__.counter = self.counter

    def tearDown(self) -> None:
        self.server.mailer = self.server.captcha = self.server.invites = None
        self.server.csp = build_csp(False)

    def signup(self, name: str, email: str, **extra: Any) -> tuple[int, dict]:
        return call(self.port, "POST", "/register", {"username": name, "password": PASSWORD, "email": email, **extra})

    def token_from(self, message: EmailMessage) -> str:
        match = re.search(r"https://alchemy\.example\.org/verify#(\S+)", message.get_content())
        assert match, message.get_content()
        return match.group(1)

    def test_full_flow_signup_email_link_login(self) -> None:
        status, data = self.signup("flowuser", "flowuser@example.org")
        self.assertEqual((status, data), (200, {"verify_email": True, "username": "flowuser"}))  # no session yet
        (mail,) = self.outbox
        self.assertEqual(mail["To"], "flowuser@example.org")
        token = self.token_from(mail)
        self.assertEqual(call(self.port, "POST", "/login", {"username": "flowuser", "password": PASSWORD})[0], 403)
        self.assertEqual(call(self.port, "POST", "/verify-email", {"token": token}), (200, {"username": "flowuser"}))
        status, data = call(self.port, "POST", "/login", {"username": "flowuser", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("session", data)
        self.assertEqual(call(self.port, "POST", "/verify-email", {"token": token})[0], 400)  # used up

    def test_signup_needs_an_email_when_verification_is_on(self) -> None:
        status, _ = call(self.port, "POST", "/register", {"username": "noemail", "password": PASSWORD})
        self.assertEqual(status, 400)
        self.assertEqual(self.signup("badmail", "not-an-email")[0], 400)
        self.assertEqual(self.outbox, [])

    def test_the_verify_page_and_config_are_served(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/verify")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("/static/verify.js", body)
        status, cfg = call(self.port, "GET", "/auth-config")
        self.assertEqual((cfg["email_required"], cfg["turnstile_site_key"]), (True, None))

    def test_resend_answers_the_same_for_everyone_and_is_limited(self) -> None:
        self.signup("resender", "resender@example.org")
        self.outbox.clear()
        known = call(self.port, "POST", "/resend-verification", {"email": "resender@example.org"})
        unknown = call(self.port, "POST", "/resend-verification", {"email": "stranger@example.org"})
        junk = call(self.port, "POST", "/resend-verification", {"email": "###"})
        self.assertEqual(known, unknown)
        self.assertEqual(known, junk)
        self.assertEqual(known, (200, {"ok": True}))
        self.assertEqual(len(self.outbox), 1)  # only the real pending address got mail
        for _ in range(4):
            call(self.port, "POST", "/resend-verification", {"email": "resender@example.org"})
        self.assertLessEqual(len(self.outbox), 3)  # at most 3 emails per mailbox per hour, signup included

    def test_resend_is_limited_per_address(self) -> None:
        self.server.resend_throttle = Throttle(2, 3600.0)
        statuses = [call(self.port, "POST", "/resend-verification", {"email": f"x{i}@example.org"})[0] for i in range(4)]
        self.assertEqual(statuses, [200, 200, 429, 429])

    def test_resend_does_not_exist_without_email_verification(self) -> None:
        self.server.mailer = None
        self.assertEqual(call(self.port, "POST", "/resend-verification", {"email": "a@b.co"})[0], 404)

    def test_turnstile_is_checked_before_anything_else(self) -> None:
        verdicts: list[object] = []

        def post(fields: dict[str, str]) -> dict[str, Any]:
            verdicts.append(fields["response"])
            return {"success": fields["response"] == "good"}

        self.server.captcha = Turnstile("site", "secret", post)
        invites = self.server.invites = InviteStore(Path(self.tmp.name) / f"inv{self.counter}.json")
        (code,) = invites.create()
        status, _ = self.signup("botty", "botty@example.org", invite=code)  # no captcha answer at all
        self.assertEqual(status, 400)
        status, _ = self.signup("botty", "botty@example.org", invite=code, captcha="bad")
        self.assertEqual(status, 400)
        self.assertEqual(self.outbox, [])
        self.assertEqual(invites.listing()[0]["used_by"], None)  # the code was not spent by a failed human check
        status, data = self.signup("human", "human@example.org", invite=code, captcha="good")
        self.assertEqual((status, data["verify_email"]), (200, True))
        self.assertEqual(verdicts, ["bad", "good"])

    def test_a_captcha_outage_blocks_signup(self) -> None:
        def down(fields: dict[str, str]) -> dict[str, Any]:
            raise OSError("cloudflare unreachable")

        self.server.captcha = Turnstile("site", "secret", down)
        self.assertEqual(self.signup("outage", "outage@example.org", captcha="whatever")[0], 503)

    def test_turnstile_alone_works_without_email(self) -> None:
        self.server.mailer = None
        self.server.captcha = Turnstile("site-key", "secret", lambda fields: {"success": True})
        status, cfg = call(self.port, "GET", "/auth-config")
        self.assertEqual(cfg["turnstile_site_key"], "site-key")
        status, data = call(self.port, "POST", "/register",
                            {"username": "justcaptcha", "password": PASSWORD, "captcha": "ok"})
        self.assertEqual(status, 200)
        self.assertIn("session", data)  # no email step: logged straight in

    def test_login_is_not_asked_for_a_captcha(self) -> None:
        self.server.mailer = None
        self.server.captcha = Turnstile("site-key", "secret", lambda fields: {"success": False})
        call(self.port, "POST", "/register", {"username": "plainlogin", "password": PASSWORD, "captcha": "x"})
        self.server.captcha = Turnstile("site-key", "secret", lambda fields: {"success": True})
        status, _ = call(self.port, "POST", "/register", {"username": "plainlogin", "password": PASSWORD, "captcha": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(call(self.port, "POST", "/login", {"username": "plainlogin", "password": PASSWORD})[0], 200)

    def test_the_csp_header_follows_the_configuration(self) -> None:
        def header() -> str:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/login")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return resp.getheader("Content-Security-Policy") or ""

        self.assertNotIn("cloudflare", header())
        self.server.csp = build_csp(True)
        self.assertIn("challenges.cloudflare.com", header())


if __name__ == "__main__":
    unittest.main()

"""The terms of use page, the contact line on it and on /privacy, and the invite-only default of the container setup."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts_admin
from games import GameRegistry
from test_coop import RECIPES, call, load_recipes, serve

ROOT = Path(__file__).resolve().parent


class TermsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def page(self, path: str, contact: str) -> str:
        self.server.contact = contact
        status, body = call(self.port, "GET", path, headers={"X-Session": ""})
        self.assertEqual(status, 200)
        return body.decode("utf-8") if isinstance(body, bytes) else str(body)

    def test_the_terms_and_privacy_pages_need_no_login(self) -> None:
        for path in ("/terms", "/privacy"):
            self.assertIn("Co-op Alchemy", self.page(path, ""))

    def test_without_a_contact_the_pages_say_to_ask_whoever_gave_you_the_address(self) -> None:
        for path in ("/terms", "/privacy"):
            text = self.page(path, "")
            self.assertIn("ask the person who gave you the address", text)
            self.assertNotIn("{{contact}}", text)

    def test_an_email_address_becomes_a_mailto_link(self) -> None:
        text = self.page("/terms", "arsh@example.com")
        self.assertIn('<a href="mailto:arsh@example.com">arsh@example.com</a>', text)
        self.assertIn('<a href="mailto:arsh@example.com">', self.page("/privacy", "arsh@example.com"))

    def test_a_web_address_becomes_a_link(self) -> None:
        text = self.page("/terms", "https://example.com/contact")
        self.assertIn('<a href="https://example.com/contact" rel="noopener">https://example.com/contact</a>', text)

    def test_anything_else_is_shown_as_plain_escaped_text(self) -> None:
        text = self.page("/terms", "ask in the group <script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        tricky = self.page("/terms", 'x@y.z" onmouseover="bad')
        self.assertNotIn('" onmouseover="', tricky)   # the quote is escaped, so it cannot start an attribute

    def test_a_javascript_address_never_becomes_a_link(self) -> None:
        text = self.page("/terms", "javascript:alert(1)")
        self.assertNotIn('href="javascript', text)

    def test_the_pages_link_to_each_other_and_the_signup_form_asks_for_agreement(self) -> None:
        self.assertIn('href="/privacy"', self.page("/terms", ""))
        self.assertIn('href="/terms"', self.page("/privacy", ""))
        login = (ROOT / "static" / "login.html").read_text(encoding="utf-8")
        self.assertIn('id="terms-hint"', login)
        self.assertIn('href="/terms"', login)
        self.assertIn('href="/terms"', (ROOT / "static" / "home.html").read_text(encoding="utf-8"))

    def test_the_terms_cover_what_the_checklist_asked_for(self) -> None:
        text = self.page("/terms", "").lower()
        for needle in ("13 or over", "report", "disable an account", "promote", "no promises", "contact"):
            self.assertIn(needle, text)


class ContainerDefaultsTests(unittest.TestCase):
    def test_the_compose_file_is_invite_only_and_passes_the_contact_line(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"--require-invite"', compose)
        self.assertRegex(compose, r'"--contact"\s*\n\s*-\s*"\$\{CONTACT_ADDRESS:-\}"')
        self.assertIn("CONTACT_ADDRESS=", (ROOT / ".env.example").read_text(encoding="utf-8"))

    def test_the_admin_tool_finds_the_data_volume_through_one_variable(self) -> None:
        self.assertIn("ALCHEMY_DATA_DIR=/data", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ALCHEMY_DATA_DIR": tmp}):
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = accounts_admin.main(["accounts_admin.py", "invite", "create", "--count", "1", "--note", "volume test"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "invites.json").is_file(), "the invite went to the data folder")
            self.assertRegex(out.getvalue(), r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}")


if __name__ == "__main__":
    unittest.main()

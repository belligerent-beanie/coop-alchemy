"""static/login.js nextPage(): after logging in, ?next= may send a player to a page on this site and nowhere else.

The function is lifted out of the page and run in Node with a made-up `location`, so a browser is not needed.
Skipped where Node is not installed (the GitHub runners have it).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import quote

LOGIN_JS = Path(__file__).parent / "static" / "login.js"
ORIGIN = "https://alchemy.example.com"


def next_page(next_value: str | None) -> str:
    source = LOGIN_JS.read_text(encoding="utf-8")
    match = re.search(r"^function nextPage\(\) \{.*?^\}", source, re.S | re.M)
    assert match, "nextPage() not found in static/login.js"
    search = "" if next_value is None else "?next=" + quote(next_value, safe="")
    script = (f"const location = {{origin: {json.dumps(ORIGIN)}, search: {json.dumps(search)}}};\n"
              f"{match.group(0)}\nprocess.stdout.write(nextPage());")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20, check=True)
    return done.stdout


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class NextPageTests(unittest.TestCase):
    def test_other_sites_are_refused(self) -> None:
        for bad in ("/\\evil.com", "/\\\\evil.com", "//evil.com", "/\t/evil.com", "/\n/evil.com", "/\r/evil.com",
                    "https://evil.com", "http://evil.com/x", "javascript:alert(1)", "evil.com", "\\evil.com"):
            with self.subTest(next=bad):
                self.assertEqual(next_page(bad), "/")

    def test_the_login_page_itself_is_not_a_destination(self) -> None:
        self.assertEqual(next_page("/login"), "/")
        self.assertEqual(next_page("/login?next=/"), "/")

    def test_missing_next_goes_home(self) -> None:
        self.assertEqual(next_page(None), "/")
        self.assertEqual(next_page(""), "/")

    def test_pages_on_this_site_are_kept(self) -> None:
        self.assertEqual(next_page("/dashboard?x=1#y"), "/dashboard?x=1#y")
        self.assertEqual(next_page("/g/abc123"), "/g/abc123")
        self.assertEqual(next_page("/"), "/")


if __name__ == "__main__":
    unittest.main()

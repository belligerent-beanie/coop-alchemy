"""Cloudflare Turnstile: a "are you human" check on signup.

The page shows Cloudflare's widget, which hands the browser a one-use token when the visitor passes. The server then
asks Cloudflare whether that token is genuine before creating the account. Configure it with --turnstile-site-key
(public, goes in the page) and the TURNSTILE_SECRET_KEY environment variable (private; never put it on the command line,
where other users of the machine can read it).

If Cloudflare cannot be reached the check fails closed (signup answers 503), so an outage never lets bots through.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Callable

from state import GameError

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ORIGIN = "https://challenges.cloudflare.com"  # what the page's CSP must allow
MAX_TOKEN_LEN = 2048

log = logging.getLogger("alchemy.captcha")

PostFn = Callable[[dict[str, str]], dict[str, Any]]


def _post_to_cloudflare(fields: dict[str, str], timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(VERIFY_URL, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URL)
        return json.loads(response.read(65536).decode("utf-8"))


class Turnstile:
    def __init__(self, site_key: str, secret: str, post: PostFn | None = None) -> None:
        self.site_key = site_key
        self._secret = secret
        self._post = post or _post_to_cloudflare

    def verify(self, token: object, client_ip: str = "") -> None:
        """Return if `token` is a genuine, unused Turnstile answer; raise GameError (400 or 503) otherwise."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
            raise GameError(400, "please complete the human check first")
        fields = {"secret": self._secret, "response": token}
        try:
            ipaddress.ip_address(client_ip)
            fields["remoteip"] = client_ip
        except ValueError:
            pass  # unknown address: Cloudflare treats the field as optional
        try:
            result = self._post(fields)
        except Exception as exc:  # network down, bad reply, timeout: fail closed
            log.error("turnstile check unavailable: %s", exc)
            raise GameError(503, "the human check is unavailable right now; please try again in a minute") from exc
        if result.get("success") is not True:
            log.warning("turnstile rejected a token: %s", result.get("error-codes"))
            raise GameError(400, "the human check failed; please try again")

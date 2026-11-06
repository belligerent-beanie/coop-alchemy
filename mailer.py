"""Sending the verification email, over SMTP.

Any provider works (Resend, Postmark, Amazon SES, Mailgun and so on all offer SMTP), so nothing here is tied to one.
Settings come from environment variables, so no secret ends up in a command line or in git:

    SMTP_HOST       the provider's SMTP server, or the word  console  to print emails to the server's log instead of
                    sending them (for trying things out before you have a domain: the log then contains live links)
    SMTP_PORT       587 (default, STARTTLS) or 465 (TLS from the start)
    SMTP_USER, SMTP_PASSWORD   the provider's login (some use an API key as the password)
    MAIL_FROM       the sender, e.g.  Co-op Alchemy <noreply@alchemy.example.com>  (default: SMTP_USER)
    SMTP_SECURITY   starttls | ssl | none   (default: ssl for port 465, otherwise starttls)

Messages are sent on a background thread so a slow mail server never holds up a signup. A failure is logged, and the
person can ask for the link again.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Callable, Mapping

log = logging.getLogger("alchemy.mail")

DeliverFn = Callable[[EmailMessage], None]


class MailConfigError(ValueError):
    pass


class Mailer:
    def __init__(self, host: str, port: int, sender: str, user: str = "", password: str = "",
                 security: str = "starttls", deliver: DeliverFn | None = None, background: bool = True) -> None:
        if security not in ("starttls", "ssl", "none"):
            raise MailConfigError("SMTP_SECURITY must be starttls, ssl or none")
        self.host, self.port, self.sender = host, port, sender
        self._user, self._password, self._security = user, password, security
        self._deliver = deliver or (self._print if host == "console" else self._smtp)
        self._background = background

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Mailer":
        host = env.get("SMTP_HOST", "").strip()
        if not host:
            raise MailConfigError("SMTP_HOST is not set (use the word 'console' to print emails instead of sending)")
        try:
            port = int(env.get("SMTP_PORT") or 587)
        except ValueError:
            raise MailConfigError("SMTP_PORT must be a number") from None
        user = env.get("SMTP_USER", "")
        sender = env.get("MAIL_FROM") or user
        if not sender or "@" not in sender:
            raise MailConfigError("MAIL_FROM (or SMTP_USER) must be an email address to send from")
        return cls(host, port, sender, user, env.get("SMTP_PASSWORD", ""),
                   env.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls"))

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(body)
        if self._background:
            threading.Thread(target=self._safely, args=(message,), daemon=True).start()
        else:
            self._safely(message)

    def _safely(self, message: EmailMessage) -> None:
        try:
            self._deliver(message)
        except Exception as exc:  # the mail server is the provider's problem; the person can resend
            log.error("could not send mail to %s: %s", message["To"], exc)

    def _smtp(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self._security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=15)
        with client:
            if self._security == "starttls":
                client.starttls(context=context)
            if self._user:
                client.login(self._user, self._password)
            client.send_message(message)

    @staticmethod
    def _print(message: EmailMessage) -> None:
        log.warning("EMAIL (console mode, not sent)\nTo: %s\nSubject: %s\n\n%s",
                    message["To"], message["Subject"], message.get_content())


def verification_message(link: str) -> tuple[str, str]:
    """(subject, body) of the email that asks someone to confirm their address."""
    return ("Confirm your Co-op Alchemy account",
            "Welcome to Co-op Alchemy!\n\n"
            "Open this link to confirm your email address and activate your account (it works for 24 hours):\n\n"
            f"{link}\n\n"
            "If you didn't sign up, ignore this email and nothing will happen.\n")

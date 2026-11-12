"""Shared logging helpers: one short id per request, so every line a request causes can be read together.

Each request gets an id and the caller's address (`r17 203.0.113.9`), kept in a context variable. The log handler stamps it on every
line, whichever module wrote it, and work handed to a background thread keeps it (see `in_context`). So a sign-up reads as a story:

    ... INFO    alchemy.auth     [r17 203.0.113.9] register start
    ... INFO    alchemy.breach   [r17 203.0.113.9] breach lookup ok in 212 ms (870 suffixes)
    ... INFO    alchemy.accounts [r17 203.0.113.9] register timing: breach check 214 ms, password hash 97 ms, save 3 ms
    ... INFO    alchemy.auth     [r17 203.0.113.9] register ok user=sam
    ... INFO    alchemy.auth     [r17 203.0.113.9] POST /register -> 200 in 330 ms

Lines written outside a request (start-up, background clean-up) show `[-]`.
"""
from __future__ import annotations

import contextvars
import itertools
import logging
from typing import Any, Callable

# Without a handler Python prints every warning to the console; the server installs its own (server.configure_logging), and tests
# should not be noisy, so the "alchemy" loggers always have one that does nothing.
logging.getLogger("alchemy").addHandler(logging.NullHandler())

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-16s [%(request)s] %(message)s"

_request: contextvars.ContextVar[str] = contextvars.ContextVar("alchemy_request", default="-")
_numbers = itertools.count(1)


def begin_request(client: str) -> str:
    """Start a request's context; returns its tag, such as 'r17 203.0.113.9'."""
    tag = f"r{next(_numbers)} {client}"
    _request.set(tag)
    return tag


def end_request() -> None:
    _request.set("-")


class RequestFilter(logging.Filter):
    """Puts the current request's tag on every record, as %(request)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request = _request.get()
        return True


def in_context(function: Callable[..., Any]) -> Callable[..., Any]:
    """`function`, but running with the request context of whoever called this (for a background thread)."""
    context = contextvars.copy_context()
    return lambda *args, **kwargs: context.run(function, *args, **kwargs)

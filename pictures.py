"""Element pictures, served only to players who have found the element.

Pictures live in static/elements/<name>.png (or --elements-dir; the folder is private and not in git), where <name> is the element's id or its label turned into a slug ("Alarm Clock"
-> alarm-clock.png). If those files were served by name, anyone could list or guess which elements exist, and see them, before
finding them. So the folder is not served directly: each picture gets an unguessable address, /pic/<token>.png, where the token
is a keyed hash of the file name, and that address is sent to a page only with the element's own data (the game sends an element
only once it is found). The key is random for each run of the server, so addresses change when the server restarts.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Callable

from recipes import RecipeBook

REFRESH_SECONDS = 5.0   # how often the folder is looked at again, so a new picture needs no restart


def slugify(text: object) -> str:
    """The same slug static/pictures.js used: accents dropped, apostrophes removed, other runs of characters become '-'."""
    s = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"['’]", "", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


class PictureIndex:
    """Which element has which picture file, and the secret address each is served at."""

    def __init__(self, folder: str | Path, book: RecipeBook, *, secret: bytes | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.folder = Path(folder)
        self.book = book
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self._checked = -REFRESH_SECONDS
        self._by_element: dict[str, str] = {}   # element id -> token
        self._by_token: dict[str, Path] = {}    # token -> file
        self._earlier: dict[str, str] = {}      # token of a replaced version -> its stem (still served: see _refresh)
        self._files: dict[str, Path] = {}
        self._stems: frozenset[tuple[str, int]] | None = None
        self._refresh(force=True)

    def _token(self, stem: str) -> str:
        return hmac.new(self._secret, stem.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _refresh(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._checked < REFRESH_SECONDS:
            return
        self._checked = now
        try:
            files = {p.stem: p for p in self.folder.glob("*.png") if p.is_file()}
            versions = {stem: path.stat().st_mtime_ns for stem, path in files.items()}
        except OSError:
            files, versions = {}, {}
        stems = frozenset(versions.items())
        if stems == self._stems:
            return
        self._stems = stems
        # A picture's address is made from its file name AND the time the file was last written, so a picture that is
        # replaced (an icon approved in the icon studio) gets a new address and browsers fetch it instead of a cached copy.
        # Pages opened before that still hold the old address: it keeps working and serves the current file.
        for token, path in self._by_token.items():
            self._earlier[token] = path.stem
        by_element: dict[str, str] = {}
        by_token: dict[str, Path] = {}
        for eid, element in self.book.elements.items():
            stem = next((s for s in (slugify(eid), slugify(element.label)) if s in files), None)
            if stem is not None:
                token = self._token(f"{stem}:{versions[stem]}")
                by_element[eid] = token
                by_token[token] = files[stem]
        self._by_element, self._by_token, self._files = by_element, by_token, files

    def url_for(self, eid: str) -> str | None:
        """The address of the element's picture, or None if it has none."""
        self._refresh()
        token = self._by_element.get(eid)
        return f"/pic/{token}.png" if token else None

    def path_for(self, token: str) -> Path | None:
        """The file behind a /pic/<token>.png address (None for anything that is not a current token)."""
        self._refresh()
        if not isinstance(token, str):
            return None
        found = self._by_token.get(token)
        if found is None and token in self._earlier:
            found = self._files.get(self._earlier[token])
        return found

    def refresh(self) -> None:
        """Look at the folder now (after a picture was written), not at the next periodic look."""
        self._refresh(force=True)

    def file_for(self, eid: str) -> Path | None:
        """The element's picture file, if it has one."""
        self._refresh()
        token = self._by_element.get(eid)
        return self._by_token.get(token) if token else None

    def all_urls(self) -> dict[str, str]:
        """Every element's picture address (for the administrators' pages, which see every element anyway)."""
        self._refresh()
        return {eid: f"/pic/{token}.png" for eid, token in self._by_element.items()}

"""Icon designs and icon proposals: the server side of the icon studio (/icons, static/icons.js).

A designer's work on an element's icon is a *design*: the studio's own description of it (layers of shapes and pixels, see
icons.js) plus the 128 x 128 PNG it makes. It is saved under the designer's account as they work, one per element.

*Proposing* a design sends a copy of its PNG to the administrators, with the designer's name (kept for the record; players are
not shown it) and an optional note. A designer has at most one waiting proposal per element: proposing again replaces it.

An administrator *approves* (the PNG goes straight into the element pictures folder, so players see it within seconds) or
*rejects* it, with an optional reply the designer sees. Approving a redraw keeps the picture it replaced, so the latest
approval of an element can be *undone*: the old picture comes back (or, for an element that had none, the picture goes away).

A waiting proposal can be *previewed*: administrators and designers then see it in the real game in place of the current picture
(players never do), so it can be judged where it will live. The designer of the proposal or an administrator switches that, and
only one proposal per element is previewed at a time.

Everything is in one JSON file (icons.json) plus a folder of PNGs (icon_files/: d-* designs, p-* proposals, b-* the pictures
approvals replaced). Both are private data, like accounts.json: not in git, and the PNGs are served only to administrators and
designers. The JSON file is written atomically and a damaged one is never overwritten.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any

from accounts import key_of
from state import GameError

ICON_SIZE = 128
MAX_PNG_BYTES = 200_000
MAX_DOC_BYTES = 450_000          # a design with a few pixel layers; also bounds the request (see MAX_ICON_BODY in server.py)
MAX_LAYERS = 24
MAX_SHAPES_PER_LAYER = 400
MAX_DESIGNS_PER_USER = 300
MAX_WAITING_PER_USER = 40
MAX_NOTE_LEN = 300
MAX_PROPOSALS = 3000             # handled ones are dropped oldest-first beyond this (their PNGs too)
DATA_URL = "data:image/png;base64,"
FILE_NAME = re.compile(r"^[dpb]-[a-z0-9]{12,32}$")

WAITING, APPROVED, REJECTED, UNDONE, WITHDRAWN = "waiting", "approved", "rejected", "undone", "withdrawn"
DRAFT = "draft"


# ---- checking what a browser sends ----

def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG, after checking it is one: the signature, IHDR first, whole chunks, IEND last."""
    if len(data) > MAX_PNG_BYTES:
        raise GameError(400, "the picture is too big")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise GameError(400, "the picture is not a PNG")
    pos, first, width, height = 8, True, 0, 0
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            break
        if first:
            if kind != b"IHDR" or length != 13:
                break
            width, height = struct.unpack(">II", data[pos + 8:pos + 16])
            first = False
        if kind == b"IEND":
            if end != len(data):
                break
            return width, height
        pos = end
    raise GameError(400, "the picture is not a complete PNG")


def icon_png(data_url: object) -> bytes:
    """The bytes of a 128 x 128 PNG sent as a data: URL (as canvas.toDataURL makes them)."""
    if not isinstance(data_url, str) or not data_url.startswith(DATA_URL):
        raise GameError(400, "the picture must be a PNG data URL")
    try:
        data = base64.b64decode(data_url[len(DATA_URL):], validate=True)
    except (binascii.Error, ValueError):
        raise GameError(400, "the picture is not valid base64") from None
    if png_size(data) != (ICON_SIZE, ICON_SIZE):
        raise GameError(400, f"the picture must be {ICON_SIZE} x {ICON_SIZE} pixels")
    return data


def check_doc(doc: object) -> dict[str, Any]:
    """A design as the studio describes it: only its outline is checked (the server never draws it; the studio does, with
    canvas calls and text, never as markup), and every pixel layer must be a 128 x 128 PNG."""
    if not isinstance(doc, dict) or not isinstance(doc.get("layers"), list) or not 1 <= len(doc["layers"]) <= MAX_LAYERS:
        raise GameError(400, f"a design has 1 to {MAX_LAYERS} layers")
    for layer in doc["layers"]:
        if not isinstance(layer, dict) or layer.get("kind") not in ("shapes", "pixels"):
            raise GameError(400, "each layer is of shapes or of pixels")
        if not isinstance(layer.get("name", ""), str) or len(layer.get("name", "")) > 40:
            raise GameError(400, "a layer name is at most 40 characters")
        if layer["kind"] == "shapes":
            shapes = layer.get("shapes")
            if not isinstance(shapes, list) or len(shapes) > MAX_SHAPES_PER_LAYER or not all(isinstance(s, dict) for s in shapes):
                raise GameError(400, f"a shapes layer holds at most {MAX_SHAPES_PER_LAYER} shapes")
        elif layer.get("pixels") is not None:
            icon_png(layer["pixels"])
    reference = doc.get("reference")
    if reference is not None and not (isinstance(reference, dict) and isinstance(reference.get("element"), str)):
        raise GameError(400, "a tracing picture names its element")
    if len(json.dumps(doc)) > MAX_DOC_BYTES:
        raise GameError(400, "the design is too big: merge or remove some pixel layers")
    return doc


class IconStore:
    def __init__(self, path: str | Path | None, files_dir: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.files = Path(files_dir) if files_dir else None
        self._lock = threading.Lock()
        self._memory: dict[str, Any] = {"version": 1, "designs": {}, "proposals": []}
        self._memory_files: dict[str, bytes] = {}

    # ---- storage ----

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "designs": {}, "proposals": []}
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the icons file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("designs"), dict) or not isinstance(data.get("proposals"), list):
            raise GameError(500, f"the icons file ({self.path.name}) is not in the expected format")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            self._memory = data
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the icon could not be saved (see the server console)") from exc

    def _put_file(self, prefix: str, data: bytes) -> str:
        name = f"{prefix}-{secrets.token_hex(8)}"
        if self.files is None:
            self._memory_files[name] = data
            return name
        try:
            self.files.mkdir(parents=True, exist_ok=True)
            tmp = self.files / f"{name}.png.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, self.files / f"{name}.png")
        except OSError as exc:
            raise GameError(500, "the icon could not be saved (see the server console)") from exc
        return name

    def _drop_file(self, name: str | None) -> None:
        if not name:
            return
        if self.files is None:
            self._memory_files.pop(name, None)
            return
        try:
            (self.files / f"{name}.png").unlink()
        except OSError:
            pass

    def file(self, name: object) -> bytes | None:
        """A stored PNG by its name (d-…, p-… or b-…); None for anything else."""
        if not isinstance(name, str) or not FILE_NAME.match(name):
            return None
        if self.files is None:
            return self._memory_files.get(name)
        try:
            return (self.files / f"{name}.png").read_bytes()
        except OSError:
            return None

    # ---- designs ----

    @staticmethod
    def _key(username: str, element: str) -> str:
        return f"{key_of(username)}\t{element}"

    @staticmethod
    def _summary(design: dict[str, Any]) -> dict[str, Any]:
        return {"element": design["element"], "status": design["status"], "updated": design["updated"],
                "reply": design.get("reply", ""), "changed": design.get("changed", False), "png": design["png"]}

    def save_design(self, username: str, element: str, doc: object, png_url: object) -> dict[str, Any]:
        doc = check_doc(doc)
        png = icon_png(png_url)
        with self._lock:
            data = self._load()
            key = self._key(username, element)
            design = data["designs"].get(key)
            if design is None:
                mine = sum(1 for d in data["designs"].values() if key_of(d["author"]) == key_of(username))
                if mine >= MAX_DESIGNS_PER_USER:
                    raise GameError(409, f"you have {MAX_DESIGNS_PER_USER} icon designs: finish or delete some first")
                design = {"author": username, "element": element, "status": DRAFT}
                data["designs"][key] = design
            old_file = design.get("png")
            design.update(doc=doc, png=self._put_file("d", png), updated=time.time())
            if design["status"] == WAITING:
                design["changed"] = True      # the waiting proposal is the copy that was sent: propose again to update it
            elif design["status"] != DRAFT:
                design.update(status=DRAFT, reply="")
            self._save(data)
        self._drop_file(old_file)
        return self._summary(design)

    @staticmethod
    def _previewed(data: dict[str, Any], design: dict[str, Any]) -> bool:
        proposal = next((p for p in data["proposals"] if p["id"] == design.get("proposal")), None)
        return bool(proposal and proposal["status"] == WAITING and proposal.get("preview"))

    def design(self, username: str, element: str) -> dict[str, Any] | None:
        data = self._load()
        design = data["designs"].get(self._key(username, element))
        if not design:
            return None
        return {**self._summary(design), "doc": design["doc"], "preview": self._previewed(data, design),
                "proposal": design.get("proposal") if design["status"] == WAITING else None}

    def mine(self, username: str) -> list[dict[str, Any]]:
        who, data = key_of(username), self._load()
        return sorted(({**self._summary(d), "preview": self._previewed(data, d),
                        "proposal": d.get("proposal") if d["status"] == WAITING else None}
                       for d in data["designs"].values() if key_of(d["author"]) == who), key=lambda d: -d["updated"])

    def delete_design(self, username: str, element: str) -> bool:
        with self._lock:
            data = self._load()
            design = data["designs"].pop(self._key(username, element), None)
            if design is None:
                return False
            self._save(data)
        self._drop_file(design.get("png"))
        return True

    # ---- proposals ----

    def propose(self, username: str, element: str, note: object) -> dict[str, Any]:
        note = note if isinstance(note, str) else ""
        if len(note) > MAX_NOTE_LEN:
            raise GameError(400, f"the note is at most {MAX_NOTE_LEN} characters")
        with self._lock:
            data = self._load()
            design = data["designs"].get(self._key(username, element))
            if design is None:
                raise GameError(404, "draw the icon first: there is nothing saved to propose")
            waiting = [p for p in data["proposals"] if p["status"] == WAITING and key_of(p["author"]) == key_of(username)]
            replaced = [p for p in waiting if p["element"] == element]
            if len(waiting) - len(replaced) >= MAX_WAITING_PER_USER:
                raise GameError(409, f"you have {MAX_WAITING_PER_USER} icons waiting for an administrator already")
            png = self.file(design["png"])
            if png is None:
                raise GameError(500, "the saved icon is missing: draw it again")
            for p in replaced:   # one waiting proposal per designer and element: the new one replaces it
                p.update(status=WITHDRAWN, handled_at=time.time())
                self._drop_file(p["png"])
            proposal = {"id": secrets.token_hex(6), "author": username, "element": element, "note": note.strip(),
                        "png": self._put_file("p", png), "created": time.time(), "status": WAITING}
            data["proposals"].append(proposal)
            design.update(status=WAITING, changed=False, reply="", proposal=proposal["id"])
            self._trim(data)
            self._save(data)
        return self._summary(design)

    def _trim(self, data: dict[str, Any]) -> None:
        handled = [p for p in data["proposals"] if p["status"] != WAITING]
        extra = len(data["proposals"]) - MAX_PROPOSALS
        for p in handled[:max(0, extra)]:
            data["proposals"].remove(p)
            self._drop_file(p.get("png"))
            self._drop_file(p.get("backup"))

    def _proposal(self, data: dict[str, Any], proposal_id: object) -> dict[str, Any]:
        found = next((p for p in data["proposals"] if p["id"] == proposal_id), None) if isinstance(proposal_id, str) else None
        if found is None:
            raise GameError(404, "no such icon proposal")
        return found

    def _set_design_status(self, data: dict[str, Any], proposal: dict[str, Any], status: str, reply: str = "") -> None:
        design = data["designs"].get(self._key(proposal["author"], proposal["element"]))
        if design is not None and design.get("proposal") == proposal["id"]:
            design.update(status=status, reply=reply, changed=False if status != DRAFT else design.get("changed", False))

    def queue(self, current_picture: Any) -> dict[str, Any]:
        """What the review page shows: the waiting proposals (oldest first), and the latest handled ones, the newest approval
        of each element marked as the one that can be undone. current_picture(element) is its picture's address, or None."""
        data = self._load()
        latest: dict[str, str] = {}
        for p in data["proposals"]:
            if p["status"] == APPROVED:
                latest[p["element"]] = p["id"]

        def row(p: dict[str, Any]) -> dict[str, Any]:
            return {"id": p["id"], "author": p["author"], "element": p["element"], "note": p.get("note", ""),
                    "created": p["created"], "status": p["status"], "png": p["png"], "reply": p.get("reply", ""),
                    "handled_by": p.get("handled_by", ""), "handled_at": p.get("handled_at"),
                    "replaced": p.get("backup"), "can_undo": p["status"] == APPROVED and latest.get(p["element"]) == p["id"],
                    "preview": p["status"] == WAITING and bool(p.get("preview"))}
        waiting = [{**row(p), "current": current_picture(p["element"])} for p in data["proposals"] if p["status"] == WAITING]
        handled = sorted((p for p in data["proposals"] if p["status"] in (APPROVED, REJECTED, UNDONE)),
                         key=lambda p: -(p.get("handled_at") or 0))[:30]
        return {"waiting": waiting, "recent": [row(p) for p in handled]}

    def approve(self, admin: str, proposal_id: object, reply: object, target: Path) -> dict[str, Any]:
        """Put the proposal's PNG into the pictures folder at `target` (the element's current picture file, or the file it
        would have), keeping the picture it replaces so the approval can be undone."""
        reply = reply.strip()[:MAX_NOTE_LEN] if isinstance(reply, str) else ""
        with self._lock:
            data = self._load()
            proposal = self._proposal(data, proposal_id)
            if proposal["status"] != WAITING:
                raise GameError(409, "that icon proposal was already handled")
            png = self.file(proposal["png"])
            if png is None:
                raise GameError(500, "the proposed icon's file is missing")
            try:
                old = target.read_bytes() if target.exists() else None
            except OSError as exc:
                raise GameError(500, "the current picture could not be read") from exc
            backup = self._put_file("b", old) if old is not None else None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_bytes(png)
                os.replace(tmp, target)
            except OSError as exc:
                self._drop_file(backup)
                raise GameError(500, "the icon could not be put in the pictures folder (see the server console)") from exc
            proposal.update(status=APPROVED, handled_by=admin, handled_at=time.time(), reply=reply, backup=backup,
                            installed=target.name, installed_sha=hashlib.sha256(png).hexdigest())
            self._set_design_status(data, proposal, APPROVED, reply)
            self._save(data)
        return {"installed": target.name}

    def reject(self, admin: str, proposal_id: object, reply: object) -> dict[str, Any]:
        reply = reply.strip()[:MAX_NOTE_LEN] if isinstance(reply, str) else ""
        with self._lock:
            data = self._load()
            proposal = self._proposal(data, proposal_id)
            if proposal["status"] != WAITING:
                raise GameError(409, "that icon proposal was already handled")
            proposal.update(status=REJECTED, handled_by=admin, handled_at=time.time(), reply=reply)
            self._set_design_status(data, proposal, REJECTED, reply)
            self._save(data)
        return {}

    def undo(self, admin: str, proposal_id: object, folder: Path) -> dict[str, Any]:
        """Take back the latest approval of an element: its old picture returns (or, if it had none, the picture goes).
        Refused if the picture was changed by hand since, so nobody's later work is overwritten."""
        with self._lock:
            data = self._load()
            proposal = self._proposal(data, proposal_id)
            if proposal["status"] != APPROVED:
                raise GameError(409, "only an approved icon can be undone")
            later = [p for p in data["proposals"] if p["status"] == APPROVED and p["element"] == proposal["element"]]
            if later and later[-1]["id"] != proposal["id"]:
                raise GameError(409, "a newer icon for this element was approved since: undo that one first")
            target = folder / proposal["installed"]
            try:
                now = target.read_bytes() if target.exists() else b""
            except OSError as exc:
                raise GameError(500, "the current picture could not be read") from exc
            if hashlib.sha256(now).hexdigest() != proposal.get("installed_sha"):
                raise GameError(409, "the picture was changed since it was approved, so it was left alone")
            old = self.file(proposal["backup"]) if proposal.get("backup") else None
            try:
                if old is None:
                    target.unlink()
                else:
                    tmp = target.with_name(target.name + ".tmp")
                    tmp.write_bytes(old)
                    os.replace(tmp, target)
            except OSError as exc:
                raise GameError(500, "the old picture could not be put back (see the server console)") from exc
            proposal.update(status=UNDONE, undone_by=admin, handled_at=time.time())
            self._set_design_status(data, proposal, UNDONE)
            self._save(data)
        return {"restored": old is not None}

    def set_preview(self, username: str, is_admin: bool, proposal_id: object, on: object) -> dict[str, Any]:
        """Show (or stop showing) a waiting proposal in the real game, to administrators and designers. Its designer or an
        administrator may; switching one on switches off any other preview of the same element."""
        with self._lock:
            data = self._load()
            proposal = self._proposal(data, proposal_id)
            if not is_admin and key_of(proposal["author"]) != key_of(username):
                raise GameError(403, "only its designer or an administrator can preview this icon")
            if proposal["status"] != WAITING:
                raise GameError(409, "only an icon waiting for an administrator can be previewed")
            if on:
                for other in data["proposals"]:
                    if other["element"] == proposal["element"] and other is not proposal:
                        other.pop("preview", None)
                proposal["preview"] = True
            else:
                proposal.pop("preview", None)
            self._save(data)
        return {"preview": bool(on)}

    def previews(self) -> dict[str, str]:
        """element id -> the stored PNG of the proposal previewed for it (waiting ones only)."""
        return {p["element"]: p["png"] for p in self._load()["proposals"] if p["status"] == WAITING and p.get("preview")}

    def delete_user(self, username: str) -> int:
        """An account is deleted: its designs go. Its proposals stay on record (as recipe proposals do), with the name."""
        who = key_of(username)
        with self._lock:
            data = self._load()
            gone = [k for k, d in data["designs"].items() if key_of(d["author"]) == who]
            files = [data["designs"].pop(k).get("png") for k in gone]
            if gone:
                self._save(data)
        for name in files:
            self._drop_file(name)
        return len(gone)

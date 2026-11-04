"""Draft recipes and promotion proposals.

On the Create Recipes page a player combines any two elements; if the pair has no recipe
yet they name the result, which becomes a *draft*: a recipe (and, if the name is new, an
element) that belongs to that account only and is never used in real games. A draft can be
*promoted*: that sends one *proposal* per recipe (the draft plus the drafts it is built on),
with credit to the author, to the administrators. They review one proposal at a time,
`A + B = C`, and accept or reject it. Accepting does not change recipes.json; the approved
proposals are collected into a snippet that an administrator adds to the recipe file by hand.

Everything lives in one JSON file (drafts.json), written atomically. A damaged file is
never overwritten.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from accounts import key_of
from hints import find_element
from recipes import MAX_ID_LEN, RecipeBook, id_problem, pair_key
from state import GameError

MAX_DRAFT_RECIPES = 200      # per account (bot accounts have no limit)
MAX_PENDING_PROPOSALS = 40   # per account, waiting for an admin (bot accounts have no limit)
MAX_NOTE_LEN = 300
MAX_PROPOSALS = 5000         # handled proposals are dropped oldest-first beyond this
MAX_DEPENDENTS_SHOWN = 60

DRAFT, REQUESTED, APPROVED, REJECTED = "draft", "requested", "approved", "rejected"
PENDING = "pending"


def clean_label(raw: object) -> str:
    """The name for a new element: spaces collapsed, no control characters, 1 to MAX_ID_LEN characters."""
    if not isinstance(raw, str):
        raise GameError(400, "the name must be text")
    name = "".join(ch for ch in " ".join(raw.split()) if unicodedata.category(ch)[0] != "C").strip()
    problem = id_problem(name)
    if problem:
        raise GameError(400, f"the name {problem}" if name else "please type a name")
    return name


def clean_note(raw: object) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise GameError(400, "the note must be text")
    text = "".join(ch for ch in raw.strip() if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return text[:MAX_NOTE_LEN]


class DraftStore:
    """Per-account drafts and the promotion proposals. `path=None` keeps everything in memory (tests)."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time,
                 is_bot: Callable[[str], bool] | None = None) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._is_bot = is_bot  # bot accounts (AccountStore.is_bot) are exempt from the per-account limits
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"version": 2, "users": {}, "proposals": [], "seq": 0}

    # ---- file ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 2, "users": {}, "proposals": [], "seq": 0}
        except (OSError, ValueError) as exc:
            raise GameError(500, f"the drafts file ({self.path.name}) cannot be read; fix or move it") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict) \
                or not isinstance(data.get("proposals"), list):
            raise GameError(500, f"the drafts file ({self.path.name}) is not in the expected format")
        data.setdefault("seq", 0)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise GameError(500, "the draft could not be saved (see the server console)") from exc

    @staticmethod
    def _user(data: dict[str, Any], username: str) -> dict[str, Any]:
        return data["users"].setdefault(key_of(username), {"name": username, "elements": {}, "recipes": [], "seq": 0})

    # ---- helpers -------------------------------------------------------

    def _unlimited(self, username: str) -> bool:
        return bool(self._is_bot and self._is_bot(username))

    @staticmethod
    def _clean_stale(user: dict[str, Any], book: RecipeBook) -> bool:
        """Catch up with the real recipe file. Returns True if anything changed.

        A draft element that now exists for real (an approved one was added) is dropped, and
        drafts that used it point at the real element; a draft whose pair now has a real recipe
        is redundant and dropped too.
        """
        changed = False
        for eid, label in list(user["elements"].items()):
            real = eid if eid in book.elements else find_element(book, label)
            if real is None:
                continue
            del user["elements"][eid]
            changed = True
            if real != eid:
                for r in user["recipes"]:
                    for key in ("a", "b", "result"):
                        if r[key] == eid:
                            r[key] = real
        kept = [r for r in user["recipes"] if not book.combine(r["a"], r["b"])]
        if len(kept) != len(user["recipes"]):
            user["recipes"], changed = kept, True
        return changed

    def _resolve(self, user: dict[str, Any], book: RecipeBook, eid: object) -> str:
        if not isinstance(eid, str) or not (eid in book.elements or eid in user["elements"]):
            raise GameError(400, "unknown element")
        return eid

    def _label(self, user: dict[str, Any], book: RecipeBook, eid: str) -> str:
        return book.elements[eid].label if eid in book.elements else user["elements"][eid]

    def _elements_json(self, user: dict[str, Any], book: RecipeBook, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for eid in ids:
            if eid in book.elements:
                e = book.elements[eid]
                out.append({"id": eid, "label": e.label, "emoji": e.emoji, "draft": False})
            elif eid in user["elements"]:
                out.append({"id": eid, "label": user["elements"][eid], "emoji": "", "draft": True})
        return out

    def _recipe_json(self, user: dict[str, Any], book: RecipeBook, r: dict[str, Any]) -> dict[str, Any]:
        return {**{k: r[k] for k in ("id", "a", "b", "result", "status", "note")},
                "a_label": self._label(user, book, r["a"]), "b_label": self._label(user, book, r["b"]),
                "result_label": self._label(user, book, r["result"]),
                "new_element": r["result"] in user["elements"]}

    # ---- the Create Recipes page ---------------------------------------

    def view(self, username: str, book: RecipeBook) -> dict[str, Any]:
        """This account's draft elements and recipes (after dropping ones the real recipes now cover)."""
        with self._lock:
            data = self._load()
            user = self._user(data, username)
            if self._clean_stale(user, book):
                self._save(data)
            return {
                "elements": self._elements_json(user, book, list(user["elements"])),
                "recipes": [self._recipe_json(user, book, r) for r in user["recipes"]],
            }

    def combine(self, username: str, book: RecipeBook, a: object, b: object) -> list[dict[str, Any]]:
        """What a and b make: a real recipe, else one of this account's drafts, else nothing."""
        with self._lock:
            user = self._user(self._load(), username)
            a, b = self._resolve(user, book, a), self._resolve(user, book, b)
            real = book.combine(a, b)
            if real:
                return self._elements_json(user, book, list(real))
            key = pair_key(a, b)
            return self._elements_json(
                user, book, [r["result"] for r in user["recipes"] if pair_key(r["a"], r["b"]) == key])

    def create(self, username: str, book: RecipeBook, a: object, b: object, raw_name: object) -> dict[str, Any]:
        """Add a draft recipe a + b -> name. A name that matches an existing element gives that element another recipe."""
        name = clean_label(raw_name)
        with self._lock:
            data = self._load()
            user = self._user(data, username)
            a, b = self._resolve(user, book, a), self._resolve(user, book, b)
            real = book.combine(a, b)
            if real:
                raise GameError(409, "that pair already makes " + ", ".join(book.elements[r].label for r in real))
            key = pair_key(a, b)
            if any(pair_key(r["a"], r["b"]) == key for r in user["recipes"]):
                raise GameError(409, "you already made a draft for that pair")
            if len(user["recipes"]) >= MAX_DRAFT_RECIPES and not self._unlimited(username):
                raise GameError(409, f"you have {MAX_DRAFT_RECIPES} drafts already: delete some first")
            result = find_element(book, name)
            if result is None:
                wanted = name.casefold()
                result = next((eid for eid, label in user["elements"].items()
                               if eid.casefold() == wanted or label.casefold() == wanted), None)
            if result is None:
                result = name  # a new element; its id is its name
                user["elements"][result] = name
            user["seq"] += 1
            recipe = {"id": f"r{user['seq']}", "a": a, "b": b, "result": result,
                      "status": DRAFT, "note": "", "proposal": None, "created": self._clock()}
            user["recipes"].append(recipe)
            self._save(data)
            return {"recipe": self._recipe_json(user, book, recipe),
                    "element": self._elements_json(user, book, [result])[0]}

    def delete(self, username: str, book: RecipeBook, rid: object) -> dict[str, Any]:
        """Remove a draft. Not allowed while it is waiting for an admin or if another draft is built on its element."""
        with self._lock:
            data = self._load()
            user = self._user(data, username)
            recipe = next((r for r in user["recipes"] if r["id"] == rid), None)
            if recipe is None:
                raise GameError(404, "no such draft")
            if recipe["status"] == REQUESTED:
                raise GameError(409, "this draft is waiting for an admin; it can be deleted once they answer")
            others = [r for r in user["recipes"] if r is not recipe]
            result = recipe["result"]
            if result in user["elements"] and not any(r["result"] == result for r in others) \
                    and any(result in (r["a"], r["b"]) for r in others):
                raise GameError(409, f"other drafts use {user['elements'][result]!r}: delete those first")
            user["recipes"] = others
            if result in user["elements"] and not any(r["result"] == result for r in others):
                del user["elements"][result]
            self._save(data)
            return self.view(username, book)

    def _needed(self, user: dict[str, Any], book: RecipeBook, recipe: dict[str, Any]) -> list[dict[str, Any]]:
        """The recipe plus, for every draft element it uses, the first draft recipe that makes that element."""
        out: list[dict[str, Any]] = []
        todo = [recipe]
        while todo:
            r = todo.pop(0)
            if any(o is r for o in out):
                continue
            out.append(r)
            for eid in (r["a"], r["b"]):
                if eid in user["elements"]:
                    maker = next((m for m in user["recipes"] if m["result"] == eid and m is not r), None)
                    if maker is not None:
                        todo.append(maker)
        return out

    def promote(self, username: str, book: RecipeBook, rid: object, note: object) -> dict[str, Any]:
        """Send a draft, and the drafts it is built on, to the admins: one proposal per recipe, ingredients first."""
        text = clean_note(note)
        with self._lock:
            data = self._load()
            user = self._user(data, username)
            if self._clean_stale(user, book):
                self._save(data)
            recipe = next((r for r in user["recipes"] if r["id"] == rid), None)
            if recipe is None:
                raise GameError(404, "no such draft")
            if recipe["status"] in (REQUESTED, APPROVED):
                raise GameError(409, "this draft has already been sent to the admins")
            chain = list(reversed(self._needed(user, book, recipe)))  # ingredients first, the draft itself last
            fresh = [r for r in chain if self._open_proposal(data, user["name"], r) is None]
            waiting = sum(1 for p in data["proposals"] if p["status"] == PENDING and key_of(p["user"]) == key_of(username))
            if waiting + len(fresh) > MAX_PENDING_PROPOSALS and not self._unlimited(username):
                raise GameError(409, f"you can have {MAX_PENDING_PROPOSALS} proposals waiting for an admin "
                                     f"({waiting} are waiting now)")
            sent = []
            for r in fresh:
                data["seq"] += 1
                proposal = {
                    "id": f"p{data['seq']}", "user": user["name"], "created": self._clock(),
                    "a": r["a"], "b": r["b"], "result": r["result"],
                    "elements": {eid: user["elements"][eid] for eid in (r["a"], r["b"], r["result"]) if eid in user["elements"]},
                    "note": text if r is recipe else "",
                    "status": PENDING, "handled_by": "", "handled_at": None, "reply": "",
                }
                data["proposals"].append(proposal)
                sent.append(proposal["id"])
                if r["status"] in (DRAFT, REJECTED):
                    r["status"], r["proposal"], r["note"] = REQUESTED, proposal["id"], ""
            if len(data["proposals"]) > MAX_PROPOSALS:
                extra = len(data["proposals"]) - MAX_PROPOSALS
                drop = [p for p in data["proposals"] if p["status"] != PENDING][:extra]
                data["proposals"] = [p for p in data["proposals"] if not any(p is d for d in drop)]
            self._save(data)
            return {"proposals": sent, "recipes": [self._recipe_json(user, book, r) for r in user["recipes"]]}

    @staticmethod
    def _open_proposal(data: dict[str, Any], username: str, recipe: dict[str, Any]) -> dict[str, Any] | None:
        """This account's proposal for exactly this recipe, if one is waiting or approved."""
        return next((p for p in data["proposals"]
                     if p["status"] in (PENDING, APPROVED) and key_of(p["user"]) == key_of(username)
                     and (p["a"], p["b"], p["result"]) == (recipe["a"], recipe["b"], recipe["result"])), None)

    # ---- administrators --------------------------------------------------

    @staticmethod
    def _element(book: RecipeBook, proposal: dict[str, Any], eid: str) -> dict[str, Any]:
        if eid in book.elements:
            e = book.elements[eid]
            return {"id": eid, "label": e.label, "emoji": e.emoji, "draft": False}
        return {"id": eid, "label": proposal["elements"].get(eid, eid), "emoji": "", "draft": True}

    def _dependents(self, data: dict[str, Any], book: RecipeBook, proposal: dict[str, Any]) -> dict[str, Any]:
        """Elements that are built on this proposal's result, so the admin sees what accepting or rejecting touches.

        For an element the game already has, that is every element the game makes with it. Either way it also
        includes elements other players have proposed (waiting or approved) on top of it, and those built on
        them in turn.
        """
        target = proposal["result"]
        seen = {target.casefold()}
        found: list[dict[str, Any]] = []
        if target in book.elements:
            for (x, y), results in book.lookup.items():
                if target in (x, y):
                    for r in results:
                        if r.casefold() not in seen:
                            seen.add(r.casefold())
                            found.append({**self._element(book, proposal, r), "status": "in the game",
                                          "via": f"{book.elements[x].label} + {book.elements[y].label}"})
        frontier = {target.casefold()}
        live = [q for q in data["proposals"] if q["status"] in (PENDING, APPROVED) and q is not proposal]
        grew = True
        while grew:
            grew = False
            for q in live:
                result = q["result"].casefold()
                if result not in seen and (q["a"].casefold() in frontier or q["b"].casefold() in frontier):
                    seen.add(result)
                    frontier.add(result)
                    found.append({**self._element(book, q, q["result"]), "status": q["status"], "user": q["user"],
                                  "via": f"{self._element(book, q, q['a'])['label']} + {self._element(book, q, q['b'])['label']}"})
                    grew = True
        found.sort(key=lambda e: e["label"].casefold())
        return {"total": len(found), "elements": found[:MAX_DEPENDENTS_SHOWN]}

    def _proposal_json(self, data: dict[str, Any], book: RecipeBook, p: dict[str, Any]) -> dict[str, Any]:
        key = pair_key(p["a"], p["b"])
        needs = []
        for eid in dict.fromkeys((p["a"], p["b"])):
            if eid in book.elements:
                continue
            makers = [q["status"] for q in data["proposals"] if q["result"].casefold() == eid.casefold() and q is not p]
            state = APPROVED if APPROVED in makers else PENDING if PENDING in makers else "missing"
            if state != APPROVED:
                needs.append({**self._element(book, p, eid), "status": state})
        return {
            **{k: p[k] for k in ("id", "user", "created", "note", "status", "handled_by", "handled_at", "reply")},
            "a": self._element(book, p, p["a"]), "b": self._element(book, p, p["b"]),
            "result": self._element(book, p, p["result"]),
            "new_element": p["result"] not in book.elements,
            "needs": needs,
            "competing": [
                {"proposal": o["id"], "user": o["user"], "status": o["status"],
                 "result_label": self._element(book, o, o["result"])["label"], "same_result": o["result"] == p["result"]}
                for o in data["proposals"]
                if o is not p and o["status"] in (PENDING, APPROVED) and pair_key(o["a"], o["b"]) == key
            ] if p["status"] in (PENDING, APPROVED) else [],
            "dependents": self._dependents(data, book, p),
        }

    def queue(self, book: RecipeBook) -> list[dict[str, Any]]:
        """Proposals waiting for an admin, oldest first (ingredients come before what is built on them)."""
        with self._lock:
            data = self._load()
            changed = False
            for p in data["proposals"]:
                if p["status"] == PENDING and book.combine(p["a"], p["b"]):
                    p.update(status=REJECTED, handled_by="(automatic)", handled_at=self._clock(),
                             reply="The game already has a recipe for this pair.")
                    changed = True
            if changed:
                self._save(data)
            return [self._proposal_json(data, book, p) for p in data["proposals"] if p["status"] == PENDING]

    def approved(self, book: RecipeBook) -> dict[str, Any]:
        """Approved proposals the recipe file doesn't have yet, with a snippet to add to it (credit on each entry)."""
        with self._lock:
            data = self._load()
            todo = [p for p in data["proposals"] if p["status"] == APPROVED and not book.combine(p["a"], p["b"])]
            elements: dict[str, dict[str, str]] = {}
            for p in todo:
                for eid, label in p["elements"].items():
                    if eid not in book.elements and eid not in elements:
                        elements[eid] = {"label": label, "credit": p["user"]}
            return {
                "proposals": [self._proposal_json(data, book, p) for p in todo],
                "snippet": {"elements": elements,
                            "recipes": [{"a": p["a"], "b": p["b"], "result": p["result"], "credit": p["user"]} for p in todo]},
            }

    def handle(self, admin: str, proposal_id: object, action: object, reply: object) -> dict[str, Any]:
        """Accept or reject one waiting proposal; the author's draft shows the outcome and the admin's reply."""
        if action not in ("approve", "reject"):
            raise GameError(400, "action must be approve or reject")
        text = clean_note(reply)
        with self._lock:
            data = self._load()
            proposal = next((p for p in data["proposals"] if p["id"] == proposal_id), None)
            if proposal is None:
                raise GameError(404, "no such proposal")
            if proposal["status"] != PENDING:
                raise GameError(409, f"that proposal was already {proposal['status']} by {proposal['handled_by']}")
            status = APPROVED if action == "approve" else REJECTED
            proposal.update(status=status, handled_by=admin, handled_at=self._clock(), reply=text)
            self._sync_draft(data, proposal, REQUESTED, status, text)
            self._save(data)
            return {"id": proposal["id"], "status": status}

    def reopen(self, admin: str, proposal_id: object) -> dict[str, Any]:
        """Take back this admin's own earlier answer (the undo of a swipe): the proposal waits again."""
        with self._lock:
            data = self._load()
            proposal = next((p for p in data["proposals"] if p["id"] == proposal_id), None)
            if proposal is None:
                raise GameError(404, "no such proposal")
            if proposal["status"] == PENDING:
                raise GameError(409, "that proposal is already waiting")
            if key_of(proposal["handled_by"]) != key_of(admin):
                raise GameError(403, f"only {proposal['handled_by']} can take that answer back")
            before = proposal["status"]
            proposal.update(status=PENDING, handled_by="", handled_at=None, reply="")
            self._sync_draft(data, proposal, before, REQUESTED, "")
            self._save(data)
            return {"id": proposal["id"], "status": PENDING}

    @staticmethod
    def _sync_draft(data: dict[str, Any], proposal: dict[str, Any], was: str, now: str, note: str) -> None:
        """Mirror a proposal's new status onto the author's draft recipe."""
        user = data["users"].get(key_of(proposal["user"]))
        for r in (user["recipes"] if user else []):
            if r.get("proposal") == proposal["id"] and r["status"] == was:
                r["status"], r["note"] = now, note

    def delete_user(self, username: str) -> int:
        """Remove an account's drafts (used when the account is deleted); returns how many were removed."""
        with self._lock:
            data = self._load()
            user = data["users"].pop(key_of(username), None)
            if user is not None:
                self._save(data)
            return len(user["recipes"]) if user else 0

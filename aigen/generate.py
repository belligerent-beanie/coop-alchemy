"""The generation run: propose new elements, build with them, score, write drafts, send them to the admins."""
from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from drafts import DraftStore, clean_label
from hints import find_element
from recipes import RecipeBook, depths, pair_key
from state import TIME, GameError

from .embeddings import EmbeddingCache
from .lmstudio import LMStudio, LMStudioError
from .propose import (NameIndex, ProposeError, SYSTEM, build_prompt, name_key, normalize_name, parse_reply,
                      pick_examples)
from .scorer import Scorer, dot, features

MAX_USES_PER_ELEMENT = 2   # in the first round, how many proposed pairs may use the same existing element
OVERSAMPLE = 3             # first round asks about this many pairs per wanted new element
LLM_ATTEMPTS = 2


@dataclass
class Settings:
    new_elements: int = 8      # how many brand-new elements the first round should aim for
    max_recipes: int = 30      # cap on recipes (new elements plus the recipes built on them)
    depth: int = 2             # rounds: 1 = new elements only, 2 = also build with them, ...
    partners: int = 6          # existing elements tried with each new element in later rounds
    per_call: int = 8          # pairs per model request
    user: str = "ai-suggester"
    promote: bool = True       # send the drafts to the admin queue
    dry_run: bool = False      # propose and score, but write nothing
    threshold: float | None = None
    temperature: float = 0.8
    seed: int | None = None


@dataclass
class Candidate:
    round: int
    a: str                     # ingredient ids (an existing id, or the id of a new element invented this run)
    b: str
    a_label: str
    b_label: str
    raw: str | None = None     # what the model said
    name: str = ""             # the cleaned result name
    target: str = ""           # id of the existing element the name matched ('' = a new element)
    score: float = 0.0
    verdict: str = "pending"   # 'accepted' or the reason it was dropped
    draft_id: str = ""
    note: str = ""             # text for the admins (a default is made if empty)

    @property
    def is_new(self) -> bool:
        return not self.target


def available_ids(book: RecipeBook) -> set[str]:
    """Elements a game can reach: from the starters, plus Time, which the game unlocks without a recipe."""
    time_id = find_element(book, TIME)
    starters = book.starters if time_id is None or time_id in book.starters else book.starters + (time_id,)
    return set(depths(replace(book, starters=starters)))


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def pick_start_pairs(book: RecipeBook, pool: list[str], wanted: int, rng: random.Random,
                     skip: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Random pairs of existing elements that make nothing yet, using no element more than a couple of times."""
    pairs: list[tuple[str, str]] = []
    uses: dict[str, int] = {}
    seen = set(skip)
    for _ in range(wanted * 50):
        if len(pairs) >= wanted:
            break
        a, b = rng.choice(pool), rng.choice(pool)
        key = pair_key(a, b)
        if key in seen or book.combine(a, b) or uses.get(a, 0) >= MAX_USES_PER_ELEMENT \
                or uses.get(b, 0) >= MAX_USES_PER_ELEMENT:
            continue
        seen.add(key)
        uses[a] = uses.get(a, 0) + 1
        uses[b] = uses.get(b, 0) + 1
        pairs.append(key)
    return pairs


def pick_build_pairs(book: RecipeBook, frontier: list[str], pool: list[str], pool_vecs: dict[str, list[float]],
                     new_vecs: dict[str, list[float]], partners: int, rng: random.Random,
                     skip: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Pair every new element with its nearest existing elements (by meaning) and a few random ones."""
    pairs: list[tuple[str, str]] = []
    seen = set(skip)
    for element in frontier:
        near = sorted(pool, key=lambda e: -dot(new_vecs[element], pool_vecs[e]))[: max(1, partners // 2)]
        chosen = list(near) + rng.sample(pool, min(len(pool), partners - len(near)))
        for partner in chosen:
            key = pair_key(element, partner)
            if key not in seen and not book.combine(element, partner):
                seen.add(key)
                pairs.append(key)
    return pairs


def ask_model(client: LMStudio, book: RecipeBook, labels: dict[str, str], pairs: list[tuple[str, str]], *,
              rnd: int, settings: Settings, rng: random.Random, log: Callable[[str], None]) -> list[Candidate]:
    """One Candidate per pair, with the model's raw answer filled in (None when it had nothing)."""
    candidates: list[Candidate] = []
    for chunk in _chunks(pairs, settings.per_call):
        involved = [e for pair in chunk for e in pair if e in book.elements]
        examples = pick_examples(book, rng, involved)
        prompt = build_prompt([(labels[a], labels[b]) for a, b in chunk], examples, new_only=(rnd == 1))
        answers: dict[int, str | None] = {}
        for attempt in range(LLM_ATTEMPTS):
            try:
                answers = parse_reply(client.chat(SYSTEM, prompt, temperature=settings.temperature), len(chunk))
                break
            except ProposeError as exc:
                log(f"  the model's reply could not be read ({exc}); {'retrying' if attempt + 1 < LLM_ATTEMPTS else 'skipping these pairs'}")
        for n, (a, b) in enumerate(chunk, 1):
            candidates.append(Candidate(rnd, a, b, labels[a], labels[b], raw=answers.get(n)))
        log(f"  asked about {len(chunk)} pairs, got {sum(1 for n in answers if answers[n])} suggestions")
    return candidates


def run(settings: Settings, *, book: RecipeBook, client: LMStudio, cache: EmbeddingCache, scorer: Scorer,
        store: DraftStore, log: Callable[[str], None] = print) -> dict[str, Any]:
    rng = random.Random(settings.seed)
    threshold = settings.threshold if settings.threshold is not None else scorer.threshold
    pool = sorted(available_ids(book))
    labels: dict[str, str] = {e: book.elements[e].label for e in book.elements}
    pool_vecs = dict(zip(pool, cache.get([labels[e] for e in pool])))

    names = NameIndex.from_book(book)
    for element in store.view(settings.user, book)["elements"]:  # drafts the bot already has
        names.add(element["label"], element["id"])
        labels[element["id"]] = element["label"]

    new_vecs: dict[str, list[float]] = {}
    accepted: list[Candidate] = []
    everything: list[Candidate] = []
    asked: set[tuple[str, str]] = set()
    frontier: list[str] = []

    for rnd in range(1, settings.depth + 1):
        room = settings.max_recipes - len(accepted)
        if room <= 0:
            break
        if rnd == 1:
            pairs = pick_start_pairs(book, pool, settings.new_elements * OVERSAMPLE, rng, asked)
        else:
            if not frontier:
                break
            pairs = pick_build_pairs(book, frontier, pool, pool_vecs, new_vecs, settings.partners, rng, asked)
        asked.update(pairs)
        log(f"round {rnd}: {len(pairs)} pairs")
        candidates = ask_model(client, book, labels, pairs, rnd=rnd, settings=settings, rng=rng, log=log)

        # Clean each answer, then score it.
        for c in candidates:
            c.name = normalize_name(c.raw) or ""
            if c.raw is None:
                c.verdict = "model had no idea"
            elif not c.name:
                c.verdict = f"unusable name {c.raw!r}"
        usable = [c for c in candidates if c.name]
        vecs = cache.get([c.name for c in usable])
        for c, vr in zip(usable, vecs):
            va = pool_vecs.get(c.a) or new_vecs[c.a]
            vb = pool_vecs.get(c.b) or new_vecs[c.b]
            c.score = scorer.score(features(va, vb, vr, c.a_label, c.b_label, c.name))
            new_vecs.setdefault(clean_label(c.name), vr)
        usable.sort(key=lambda c: -c.score)

        new_this_round: list[str] = []
        limit = settings.new_elements if rnd == 1 else room
        for c in usable:
            if len(accepted) >= settings.max_recipes or (rnd == 1 and len(new_this_round) >= limit):
                c.verdict = "over the limit"
                continue
            if c.score < threshold:
                c.verdict = f"score {c.score:.2f} is below {threshold:.2f}"
                continue
            existing = names.lookup(c.name)
            if existing in (c.a, c.b) or name_key(c.name) in (name_key(c.a_label), name_key(c.b_label)):
                c.verdict = "same as an ingredient"
                continue
            if existing and rnd == 1:
                c.verdict = f"already exists as {labels.get(existing, existing)!r}"
                continue
            c.target = existing or ""
            if c.is_new:
                try:
                    element_id = clean_label(c.name)
                except GameError as exc:
                    c.verdict = f"bad name: {exc}"
                    continue
                names.add(c.name, element_id)
                labels[element_id] = c.name
                new_this_round.append(element_id)
            c.verdict = "accepted"
            accepted.append(c)
        everything += candidates
        frontier = new_this_round
        log(f"  kept {sum(1 for c in candidates if c.verdict == 'accepted')} recipes, "
            f"{len(new_this_round)} of them new elements")

    written = [] if settings.dry_run else write_drafts(settings, book, store, accepted, log)
    report = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": asdict(settings), "threshold": threshold,
        "chat_model": client.chat_model, "embed_model": client.embed_model,
        "accepted": [asdict(c) for c in accepted], "candidates": [asdict(c) for c in everything],
        "written_drafts": written,
    }
    return report


def write_drafts(settings: Settings, book: RecipeBook, store: DraftStore, accepted: list[Candidate],
                 log: Callable[[str], None]) -> list[str]:
    """Create the drafts in order (so new elements exist before they are used), then promote them."""
    made: list[Candidate] = []
    for c in accepted:
        try:
            out = store.create(settings.user, book, c.a, c.b, c.name)
        except GameError as exc:
            c.verdict = f"draft refused: {exc}"
            log(f"  skipped {c.a_label} + {c.b_label} = {c.name}: {exc}")
            continue
        c.draft_id = out["recipe"]["id"]
        made.append(c)
    if settings.promote:
        for c in made:
            note = c.note or f"AI-suggested (plausibility {c.score:.2f}, round {c.round})"
            try:
                store.promote(settings.user, book, c.draft_id, note)
            except GameError as exc:
                if exc.status == 409 and "already been sent" in str(exc):
                    continue
                log(f"  stopped sending to the admins: {exc}; the rest stay as drafts")
                break
    return [c.draft_id for c in made]


def save_report(report: dict[str, Any], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("run-%Y%m%d-%H%M%S.json")
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return path

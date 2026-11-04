"""Target mode: given a noun, find a chain of recipes from existing elements that ends in it.

Works backwards. For the goal it gathers candidate ingredient pairs from three places:
  * the language model, which sees every existing element and proposes pairs;
  * the code, which pairs elements of the same KIND as the goal (bread, cake) with elements it LOOKS LIKE
    (moon, horn), so shape-based recipes such as moon + bread = croissant are always considered;
  * helper elements the model invents when no pair of existing elements works (up to `max_intermediates`).
A second request then rates all candidates in one batch (so they are compared with each other, not graded by the
model that wrote them). The final score is mostly that rating, with the scorer trained on recipes.json as a
tiebreaker. A chain is ranked by its weakest link.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from drafts import DraftStore, clean_label
from recipes import RecipeBook, pair_key
from state import GameError

from .embeddings import EmbeddingCache
from .generate import Candidate, Settings, available_ids, write_drafts
from .lmstudio import LMStudio
from .ratings import RatingCache
from .propose import (Judgement, NameIndex, ProposeError, RawOption, SYSTEM, build_goal_prompt, build_judge_prompt,
                      build_profile_prompt, name_key, normalize_name, parse_options, parse_profile, parse_ratings,
                      pick_examples)
from .scorer import Scorer, dot, features

NEW_ELEMENT_PENALTY = 0.10   # between similar chains, prefer the one that invents fewer elements
LLM_ATTEMPTS = 2
JUDGE_WEIGHT = 0.14          # score = 0.14 * rating (1-5) + 0.30 * scorer value; a 3 passes, a 2 needs a good scorer
SCORER_WEIGHT = 0.30
MAX_GENERATED = 14           # kind x look-alike pairs added by the code
MAX_JUDGED = 24              # candidates rated per question
JUDGE_CHUNK = 8              # candidates per rating request (shorter answers are not cut off)
JUDGE_TOKENS = 3500          # room for the answer, since a reasoning model spends tokens thinking first
GENERIC_RIVALS = 3           # a pair the judge says could just as easily make this many other things is capped at 3


@dataclass
class PlanSettings(Settings):
    max_intermediates: int = 2   # new helper elements allowed per target (the target itself is extra)
    options: int = 6             # ingredient pairs requested per question
    beam: int = 3                # chains kept per target (only the best is written)
    expand: int = 3              # top-level options followed into sub-goals
    shortlist: int = 0           # elements shown to the model: 0 = all of them (A-Z), else the nearest N
    hint_neighbors: int = 4      # with --shortlist: plus this many nearest to each look-alike phrase
    connectors: int = 20         # with --shortlist: plus this many of the most used ingredients
    min_rating: int = 4          # every link of a chain must be rated at least this (1-5) by the judging step
    verbose: bool = False        # print every option and why it was kept or dropped
    max_targets: int = 0         # stop after this many targets that needed the model (0 = no limit)
    token_budget: int = 0        # stop before a target once this many chat tokens are used (0 = no limit)


@dataclass
class Link:
    a: str
    b: str
    a_label: str
    b_label: str
    result: str          # label; the id of a new element is clean_label(result)
    score: float         # what the chain is judged by
    semantic: float = 0.0   # the trained scorer's own value
    link_type: str = ""     # shape, material, function, origin or other
    why: str = ""           # short reason
    rating: int = 0         # the rating pass, 1 to 5 (after the generic-pair cap)
    reads_as: str = ""      # what the pair literally suggests, e.g. 'moon-shaped bread'


@dataclass
class Plan:
    links: list[Link]    # ingredients first, the goal last
    score: float         # weakest link
    new_elements: int

    @property
    def rank(self) -> float:
        return self.score - NEW_ELEMENT_PENALTY * self.new_elements


@dataclass
class Option:
    link: Link
    new_ingredient: str | None   # label of the helper element this option needs, if any


@dataclass
class TargetResult:
    target: str
    status: str                  # 'planned', 'exists', 'invalid' or 'no chain'
    detail: str = ""
    plan: Plan | None = None
    alternatives: list[Plan] = field(default_factory=list)


@dataclass
class _Cand:
    raw: RawOption
    ids: list[str]
    labels: list[str]
    new: str | None
    entry: dict[str, Any]
    semantic: float = 0.0


class Planner:
    def __init__(self, settings: PlanSettings, *, book: RecipeBook, client: LMStudio, cache: EmbeddingCache,
                 scorer: Scorer, store: DraftStore, log: Callable[[str], None],
                 ratings: RatingCache | None = None) -> None:
        self.ratings = ratings or RatingCache(None, client.chat_model or "")
        self.s, self.book, self.client, self.cache, self.scorer, self.log = settings, book, client, cache, scorer, log
        self.rng = random.Random(settings.seed)
        self.trace: list[dict[str, Any]] = []   # every candidate, and what became of it
        self.profiles: list[dict[str, Any]] = []   # how the model described each goal
        self.threshold = settings.threshold if settings.threshold is not None else scorer.threshold
        self.labels: dict[str, str] = {e: book.elements[e].label for e in book.elements}
        pool = sorted(available_ids(book))
        self.vecs: dict[str, list[float]] = dict(zip(pool, cache.get([self.labels[e] for e in pool])))
        self.names = NameIndex.from_book(book)
        self.taken: set[tuple[str, str]] = set(book.lookup)
        view = store.view(settings.user, book)  # drafts this account already has are usable ingredients
        extra = view["elements"]
        for element, vec in zip(extra, cache.get([e["label"] for e in extra])):
            self.names.add(element["label"], element["id"])
            self.labels[element["id"]] = element["label"]
            self.vecs[element["id"]] = vec
        self.taken |= {pair_key(r["a"], r["b"]) for r in view["recipes"]}
        uses: dict[str, int] = {}
        for a, b in book.lookup:
            uses[a] = uses.get(a, 0) + 1
            uses[b] = uses.get(b, 0) + 1
        self.connectors = [e for e, _ in sorted(uses.items(), key=lambda kv: -kv[1]) if e in self.vecs][:settings.connectors]

    # ---- what the goal is like -----------------------------------------------------

    def _profile(self, goal: str, available: list[str] | None) -> dict[str, list[str]]:
        try:
            return parse_profile(self.client.chat(SYSTEM, build_profile_prompt(goal, available), temperature=0.4,
                                                  effort="medium"))
        except ProposeError as exc:
            self.log(f"    no description of {goal!r} ({exc}); using meaning only")
            return {}

    def _elements_for(self, phrases: list[str], goal: str, *, neighbors: int) -> list[str]:
        """Existing elements named by the phrases; for a phrase that is not an element (such as 'baked good'),
        its `neighbors` nearest elements by meaning (bread, cake ...). Exact matches come first."""
        exact: list[str] = []
        missing: list[str] = []
        for phrase in phrases:
            element = self.names.lookup(phrase)
            if element in self.vecs:
                exact.append(element)
            else:
                missing.append(phrase)
        near: list[str] = []
        for vec in self.cache.get(missing) if missing else []:
            near += sorted(self.vecs, key=lambda e: -dot(vec, self.vecs[e]))[:neighbors]
        return [e for e in dict.fromkeys(exact + near) if name_key(self.labels[e]) != name_key(goal)]

    def _generated(self, profile: dict[str, list[str]], goal: str) -> list[RawOption]:
        """Pairs of (an element of the same kind as the goal) x (an element the goal looks like).

        The model picks them from the element list; otherwise (a short --shortlist) free-text phrases are matched to
        elements. A phrase that is not an element is replaced by its nearest element(s) by meaning."""
        if "kind_elements" in profile or "look_elements" in profile:
            # A pick that is not an element ("crescent") is replaced by its nearest element, so one slip cannot empty a group.
            kinds = self._elements_for(profile.get("kind_elements", []), goal, neighbors=1)[:5]
            looks = self._elements_for(profile.get("look_elements", []), goal, neighbors=1)[:5]
        else:
            kinds = self._elements_for(profile.get("kind_of", []), goal, neighbors=3)[:5]
            looks = self._elements_for(profile.get("resembles", []) + profile.get("looks_like", []), goal, neighbors=1)[:4]
        out = [RawOption(self.labels[k], self.labels[l], "shape", f"{self.labels[k]} shaped like {self.labels[l]}", 0,
                         "generated") for l in looks for k in kinds if k != l]
        return out[:MAX_GENERATED]

    def _shortlist(self, goal_vec: list[float], profile: dict[str, list[str]]) -> list[str]:
        """Every element (A-Z) or, with --shortlist N, the N nearest the goal plus those matching the profile."""
        if self.s.shortlist <= 0:
            return sorted(self.vecs, key=lambda e: self.labels[e].casefold())
        chosen = sorted(self.vecs, key=lambda e: -dot(goal_vec, self.vecs[e]))[: self.s.shortlist]
        phrases = [p for key in profile for p in profile[key]]
        for phrase, vec in zip(phrases, self.cache.get(phrases) if phrases else []):
            exact = self.names.lookup(phrase)
            chosen += ([exact] if exact in self.vecs else []) + \
                sorted(self.vecs, key=lambda e: -dot(vec, self.vecs[e]))[: self.s.hint_neighbors]
        chosen += self.connectors
        return list(dict.fromkeys(chosen))

    # ---- candidates ----------------------------------------------------------------

    def _resolve(self, raw: str, forbidden: set[str]) -> tuple[str, str]:
        """('old', id) existing element, ('new', name) helper to invent, or ('bad', reason)."""
        name = normalize_name(raw)
        if name is None:
            return ("bad", f"{raw!r} is not a usable name")
        if name_key(name) in forbidden:
            return ("bad", f"{name!r} is the goal itself")
        existing = self.names.lookup(name)
        if existing is not None:
            return ("old", existing) if existing in self.vecs else ("bad", f"{name!r} is not available")
        parts = [w for w in re.split(r"[\s-]+", name) if w]
        head = self.names.lookup(parts[-1])
        if len(parts) > 1 and head in self.vecs:
            return ("bad", f"{name!r} is just a variant of the existing {self.labels[head]!r}; use that instead")
        return ("new", name)

    def _ask_model(self, goal: str, shown: list[str], hints: list[str], may_invent: bool) -> list[RawOption]:
        prompt = build_goal_prompt(goal, [self.labels[e] for e in shown],
                                   pick_examples(self.book, self.rng, [], general=12),
                                   options=self.s.options, may_invent=may_invent, hints=hints)
        for _ in range(LLM_ATTEMPTS):
            try:
                return parse_options(self.client.chat(SYSTEM, prompt, temperature=self.s.temperature))
            except ProposeError as exc:
                self.log(f"    the model's reply for {goal!r} could not be read ({exc})")
        return []

    def _rate(self, goal: str, cands: list[_Cand]) -> dict[int, Judgement]:
        """Judge every candidate (index in `cands`), JUDGE_CHUNK at a time. Entries the model skipped or cut off are
        asked again once; whatever is still missing is simply absent from the result."""
        out: dict[int, Judgement] = {}
        for i, cand in enumerate(cands):   # ratings remembered from earlier runs cost nothing
            known = self.ratings.get(goal, cand.labels[0], cand.labels[1])
            if known is not None:
                out[i] = known
        todo = [i for i in range(len(cands)) if i not in out]
        for start in range(0, len(todo), JUDGE_CHUNK):
            pending = todo[start:start + JUDGE_CHUNK]
            for attempt in range(LLM_ATTEMPTS):
                if not pending:
                    break
                prompt = build_judge_prompt(goal, [(cands[i].labels[0], cands[i].labels[1]) for i in pending])
                try:
                    got = parse_ratings(self.client.chat(SYSTEM, prompt, temperature=0.2, effort="medium",
                                                         max_tokens=JUDGE_TOKENS), len(pending))
                except ProposeError as exc:
                    self.log(f"    the rating reply for {goal!r} could not be read ({exc})"
                             + ("; asking again" if attempt + 1 < LLM_ATTEMPTS else ""))
                    continue
                for n, judgement in got.items():
                    out[pending[n - 1]] = judgement
                    self.ratings.put(goal, cands[pending[n - 1]].labels[0], cands[pending[n - 1]].labels[1], judgement)
                pending = [i for i in pending if i not in out]
        return out

    def options_for(self, goal: str, *, may_invent: bool, forbidden: set[str]) -> list[Option]:
        goal_vec = self.cache.get([goal])[0]
        everything = self.s.shortlist <= 0
        profile = self._profile(goal, [self.labels[e] for e in self._shortlist(goal_vec, {})] if everything else None)
        hints = list(dict.fromkeys(p for key in profile for p in profile[key]))
        shown = self._shortlist(goal_vec, profile)
        generated = self._generated(profile, goal)
        raws = self._ask_model(goal, shown, hints, may_invent) + generated
        self.profiles.append({"goal": goal, "profile": profile, "generated_pairs": len(generated)})
        if self.s.verbose:
            self.log(f"      {goal} is described as {profile or 'nothing (no description)'}; "
                     f"{len(generated)} kind x look-alike pairs")
        if not raws:
            self.trace.append({"goal": goal, "verdict": "no candidates at all"})

        cands: list[_Cand] = []
        seen: set[tuple[str, str]] = set()
        for raw in raws:
            entry: dict[str, Any] = {"goal": goal, "a": raw.a, "b": raw.b, "link": raw.link, "why": raw.why,
                                     "self_rating": raw.rating, "source": raw.source, "hints": hints, "shown": len(shown)}
            self.trace.append(entry)
            ra, rb = self._resolve(raw.a, forbidden), self._resolve(raw.b, forbidden)
            bad = [r[1] for r in (ra, rb) if r[0] == "bad"]
            if bad:
                entry["verdict"] = "dropped: " + bad[0]
                continue
            if ra[0] == "new" and rb[0] == "new":
                entry["verdict"] = "dropped: both ingredients are new"
                continue
            if not may_invent and "new" in (ra[0], rb[0]):
                entry["verdict"] = "dropped: needs a new helper element and none is allowed here"
                continue
            ids, labels, new = [], [], None
            for kind, value in (ra, rb):
                if kind == "old":
                    ids.append(value); labels.append(self.labels[value])
                else:
                    ids.append(clean_label(value)); labels.append(value); new = value
            key = pair_key(ids[0], ids[1])
            if key in seen:
                entry["verdict"] = "dropped: the same pair twice"
                continue
            if key in self.taken:
                entry["verdict"] = "dropped: this pair already has a recipe"
                continue
            seen.add(key)
            if len(cands) >= MAX_JUDGED:
                entry["verdict"] = "dropped: too many candidates to rate"
                continue
            cands.append(_Cand(raw, ids, labels, new, entry))

        ratings = self._rate(goal, cands) if cands else {}
        if cands and not ratings:
            self.log(f"    the judging step failed for {goal!r}; nothing can be accepted without it")
        found: list[Option] = []
        for i, c in enumerate(cands):
            va, vb = (self.vecs[x] if x in self.vecs else self.cache.get([l])[0] for x, l in zip(c.ids, c.labels))
            c.semantic = self.scorer.score(features(va, vb, goal_vec, c.labels[0], c.labels[1], goal))
            judgement = ratings.get(i)
            if judgement is None:  # no fallback to the proposer's own rating: that is what let generic pairs win
                c.entry["verdict"] = "dropped: the judging step did not rate it"
                continue
            rating = judgement.rating
            c.entry.update(judge_rating=judgement.rating, reads_as=judgement.reads_as, others=list(judgement.others))
            if len(judgement.others) >= GENERIC_RIVALS and rating > 3:
                rating = 3  # the pair is generic: it fits several other results just as well
                c.entry["generic"] = True
            score = JUDGE_WEIGHT * rating + SCORER_WEIGHT * c.semantic
            c.entry.update(rating=rating, semantic=round(c.semantic, 3), score=round(score, 3), new_helper=c.new)
            if rating < self.s.min_rating:
                c.entry["verdict"] = f"dropped: rated only {rating}/5 (need {self.s.min_rating})"
                continue
            if score < self.threshold:
                c.entry["verdict"] = f"dropped: score {score:.2f} is below {self.threshold:.2f}"
                continue
            c.entry["verdict"] = "kept"
            found.append(Option(Link(c.ids[0], c.ids[1], c.labels[0], c.labels[1], goal, score, c.semantic,
                                     c.raw.link, c.raw.why, rating, judgement.reads_as), c.new))
        if self.s.verbose:
            for entry in self.trace[-len(raws):] if raws else []:
                self.log(f"      {entry.get('a', '?')} + {entry.get('b', '?')} [{entry.get('link') or '-'}, "
                         f"{entry.get('source')}, rated {entry.get('rating', '-')}{' (generic)' if entry.get('generic') else ''}] "
                         f"{entry.get('why', '')}; reads as {entry.get('reads_as') or '?'!r}, could also be "
                         f"{', '.join(entry.get('others') or []) or '-'} -> {entry['verdict']}")
        return sorted(found, key=lambda o: -o.link.score)

    # ---- the search ----------------------------------------------------------------

    def solve(self, goal: str, *, left: int, top: bool, forbidden: set[str]) -> list[Plan]:
        plans: list[Plan] = []
        options = self.options_for(goal, may_invent=left > 0, forbidden=forbidden)
        self.log(f"    {'  ' * (self.s.max_intermediates - left)}{goal}: {len(options)} usable options")
        for option in options[: self.s.expand if top else 2]:
            if option.new_ingredient is None:
                plans.append(Plan([option.link], option.link.score, 0))
                continue
            for sub in self.solve(option.new_ingredient, left=left - 1, top=False,
                                  forbidden=forbidden | {name_key(goal)})[:1]:
                links = sub.links + [option.link]
                pairs = [pair_key(l.a, l.b) for l in links]
                if len(set(pairs)) == len(pairs):
                    plans.append(Plan(links, min(sub.score, option.link.score), sub.new_elements + 1))
        return sorted(plans, key=lambda p: -p.rank)[: self.s.beam]

    def plan(self, raw_target: str) -> TargetResult:
        target = normalize_name(raw_target)
        if target is None:
            return TargetResult(raw_target, "invalid", "not a usable element name (letters, spaces and hyphens, up to 3 words)")
        existing = self.names.lookup(target)
        if existing is not None:
            return TargetResult(target, "exists", f"already an element ({self.labels.get(existing, existing)!r}); skipped")
        self.log(f"  planning {target!r}")
        plans = self.solve(target, left=self.s.max_intermediates, top=True, forbidden={name_key(target)})
        if not plans:
            misses = sorted((e for e in self.trace if e.get("goal") == target and "score" in e), key=lambda e: -e["score"])[:3]
            closest = "; ".join(f"{e['a']} + {e['b']} ({e.get('rating', '-')}/5)" for e in misses)
            return TargetResult(target, "no chain", "nothing was good enough"
                                + (f". Closest: {closest}" if closest else "")
                                + f". Lower --min-rating (now {self.s.min_rating}) to accept weaker recipes, or use --verbose")
        best = plans[0]
        for link in best.links:  # later targets may build on what this chain creates
            rid = clean_label(link.result)
            self.names.add(link.result, rid)
            self.labels[rid] = link.result
            self.vecs[rid] = self.cache.get([link.result])[0]
            self.taken.add(pair_key(link.a, link.b))
        return TargetResult(target, "planned", plan=best, alternatives=plans[1:])


def plan_targets(targets: list[str], settings: PlanSettings, *, book: RecipeBook, client: LMStudio,
                 cache: EmbeddingCache, scorer: Scorer, store: DraftStore,
                 log: Callable[[str], None] = print, ratings: RatingCache | None = None) -> dict[str, Any]:
    planner = Planner(settings, book=book, client=client, cache=cache, scorer=scorer, store=store, log=log,
                      ratings=ratings)
    results: list[TargetResult] = []
    accepted: list[Candidate] = []
    written: list[Any] = []
    stopped = ""
    used = 0   # targets that needed the model
    for target in dict.fromkeys(t.strip() for t in targets if t.strip()):
        if settings.max_targets and used >= settings.max_targets:
            stopped = f"stopped before {target!r}: --max-targets {settings.max_targets} reached"
            log(f"\n{stopped}. Run again for the rest.")
            break
        if settings.token_budget and client.usage["total"] >= settings.token_budget:
            stopped = (f"stopped before {target!r}: used {client.usage['total']:,} tokens "
                       f"(--token-budget {settings.token_budget:,})")
            log(f"\n{stopped}. Run again for the rest.")
            break
        try:
            res = planner.plan(target)
        except Exception as exc:  # e.g. a daily rate limit: keep what is already done
            planner.ratings.save()
            stopped = f"stopped at {target!r}: {exc}"
            log(f"\n{stopped}\nEverything before it has been saved; run the remaining targets again later.")
            break
        planner.ratings.save()   # kept even if a later target fails
        if res.status not in ("exists", "invalid"):
            used += 1
        results.append(res)
        if res.plan is None:
            continue
        mine: list[Candidate] = []
        for depth, link in enumerate(res.plan.links, 1):
            mine.append(Candidate(
                round=depth, a=link.a, b=link.b, a_label=link.a_label, b_label=link.b_label, raw=link.result,
                name=link.result, score=link.score, verdict="accepted",
                note=f"AI-suggested for the target '{res.target}' (link {depth} of {len(res.plan.links)}, "
                     f"rating {link.rating}/5, plausibility {link.score:.2f}"
                     + (f"; {link.link_type}: {link.why}" if link.why else "")
                     + (f"; reads as '{link.reads_as}'" if link.reads_as else "") + ")"))
        accepted.extend(mine)
        if not settings.dry_run:  # saved target by target, so a later failure loses nothing
            written.extend(write_drafts(settings, book, store, mine, log))
    return {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": asdict(settings), "threshold": planner.threshold,
        "chat_model": client.chat_model, "embed_model": client.embed_model,
        "targets": [{"target": r.target, "status": r.status, "detail": r.detail,
                     "plan": asdict(r.plan) if r.plan else None, "alternatives": [asdict(p) for p in r.alternatives]}
                    for r in results],
        "accepted": [asdict(c) for c in accepted], "written_drafts": written, "stopped": stopped, "trace": planner.trace,
        "profiles": planner.profiles,
        "usage": dict(client.usage), "rating_cache_hits": planner.ratings.hits,
    }

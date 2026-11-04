"""Plausibility scorer: a small logistic regression, trained in pure Python on the real recipes.

For a candidate recipe `a + b = r` it looks at how close the embeddings of the ingredients and the
result are (plus whether the result reuses an ingredient's word) and returns a value between 0 and 1
that says how much the recipe resembles the ones in recipes.json. Real recipes are the positive
examples; negatives are the same recipes with a random result or a random ingredient swapped in.

The features deliberately say nothing about the result *itself* (such as whether it already exists),
because the scorer has to judge brand-new element names.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from recipes import RecipeBook

FEATURES = ("best_ingredient_sim", "worst_ingredient_sim", "ingredient_sim", "blend_sim",
            "shares_word", "word_count")
MIN_RECIPES = 30
HOLDOUT = 0.1
RANK_CANDIDATES = 99


def dot(u: Sequence[float], v: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(u, v))


def _stem(word: str) -> str:
    word = word.casefold()
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def word_set(label: str) -> set[str]:
    return {_stem(w) for w in "".join(ch if ch.isalpha() else " " for ch in label).split()}


def features(va: Sequence[float], vb: Sequence[float], vr: Sequence[float],
             a_label: str, b_label: str, r_label: str) -> list[float]:
    """Order-independent in a and b. The vectors must be unit length."""
    sa, sb = dot(va, vr), dot(vb, vr)
    blend = [x + y for x, y in zip(va, vb)]
    norm = math.sqrt(dot(blend, blend)) or 1.0
    shares = 1.0 if word_set(r_label) & (word_set(a_label) | word_set(b_label)) else 0.0
    return [max(sa, sb), min(sa, sb), dot(va, vb), dot(blend, vr) / norm, shares, float(len(r_label.split()))]


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def auc(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """Chance that a random positive outscores a random negative (ties count half)."""
    if not positives or not negatives:
        return float("nan")
    ranked = sorted([(s, 1) for s in positives] + [(s, 0) for s in negatives])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        average_rank = (i + 1 + j) / 2
        rank_sum += average_rank * sum(label for _, label in ranked[i:j])
        i = j
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


@dataclass
class Scorer:
    weights: list[float]
    bias: float
    mean: list[float]
    scale: list[float]
    threshold: float
    embed_tag: str
    metrics: dict[str, Any]

    def score(self, feats: Sequence[float]) -> float:
        z = self.bias + sum(w * (f - m) / s for w, f, m, s in zip(self.weights, feats, self.mean, self.scale))
        return _sigmoid(z)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "features": list(FEATURES), "weights": self.weights, "bias": self.bias, "mean": self.mean,
            "scale": self.scale, "threshold": self.threshold, "embed_tag": self.embed_tag,
            "metrics": self.metrics}, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Scorer":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data["features"] != list(FEATURES):
                raise ValueError("the feature list changed; train again")
            return cls(data["weights"], data["bias"], data["mean"], data["scale"],
                       data["threshold"], data["embed_tag"], data.get("metrics", {}))
        except FileNotFoundError as exc:
            raise ValueError(f"{path} does not exist: run 'python3 -m aigen train' first") from exc
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"cannot use the scorer file {path}: {exc}") from exc


def _fit(X: list[list[float]], y: list[int], *, iterations: int = 300, lr: float = 0.5,
         l2: float = 1e-3) -> tuple[list[float], float]:
    n, d = len(X), len(X[0])
    w, b = [0.0] * d, 0.0
    for _ in range(iterations):
        gw, gb = [0.0] * d, 0.0
        for xi, yi in zip(X, y):
            err = _sigmoid(b + sum(wj * xj for wj, xj in zip(w, xi))) - yi
            gb += err
            for j in range(d):
                gw[j] += err * xi[j]
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return w, b


def recipe_triples(book: RecipeBook) -> list[tuple[str, str, str]]:
    return [(a, b, r) for (a, b), results in book.lookup.items() for r in results]


def train(book: RecipeBook, embed: Callable[[list[str]], list[list[float]]], embed_tag: str, *,
          seed: int = 0, log: Callable[[str], None] = print) -> Scorer:
    """Fit the scorer on a recipe book. `embed` maps labels to unit vectors."""
    triples = recipe_triples(book)
    if len(triples) < MIN_RECIPES:
        raise ValueError(f"need at least {MIN_RECIPES} recipes to train, found {len(triples)}")
    rng = random.Random(seed)
    ids = list(book.elements)
    vec = dict(zip(ids, embed([book.elements[i].label for i in ids])))

    def feat(a: str, b: str, r: str) -> list[float]:
        label = lambda e: book.elements[e].label  # noqa: E731
        return features(vec[a], vec[b], vec[r], label(a), label(b), label(r))

    def negatives(a: str, b: str, r: str) -> list[tuple[str, str, str]]:
        true_results = set(book.combine(a, b))
        wrong = rng.choice([e for e in rng.sample(ids, 5) if e not in true_results] or ids)
        swapped = rng.choice(ids)
        while r in book.combine(swapped, b):
            swapped = rng.choice(ids)
        return [(a, b, wrong), (swapped, b, r)]

    rng.shuffle(triples)
    cut = max(1, int(len(triples) * HOLDOUT))
    held, fit_set = triples[:cut], triples[cut:]

    def examples(rows: list[tuple[str, str, str]]) -> tuple[list[list[float]], list[int]]:
        X, y = [], []
        for t in rows:
            X.append(feat(*t)); y.append(1)
            for n in negatives(*t):
                X.append(feat(*n)); y.append(0)
        return X, y

    def standardise(X: list[list[float]], mean: list[float], scale: list[float]) -> list[list[float]]:
        return [[(x - m) / s for x, m, s in zip(row, mean, scale)] for row in X]

    def fit_on(X: list[list[float]], y: list[int]) -> Scorer:
        d = len(X[0])
        mean = [sum(row[j] for row in X) / len(X) for j in range(d)]
        scale = [math.sqrt(sum((row[j] - mean[j]) ** 2 for row in X) / len(X)) or 1.0 for j in range(d)]
        w, b = _fit(standardise(X, mean, scale), y)
        return Scorer(w, b, mean, scale, 0.5, embed_tag, {})

    log(f"training on {len(fit_set)} recipes, checking on {len(held)} held-out ones ...")
    X_train, y_train = examples(fit_set)
    model = fit_on(X_train, y_train)

    X_held, y_held = examples(held)
    scores = [model.score(x) for x in X_held]
    pos = [s for s, y in zip(scores, y_held) if y == 1]
    neg = sorted(s for s, y in zip(scores, y_held) if y == 0)
    threshold = neg[min(len(neg) - 1, int(0.8 * len(neg)))]  # lets 20% of the wrong recipes through
    recall = sum(1 for s in pos if s >= threshold) / len(pos)

    ranks = []
    for a, b, r in held:
        true_score = model.score(feat(a, b, r))
        others = [e for e in rng.sample(ids, min(len(ids), RANK_CANDIDATES + 5)) if e != r][:RANK_CANDIDATES]
        ranks.append(1 + sum(1 for e in others if model.score(feat(a, b, e)) > true_score))
    ranks.sort()
    metrics = {
        "recipes_total": len(triples), "held_out": len(held),
        "auc": round(auc(pos, neg), 4), "threshold": round(threshold, 4), "recall_at_threshold": round(recall, 4),
        "true_result_top1": round(sum(1 for r in ranks if r == 1) / len(ranks), 4),
        "true_result_top10_of_100": round(sum(1 for r in ranks if r <= 10) / len(ranks), 4),
        "true_result_median_rank_of_100": ranks[len(ranks) // 2],
    }

    final = fit_on(*examples(triples))  # refit on everything; keep the threshold measured on held-out data
    final.threshold, final.metrics = threshold, metrics
    return final

"""Embedding cache: element names -> unit vectors, stored in a JSON file so each name is embedded once."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Callable, Sequence


def unit(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class EmbeddingCache:
    """`embed_fn` turns a list of texts into vectors; `tag` names the model so a changed model resets the cache."""

    def __init__(self, embed_fn: Callable[[list[str]], list[list[float]]], path: str | Path | None, tag: str) -> None:
        self._embed = embed_fn
        self.path = Path(path) if path else None
        self.tag = tag
        self._vectors: dict[str, list[float]] = {}
        self._dirty = False
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("model") == tag and isinstance(data.get("vectors"), dict):
                    self._vectors = data["vectors"]
            except (OSError, ValueError):
                pass  # a damaged cache is simply rebuilt

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).casefold()

    def get(self, texts: Sequence[str]) -> list[list[float]]:
        missing = list(dict.fromkeys(self._key(t) for t in texts if self._key(t) not in self._vectors))
        if missing:
            for key, vec in zip(missing, self._embed(missing)):
                self._vectors[key] = [round(x, 6) for x in unit(vec)]
            self._dirty = True
        return [self._vectors[self._key(t)] for t in texts]

    def save(self) -> None:
        if not (self.path and self._dirty):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"model": self.tag, "vectors": self._vectors}), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

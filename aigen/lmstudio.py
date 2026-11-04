"""Minimal client for OpenAI-compatible servers (standard library only).

Embeddings always come from the local LM Studio server (`base_url`). Chat can come from the same server
or from another OpenAI-compatible service such as Groq (`chat_base_url` + `chat_api_key`).
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

DEFAULT_URL = "http://localhost:1234/v1"
GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-20b"
EMBED_BATCH = 32
MAX_RETRIES = 6            # for "too many requests" answers
MAX_WAIT = 65.0            # longest single pause, seconds
USER_AGENT = "coop-alchemy-aigen/1.0"

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_WAIT_RE = re.compile(r"try again in ([0-9.]+)\s*(ms|s|m)", re.I)


class LMStudioError(RuntimeError):
    """The server is unreachable, refused a request, or answered with something unusable."""


class DailyLimitError(LMStudioError):
    """A per-day quota is used up (for example Groq's tokens per day): waiting a minute will not help."""


def strip_reasoning(text: str) -> str:
    """Keep only the final answer of a reasoning model (gpt-oss channel markers, <think> blocks)."""
    marker = "<|channel|>final<|message|>"
    if marker in text:
        text = text.rsplit(marker, 1)[1]
    for end in ("<|return|>", "<|end|>"):
        text = text.split(end, 1)[0]
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # an opening tag the server already removed
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def is_daily_limit(body: str) -> bool:
    """True if a 429 answer says the DAILY quota (tokens or requests per day) is spent, not the per-minute one."""
    lowered = body.lower()
    return "per day" in lowered or "(tpd)" in lowered or "(rpd)" in lowered


def limit_message(body: str) -> str:
    """The human-readable part of a rate-limit answer (the JSON 'message' if there is one)."""
    try:
        message = json.loads(body)["error"]["message"]
        if isinstance(message, str):
            return message
    except (ValueError, KeyError, TypeError):
        pass
    return body


def retry_delay(headers: Any, body: str, attempt: int) -> float:
    """How long to pause after a 429: the server's hint if it gave one, else a growing default."""
    try:
        return min(MAX_WAIT, float(headers.get("Retry-After")) + 1.0)
    except (TypeError, ValueError):
        pass
    match = _WAIT_RE.search(body)
    if match:
        unit = {"ms": 0.001, "s": 1.0, "m": 60.0}[match.group(2).lower()]
        return min(MAX_WAIT, float(match.group(1)) * unit + 1.0)
    return min(MAX_WAIT, 5.0 * (attempt + 1))


class LMStudio:
    def __init__(self, base_url: str = DEFAULT_URL, chat_model: str | None = None,
                 embed_model: str | None = None, timeout: float = 600.0, *,
                 chat_base_url: str | None = None, chat_api_key: str | None = None,
                 max_tokens: int = 3500, sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_base_url = (chat_base_url or base_url).rstrip("/")
        self.chat_api_key = chat_api_key
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._sleep = sleep
        # What the chat service reports using, summed over this client's calls (see usage_summary).
        self.usage: dict[str, int] = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}

    @property
    def remote_chat(self) -> bool:
        return self.chat_base_url != self.base_url

    # ---- transport -------------------------------------------------------

    def _call(self, base: str, path: str, payload: dict[str, Any] | None = None, *,
              api_key: str | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        for attempt in range(MAX_RETRIES + 1):
            request = urllib.request.Request(base + path, data=data, headers=headers,
                                             method="GET" if payload is None else "POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                if exc.code == 429 and is_daily_limit(detail):
                    raise DailyLimitError(
                        f"the daily limit of {base} is used up, so waiting a minute will not help: "
                        f"{limit_message(detail)[:400]}") from exc
                if exc.code == 429 and attempt < MAX_RETRIES:
                    wait = retry_delay(exc.headers, detail, attempt)
                    print(f"  rate limit reached; waiting {wait:.0f}s ...", file=sys.stderr)
                    self._sleep(wait)
                    continue
                hint = " (check the API key)" if exc.code in (401, 403) else ""
                raise LMStudioError(f"the server at {base} answered {exc.code} for {path}{hint}: {detail}") from exc
            except (urllib.error.URLError, OSError) as exc:  # includes timeouts and refused connections
                where = "the chat service" if base == self.chat_base_url and self.remote_chat else "LM Studio"
                raise LMStudioError(
                    f"cannot reach {where} at {base} ({exc})."
                    + ("" if self.remote_chat and base == self.chat_base_url else
                       " Open LM Studio, go to the Developer tab and start the local server.")) from exc
            except ValueError as exc:
                raise LMStudioError(f"{base} sent something that is not JSON for {path}") from exc
        raise LMStudioError("gave up after repeated rate-limit answers; try again later")

    # ---- models ----------------------------------------------------------

    def models(self) -> list[str]:
        reply = self._call(self.base_url, "/models")
        return [m["id"] for m in reply.get("data", []) if isinstance(m, dict) and "id" in m]

    def resolve_models(self, *, need_chat: bool, need_embed: bool) -> None:
        """Pick models automatically when none were named: a local chat model, and an embedding model."""
        if need_chat and self.remote_chat and not self.chat_model:
            raise LMStudioError("name the chat model for the remote service with --chat-model")
        ids = self.models() if ((need_chat and not self.remote_chat and not self.chat_model)
                                or (need_embed and not self.embed_model)) else []
        if need_chat and not self.chat_model:
            chat = [i for i in ids if "embed" not in i.lower()]
            if not chat:
                raise LMStudioError("no chat model found in LM Studio; download a small one (for example a 4B model)")
            self.chat_model = next((i for i in chat if "gpt-oss" in i.lower()), chat[0])
        if need_embed and not self.embed_model:
            embed = [i for i in ids if "embed" in i.lower()]
            if not embed:
                raise LMStudioError(
                    "no embedding model found in LM Studio. In LM Studio's model search download "
                    "'nomic-embed-text-v1.5' (about 80 MB), then try again.")
            self.embed_model = embed[0]

    # ---- calls -----------------------------------------------------------

    def chat(self, system: str, user: str, *, temperature: float = 0.8, max_tokens: int | None = None,
             effort: str = "low") -> str:
        if not self.chat_model:
            raise LMStudioError("no chat model selected")
        payload: dict[str, Any] = {
            "model": self.chat_model, "stream": False, "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if "gpt-oss" in self.chat_model.lower():
            payload["reasoning_effort"] = effort  # "low" for naming; the judging steps ask for more thought
        reply = self._call(self.chat_base_url, "/chat/completions", payload, api_key=self.chat_api_key)
        self._count_usage(reply)
        try:
            choice = reply["choices"][0]
            text = strip_reasoning(choice["message"].get("content") or "")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LMStudioError("the chat answer has an unexpected shape") from exc
        if not text:
            raise LMStudioError(f"the model returned an empty answer (finish_reason={choice.get('finish_reason')}); "
                                "raise --max-tokens (or the model's context length in LM Studio)")
        return text

    def _count_usage(self, reply: dict[str, Any]) -> None:
        """Add the token counts the service reported for one chat call (missing counts are ignored)."""
        self.usage["calls"] += 1
        usage = reply.get("usage")
        if not isinstance(usage, dict):
            return
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        total = usage.get("total_tokens")
        prompt = prompt if isinstance(prompt, int) else 0
        completion = completion if isinstance(completion, int) else 0
        self.usage["prompt"] += prompt
        self.usage["completion"] += completion
        self.usage["total"] += total if isinstance(total, int) else prompt + completion

    def usage_summary(self) -> str:
        u = self.usage
        return (f"Chat service used {u['total']:,} tokens in {u['calls']} calls "
                f"({u['prompt']:,} in, {u['completion']:,} out).")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.embed_model:
            raise LMStudioError("no embedding model selected")
        out: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = list(texts[start:start + EMBED_BATCH])
            reply = self._call(self.base_url, "/embeddings", {"model": self.embed_model, "input": batch})
            try:
                rows = sorted(reply["data"], key=lambda row: row["index"])
                vectors = [[float(x) for x in row["embedding"]] for row in rows]
            except (KeyError, TypeError, ValueError) as exc:
                raise LMStudioError("LM Studio's embedding answer has an unexpected shape") from exc
            if len(vectors) != len(batch):
                raise LMStudioError("LM Studio returned the wrong number of embeddings")
            out.extend(vectors)
        return out

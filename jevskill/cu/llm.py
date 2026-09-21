"""The LLM that works *between* Jev steps: one OpenRouter chat client, any model.

Jev decides one step in ~300 ms and cannot write text (act.md §0). Everything
that needs language — turning "open Notepad, type hello, save it on the desktop"
into sub-goals, composing the text a field needs, judging whether a sub-goal is
done, answering an escalation — goes to a model the user picks. It goes through
OpenRouter because that is one HTTP shape for every vendor, and the machine
already holds an OpenRouter key for Jev itself (``KEY_ENV_VARS``).

Stdlib only (``urllib``), like the rest of the package. Every call returns the
tokens and the cost it spent so the operator can put them in the ledger next to
the Jev rows: a run that "cost $0.0004 of Jev" and an unrecorded dollar of
planning is the kind of number this project refuses to publish.

The key is read once and never logged, never echoed and never placed in a
message. ``LLMError`` messages quote the upstream status and the body's first
200 characters — bodies do not contain the key.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import find_api_key
from ..errors import JevConfigError

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_S = 60.0
MODELS_TTL_S = 600.0

#: Shown when OpenRouter's model list cannot be fetched (offline, blocked). The
#: ids are what the list held when this file was written and are marked
#: ``source: "fallback"`` in the API so the page can say they are unverified.
FALLBACK_MODELS: Tuple[Dict[str, Any], ...] = (
    {"id": "anthropic/claude-sonnet-5", "name": "Anthropic: Claude Sonnet 5"},
    {"id": "anthropic/claude-haiku-4.5", "name": "Anthropic: Claude Haiku 4.5"},
    {"id": "anthropic/claude-opus-5", "name": "Anthropic: Claude Opus 5"},
    {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini"},
    {"id": "google/gemini-2.5-flash-lite", "name": "Google: Gemini 2.5 Flash Lite"},
)

#: A family prefix plus one of these is a *different* product, not a newer
#: generation: the picker must not recommend an image model for planning.
_NOT_A_PLANNER = ("image", "audio", "tts", "realtime", "search", "embed", "vision-only")

#: One newest model per family, in this order, becomes the "recommended" list.
#: A prefix match against the live list, so a renamed generation still shows.
RECOMMENDED_FAMILIES: Tuple[str, ...] = (
    "anthropic/claude-sonnet",
    "anthropic/claude-haiku",
    "anthropic/claude-opus",
    "openai/gpt-5-mini",
    "openai/gpt-5",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash",
    "google/gemini-3",
    "x-ai/grok-4",
    "deepseek/deepseek-v3",
)

#: Which family is the default when nothing was chosen: strong enough to plan,
#: cheap enough that a run's planning stays cents.
DEFAULT_FAMILY = "anthropic/claude-sonnet"

Transport = Callable[[str, Optional[bytes], Dict[str, str], float], Tuple[int, bytes]]


class LLMError(RuntimeError):
    """The upstream answered with an error, or did not answer."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class LLMReply:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_source: str = "unknown"
    latency_ms: float = 0.0
    raw: Any = field(default=None, repr=False)

    def json(self) -> Any:
        return extract_json(self.text)


def _urllib_transport(url: str, data: Optional[bytes], headers: Dict[str, str],
                      timeout: float) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def extract_json(text: str) -> Any:
    """The first JSON object (or array) in a model reply, or ``None``.

    Models fence JSON, prefix it with a sentence, or append a note. The parse is
    forgiving about *where* the JSON is and strict about *what* it is: no
    repair, no eval. A reply this cannot parse is a reply the operator treats
    as "no answer", which is the safe direction for an agent that clicks.
    """
    if not text:
        return None
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    try:
        return json.loads(body)
    except ValueError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = body.find(open_ch), body.rfind(close_ch)
        if 0 <= start < end:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                continue
    return None


class OpenRouterLLM:
    """One model behind OpenRouter's ``/chat/completions``.

    ``key`` defaults to the OpenRouter key the package already resolves for Jev
    (``OPENROUTER_API_KEY`` and friends); a missing one raises
    :class:`jevskill.errors.JevConfigError` at construction, not at the first
    call, so the operator can refuse a run before touching the desktop.
    """

    def __init__(self, model: str, *, key: Optional[str] = None,
                 base_url: str = OPENROUTER_BASE,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 transport: Optional[Transport] = None,
                 pricing: Optional[Dict[str, Tuple[float, float]]] = None) -> None:
        if not model or not str(model).strip():
            raise ValueError("an LLM needs a model id")
        self.model = str(model).strip()
        self._key = key if key is not None else find_api_key("openrouter")
        if not self._key:
            raise JevConfigError(
                "no OpenRouter key for the planning model: set OPENROUTER_API_KEY "
                "(the vendor JEV_API_KEY only reaches Jev)")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._transport = transport or _urllib_transport
        self._pricing = pricing if pricing is not None else {}
        self.calls = 0

    def __repr__(self) -> str:  # never the key
        return "OpenRouterLLM(model=%r)" % self.model

    def chat(self, system: str, user: str, *, json_reply: bool = True,
             max_tokens: int = 900, temperature: float = 0.0) -> LLMReply:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "usage": {"include": True},
        }
        if json_reply:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/lazniak/jevskill",
            "X-Title": "jevskill computer use",
        }
        started = time.perf_counter()
        try:
            status, body = self._transport(self.base_url + "/chat/completions",
                                           json.dumps(payload).encode("utf-8"),
                                           headers, self.timeout_s)
        except Exception as exc:  # socket, TLS, timeout
            raise LLMError("%s: %s" % (type(exc).__name__, exc)) from None
        latency = (time.perf_counter() - started) * 1000.0
        self.calls += 1
        text_body = body.decode("utf-8", "replace") if body else ""
        if status >= 400:
            raise LLMError("openrouter %d for %s: %s"
                           % (status, self.model, text_body[:200]), status=status)
        try:
            data = json.loads(text_body)
        except ValueError:
            raise LLMError("openrouter answered non-JSON: %s" % text_body[:200]) from None
        if not isinstance(data, dict) or not data.get("choices"):
            raise LLMError("openrouter answered without choices: %s" % text_body[:200])
        message = (data["choices"][0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some vendors return content parts
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        if cost is not None:
            cost_usd, source = float(cost), "provider"
        elif self.model in self._pricing:
            prompt_rate, completion_rate = self._pricing[self.model]
            cost_usd = tokens_in * prompt_rate + tokens_out * completion_rate
            source = "computed"
        else:
            cost_usd, source = 0.0, "unknown"
        return LLMReply(text=str(content or ""), model=str(data.get("model") or self.model),
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                        cost_source=source, latency_ms=latency, raw=data)


# --------------------------------------------------------------------------- #
# The model list
# --------------------------------------------------------------------------- #

_models_lock = threading.Lock()
_models_cache: Dict[str, Any] = {"at": 0.0, "models": None}


def fetch_models(*, transport: Optional[Transport] = None, timeout_s: float = 6.0,
                 base_url: str = OPENROUTER_BASE) -> List[Dict[str, Any]]:
    """OpenRouter's public model list, trimmed to what the page needs.

    No key is required for ``GET /models``. Raises :class:`LLMError` when the
    list cannot be fetched; the caller falls back to :data:`FALLBACK_MODELS`.
    """
    send = transport or _urllib_transport
    try:
        status, body = send(base_url.rstrip("/") + "/models", None,
                            {"Accept": "application/json"}, timeout_s)
    except Exception as exc:
        raise LLMError("%s: %s" % (type(exc).__name__, exc)) from None
    if status >= 400:
        raise LLMError("openrouter /models answered %d" % status, status=status)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise LLMError("openrouter /models answered non-JSON") from None
    out: List[Dict[str, Any]] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pricing = item.get("pricing") or {}
        out.append({
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "created": int(item.get("created") or 0),
            "prompt": _rate(pricing.get("prompt")),
            "completion": _rate(pricing.get("completion")),
            "context": int(item.get("context_length") or 0),
        })
    return out


def _rate(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cached_models(*, transport: Optional[Transport] = None,
                  ttl_s: float = MODELS_TTL_S) -> Tuple[List[Dict[str, Any]], str]:
    """``(models, source)`` — ``"openrouter"`` when live, ``"fallback"`` otherwise."""
    now = time.time()
    with _models_lock:
        cached = _models_cache["models"]
        if cached is not None and now - _models_cache["at"] < ttl_s:
            return cached, "openrouter"
    try:
        models = fetch_models(transport=transport)
    except LLMError:
        return [dict(m) for m in FALLBACK_MODELS], "fallback"
    with _models_lock:
        _models_cache["models"] = models
        _models_cache["at"] = now
    return models, "openrouter"


def recommended(models: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The newest model of each family in :data:`RECOMMENDED_FAMILIES`.

    "Newest" is OpenRouter's ``created`` timestamp; ``:free``, ``:thinking``
    and other variants are skipped so the pick is the plain endpoint.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for family in RECOMMENDED_FAMILIES:
        candidates = [m for m in models
                      if _in_family(m["id"], family) and m["id"] not in seen]
        if not candidates:
            continue
        best = max(candidates, key=lambda m: (int(m.get("created") or 0), m["id"]))
        seen.add(best["id"])
        out.append(best)
    return out


def _in_family(model_id: str, family: str) -> bool:
    """``anthropic/claude-haiku-4.5`` is in ``anthropic/claude-haiku``;
    ``google/gemini-2.5-flash-image`` is not in ``google/gemini-2.5-flash``,
    and neither is any ``:free`` / ``:thinking`` variant."""
    if ":" in model_id or not model_id.startswith(family):
        return False
    rest = model_id[len(family):]
    if any(word in rest for word in _NOT_A_PLANNER):
        return False
    return re.match(r"^(-?\d[\w.]*)?$", rest) is not None


def default_model(models: Sequence[Dict[str, Any]]) -> Optional[str]:
    picks = recommended(models)
    for model in picks:
        if model["id"].startswith(DEFAULT_FAMILY):
            return model["id"]
    return picks[0]["id"] if picks else None


def pricing_table(models: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    return {m["id"]: (float(m.get("prompt") or 0.0), float(m.get("completion") or 0.0))
            for m in models}


__all__ = ["DEFAULT_FAMILY", "FALLBACK_MODELS", "LLMError", "LLMReply",
           "OPENROUTER_BASE", "OpenRouterLLM", "RECOMMENDED_FAMILIES",
           "cached_models", "default_model", "extract_json", "fetch_models",
           "pricing_table", "recommended"]

"""The Decisions API client.

Optimised for the way a coding harness actually uses Jev — a short burst of
decisions, not a hot loop — so the priorities differ from a latency-critical
agent loop:

* **One connection, reused.** A warm TCP/TLS/H2 pool removes the handshake from
  every request after the first.
* **Precompiled question bytes.** Question bundles are built once per intent and
  reused, so a burst costs serialisation only for the state.
* **Per-stage timing.** ``serialize``, ``http`` and ``parse`` are measured
  separately and returned on the result. That is what makes the skill's own
  effectiveness statistics real rather than estimated.
* **Typed answers.** ``noul``/``choice``/``score`` come back as objects with the
  probability distribution intact, so callers can gate on confidence.

Only the standard library is required; ``httpx`` and ``orjson`` are used when
present.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import CHARS_PER_TOKEN, Config
from .errors import RETRYABLE_STATUS as _RETRYABLE_STATUS
from .errors import JevApiError, JevConfigError
from .primitives import validate_questions

try:  # pragma: no cover - trivial import shims
    import orjson

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

    def _loads(raw: bytes) -> Any:
        return orjson.loads(raw)

    HAS_ORJSON = True
except ImportError:  # pragma: no cover
    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _loads(raw: bytes) -> Any:
        return json.loads(raw.decode("utf-8"))

    HAS_ORJSON = False

try:
    import httpx

    HAS_HTTPX = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HAS_HTTPX = False

import urllib.error
import urllib.request


@dataclass(slots=True)
class Answer:
    """One typed answer. Exactly one payload field is populated, per ``kind``."""

    kind: str
    name: str
    raw: dict

    @property
    def value(self) -> Any:
        if self.kind == "noul":
            return self.raw.get("noul")
        if self.kind == "choice":
            return self.raw.get("choice")
        if self.kind == "score":
            return self.raw.get("score")
        return None

    @property
    def confidence(self) -> float | None:
        value = self.raw.get("confidence")
        return float(value) if value is not None else None

    @property
    def probabilities(self) -> dict[str, float]:
        return {str(k): float(v) for k, v in (self.raw.get("probabilities") or {}).items()}

    @property
    def legend(self) -> dict[str, Any]:
        return self.raw.get("legend") or {}

    def as_bool(self, threshold: float = 0.5) -> bool:
        """Interpret a ``noul`` answer with an explicit threshold."""
        value = self.value
        return bool(value is not None and float(value) >= threshold)

    def top(self, n: int = 3) -> list[tuple[str, float]]:
        """Ranked options — the input to any iterative / shortlist strategy."""
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, **self.raw}


@dataclass(slots=True)
class Decisions:
    """The full response: answers, usage and stage timings."""

    answers: dict[str, Answer]
    model: str
    request_id: str
    usage: dict[str, float]
    timing_ms: dict[str, float] = field(default_factory=dict)
    attempts: int = 1
    session_id: str | None = None

    # ---- typed accessors -------------------------------------------------
    def noul(self, name: str) -> float | None:
        answer = self.answers.get(name)
        return float(answer.raw["noul"]) if answer and answer.kind == "noul" else None

    def choice(self, name: str) -> str | None:
        answer = self.answers.get(name)
        return str(answer.raw["choice"]) if answer and answer.kind == "choice" else None

    def score(self, name: str) -> float | None:
        answer = self.answers.get(name)
        return float(answer.raw["score"]) if answer and answer.kind == "score" else None

    def confidence(self, name: str) -> float | None:
        answer = self.answers.get(name)
        return answer.confidence if answer else None

    def probs(self, name: str) -> dict[str, float]:
        answer = self.answers.get(name)
        return answer.probabilities if answer else {}

    def top(self, name: str, n: int = 3) -> list[tuple[str, float]]:
        answer = self.answers.get(name)
        return answer.top(n) if answer else []

    def value(self, name: str) -> Any:
        answer = self.answers.get(name)
        return answer.value if answer else None

    # ---- reporting -------------------------------------------------------
    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("input_tokens", 0) or 0)

    @property
    def cost_usd(self) -> float:
        return float(self.usage.get("cost", 0.0) or 0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.request_id,
            "model": self.model,
            "answers": {k: v.to_dict() for k, v in self.answers.items()},
            "usage": self.usage,
            "timing_ms": self.timing_ms,
            "attempts": self.attempts,
            "session_id": self.session_id,
        }


def _parse_answer(name: str, raw: Any) -> Answer:
    if not isinstance(raw, dict):
        return Answer("unknown", name, {"value": raw})
    return Answer(str(raw.get("type", "unknown")), name, raw)


def estimate_tokens(text: str | bytes | Any) -> int:
    """Cheap pre-flight size estimate, used only for warnings.

    Authoritative token counts always come back in ``usage``.
    """
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return max(1, int(len(text) / CHARS_PER_TOKEN))


class JevClient:
    """A reusable, warm connection to the OpenRouter Decisions API.

    Use as a context manager so the connection is reused across a burst of
    decisions::

        with JevClient() as jev:
            jev.warm()
            result = jev.decide(state, {"is_bug": noul(...)})
    """

    def __init__(self, config: Config | None = None, *, client: Any = None) -> None:
        self.config = config or Config.from_env()
        if not self.config.has_key():
            raise JevConfigError(
                "No OpenRouter API key found. Set OPENROUTER_API_KEY (or JEVSKILL_API_KEY), "
                "or write {'api_key': '...'} to ~/.jevskill/config.json."
            )
        self.requests_sent = 0
        self._client = client
        self._owns_client = client is None
        self._body_prefix = (
            b'{"model":"' + self.config.model.encode("utf-8") + b'","state":'
        )
        if self._owns_client and HAS_HTTPX:
            self._client = httpx.Client(
                http2=True,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    # Lets OpenRouter attribute traffic to this skill on the
                    # public app leaderboard.
                    "HTTP-Referer": "https://github.com/lazniak/jevskill",
                    "X-Title": "jevskill",
                },
                timeout=httpx.Timeout(
                    self.config.timeout_read_s, connect=self.config.timeout_connect_s
                ),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )

    # ------------------------------------------------------------------ warm
    def warm(self) -> float:
        """Open the connection now so the first real decision is not the cold one."""
        started = time.perf_counter()
        try:
            if self._client is not None:
                self._client.head(self.config.warm_url)
            else:
                request = urllib.request.Request(self.config.warm_url, method="HEAD")
                urllib.request.urlopen(request, timeout=self.config.timeout_connect_s).close()
        except Exception:
            pass  # A failed handshake still leaves the pool warm-ish.
        return (time.perf_counter() - started) * 1000.0

    # ------------------------------------------------------------------ decide
    def decide(
        self,
        state: Any,
        questions: Mapping[str, dict],
        *,
        session_id: str | None = None,
    ) -> Decisions:
        """One round trip: one state, all questions, all answers.

        Questions are evaluated in parallel, so adding a question is nearly free.
        Prefer one call with six questions over six calls with one.
        """
        validate_questions(questions)
        t0 = time.perf_counter_ns()
        state_bytes = _dumps(state)
        questions_bytes = _dumps(dict(questions))
        body = (
            self._body_prefix
            + state_bytes
            + b',"questions":'
            + questions_bytes
        )
        if session_id:
            safe = str(session_id).encode("utf-8", "replace")[:256].replace(b'"', b"'")
            body += b',"session_id":"' + safe + b'"'
        body += b"}"
        t_serialized = time.perf_counter_ns()

        payload: dict | None = None
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.config.retries + 1):
            attempts = attempt + 1
            if attempt:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
            try:
                raw, status = self._post(body)
            except Exception as exc:  # transport-level
                last_error = exc
                if attempt >= self.config.retries:
                    break
                continue
            if status == 200:
                t_http = time.perf_counter_ns()
                payload = _loads(raw)
                t_parsed = time.perf_counter_ns()
                answers = {
                    str(k): _parse_answer(str(k), v)
                    for k, v in (payload.get("answers") or {}).items()
                }
                usage = {
                    str(k): (float(v) if isinstance(v, (int, float)) else v)
                    for k, v in (payload.get("usage") or {}).items()
                }
                return Decisions(
                    answers=answers,
                    model=str(payload.get("model", self.config.model)),
                    request_id=str(payload.get("id", "")),
                    usage=usage,
                    timing_ms={
                        "serialize_ms": round((t_serialized - t0) / 1e6, 3),
                        "http_ms": round((t_http - t_serialized) / 1e6, 3),
                        "parse_ms": round((t_parsed - t_http) / 1e6, 3),
                        "total_ms": round((t_parsed - t0) / 1e6, 3),
                    },
                    attempts=attempts,
                    session_id=session_id,
                )
            last_error = JevApiError(status, _short(raw), raw.decode("utf-8", "replace"))
            if not last_error.retryable:
                raise last_error
            if attempt >= self.config.retries:
                break
        if isinstance(last_error, JevApiError):
            raise last_error
        raise JevApiError(0, f"transport failure after {attempts} attempt(s): {last_error}")

    # ------------------------------------------------------------------ decide_many
    def decide_many(self, items: list[tuple[Any, Mapping[str, dict]]], **kwargs: Any) -> list[Decisions]:
        """Run several independent (state, questions) pairs, reusing one connection.

        This is the *iteration* path — N genuinely different states. When the
        states are the same and only the questions differ, use one :meth:`decide`
        call instead. See :func:`jevskill.orchestrate.best_strategy`.
        """
        return [self.decide(state, questions, **kwargs) for state, questions in items]

    # ------------------------------------------------------------------ transport
    def _post(self, body: bytes) -> tuple[bytes, int]:
        if self._client is not None:
            response = self._client.post(self.config.decisions_url, content=body)
            self.requests_sent += 1
            return response.content, response.status_code
        request = urllib.request.Request(
            self.config.decisions_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_read_s) as response:
                self.requests_sent += 1
                return response.read(), response.status
        except urllib.error.HTTPError as exc:
            self.requests_sent += 1
            return exc.read(), exc.code

    def close(self) -> None:
        if self._owns_client and self._client is not None and HAS_HTTPX:
            self._client.close()
        self._client = None

    def __enter__(self) -> "JevClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _short(raw: bytes, limit: int = 300) -> str:
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message", text))[:limit]
    except Exception:
        pass
    return text[:limit]
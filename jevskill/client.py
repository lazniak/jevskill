"""The Decisions API client.

Optimised by default for the way a coding harness usually uses Jev — a short
burst of decisions — and switchable into a hot-loop mode for the other shape,
an agent that decides once per step (see :class:`JevClient` ``hot=True`` and
:class:`AsyncJevClient`):

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
present. The async client needs ``httpx`` and says so rather than pretending.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .config import (
    CHARS_PER_TOKEN,
    HOT_RETRIES,
    HOT_TIMEOUT_CONNECT_S,
    HOT_TIMEOUT_READ_S,
    MIN_HEDGE_AFTER_MS,
    Config,
)
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
    #: Stage durations in milliseconds, plus — on a hedged call — the two facts
    #: a caller needs to read the number: ``hedged`` (bool) and ``winner``
    #: (``"primary"`` / ``"hedge"``). They live here rather than in a separate
    #: field so that every existing consumer, including the ledger, records them
    #: without being changed.
    timing_ms: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    session_id: str | None = None
    provider: str = "openrouter"
    #: True when this came from the local cache rather than the model. Reported
    #: rather than hidden, because a cached answer has no fresh latency to trust.
    cached: bool = False
    cached_age_s: float | None = None

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
        """Total spend for this decision, **including an abandoned hedge**.

        A hedged call sends the same request twice; the loser is abandoned by us
        but still answered, and still billed, by the provider. Its usage never
        comes back, so the second leg is an estimate
        (``usage["hedge_cost_usd_est"]``, see
        :meth:`JevClient._hedge_cost_estimate`) — but an estimate added in is far
        closer to the truth than a zero left out, and this number feeds the
        ledger that the whole project uses to claim savings.

        ``usage["cost"]`` still holds the winner's billed cost alone, so the two
        halves stay separable in every record.
        """
        return float(self.usage.get("cost", 0.0) or 0.0) + float(
            self.usage.get("hedge_cost_usd_est", 0.0) or 0.0
        )

    @property
    def hedged(self) -> bool:
        """Whether a duplicate request was actually sent for this decision."""
        return bool(self.timing_ms.get("hedged", False))

    @property
    def winner(self) -> str:
        """``"primary"``, ``"hedge"``, or ``""`` when nothing was hedged."""
        return str(self.timing_ms.get("winner", "") or "")

    def to_dict(self) -> dict:
        return {
            "id": self.request_id,
            "model": self.model,
            "provider": self.provider,
            "answers": {k: v.to_dict() for k, v in self.answers.items()},
            "usage": self.usage,
            # The winner *plus* an abandoned hedge, which is what was actually
            # spent. `usage["cost"]` still holds the winner alone, so a reader
            # of this dict can still separate measured from estimated money —
            # leaving the total out meant every consumer recomputed it, and a
            # consumer that forgot silently under-reported hedged spend.
            "cost_usd": self.cost_usd,
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


#: Config fields the hot path retunes, and what it retunes them to. Whether a
#: field is *still* a default is asked of the config (``Config.is_defaulted``)
#: rather than answered by comparing values here — a caller who states the
#: documented number means it, and comparing values cannot tell that apart from
#: silence. See :data:`jevskill.config.STATED_DEFAULTS`.
_HOT_TUNING: dict[str, Any] = {
    "timeout_read_s": HOT_TIMEOUT_READ_S,
    "timeout_connect_s": HOT_TIMEOUT_CONNECT_S,
    "retries": HOT_RETRIES,
}

#: How many abandoned hedge legs may be in flight at once, across the client.
#:
#: The loser of a hedged call is abandoned, not cancelled: a sync HTTP request
#: cannot be recalled, so its connection stays checked out of the pool until it
#: answers or hits ``timeout_read_s``. The pool is ``max_connections=8``, so a
#: loop that hedges faster than the losers retire can hold every connection and
#: then block new calls on the pool timeout — which surfaces as ``PoolTimeout``,
#: a failure the hedge was supposed to prevent. Capping the abandoned legs at 2
#: leaves 6 connections for work that is actually being waited on; past the cap
#: the client simply does not hedge and says so in
#: ``timing_ms["note"] = "hedge_skipped_saturated"``, which is a measurable
#: event rather than a silent degradation.
#:
#: The async client needs no such cap: ``asyncio`` really does cancel the loser,
#: and cancelling an ``httpx`` request returns its connection to the pool.
MAX_ABANDONED_HEDGE_LEGS = 2

#: Whether ``hot=True`` also turns hedging on. **It does not** — and that is a
#: measurement overruling the design, not a preference.
#:
#: The hot path has ``retries=0``, so the plan expected a duplicate request to
#: cover the tail. Measured 2026-09-20 (``bench/cu_results.json``), 100 hedged
#: calls across both providers at N=12..240 plus a 60-call probe at a 320 ms
#: delay: **66 duplicates fired and not one of them won**, while the p50 of the
#: calls that fired one got worse — OpenRouter at N=240 went 503 -> 876 ms, and
#: the 320 ms probe took N=12 from ~298 to 642 ms.
#:
#: The mechanism is visible in the shape of it. The duplicate goes out on the
#: *same warm pool*, which is one multiplexed HTTP/2 connection to one provider:
#: it cannot escape a slow connection, and it makes the server do the work
#: twice, so the leg we are waiting on finishes later. On top of that the
#: primary starts ``hedge_after_ms`` earlier, so the twin can only win when the
#: primary is effectively stuck — which never happened in 66 fires.
#:
#: Hedging is kept, off, because the one case it is built for (a leg that is
#: stuck rather than slow) is real and not represented in this sample, and
#: because a hedge over a *separate* connection or provider is a different
#: experiment that has not been run. ``JevClient(hot=True, hedge=True)`` enables
#: it; the cost of the loser is always recorded.
HOT_HEDGE_DEFAULT = False

#: The warm-up decision: the smallest well-formed call that exercises the whole
#: path (TLS, H2, auth, the decisions endpoint, the parser) rather than only the
#: socket. Costs one decision — ~300 input tokens, ~$0.000013 — which is the
#: price of not paying the cold penalty on the first *real* step.
WARM_STATE = "warm-up ping"
WARM_QUESTIONS: dict[str, dict] = {
    "ready": {
        "type": "noul",
        "instructions": "Is `state` a warm-up ping rather than real work?",
        "criteria": {"true": "It is a warm-up ping.", "false": "It is real work."},
    }
}


def _timeouts(read_s: float, connect_s: float) -> Any:
    """``httpx`` timeouts with the **pool** timeout stated rather than inherited.

    ``httpx.Timeout(1.5, connect=2.0)`` sets read, write *and pool* to 1.5 s.
    The pool timeout is how long a request waits for a free connection, and on
    the hot path it was therefore shorter than the read timeout of the request
    already holding one: with ``max_connections=8`` and abandoned hedge legs
    each holding a connection for up to ``timeout_read_s``, a loop could queue
    behind them and raise ``PoolTimeout`` — a failure invented by the hedging
    that was meant to remove failures.

    Waiting for a connection is strictly cheaper than failing, so the pool gets
    the longest single wait either leg can impose, plus a second of slack. It is
    a ceiling, not a target: on a healthy pool nothing ever reaches it.
    """
    return httpx.Timeout(read_s, connect=connect_s, pool=max(read_s, connect_s) + 1.0)


def _copy_config(config: Config, **changes: Any) -> Config:
    """``dataclasses.replace``, with the mutable fields actually copied.

    ``replace`` re-runs ``__init__`` with the *same objects*, so a copy's
    ``extra`` was the caller's dict rather than a copy of it: a hot client and
    the config the caller still held shared one dictionary, and a write through
    either was visible in the other (verified). Every copy made in this module
    goes through here so that cannot come back.
    """
    changes.setdefault("extra", dict(config.extra))
    return replace(config, **changes)


def _apply_hot_defaults(
    config: Config,
    *,
    hot: bool,
    hedge: bool | None,
    hedge_after_ms: float | None,
) -> Config:
    """Return the config the client will actually use.

    Hot mode changes *defaults*, never a caller's explicit choice: a field is
    retuned only while the caller has said nothing about it, which the config
    knows (:meth:`jevskill.config.Config.is_defaulted`) because an unset field
    arrives as a sentinel rather than as the number itself. Passing
    ``Config(timeout_read_s=8)`` with ``hot=True`` therefore keeps 8 seconds —
    and so does ``Config(timeout_read_s=60.0)``, the documented default stated
    out loud, which the previous value comparison silently retuned to 1.5 s.

    ``replace`` reports nothing as defaulted on the copy, so the question is
    asked of the *original*, before any copy is made.

    The caller's own object is never mutated — a ``Config`` is resolved once and
    may be shared by several clients, and a hot client silently shortening a
    shared config's timeout would be a bug at a distance.
    """
    changes: dict[str, Any] = {}
    if hot:
        for name, hot_value in _HOT_TUNING.items():
            if config.is_defaulted(name):
                changes[name] = hot_value
        if hedge is None and HOT_HEDGE_DEFAULT:
            changes["hedge"] = True
    if hedge is not None:
        changes["hedge"] = bool(hedge)
    if hedge_after_ms is not None:
        # Checked here as well as in ``Config.__post_init__`` so a caller who
        # passes the knob to the *client* is told at the place the mistake was
        # made, not by a copy made three frames deeper.
        if float(hedge_after_ms) < MIN_HEDGE_AFTER_MS:
            raise ValueError(
                f"hedge_after_ms={hedge_after_ms!r} is below the "
                f"{MIN_HEDGE_AFTER_MS:.0f} ms floor: a duplicate that early is "
                "not a tail cover, it is a second copy of every call — see "
                "jevskill.config.MIN_HEDGE_AFTER_MS."
            )
        changes["hedge_after_ms"] = float(hedge_after_ms)
    return _copy_config(config, **changes) if changes else config


class _DecisionCore:
    """Everything in a client that is not transport: bytes in, ``Decisions`` out.

    The sync and the async client inherit it, and that is the whole point. An
    async client with its own copy of the parsing would be a second
    implementation of the response contract, and the two would drift the first
    time a provider changed a field — the same reason the cache path shares
    :meth:`_decisions_from_payload` rather than decoding for itself.
    """

    config: Config
    _body_prefix: bytes

    # ------------------------------------------------------------------ body
    def _compose_body(
        self, state: Any, questions: Mapping[str, dict], session_id: str | None
    ) -> bytes:
        """The request bytes. Identical on both clients, byte for byte.

        ``session_id`` is an OpenRouter observability extension. The vendor
        endpoint rejects any body that carries it (HTTP 400 ``api_usage_error``,
        measured 2026-09-20), so it is sent only where accepted — and is still
        kept on the result, because the ledger's grouping is local and unaffected.
        """
        body = (
            self._body_prefix
            + _dumps(state)
            + b',"questions":'
            + _dumps(dict(questions))
        )
        if session_id and self.config.accepts_session_id:
            safe = str(session_id).encode("utf-8", "replace")[:256].replace(b'"', b"'")
            body += b',"session_id":"' + safe + b'"'
        return body + b"}"

    # ------------------------------------------------------------------ normalise
    def _normalise_usage(self, usage: dict) -> dict:
        """Guarantee a ``cost`` on every provider.

        OpenRouter returns ``usage.cost`` from its own accounting; the TypeSafe
        endpoint returns only ``input_tokens`` and ``output_tokens``. Without this
        the ledger would record every vendor-endpoint decision as free, which
        would silently inflate the reported savings — the exact class of error
        this project exists to avoid.
        """
        if usage.get("cost") is None:
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            usage["cost"] = self.config.cost_for_tokens(input_tokens)
            usage["cost_source"] = "computed"
        else:
            usage["cost_source"] = "provider"
        return usage

    def _hedge_cost_estimate(self, usage: dict, body: bytes | None) -> float:
        """What the abandoned twin cost, as well as it can be known.

        The loser's response is dropped, so its ``usage`` never arrives — but the
        two requests are the *same bytes*, so the winner's ``input_tokens`` is the
        count the provider billed for the loser too, and output is free on both
        providers. When even that is missing, fall back to the calibrated
        character estimate. An estimate recorded is honest; a zero recorded would
        make hedging look free, and free is the one thing it is not.
        """
        tokens = int(usage.get("input_tokens", 0) or 0)
        if not tokens and body is not None:
            tokens = estimate_tokens(body)
        return self.config.cost_for_tokens(tokens)

    def _decisions_from_payload(
        self,
        payload: dict,
        *,
        t0: int,
        t_serialized: int,
        attempts: int,
        session_id: str | None,
        t_http: int | None = None,
        t_parsed: int | None = None,
        cached: bool = False,
        cached_age_s: float | None = None,
        hedge: dict | None = None,
        body: bytes | None = None,
    ) -> "Decisions":
        """Turn a response payload into ``Decisions``.

        Shared by the live path and the cache path so a cached answer is parsed by
        exactly the same code — a cache that decoded differently would be a second
        implementation of the response format, and the two would drift.
        """
        answers = {
            str(k): _parse_answer(str(k), v)
            for k, v in (payload.get("answers") or {}).items()
        }
        usage = {
            str(k): (float(v) if isinstance(v, (int, float)) else v)
            for k, v in (payload.get("usage") or {}).items()
        }
        if cached:
            # The model did no work for this answer, so it must not appear to have
            # spent tokens or money. Anything else inflates reported savings.
            usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                     "cost_source": "cache"}
        else:
            usage = self._normalise_usage(usage)
        now = time.perf_counter_ns()
        t_http = now if t_http is None else t_http
        t_parsed = now if t_parsed is None else t_parsed
        # A cache hit has no network stages; reporting the lookup as `http` would
        # be a lie about where the time went, so the split collapses to zero and
        # the stage breakdown shows an honest ~0 ms total.
        http_ms = 0.0 if cached else round((t_http - t_serialized) / 1e6, 3)
        parse_ms = 0.0 if cached else round((t_parsed - t_http) / 1e6, 3)
        timing: dict[str, Any] = {
            "serialize_ms": round((t_serialized - t0) / 1e6, 3),
            "http_ms": http_ms,
            "parse_ms": parse_ms,
            "total_ms": round((now - t0) / 1e6, 3),
        }
        if hedge is not None:
            # Reported even when the duplicate never fired: "we were willing to
            # hedge and did not have to" is the number that says how often the
            # tail is actually reached, and a bench that only sees the fired
            # hedges cannot compute it.
            timing["hedged"] = bool(hedge.get("hedged", False))
            timing["winner"] = str(hedge.get("winner", "primary"))
            if hedge.get("note"):
                # Why a willing hedge did not fire. Only present when there is
                # something to say, so the common row keeps its shape.
                timing["note"] = str(hedge["note"])
            if timing["hedged"] and not cached:
                usage["hedge_cost_usd_est"] = self._hedge_cost_estimate(usage, body)
                usage["hedge_cost_source"] = "estimated"
        return Decisions(
            answers=answers,
            model=str(payload.get("model", self.config.model)),
            request_id=str(payload.get("id", "")),
            usage=usage,
            timing_ms=timing,
            attempts=attempts,
            session_id=session_id,
            provider=self.config.provider,
            cached=cached,
            cached_age_s=cached_age_s,
        )

    def _annotate(self, error: JevApiError) -> None:
        """Add provider-specific detail to an error hint.

        The documented limits differ per endpoint — 32K on OpenRouter, 64K per
        request on the vendor's own API — so a payload-size error should say which
        ceiling it hit rather than a generic number. Errors are the one place a
        caller cannot look anything up, so they carry the specifics.
        """
        if error.status in (413, 422):
            spec = self.config.spec
            detail = f" This endpoint allows {spec['context_tokens']:,} tokens per request"
            if spec.get("state_plus_question_tokens"):
                detail += (
                    f", of which {spec['state_plus_question_tokens']:,} for state plus "
                    "the longest single question"
                )
            error.hint = error.hint.rstrip(".") + "." + detail + "."


class JevClient(_DecisionCore):
    """A reusable, warm connection to the OpenRouter Decisions API.

    Use as a context manager so the connection is reused across a burst of
    decisions::

        with JevClient() as jev:
            jev.warm()
            result = jev.decide(state, {"is_bug": noul(...)})

    **Hot mode** is the same client with the loop's priorities instead of the
    burst's::

        with JevClient(hot=True) as jev:       # 1.5 s read, no retries, hedged
            jev.warm()                          # one real mini-decision
            step = jev.decide(ui_tree, questions)

    ``hot`` is a constructor flag rather than a ``Config.hot()`` classmethod on
    purpose. A ``Config`` answers *where do I send this and with what key* — it
    is resolved once from the environment and may be shared by several clients
    and by the CLI. Hot is not a fact about the endpoint; it is a statement about
    the **call pattern of one client**, and putting it where the client is
    constructed keeps a loop from re-tuning a config that something else is also
    using. ``Config`` still holds the knobs (``hedge``, ``hedge_after_ms``,
    ``warm_mode``) so they can be set explicitly, or read back by ``doctor``.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: Any = None,
        hot: bool = False,
        hedge: bool | None = None,
        hedge_after_ms: float | None = None,
    ) -> None:
        self.hot = bool(hot)
        self.config = _apply_hot_defaults(
            config or Config.from_env(),
            hot=self.hot,
            hedge=hedge,
            hedge_after_ms=hedge_after_ms,
        )
        if not self.config.has_key():
            raise JevConfigError(
                "No Jev API key found. Set JEV_API_KEY (vendor endpoint) or "
                "OPENROUTER_API_KEY, or JEVSKILL_API_KEY for either, or write "
                "{'api_key': '...'} to ~/.jevskill/config.json."
            )
        self.requests_sent = 0
        #: The last warm-up decision, when ``warm(mode="decision")`` made one
        #: and it succeeded. ``warm()`` returns milliseconds, which says nothing
        #: about what the warm-up *cost*; a bench that wants to publish that
        #: number should not have to re-implement the warm-up to get it.
        self.last_warm_decision: "Decisions | None" = None
        # Hedging sends from two threads, and ``+= 1`` is not atomic. The lock
        # costs ~100 ns on a path that spends ~300 ms in the network.
        self._counter_lock = threading.Lock()
        # Abandoned hedge legs hold a pool connection until they answer; this
        # caps how many may do so at once. See MAX_ABANDONED_HEDGE_LEGS.
        self._hedge_slots = threading.Semaphore(MAX_ABANDONED_HEDGE_LEGS)
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
                timeout=_timeouts(
                    self.config.timeout_read_s, self.config.timeout_connect_s
                ),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )

    # ------------------------------------------------------------------ warm
    def warm(self, mode: str | None = None) -> float:
        """Pay the cold cost now, so the first real decision does not.

        ``mode="head"`` sends ``HEAD /v1/models``: free, but it only proves the
        socket. ``mode="decision"`` sends one minimal real decision
        (:data:`WARM_STATE` / :data:`WARM_QUESTIONS`): ~300 input tokens,
        ~$0.000013, and it exercises the path the next call will actually take.

        The default comes from measurement, not from taste — see
        :data:`jevskill.config.DEFAULT_WARM_MODE`,
        ``skills/jev/references/hotloop.md`` and ``bench/cu_results.json``.
        Failures are swallowed on both paths: a warm-up that did not work is not
        a reason to fail the work.

        The warm-up decision is **one attempt**, whatever ``retries`` says. It
        inherited the config's retries, so a warm-up against a dead endpoint
        spent 751 ms on three attempts and a backoff sleep before returning the
        "failure does not matter" that the docstring promises — the opposite of
        paying the cold cost *now*. A warm-up either works on the first try or
        has nothing to tell us. ``retries`` is pinned by swapping the config for
        the duration, which is safe because ``warm()`` is a start-up call made
        before the loop that shares this client begins.

        On success the result is kept on :attr:`last_warm_decision`, so a caller
        that needs to publish what the warm-up cost does not have to rebuild it.
        """
        mode = (mode or self.config.warm_mode or "head").strip().lower()
        started = time.perf_counter()
        if mode == "decision":
            self.last_warm_decision = None
            config, self.config = self.config, _copy_config(self.config, retries=0)
            try:
                self.last_warm_decision = self.decide(WARM_STATE, WARM_QUESTIONS)
            except Exception:
                pass
            finally:
                self.config = config
            return (time.perf_counter() - started) * 1000.0
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
        timeout_s: float | None = None,
        cache: Any = None,
    ) -> Decisions:
        """One round trip: one state, all questions, all answers.

        Questions are evaluated in parallel, so adding a question is nearly free.
        Prefer one call with six questions over six calls with one.

        ``timeout_s`` overrides the read timeout for this call only. Callers that
        batch — many items in one state, hence a long payload — should raise it,
        while the default stays tuned for a single small decision.

        ``cache`` is an optional :class:`jevskill.cache.DecisionCache`. When given,
        a byte-identical request is answered from disk with ``cached=True``, zero
        tokens and zero cost.
        """
        validate_questions(questions)
        t0 = time.perf_counter_ns()
        body = self._compose_body(state, questions, session_id)
        t_serialized = time.perf_counter_ns()

        if cache is not None:
            entry = cache.lookup(body)
            if entry is not None:
                return self._decisions_from_payload(
                    entry.payload,
                    t0=t0,
                    t_serialized=t_serialized,
                    attempts=0,
                    session_id=session_id,
                    cached=True,
                    cached_age_s=entry.age_s,
                )

        payload: dict | None = None
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.config.retries + 1):
            attempts = attempt + 1
            if attempt:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
            try:
                raw, status, hedge_info = self._send(
                    body, timeout_s=timeout_s, allow_hedge=attempt == 0
                )
            except Exception as exc:  # transport-level
                last_error = exc
                if attempt >= self.config.retries:
                    break
                continue
            if status == 200:
                t_http = time.perf_counter_ns()
                payload = _loads(raw)
                t_parsed = time.perf_counter_ns()
                if cache is not None:
                    cache.store(body, payload)
                return self._decisions_from_payload(
                    payload,
                    t0=t0,
                    t_serialized=t_serialized,
                    attempts=attempts,
                    session_id=session_id,
                    t_http=t_http,
                    t_parsed=t_parsed,
                    hedge=hedge_info,
                    body=body,
                )
            last_error = JevApiError(status, _short(raw), raw.decode("utf-8", "replace"))
            self._annotate(last_error)
            if not last_error.retryable:
                raise last_error
            if attempt >= self.config.retries:
                break
        if isinstance(last_error, JevApiError):
            self._annotate(last_error)
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
    def _send(
        self, body: bytes, *, timeout_s: float | None = None, allow_hedge: bool = True
    ) -> tuple[bytes, int, dict | None]:
        """One attempt — hedged when the config says so, and only the first one.

        Returns ``(raw, status, hedge_info)``; ``hedge_info`` is ``None`` unless
        hedging is enabled, which is what keeps the default path byte-identical
        to what it was: no extra thread, no extra field on the result.

        ``allow_hedge=False`` on every attempt after the first, because the two
        strategies multiply instead of adding: ``retries=2, hedge=True`` put six
        requests on the wire for one decision (measured), paying twice for each
        of three attempts. A hedge covers the *tail* of a healthy call; once an
        attempt has already failed, the situation is no longer a tail and the
        retry is the answer. Retries therefore go single-leg, which bounds one
        decision at ``retries + 2`` requests instead of ``2 * (retries + 1)``.
        """
        if not self.config.hedge or not allow_hedge:
            raw, status = self._post(body, timeout_s=timeout_s)
            return raw, status, None
        return self._post_hedged(body, timeout_s=timeout_s)

    def _post_hedged(
        self, body: bytes, *, timeout_s: float | None = None
    ) -> tuple[bytes, int, dict]:
        """Send the same request twice, ``hedge_after_ms`` apart; first 200 wins.

        The theory: a retry starts *after* the first attempt has failed, so it
        costs the failure plus a whole round trip, while a hedge overlaps and the
        worst case becomes the faster of two draws rather than the sum of two.

        The measurement disagrees, and it is worth reading before turning this
        on. Across 160 hedged calls on both providers (``bench/cu_results.json``,
        2026-09-20) the duplicate fired 66 times and won **zero**, and the calls
        that fired one were *slower*: both legs share one multiplexed HTTP/2
        connection to one provider, so the twin cannot escape a slow connection
        and does make the provider do the work twice. It also starts
        ``hedge_after_ms`` late, so it can only win against a primary that is
        stuck, not merely slow. The tail this bench sees at N>=120 is a property
        of the request — a 23 000-token state is slow on every attempt — and
        hedging that simply pays twice for the same wait.

        The loser is *abandoned*, not cancelled: a sync HTTP request in flight
        cannot be recalled, the provider has already done the work, and we are
        billed for it. That is why
        :meth:`_DecisionCore._hedge_cost_estimate` exists — and it is also why
        the duplicate needs a slot from :attr:`_hedge_slots` before it may go
        out. An abandoned leg keeps a pool connection for up to
        ``timeout_read_s``; with ``max_connections=8`` a loop that hedges faster
        than the losers retire starves itself and then fails on the pool
        timeout. Past :data:`MAX_ABANDONED_HEDGE_LEGS` outstanding legs the call
        therefore does **not** hedge and reports
        ``timing_ms["note"] = "hedge_skipped_saturated"`` — visible in the
        ledger, so "the hedge stopped firing" is a number rather than a mystery.
        """
        delay = max(0.0, float(self.config.hedge_after_ms) / 1000.0)
        read_s = self.config.timeout_read_s if timeout_s is None else timeout_s
        # Both legs give up on their own read timeout; this is only the guard
        # that stops the caller waiting forever if a thread never reports.
        budget = read_s + self.config.timeout_connect_s + 0.5
        inbox: "queue.Queue[tuple[str, bytes, int, Exception | None]]" = queue.Queue()

        # The slot is released by whichever leg finishes *last*, because that is
        # the moment the abandoned one stops holding a connection. Counting in
        # the threads rather than at the return keeps it honest even when the
        # caller has long since taken the winner's answer and moved on.
        legs = {"outstanding": 1, "slot": False}
        legs_lock = threading.Lock()

        def attempt(label: str) -> None:
            try:
                raw, status = self._post(body, timeout_s=timeout_s)
            except Exception as exc:  # reported, not raised: this is a thread
                inbox.put((label, b"", 0, exc))
            else:
                inbox.put((label, raw, status, None))
            finally:
                with legs_lock:
                    legs["outstanding"] -= 1
                    release = legs["outstanding"] == 0 and legs["slot"]
                    if release:
                        legs["slot"] = False
                if release:
                    self._hedge_slots.release()

        started = time.perf_counter()
        threading.Thread(target=attempt, args=("primary",), daemon=True).start()
        hedged = False
        note = ""
        try:
            first = inbox.get(timeout=delay)
        except queue.Empty:
            if self._hedge_slots.acquire(blocking=False):
                with legs_lock:
                    legs["slot"] = True
                    legs["outstanding"] += 1
                hedged = True
                threading.Thread(target=attempt, args=("hedge",), daemon=True).start()
            else:
                note = "hedge_skipped_saturated"
            first = self._await_leg(inbox, started, budget)
        if not hedged or first[2] == 200:
            return _unwrap_leg(first, hedged=hedged, note=note)
        # The first answer back was an error or a retryable status. The twin is
        # still running, and a 200 from it is worth more than a 429 from this
        # one, so give it the rest of the budget before reporting a failure.
        try:
            second = self._await_leg(inbox, started, budget)
        except Exception:
            return _unwrap_leg(first, hedged=True)
        return _unwrap_leg(second if second[2] == 200 else first, hedged=True)

    @staticmethod
    def _await_leg(
        inbox: "queue.Queue[tuple[str, bytes, int, Exception | None]]",
        started: float,
        budget: float,
    ) -> tuple[str, bytes, int, Exception | None]:
        remaining = max(0.05, budget - (time.perf_counter() - started))
        try:
            return inbox.get(timeout=remaining)
        except queue.Empty as exc:
            raise JevApiError(
                0, f"no response from either request within {budget:.1f}s"
            ) from exc

    def _post(self, body: bytes, *, timeout_s: float | None = None) -> tuple[bytes, int]:
        if self._client is not None:
            kwargs = {}
            if timeout_s is not None and httpx is not None:
                kwargs["timeout"] = _timeouts(timeout_s, self.config.timeout_connect_s)
            self._count_request()
            response = self._client.post(self.config.decisions_url, content=body, **kwargs)
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
        self._count_request()
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_s or self.config.timeout_read_s
            ) as response:
                return response.read(), response.status
        except urllib.error.HTTPError as exc:
            return exc.read(), exc.code

    def _count_request(self) -> None:
        """Count a request as *sent*, before it is issued — not when it returns.

        Counting on return undercounts exactly the requests that matter: a hedge
        whose loser times out was never counted at all, so a client that put two
        bodies on the wire reported ``requests_sent == 1`` (measured). The
        provider bills for what was sent, so that is what this counts. It can
        therefore over-count a request that failed before leaving the socket,
        which is the harmless direction: it never claims money was not spent.
        """
        with self._counter_lock:
            self.requests_sent += 1

    def close(self) -> None:
        if self._owns_client and self._client is not None and HAS_HTTPX:
            self._client.close()
        self._client = None

    def __enter__(self) -> "JevClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _unwrap_leg(
    leg: tuple[str, bytes, int, Exception | None], *, hedged: bool, note: str = ""
) -> tuple[bytes, int, dict]:
    """Turn one finished leg into the ``(raw, status, hedge_info)`` triple.

    A leg that failed at the transport level re-raises here, so hedging changes
    nothing about how a network failure is reported — the retry loop above sees
    exactly the exception it would have seen without a twin.

    ``note`` records why a willing hedge did not fire, and is left out of the
    timing dict entirely when there is nothing to say.
    """
    label, raw, status, exc = leg
    if exc is not None:
        raise exc
    info: dict[str, Any] = {"hedged": hedged, "winner": label}
    if note:
        info["note"] = note
    return raw, status, info


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


class AsyncJevClient(_DecisionCore):
    """The same decisions, driven from an asyncio loop. Needs ``httpx``.

    Same request bytes (:meth:`_DecisionCore._compose_body`), same parsing
    (:meth:`_DecisionCore._decisions_from_payload`), so a decision taken here is
    indistinguishable from a sync one in the ledger and in a benchmark. Only the
    transport differs.

    Two genuine differences, both worth knowing:

    * A hedge is **cancelled**, not merely abandoned — ``asyncio`` can cancel a
      task, a thread cannot be recalled. The provider has still done the work and
      still bills for it, so the cost estimate is recorded exactly as in the sync
      client. Cancellation saves the socket, not the money.
    * There is no stdlib fallback. The sync client runs on ``urllib`` when
      ``httpx`` is missing; an async one on ``urllib`` would be a thread pool
      pretending to be a loop. Constructing this without ``httpx`` raises a
      :class:`~jevskill.errors.JevConfigError` that names the install, instead of
      silently being slower than the sync client.

    ::

        async with AsyncJevClient(hot=True) as jev:
            await jev.warm()
            result = await jev.decide(state, questions)
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: Any = None,
        hot: bool = False,
        hedge: bool | None = None,
        hedge_after_ms: float | None = None,
    ) -> None:
        if client is None and not HAS_HTTPX:
            raise JevConfigError(
                "AsyncJevClient requires httpx (pip install 'httpx[http2]'). "
                "The synchronous JevClient works on the standard library alone."
            )
        self.hot = bool(hot)
        self.config = _apply_hot_defaults(
            config or Config.from_env(),
            hot=self.hot,
            hedge=hedge,
            hedge_after_ms=hedge_after_ms,
        )
        if not self.config.has_key():
            raise JevConfigError(
                "No Jev API key found. Set JEV_API_KEY (vendor endpoint) or "
                "OPENROUTER_API_KEY, or JEVSKILL_API_KEY for either, or write "
                "{'api_key': '...'} to ~/.jevskill/config.json."
            )
        self.requests_sent = 0
        #: See :attr:`JevClient.last_warm_decision`.
        self.last_warm_decision: "Decisions | None" = None
        self._body_prefix = (
            b'{"model":"' + self.config.model.encode("utf-8") + b'","state":'
        )
        self._client = client
        self._owns_client = client is None
        if self._owns_client:
            self._client = httpx.AsyncClient(
                http2=True,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/lazniak/jevskill",
                    "X-Title": "jevskill",
                },
                timeout=_timeouts(
                    self.config.timeout_read_s, self.config.timeout_connect_s
                ),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )

    # ------------------------------------------------------------------ warm
    async def warm(self, mode: str | None = None) -> float:
        """See :meth:`JevClient.warm` — same modes, same measured default.

        Single-attempt for the same reason, and the result is kept on
        :attr:`last_warm_decision`.
        """
        mode = (mode or self.config.warm_mode or "head").strip().lower()
        started = time.perf_counter()
        if mode == "decision":
            self.last_warm_decision = None
            config, self.config = self.config, _copy_config(self.config, retries=0)
            try:
                self.last_warm_decision = await self.decide(WARM_STATE, WARM_QUESTIONS)
            except Exception:
                pass
            finally:
                self.config = config
            return (time.perf_counter() - started) * 1000.0
        try:
            await self._client.head(self.config.warm_url)
        except Exception:
            pass
        return (time.perf_counter() - started) * 1000.0

    # ------------------------------------------------------------------ decide
    async def decide(
        self,
        state: Any,
        questions: Mapping[str, dict],
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
        cache: Any = None,
    ) -> Decisions:
        """One round trip — see :meth:`JevClient.decide` for the contract."""
        import asyncio  # imported here so a sync-only CLI never pays for it

        validate_questions(questions)
        t0 = time.perf_counter_ns()
        body = self._compose_body(state, questions, session_id)
        t_serialized = time.perf_counter_ns()

        if cache is not None:
            entry = cache.lookup(body)
            if entry is not None:
                return self._decisions_from_payload(
                    entry.payload,
                    t0=t0,
                    t_serialized=t_serialized,
                    attempts=0,
                    session_id=session_id,
                    cached=True,
                    cached_age_s=entry.age_s,
                )

        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.config.retries + 1):
            attempts = attempt + 1
            if attempt:
                await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
            try:
                raw, status, hedge_info = await self._send(
                    body, timeout_s=timeout_s, allow_hedge=attempt == 0
                )
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.retries:
                    break
                continue
            if status == 200:
                t_http = time.perf_counter_ns()
                payload = _loads(raw)
                t_parsed = time.perf_counter_ns()
                if cache is not None:
                    cache.store(body, payload)
                return self._decisions_from_payload(
                    payload,
                    t0=t0,
                    t_serialized=t_serialized,
                    attempts=attempts,
                    session_id=session_id,
                    t_http=t_http,
                    t_parsed=t_parsed,
                    hedge=hedge_info,
                    body=body,
                )
            last_error = JevApiError(status, _short(raw), raw.decode("utf-8", "replace"))
            self._annotate(last_error)
            if not last_error.retryable:
                raise last_error
            if attempt >= self.config.retries:
                break
        if isinstance(last_error, JevApiError):
            self._annotate(last_error)
            raise last_error
        raise JevApiError(0, f"transport failure after {attempts} attempt(s): {last_error}")

    async def decide_many(
        self, items: list[tuple[Any, Mapping[str, dict]]], **kwargs: Any
    ) -> list[Decisions]:
        """N independent decisions **concurrently** — the one thing the sync
        client cannot do. Same caveat as :meth:`JevClient.decide_many`: when the
        state is shared and only the questions differ, one ``decide`` with all
        the questions is cheaper and faster than N calls, concurrent or not.

        **On failure this raises the first exception, after every task has
        finished** — the same contract the sync version has, where a raise
        happens with nothing else in flight. A plain ``gather`` returns as soon
        as one task raises, so the other N-1 were left running against a client
        the caller was about to close: their answers were discarded, their cost
        was not, and closing the client underneath them turned one error into
        several. ``return_exceptions=True`` waits for all of them first; the
        results of a partly successful batch are still dropped, because a caller
        that asked for N answers cannot use a list that silently holds fewer.
        """
        import asyncio

        results = await asyncio.gather(
            *(self.decide(state, questions, **kwargs) for state, questions in items),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return list(results)

    # ------------------------------------------------------------------ transport
    async def _send(
        self, body: bytes, *, timeout_s: float | None = None, allow_hedge: bool = True
    ) -> tuple[bytes, int, dict | None]:
        """See :meth:`JevClient._send` — including why only attempt 0 hedges."""
        if not self.config.hedge or not allow_hedge:
            raw, status = await self._post(body, timeout_s=timeout_s)
            return raw, status, None
        return await self._post_hedged(body, timeout_s=timeout_s)

    async def _post_hedged(
        self, body: bytes, *, timeout_s: float | None = None
    ) -> tuple[bytes, int, dict]:
        """Hedge with two tasks; first 200 wins, the loser is cancelled.

        See :meth:`JevClient._post_hedged` for the theory, and for the
        measurement that says this did not pay on either provider.
        """
        import asyncio

        delay = max(0.0, float(self.config.hedge_after_ms) / 1000.0)
        read_s = self.config.timeout_read_s if timeout_s is None else timeout_s
        budget = read_s + self.config.timeout_connect_s + 0.5

        labels: dict[Any, str] = {}
        # The clock starts before the primary is launched, exactly as in the
        # sync client. Starting it after the `hedge_after_ms` wait gave the
        # async path a budget of `delay + budget`, so two clients with the same
        # config gave up at different times — a difference nobody chose.
        started = time.perf_counter()
        primary = asyncio.ensure_future(self._post(body, timeout_s=timeout_s))
        labels[primary] = "primary"
        done, pending = await asyncio.wait({primary}, timeout=delay)
        hedged = False
        if not done:
            hedged = True
            twin = asyncio.ensure_future(self._post(body, timeout_s=timeout_s))
            labels[twin] = "hedge"
            pending = {primary, twin}

        fallback: tuple[str, bytes, int] | None = None
        first_error: Exception | None = None
        while True:
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    first_error = first_error or error
                    continue
                raw, status = task.result()
                if status == 200:
                    for other in pending:
                        other.cancel()
                    return raw, status, {"hedged": hedged, "winner": labels[task]}
                if fallback is None:
                    fallback = (labels[task], raw, status)
            if not pending:
                break
            remaining = max(0.05, budget - (time.perf_counter() - started))
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                for other in pending:
                    other.cancel()
                break
        if fallback is not None:
            label, raw, status = fallback
            return raw, status, {"hedged": hedged, "winner": label}
        if first_error is not None:
            raise first_error
        raise JevApiError(0, f"no response from either request within {budget:.1f}s")

    async def _post(self, body: bytes, *, timeout_s: float | None = None) -> tuple[bytes, int]:
        kwargs: dict[str, Any] = {}
        if timeout_s is not None and httpx is not None:
            kwargs["timeout"] = _timeouts(timeout_s, self.config.timeout_connect_s)
        # Counted before the await, for the reason in `JevClient._count_request`:
        # a cancelled hedge leg never returns here, and it was still sent and
        # still billed. One event loop, one thread, so no lock — unlike the sync
        # client, where two threads share the counter.
        self.requests_sent += 1
        response = await self._client.post(
            self.config.decisions_url, content=body, **kwargs
        )
        return response.content, response.status_code

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "AsyncJevClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

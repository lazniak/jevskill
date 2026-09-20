"""A single-user web console for issuing Jev decisions, served from localhost.

Why this exists: the CLI is the right surface for a harness, and the wrong one
for a human writing a question bundle by hand. Options, criteria and escape
hatches are fiddly to type as JSON on a command line, and the numbers that
matter — the distribution, the margin between the top two options, the token
budget — are easier to judge when drawn than when printed.

Three facts about this server belong together and must be read together:

* it binds **127.0.0.1 only** (:func:`make_server` refuses any other host),
* it has **no authentication**,
* it is therefore a **single-user, local** tool, never a service.

That is a deliberate trade, not an omission. Adding auth would mean storing a
secret next to a key that is already on the machine; binding to a routable
address would expose an unauthenticated spend endpoint. The console is the local
half of a local tool, so it is reachable exactly as far as the terminal that
started it. Two further guards close the one hole loopback does not: the ``Host``
header must name a loopback name (DNS rebinding), and a cross-origin ``Origin``
header is refused outright (a page in the user's browser must not be able to
spend their key).

**Nothing here re-implements the CLI.** The redaction, the review thresholds, the
token estimate and — most importantly — the ledger writer are the same functions
``jevskill ask`` calls, so a decision made in the browser lands in the same
``ledger.jsonl`` and shows up in ``jevskill stats``. It is tagged ``which="web"``
so console decisions can be told apart from a harness's, which is what
``GET /api/history`` filters on.

Stdlib only, like the rest of the package: :class:`http.server.ThreadingHTTPServer`
plus :mod:`importlib.resources` for the three static files. A build step for a
page with one form would be a dependency the Skill's zero-install half could
never carry.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .. import __version__

# The CLI's own helpers, imported rather than copied. `_ledger_extra` decides
# what an optional ledger payload looks like and `_baseline_tokens` decides what
# "the context an LLM would have needed" means; a second copy of either would
# drift, and the ledger would then hold two subtly different row shapes claiming
# to measure the same thing. `jevskill.cli` does not import this module at
# import time (only inside `cmd_web`), so this direction is not a cycle.
from ..cli import _baseline_tokens, _ledger_extra
from ..client import JevClient
from ..config import (
    DEFAULT_MAX_STATE_TOKENS,
    INPUT_PRICE_PER_MTOK,
    PROVIDERS,
    Config,
)
from ..errors import JevApiError, JevConfigError, JevError, JevQuestionError
from ..orchestrate import count_tokens, should_use_jev
from ..primitives import validate_questions
from ..redact import redact_state
from ..review import DEFAULT_REVIEW_BELOW, DEFAULT_REVIEW_MARGIN, needs_review
from ..stages import Stages
from ..stats import ledger_path, load_records, record_decision

__all__ = ["make_server", "serve", "TEMPLATES", "DEFAULT_PORT", "MAX_BODY_BYTES"]

#: Arbitrary, memorable, and outside the range a dev server usually squats on.
DEFAULT_PORT = 8765

#: The only addresses this server will bind. Not a configuration knob: see the
#: module docstring for why an unauthenticated spend endpoint stays on loopback.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: A request body larger than this is refused with 413 before it is read. The
#: state that belongs in a *decision* is a few thousand tokens; a megabyte is
#: already two orders of magnitude past the budget `profile()` would refuse, so
#: reading it would only be a way to spend memory on a request that cannot work.
MAX_BODY_BYTES = 1024 * 1024

#: Served static assets, with the content type stated rather than guessed.
#: `mimetypes.guess_type` reads the Windows registry, where `.js` is routinely
#: `text/plain`; a console whose script is served as plain text does not run.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}

#: Only these characters may name a static file. A name that cannot contain a
#: separator or a dot-segment cannot traverse out of the package.
_SAFE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# Question-bundle templates
# --------------------------------------------------------------------------- #

#: The console's starting points, one per pattern, taken from
#: ``skills/jev/references/patterns.md``. They are a Python dict rather than
#: JSON in the page so that the test suite can run every one of them through
#: :func:`jevskill.primitives.validate_questions` — a template that the API would
#: reject is worse than no template, because the user assumes the shipped example
#: is the correct shape.
#:
#: Each entry names its source section so a reader can check the wording against
#: the reference rather than against this file.
TEMPLATES: dict = {
    "gate": {
        "pattern": "gate",
        "title": "gate — one boolean about one text",
        "source": "references/patterns.md §1",
        "hint": "Name both sides. A gate that only says what counts as true invites a coin flip.",
        "questions": {
            "breaks_api": {
                "type": "noul",
                "instructions": (
                    "Does the diff at `state` change the signature or return type of a "
                    "public function?"
                ),
                "criteria": {
                    "true": "A name, parameter list, return type or exported constant changes.",
                    "false": (
                        "Only internal helpers, comments, tests, formatting or private "
                        "members change."
                    ),
                },
            }
        },
    },
    "triage": {
        "pattern": "triage",
        "title": "triage — one bounded category per item",
        "source": "references/patterns.md §2",
        "hint": "Always ship an escape hatch. Without `unclear`, an item that fits nothing forces a wrong label.",
        "questions": {
            "owner": {
                "type": "choice",
                "instructions": "Which module should own the fix for the failure at `state`?",
                "criteria": {
                    "billing": "Invoices, tax, pricing, payments.",
                    "api": "HTTP layer, request/response serialization, auth middleware.",
                    "ui": "Components, rendering, styles, templates.",
                    "db": "Schema, migrations, queries, indexes.",
                    "unclear": "Not enough information in the report to decide.",
                },
            }
        },
    },
    "grade": {
        "pattern": "verify",
        "title": "grade — one artifact on one ordered axis",
        "source": "SKILL.md §1 (the `risk` bundle)",
        "hint": "One axis per score. To grade two things, send two scores in the same call.",
        "questions": {
            "risk": {
                "type": "score",
                "instructions": "How risky is deploying the change at `state` unreviewed?",
                "criteria": ["Trivial", "Low", "Moderate", "High", "Critical"],
            }
        },
    },
    "route": {
        "pattern": "route",
        "title": "route — one task to one tier of effort",
        "source": "references/patterns.md §5",
        "hint": "Verify the route cheaply: a `trivial` label on a `design` problem is the expensive failure.",
        "questions": {
            "tier": {
                "type": "choice",
                "instructions": "What level of effort does the change at `state` require?",
                "criteria": {
                    "trivial": "A local edit: constant, string, comment, obvious typo.",
                    "local": "One function or file, clear intent, no design decision.",
                    "design": "Touches several files or needs an interface decision.",
                    "research": "Requires reading unfamiliar code or an external spec first.",
                    "unclear": "Not enough information in `state` to decide.",
                },
            }
        },
    },
    "verify": {
        "pattern": "verify",
        "title": "verify — atomic checks, combined in code",
        "source": "references/patterns.md §6",
        "hint": (
            "Do not ask 'is this good?'. Measured on 2,000 emails, one broad verdict "
            "scored 62.6% and the same call's atomic signals scored 95.1%."
        ),
        "questions": {
            "tests_actually_fail": {
                "type": "noul",
                "instructions": "Would the test at `state` fail if the fix were reverted?",
                "criteria": {
                    "true": "The test asserts the specific behaviour the fix changed.",
                    "false": "The test would pass with or without the fix.",
                },
            },
            "covers_reported_case": {
                "type": "noul",
                "instructions": "Does the test at `state` exercise the exact case from the bug report?",
            },
            "asserts_side_effects": {
                "type": "noul",
                "instructions": (
                    "Does the test at `state` assert observable behaviour rather than "
                    "implementation details?"
                ),
            },
        },
    },
    "rank": {
        "pattern": "rank",
        "title": "rank — one score per item, sorted in code",
        "source": "references/patterns.md §4",
        "hint": "Never ask one question to order N items. Score each, then sort deterministically.",
        "questions": {
            "impact_0": {
                "type": "score",
                "instructions": "How much user impact does finding 0 in `state` have?",
                "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"],
            },
            "impact_1": {
                "type": "score",
                "instructions": "How much user impact does finding 1 in `state` have?",
                "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"],
            },
            "impact_2": {
                "type": "score",
                "instructions": "How much user impact does finding 2 in `state` have?",
                "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"],
            },
        },
    },
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _static_bytes(name: str) -> Optional[bytes]:
    """Read one packaged asset, or ``None`` when it is not there.

    Read through :mod:`importlib.resources` rather than ``__file__`` so the
    console works from a wheel, a zipapp or an editable checkout without three
    code paths. ``pyproject.toml`` ships ``web/static/*`` as package data; if
    that entry is ever dropped, this returns ``None`` and the page 404s with a
    JSON error instead of tracebacking.
    """
    if not _SAFE_ASSET.match(name):
        return None
    try:
        return (resources.files(__package__) / "static" / name).read_bytes()
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        return None


def _coerce_state(value: Any) -> Any:
    """Accept a JSON state and a plain-text state through the same field.

    The textarea in the browser only ever produces a string. Sending
    ``"{\\"a\\": 1}"`` as a *string* would make every backticked path in the
    question (``state.a``) point at nothing, so text that parses as a JSON
    object or array is sent as that object or array. Anything else is sent as
    the text it is — a log file is not improved by being guessed at.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _review_reasons(answers: dict, *, below: float, margin: float) -> dict:
    """``{name: why it needs review}`` for the answers that do.

    The *decision* is :func:`jevskill.review.needs_review` — one threshold rule
    for the CLI and the console both. Only the sentence is new, because a badge
    saying "needs review" with no number is an instruction to ignore it.
    """
    reasons: dict = {}
    for name, answer in answers.items():
        if not needs_review(answer, below=below, margin=margin):
            continue
        if answer.kind == "noul":
            value = answer.value
            reasons[name] = (
                f"P(true)={float(value):.2f} sits between {1.0 - below:.2f} and "
                f"{below:.2f} — a rephrase could flip it"
                if value is not None
                else "no probability returned"
            )
        elif answer.kind == "choice":
            ranked = answer.top(2)
            if not ranked:
                reasons[name] = "no distribution returned"
            elif len(ranked) == 1:
                reasons[name] = f"only one option scored ({ranked[0][1]:.2f})"
            else:
                top, second = ranked[0], ranked[1]
                gap = top[1] - second[1]
                # Both conditions are reported when both hold, and they usually
                # do: over a normalised distribution a top below 0.75 is exactly
                # the region where the gap can also be small, so printing only
                # the first would hide the number the user is judging by.
                parts = []
                if top[1] < below:
                    parts.append(
                        f"top option {top[0]!r} at {top[1]:.2f} is below the {below:.2f} floor"
                    )
                if gap < margin:
                    parts.append(
                        f"margin {gap:.2f} between {top[0]!r} and {second[0]!r} "
                        f"is under {margin:.2f}"
                    )
                reasons[name] = "; ".join(parts)
        elif answer.kind == "score":
            confidence = answer.confidence
            reasons[name] = (
                f"confidence {confidence:.2f} is below the {below:.2f} floor"
                if confidence is not None
                else "no confidence returned"
            )
        else:
            reasons[name] = f"unknown answer type {answer.kind!r} — not trusted"
    return reasons


# --------------------------------------------------------------------------- #
# Console state
# --------------------------------------------------------------------------- #


class Console:
    """Everything the handlers share: one client, one lock, one ledger root.

    **One client, warmed once.** The whole point of the console over repeated
    ``jevskill ask`` invocations is that the connection survives between
    decisions — a cold first call measured 1189 ms of "http" against a 345 ms
    decision (see ``JevClient.warm``). A process that re-resolves the config and
    re-opens TLS per click would throw that away.

    **One lock around ``decide``.** This is a single-user tool; two concurrent
    decisions from one browser tab are a double-click, not a workload. Serialising
    them keeps the shared warm connection meaningful and makes the session totals
    on the page add up.

    The config is resolved **per request**, not once, so adding a key and
    reloading the page is enough to make the console work — which is what the
    "no key" state on the page tells the user to do.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        client_factory: Optional[Callable[[Config], Any]] = None,
        ledger_root: Any = None,
    ) -> None:
        self.ledger_root = ledger_root
        self.lock = threading.Lock()
        self._factory = client_factory or (lambda config: JevClient(config))
        self._client = client
        self._owns_client = client is None
        self.warm_ms: Optional[float] = None
        self.started_at = time.time()
        self._operator: Any = None
        if self._client is None:
            self._client = self._build_client()
        if self._client is not None:
            self.warm_ms = self._warm()

    # -- client ------------------------------------------------------------
    def _build_client(self) -> Any:
        """The client, or ``None`` when there is no key.

        A missing key must not stop the server starting: the page's job in that
        state is to say which variable to set, and it cannot do that if the
        process exited before serving it.
        """
        try:
            return self._factory(config())
        except (JevConfigError, JevError):
            return None

    def _warm(self) -> Optional[float]:
        try:
            return round(float(self._client.warm()), 1)
        except Exception:  # pragma: no cover - warm() already swallows its own
            return None

    def client(self) -> Any:
        """The shared client, built on first use if the key arrived late."""
        if self._client is None and self._owns_client:
            self._client = self._build_client()
            if self._client is not None:
                self.warm_ms = self._warm()
        return self._client

    def close(self) -> None:
        if self._operator is not None and self._operator.busy:
            try:
                self._operator.stop("console closing")
            except Exception:  # pragma: no cover
                pass
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    # -- computer use ------------------------------------------------------
    def operator(self) -> Any:
        """The computer-use operator, built on first use.

        Lazy because :mod:`jevskill.cu` is ~100 ms of imports the decide-only
        user never pays for, and because tests inject their own.
        """
        if self._operator is None:
            from ..cu.runner import Operator

            self._operator = Operator(ledger_root=self.ledger_root, client_getter=self.client)
        return self._operator


def config() -> Config:
    """Resolve the configuration from the environment.

    A function, not a constant, so a key added while the console is running is
    picked up on the next request — and so tests can patch the resolution the
    same way ``tests/test_key_precedence.py`` does.
    """
    return Config.from_env()


# --------------------------------------------------------------------------- #
# Payload builders (pure: no HTTP, so they are directly testable)
# --------------------------------------------------------------------------- #


def doctor_payload(console: Console) -> dict:
    """What the status pill reads. **Never any key material.**

    ``key_fingerprint`` is eight hex characters of SHA-256 — enough to tell two
    keys apart, useless for recovering either. An earlier version of the CLI's
    doctor printed the first twelve characters of the secret instead, which is a
    partial credential in every log that captured it; the console must not
    re-introduce that, which is why ``tests/test_web.py`` asserts that a known
    substring of the key appears in no response at all.
    """
    cfg = config()
    spec = PROVIDERS[cfg.provider]
    return {
        "version": __version__,
        "provider": cfg.provider,
        "provider_options": sorted(PROVIDERS),
        "model": cfg.model,
        "base_url": cfg.base_url,
        "endpoint": cfg.decisions_url,
        "key_found": cfg.has_key(),
        "key_name": cfg.extra.get("key_name") or None,
        "key_source": cfg.extra.get("key_source") or None,
        "key_fingerprint": cfg.key_fingerprint() or None,
        "key_env_names": list(spec["key_env"]),
        "context_tokens": cfg.context_tokens,
        "cost_reported_by_provider": cfg.reports_cost,
        "input_price_per_mtok": INPUT_PRICE_PER_MTOK,
        "budget_tokens": DEFAULT_MAX_STATE_TOKENS,
        "warm_ms": console.warm_ms,
        "ledger": str(ledger_path(console.ledger_root)),
    }


def estimate_payload(state: Any) -> dict:
    """Size a state before spending anything.

    Routed through :func:`jevskill.orchestrate.count_tokens` — the one calibrated
    constant — rather than an open-coded division. A prose rule of thumb
    under-counted logs by 2.15x, and a budget bar that lies is worse than none.
    """
    value = _coerce_state(state)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    tokens = count_tokens(value)
    return {
        "tokens": tokens,
        "budget": DEFAULT_MAX_STATE_TOKENS,
        "over_budget": tokens > DEFAULT_MAX_STATE_TOKENS,
        "chars": len(text),
        "est_cost_usd": round(tokens / 1_000_000 * INPUT_PRICE_PER_MTOK, 8),
    }


def history_payload(console: Console, limit: int = 20) -> dict:
    """The last ``limit`` console decisions, read through the ledger loader.

    Never by parsing the file here: :func:`jevskill.stats.load_records` is what
    applies outcome patches onto their decisions, and a second reader would be a
    second definition of what a row means. Explicit paths mean exactly those
    paths, so the global ledger is not merged in.
    """
    path = ledger_path(console.ledger_root)
    rows = [r for r in load_records([path]) if r.get("which") == "web"]
    limit = max(1, min(int(limit), 200))
    recent = rows[-limit:][::-1]
    return {
        "ledger": str(path),
        "count": len(rows),
        "decisions": [
            {
                "decision_id": r.get("decision_id", ""),
                "ts": r.get("ts", ""),
                "intent": r.get("intent", ""),
                "questions": r.get("questions", 0),
                "tokens_in": r.get("tokens_in", 0),
                "state_tokens": r.get("state_tokens", 0),
                "cost_usd": r.get("cost_usd", 0.0),
                "latency_ms": r.get("latency_ms", 0.0),
                "outcome": r.get("outcome", ""),
                # The ledger stores measurements, not payloads: the state and the
                # questions of a past decision are deliberately not in it, so the
                # page shows the summary and says so rather than inventing one.
                "has_request": False,
            }
            for r in recent
        ],
    }


def decide_payload(console: Console, request: dict) -> dict:
    """Run one decision and record it exactly as ``jevskill ask`` would.

    Raises :class:`jevskill.errors.JevQuestionError` for a bundle the API would
    reject, :class:`jevskill.errors.JevConfigError` when there is no key, and
    :class:`jevskill.errors.JevApiError` for anything the provider said no to.
    The HTTP layer turns those into the JSON error shape; this function stays
    about decisions.
    """
    stages = Stages.begin()
    # The console's decision began when the request arrived: that is the earliest
    # honest "the user committed to Jev" in this process.
    stages.mark("t_decision")

    state = _coerce_state(request.get("state", ""))
    questions = request.get("questions") or {}
    if not isinstance(questions, dict):
        raise JevQuestionError("'questions' must be an object of {name: question}")
    validate_questions(questions)

    redactions: list = []
    if request.get("redact", True):
        state, redactions = redact_state(state)
    stages.mark("profile")

    below = float(request.get("review_below", DEFAULT_REVIEW_BELOW))
    margin = float(request.get("review_margin", DEFAULT_REVIEW_MARGIN))
    intent = str(request.get("intent") or "")
    state_tokens = count_tokens(state)
    baseline_tokens = _baseline_tokens(state, questions)
    stages.mark("plan")

    client = console.client()
    if client is None:
        raise JevConfigError(
            "No Jev API key found. Set JEV_API_KEY (the vendor endpoint) or "
            "OPENROUTER_API_KEY in your environment and reload this page."
        )
    stages.mark("build")

    started = time.perf_counter()
    with console.lock:
        # One warm connection, one decision at a time. See Console's docstring.
        result = client.decide(state, questions)
    wall_ms = (time.perf_counter() - started) * 1000
    stages.mark("http")
    stages.absorb(result.timing_ms)

    review = _review_reasons(result.answers, below=below, margin=margin)
    confidences = {
        name: answer.confidence
        for name, answer in result.answers.items()
        if answer.confidence is not None
    }

    stages.mark("act")
    decision_id = record_decision(
        # Tagged as the console rather than as a pattern: `GET /api/history`
        # filters on it, and a browser decision is not evidence about how well a
        # *pattern* performs in a harness.
        which="web",
        intent=intent,
        stages_ms=stages.ordered(),
        latency_ms=result.timing_ms.get("total_ms", wall_ms),
        tokens_in=result.input_tokens,
        tokens_out=int(result.usage.get("output_tokens", 0) or 0),
        cost_usd=result.cost_usd,
        hedge_cost_usd_est=float(result.usage.get("hedge_cost_usd_est", 0.0) or 0.0),
        hedge_cost_source=str(result.usage.get("hedge_cost_source", "") or ""),
        questions=len(questions),
        state_tokens=state_tokens,
        confidence=confidences,
        baseline_tokens=baseline_tokens,
        version=__version__,
        extra=_ledger_extra(sorted(set(redactions)), sorted(review)),
        root=console.ledger_root,
    )
    stages.mark("report")

    return {
        "decisions": result.to_dict(),
        "review": review,
        "redactions": {"count": len(redactions), "kinds": sorted(set(redactions))},
        "timing_ms": result.timing_ms,
        "usage": result.usage,
        "cost_usd": result.cost_usd,
        "cost_source": str(result.usage.get("cost_source", "") or ""),
        "model": result.model,
        "provider": result.provider,
        "request_id": result.request_id,
        "decision_id": decision_id,
        "state_tokens": state_tokens,
        "baseline_tokens": baseline_tokens,
        "stages_ms": stages.ordered(),
        "wall_ms": round(wall_ms, 1),
        "ledger": str(ledger_path(console.ledger_root)),
    }


def plan_payload(request: dict) -> dict:
    """The free go/no-go check. Spends nothing, so the page may call it freely."""
    problem = str(request.get("problem") or "")
    if not problem.strip():
        raise JevQuestionError("'problem' is required: describe the task in plain language")
    state = request.get("state")
    data = _coerce_state(state) if state else None
    return should_use_jev(problem, data)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def cu_models_payload() -> dict:
    """The planning-model picker: OpenRouter's list when reachable, else a fallback.

    ``key_found`` is a boolean and nothing else — the page needs to know whether
    a model *can* be chosen, never what the key is.
    """
    from ..config import find_api_key
    from ..cu.llm import cached_models, default_model, recommended

    models, source = cached_models()
    try:
        key_found = bool(find_api_key("openrouter"))
    except JevError:
        key_found = False
    return {
        "source": source,
        "key_found": key_found,
        "key_hint": "OPENROUTER_API_KEY" if not key_found else "",
        "default": default_model(models),
        "recommended": [
            {"id": m["id"], "name": m.get("name", m["id"]),
             "prompt": m.get("prompt", 0.0), "completion": m.get("completion", 0.0)}
            for m in recommended(models)
        ],
        "all": sorted(m["id"] for m in models),
    }


class _Handler(BaseHTTPRequestHandler):
    """The whole HTTP surface. Every failure leaves as JSON, never as HTML.

    A console that answers a bad request with the stdlib's HTML error page gives
    the page nothing to show the user; the shape ``{"error", "hint", "status"}``
    is the one the front end renders, and ``hint`` carries
    :attr:`jevskill.errors.JevApiError.hint` whenever the failure came from the
    provider, because that field already says what to *do* about each status.
    """

    server_version = "jevskill-console/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # The console is a foreground tool: a log line per static asset is noise in
    # the terminal the user is reading the URL from. Errors still print.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # -- plumbing ----------------------------------------------------------
    @property
    def console(self) -> Console:
        return self.server.console  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes, content_type: str, *, close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The console serves only its own assets and talks only to itself; a
        # page that could be framed or could load a remote script is a page that
        # could be made to spend the user's key.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200, *, close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", close=close)

    def _error(self, status: int, message: str, hint: str = "", **extra: Any) -> None:
        payload = {"error": message, "hint": hint, "status": status}
        payload.update(extra)
        # 4xx/5xx close the connection: several of them (413 above all) leave an
        # unread body on the socket, and keep-alive would then parse it as the
        # next request line.
        self._json(payload, status, close=True)

    def _guard_origin(self) -> bool:
        """Refuse anything that is not this console talking to itself.

        Binding to loopback stops another machine connecting; it does **not**
        stop a page the user has open in the same browser from POSTing here
        (CSRF), nor a hostname that resolves to 127.0.0.1 from being used to
        reach it (DNS rebinding). The ``Host`` check closes the second and the
        ``Origin`` check the first. Both are cheap and neither is optional for
        an unauthenticated endpoint that spends money.
        """
        host = (self.headers.get("Host") or "").strip()
        hostname = host.rsplit(":", 1)[0].strip("[]") if host else ""
        if hostname and hostname.lower() not in LOOPBACK_HOSTS:
            self._error(
                403,
                f"refusing a request for host {host!r}",
                "The console answers only to 127.0.0.1 / localhost. Open the URL "
                "the command printed.",
            )
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            parsed = urlparse(origin)
            if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
                self._error(
                    403,
                    f"refusing a cross-origin request from {origin!r}",
                    "This console has no authentication, so it accepts requests "
                    "only from its own page.",
                )
                return False
        return True

    def _drain(self, length: int) -> None:
        """Swallow a body we are about to refuse, in bounded chunks.

        Answering before the client has finished writing gets the response
        discarded and the connection reset (WinError 10053 on Windows), so the
        refusal would never be *read*. Draining keeps peak memory flat while
        letting the client finish its `send`, and the drain itself is capped so
        an endless body still cannot hold a thread forever.
        """
        remaining = min(max(0, length), MAX_BODY_BYTES * 4)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self) -> Optional[dict]:
        """The JSON body, or ``None`` when the request was already answered."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._drain(length)
            self._error(
                415,
                "expected Content-Type: application/json, got %r" % (ctype or "nothing",),
                "Send the body as JSON. The console has no form-encoded endpoints.",
            )
            return None
        if length < 0:
            self._error(411, "a Content-Length is required", "Send the body length.")
            return None
        if length > MAX_BODY_BYTES:
            self._drain(length)
            self._error(
                413,
                f"body is {length} bytes; the limit is {MAX_BODY_BYTES}",
                "A state this large cannot be decided well anyway — cut it in code "
                "first, or run REDUCE from the CLI.",
                limit=MAX_BODY_BYTES,
            )
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"body is not valid JSON: {exc}", "Check the request body.")
            return None
        if not isinstance(payload, dict):
            self._error(400, "body must be a JSON object", "Wrap the fields in {...}.")
            return None
        return payload

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._guard_origin():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/healthz":
                self._json({"ok": True})
            elif route == "/api/doctor":
                self._json(doctor_payload(self.console))
            elif route == "/api/templates":
                self._json({"templates": TEMPLATES})
            elif route == "/api/history":
                query = parse_qs(parsed.query)
                raw_limit = (query.get("limit") or ["20"])[0]
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = 20
                self._json(history_payload(self.console, limit))
            elif route == "/api/cu/status":
                query = parse_qs(parsed.query)
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._json(self.console.operator().status(since))
            elif route == "/api/cu/models":
                self._json(cu_models_payload())
            elif route in ("/", "/index.html"):
                self._static("index.html")
            elif route.startswith("/api/"):
                self._error(404, f"no such endpoint: {route}", "See GET / for the console.")
            else:
                self._static(route.lstrip("/"))
        except Exception as exc:  # pragma: no cover - defensive
            self._unexpected(exc)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard_origin():
            return
        route = urlparse(self.path).path
        if route not in ("/api/decide", "/api/estimate", "/api/plan", "/api/cu/start",
                         "/api/cu/plan", "/api/cu/stop", "/api/cu/confirm"):
            self._error(404, f"no such endpoint: {route}", "See GET / for the console.")
            return
        payload = self._read_body()
        if payload is None:
            return
        try:
            if route == "/api/estimate":
                self._json(estimate_payload(payload.get("state", "")))
            elif route == "/api/plan":
                self._json(plan_payload(payload))
            elif route.startswith("/api/cu/"):
                self._computer_use(route, payload)
            else:
                self._json(decide_payload(self.console, payload))
        except JevQuestionError as exc:
            self._error(
                400,
                str(exc),
                "Every question needs a type of noul/choice/score, non-empty "
                "instructions, and — for choice and score — at least two options "
                "or levels. Name the target value with a backticked path.",
            )
        except JevConfigError as exc:
            # 503, with the doctor payload attached: the page can then show which
            # variable to set without a second round trip.
            self._error(503, str(exc), "Set a key and reload.", doctor=doctor_payload(self.console))
        except JevApiError as exc:
            status = exc.status if 400 <= exc.status <= 599 else 502
            self._error(status, str(exc), exc.hint, upstream_status=exc.status)
        except JevError as exc:
            self._error(400, str(exc), getattr(exc, "hint", ""))
        except Exception as exc:  # pragma: no cover - defensive
            self._unexpected(exc)

    def _computer_use(self, route: str, payload: dict) -> None:
        """The four computer-use writes. Status codes are the contract:

        * ``409`` a run is active (start) or nothing is waiting (confirm)
        * ``501`` a live run cannot happen on this machine (the reason says why)
        * ``503`` a key is missing — Jev's, or the planning model's
        * ``502`` the planning model's provider failed
        """
        from ..cu.llm import LLMError
        from ..cu.runner import OperatorBusy, OperatorUnavailable

        operator = self.console.operator()
        try:
            if route == "/api/cu/start":
                run_id = operator.start(
                    str(payload.get("command", "")),
                    payload.get("model"),
                    dry_run=bool(payload.get("dry_run", False)),
                    plan=payload.get("plan") or None,
                    max_steps=int(payload.get("max_steps", 25) or 25),
                    budget_s=float(payload.get("budget_s", 90) or 90),
                    total_budget_s=float(payload.get("total_budget_s", 600) or 600),
                    usd_cap=float(payload.get("usd_cap", 0.5) or 0.0),
                )
                self._json({"ok": True, "run_id": run_id, "status": operator.status()}, 202)
            elif route == "/api/cu/plan":
                self._json(operator.plan(str(payload.get("command", "")), payload.get("model")))
            elif route == "/api/cu/stop":
                self._json({"ok": True, "status": operator.stop(
                    str(payload.get("reason") or "STOP button"))})
            else:
                self._json({"ok": True, "status": operator.confirm(bool(payload.get("allow")))})
        except OperatorBusy as exc:
            self._error(409, str(exc), "Stop the active run first.", status_payload=operator.status())
        except OperatorUnavailable as exc:
            self._error(501, str(exc), "A dry run (dry_run: true) still works here.")
        except LookupError as exc:
            self._error(409, str(exc), "The run is not waiting for a confirmation.")
        except LLMError as exc:
            self._error(502, str(exc), "The planning model's provider failed; pick another model or retry.")
        except (TypeError, ValueError) as exc:
            self._error(400, str(exc), "command is a non-empty string; model an OpenRouter id or null.")

    def _unexpected(self, exc: Exception) -> None:  # pragma: no cover - defensive
        self._error(
            500,
            f"{type(exc).__name__}: {exc}",
            "This is a bug in the console; the terminal running it has the traceback.",
        )

    def _static(self, name: str) -> None:
        body = _static_bytes(name)
        if body is None:
            self._error(404, f"no such file: {name}", "The console serves /, /app.css and /app.js.")
            return
        suffix = name[name.rfind(".") :] if "." in name else ""
        self._send(200, body, STATIC_TYPES.get(suffix, "application/octet-stream"))


class _ConsoleServer(ThreadingHTTPServer):
    """A threading server that owns its :class:`Console` and closes it."""

    daemon_threads = True
    # A console is restarted often (edit, Ctrl+C, restart); without this the
    # socket sits in TIME_WAIT and the next start fails on the same port.
    allow_reuse_address = True

    def __init__(self, address: tuple, handler: type, console: Console) -> None:
        self.console = console
        super().__init__(address, handler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.console.close()


def make_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    client: Any = None,
    client_factory: Optional[Callable[[Config], Any]] = None,
    ledger_root: Any = None,
) -> _ConsoleServer:
    """Build the console server. **Loopback only.**

    ``host`` exists so the refusal can be explicit rather than implied: any
    address other than ``127.0.0.1`` / ``localhost`` raises :class:`ValueError`.
    This endpoint has no authentication and spends a live API key, so "bind it
    anywhere" is not a preference the caller gets to express — the CLI does not
    even offer a ``--host`` flag.

    ``client`` injects a ready client (tests), ``client_factory`` injects the way
    one is built, and ``ledger_root`` points the ledger at a directory of the
    caller's choosing so a test never writes to the real one. ``port=0`` picks a
    free port, which is what the tests use.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}: the console has no authentication and "
            "spends a live API key, so it is served on 127.0.0.1 only. Use an SSH "
            "tunnel if you need it from another machine."
        )
    console = Console(client=client, client_factory=client_factory, ledger_root=ledger_root)
    # "localhost" can resolve to ::1 first on some machines, which would then
    # not be reachable at the http://127.0.0.1 URL the command prints.
    bind = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
    return _ConsoleServer((bind, int(port)), _Handler, console)


def serve(
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
    ledger_root: Any = None,
) -> int:
    """Run the console until Ctrl+C. Returns a process exit code."""
    server = make_server(port=port, ledger_root=ledger_root)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    report = doctor_payload(server.console)
    lines = [f"jev · console {__version__} — {url}"]
    if report["key_found"]:
        lines.append(
            f"  {report['provider']} · {report['model']} · "
            f"{report['key_name']} ({report['key_source']}) · {report['key_fingerprint']}"
        )
    else:
        lines.append(
            "  NO KEY — set JEV_API_KEY (vendor) or OPENROUTER_API_KEY, then reload "
            "the page. The console will not simulate an answer."
        )
    lines.append(f"  ledger {report['ledger']}")
    lines.append("  local only, no authentication — Ctrl+C to stop")
    # Flushed explicitly: stdout is block-buffered when it is a pipe, and the
    # next thing this process does is block in serve_forever — so `jevskill web
    # | tee run.log` showed nothing at all until Ctrl+C.
    print("\n".join(lines), flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - a headless box has no browser
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.shutdown()
        server.server_close()
    return 0

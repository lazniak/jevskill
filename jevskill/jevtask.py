"""Batch decisions: apply one set of questions to many items, measurably cheaper.

The objective this project was built for names *large datasets* as the case that
matters most: deciding what to do next when there is too much to read. Everything
else in the toolkit handles one decision well; this module handles N.

Why one call with N items beats N calls with one item
-----------------------------------------------------
Jev evaluates every question independently in parallel against a *single* state,
and the state is sent once. So putting many items into one state, with one
question per item, sends the shared framing once instead of N times.

Measured directly, both strategies on identical items, by
``python bench/batch_bench.py`` (60 items, 30 genuinely salient, window 8):

===============================================  =========  ==========
                                                   windowed   per-item
===============================================  =========  ==========
input tokens                                         10,674     23,288
cost (USD)                                         0.000448   0.000978
wall clock (ms)                                         667      7,979
API calls                                                 8         60
salient lines found                                   30/30     29/30
===============================================  =========  ==========

**2.18x fewer input tokens, 12.0x faster, and no accuracy cost** — windowed found
every salient line while one per-item call returned no answer at all.

Single-run measurements on one machine vary, so the script is the reference rather
than this table. A 24-item probe during design measured 2.69x, which is why the
reproducible benchmark exists.

Two strategies, because they answer different questions
-------------------------------------------------------
``windowed`` (default)
    Many items per call. Fewest tokens and by far the fastest. Use it when the
    items are independent and the questions are the same — triage, classification,
    labelling, filtering.

``per-item``
    One item per call. More tokens, but each decision sees only its own item, so a
    long or ambiguous item cannot influence a neighbour's answer. Use it when
    isolation matters more than cost, or when an item is too large to co-locate.

Both write the same ledger rows, so ``jevskill stats`` and ``advice`` see them.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import JevClient
from .orchestrate import count_tokens
from .primitives import validate_questions

#: Questions are re-issued per item, so their ids must be namespaced to avoid
#: collision when many items share one state. This is the separator.
SEP = "__"


@dataclass
class BatchItem:
    """One thing to decide about."""

    index: int
    data: Any
    label: str = ""

    def as_state(self) -> Any:
        return self.data


@dataclass
class ItemOutcome:
    index: int
    label: str
    values: dict[str, Any]
    confidence: dict[str, float | None]
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        out = {"index": self.index, "label": self.label, "values": self.values,
               "confidence": self.confidence}
        if self.probabilities:
            out["probabilities"] = self.probabilities
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class BatchResult:
    outcomes: list[ItemOutcome]
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    wall_ms: float
    #: What the same work would have cost at one item per call. Either measured
    #: with a real probe call or estimated from item text (a lower bound).
    separate_tokens: int = 0
    separate_cost_usd: float = 0.0
    separate_calls: int = 0
    baseline_measured: bool = False
    baseline_probe_items: int = 0
    strategy: str = "windowed"
    errors: int = 0

    @property
    def token_saving_pct(self) -> float | None:
        if not self.separate_tokens:
            return None
        return (1 - self.input_tokens / self.separate_tokens) * 100

    def to_dict(self) -> dict:
        return {
            "items": len(self.outcomes),
            "strategy": self.strategy,
            "calls": self.calls,
            "errors": self.errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "wall_ms": round(self.wall_ms, 1),
            "separate_calls": self.separate_calls,
            "separate_tokens": self.separate_tokens,
            "separate_cost_usd": round(self.separate_cost_usd, 8),
            "baseline_measured": self.baseline_measured,
            "baseline_probe_items": self.baseline_probe_items,
            "token_saving_pct": (
                round(self.token_saving_pct, 1)
                if self.token_saving_pct is not None else None
            ),
            "results": [o.to_dict() for o in self.outcomes],
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_items(path: Path | str | None, *, text_key: str | None = None) -> list[BatchItem]:
    """Read items from JSONL, a JSON array, or a plain-text line file.

    Formats, in the order they are tried:

    * **JSONL** — one JSON value per line. The usual case.
    * **JSON array** — a single array of values.
    * **text** — one item per line. Anything that is not JSON is treated as a
      string, because the most common input in practice is a file of log lines.

    ``text_key`` pulls one field out of each object, for the common shape where
    the lines to classify live under a key (``--text-key message``).
    """
    if path is None:
        raise ValueError("no input given")

    raw = Path(path).read_text(encoding="utf-8")
    stripped = raw.lstrip()

    items: list[Any] = []
    if stripped.startswith("["):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                items = loaded
        except json.JSONDecodeError:
            items = []
    if not items:
        # JSONL, falling back to plain text per line.
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                items.append(line)

    out: list[BatchItem] = []
    for i, item in enumerate(items):
        label = ""
        if text_key and isinstance(item, dict):
            if text_key not in item:
                raise ValueError(
                    f"--text-key {text_key!r} is not present in item {i}; "
                    f"available keys: {sorted(item)}"
                )
            data = item[text_key]
            label = str(item.get("label") or item.get("id") or "")
        elif isinstance(item, dict):
            data = item
            label = str(item.get("label") or item.get("id") or "")
        else:
            data = item
        out.append(BatchItem(index=i, data=data, label=label))
    if not out:
        raise ValueError(f"no items parsed from {path}")
    return out


# --------------------------------------------------------------------------- #
# Question templating
# --------------------------------------------------------------------------- #

def build_bundle(
    template: Mapping[str, dict],
    windows: Sequence[Sequence[BatchItem]],
) -> list[tuple[dict[str, Any], dict[str, dict], dict[str, tuple[int, str]]]]:
    """Expand a question template over windows of items.

    Returns one ``(state, questions, mapping)`` triple per call, where ``mapping``
    maps a generated question id back to ``(item index, question name)``.

    The template is written in terms of a single item and names it ``item``. Two
    things are rewritten per item:

    * the item is placed under ``item_<i>`` in the state, and
    * any backticked reference to ``item`` inside the question is rewritten to
      ``item_<i>``.

    That second rewrite is the important one. This project already learned — the
    hard way, in `references/prompting.md` §1 — that a question which does not name
    the value it is about returns a flat, meaningless answer for every candidate.
    Batching makes that failure *more* likely, not less, because many items now sit
    in one state. So the rewrite is done mechanically rather than left to whoever
    writes the template.
    """
    calls: list[tuple[dict[str, Any], dict[str, dict], dict[str, tuple[int, str]]]] = []
    for window in windows:
        state: dict[str, Any] = {}
        questions: dict[str, dict] = {}
        mapping: dict[str, tuple[int, str]] = {}
        for item in window:
            key = f"item_{item.index}"
            state[key] = item.as_state()
            for name, question in template.items():
                generated = f"{name}{SEP}{item.index}"
                questions[generated] = _retarget(question, key)
                mapping[generated] = (item.index, name)
        validate_questions(questions)
        calls.append((state, questions, mapping))
    return calls


def _retarget(question: dict, key: str) -> dict:
    """Rewrite backticked ``item`` references to the item's actual state key."""
    import copy
    import re

    q = copy.deepcopy(question)

    def fix(value: Any) -> Any:
        if isinstance(value, str):
            # `item` or `item.foo` -> `item_3` / `item_3.foo`
            return re.sub(r"`item(\.[A-Za-z0-9_.\[\]]+)?`", lambda m: f"`{key}{m.group(1) or ''}`", value)
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        return value

    return fix(q)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def _window(items: Sequence[BatchItem], size: int) -> list[list[BatchItem]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def run_batch(
    client: JevClient,
    items: Sequence[BatchItem],
    template: Mapping[str, dict],
    *,
    strategy: str = "windowed",
    window_size: int = 8,
    concurrency: int = 4,
    timeout_s: float = 120.0,
    session_id: str | None = None,
    on_call: Callable[[Any, dict], None] | None = None,
) -> BatchResult:
    """Apply ``template`` to every item and return the outcomes plus real usage.

    ``on_call`` is invoked with ``(Decisions, mapping)`` after each successful
    call so the caller can write ledger rows without this module depending on the
    statistics layer.

    Concurrency applies only to the ``per-item`` strategy in practice: windowed
    batching already collapses the work into a handful of calls, so its default
    ``concurrency`` of 1 avoids sending several large payloads at once while a
    ``per-item`` run benefits from the pool.
    """
    if strategy not in ("windowed", "per-item"):
        raise ValueError(f"unknown strategy {strategy!r}")
    if not items:
        return BatchResult([], 0, 0, 0, 0.0, 0.0, strategy=strategy)

    windows = ([[item] for item in items] if strategy == "per-item"
               else _window(items, max(1, window_size)))
    calls = build_bundle(template, windows)

    outcomes: dict[int, ItemOutcome] = {}
    totals = {"in": 0, "out": 0, "cost": 0.0}
    lock = threading.Lock()
    started = time.perf_counter()

    def run_one(call: tuple) -> None:
        state, questions, mapping = call
        try:
            result = client.decide(state, questions, session_id=session_id,
                                   timeout_s=timeout_s)
        except Exception as exc:
            # One failed call must not lose the batch. Record the error against
            # every item that call covered and carry on.
            failed = {
                index: ItemOutcome(index=index, label="", values={}, confidence={},
                                   error=f"{type(exc).__name__}: {exc}")
                for index, _name in mapping.values()
            }
            with lock:
                outcomes.update(failed)
            return

        per_item: dict[int, ItemOutcome] = {}
        for generated, (index, name) in mapping.items():
            row = per_item.setdefault(
                index, ItemOutcome(index=index, label="", values={}, confidence={}))
            answer = result.answers.get(generated)
            if answer is None:
                continue
            row.values[name] = answer.value
            if answer.confidence is not None:
                row.confidence[name] = answer.confidence
            if answer.probabilities:
                row.probabilities[name] = answer.probabilities

        if on_call is not None:
            on_call(result, mapping)
        with lock:
            totals["in"] += result.input_tokens
            totals["out"] += int(result.usage.get("output_tokens", 0) or 0)
            totals["cost"] += result.cost_usd
            outcomes.update(per_item)

    if concurrency > 1 and len(calls) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(run_one, calls))
    else:
        for call in calls:
            run_one(call)

    ordered = [outcomes[i] for i in sorted(outcomes)]
    for item, row in zip(items, ordered):
        row.label = item.label
    return BatchResult(
        outcomes=ordered, calls=len(calls), input_tokens=totals["in"],
        output_tokens=totals["out"], cost_usd=totals["cost"],
        wall_ms=(time.perf_counter() - started) * 1000,
        strategy=strategy, errors=sum(1 for r in ordered if not r.ok),
    )


def measure_separate_cost(
    client: JevClient,
    items: Sequence[BatchItem],
    template: Mapping[str, dict],
    *,
    sample: int = 1,
    timeout_s: float = 120.0,
) -> tuple[int, float, int]:
    """Measure what one item per call actually costs, by making that call.

    Returns ``(tokens, cost_usd, sample_size)`` extrapolated to all items.

    This exists because the obvious baseline — the sum of the items' own tokens —
    is wrong in the direction that flatters batching. A one-item-per-call run also
    pays for the question text and the API's per-request framing, which measured
    larger than the items themselves on small items. Comparing a batched payload
    against item text alone reported a "-313% saving" on a run that was in fact
    cheaper, which is the same class of units error this project has already made
    twice in the advice layer.

    Sampling one real call and scaling it keeps the claim honest without paying for
    the full control run.
    """
    if not items:
        return 0, 0.0, 0
    probe = list(items[:max(1, min(sample, len(items)))])
    calls = build_bundle(template, [[item] for item in probe])
    tokens = cost = 0
    for state, questions, _mapping in calls:
        result = client.decide(state, questions, timeout_s=timeout_s)
        tokens += result.input_tokens
        cost += result.cost_usd
    scale = len(items) / len(probe)
    return int(round(tokens * scale)), cost * scale, len(probe)


def estimate_separate_tokens(items: Sequence[BatchItem]) -> int:
    """Item text alone, at one item per call. A lower bound on the real baseline.

    Prefer :func:`measure_separate_cost` when a live client is available; this is
    the offline fallback and is deliberately labelled as a lower bound.
    """
    return sum(count_tokens(item.as_state()) for item in items)


def save_results(result: BatchResult, path: Path | str) -> None:
    """Write one JSON object per line, so results stream and pipe cleanly."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in result.outcomes:
            handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")


def summarize_outcomes(result: BatchResult, question: str | None = None) -> dict:
    """Tally the answers, which is usually the 'what next' the caller wanted."""
    tally: dict[str, dict[str, int]] = {}
    for row in result.outcomes:
        for name, value in row.values.items():
            if question and name != question:
                continue
            key = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            tally.setdefault(name, {})
            tally[name][key] = tally[name].get(key, 0) + 1
    return tally

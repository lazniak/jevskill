"""Planning layer: which pattern to use, and how to use it *iteratively*.

A harness should not reach for Jev question-by-question. It should:

1. classify the shape of the problem,
2. pick one of the patterns in ``references/patterns.md``,
3. size the data and decide whether one call is enough or the data must be
   reduced first,
4. run the first call, then let the *probability distribution* decide what to
   ask next.

That last point is the iterative property worth internalising. A Choice answer is
not a label, it is a distribution. When the top two candidates are close, the
correct move is not to accept the winner — it is to narrow to the shortlist and
ask again with more context. :func:`next_round` makes that mechanical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import CHARS_PER_TOKEN

# --------------------------------------------------------------------------- #
# Data profiling
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Shape:
    """What we learned about the payload before deciding how to send it."""

    tokens: int
    items: int
    is_sequence: bool
    fits: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "tokens": self.tokens,
            "items": self.items,
            "is_sequence": self.is_sequence,
            "fits": self.fits,
            "note": self.note,
        }


def count_tokens(text: str | bytes | Any) -> int:
    """Estimate tokens.

    Prefer :meth:`jevskill.client.JevClient.decide`'s returned ``usage`` when a
    real number is needed; this is for planning before spending a request. For a
    list of strings the estimate is the sum of the parts plus JSON punctuation,
    which matches a real request closely.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def profile(
    data: Any,
    *,
    max_state_tokens: int = 8000,
    chunk_tokens: int = 6000,
) -> Shape:
    """Decide whether ``data`` can go to Jev as-is, and if not, what to do.

    The advice is deliberately prescriptive, because "the state is too big" is
    the failure a harness is most likely to hit and the one with the least
    obvious recovery.
    """
    tokens = count_tokens(data)
    items = len(data) if isinstance(data, (list, tuple, dict)) else 1
    is_sequence = isinstance(data, (list, tuple))
    if tokens <= max_state_tokens:
        return Shape(tokens, items, is_sequence, True, "one call is enough")
    if is_sequence and items > 1:
        chunks = max(1, -(-tokens // chunk_tokens))
        return Shape(
            tokens,
            items,
            True,
            False,
            (
                f"{tokens} tokens exceeds the {max_state_tokens} budget: split into ~{chunks} "
                f"chunks of ~{chunk_tokens} tokens and use the REDUCE pattern — score or filter "
                "each chunk in code, keep the shortlist, and only then ask a final question over "
                "the survivors. Do not raise the budget: accuracy degrades before the 32K "
                "hard limit, because irrelevant state is a distractor."
            ),
        )
    return Shape(
        tokens,
        items,
        False,
        False,
        (
            f"{tokens} tokens exceeds the {max_state_tokens} budget and the state is not a "
            "sequence, so it cannot be chunked trivially. Cut it with code first (grep for the "
            "relevant region, drop boilerplate, strip repeated log prefixes) — a decision over a "
            "small, relevant state is both cheaper and more accurate than one over everything."
        ),
    )


def chunk(items: Sequence[Any], *, chunk_tokens: int = 6000, budget_tokens: int | None = None) -> list[list[Any]]:
    """Split a sequence into chunks that each fit the budget.

    ``budget_tokens`` is a hard ceiling for the *whole* payload; ``chunk_tokens``
    sizes the individual chunks. Sized by real serialized length, not by count,
    so one huge log line cannot blow a chunk.
    """
    if not items:
        return []
    limit = chunk_tokens
    if budget_tokens and budget_tokens > 0:
        limit = min(limit, budget_tokens)
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_tokens = 0
    for item in items:
        item_tokens = count_tokens(item) + 4
        if current and current_tokens + item_tokens > limit:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Pattern selection
# --------------------------------------------------------------------------- #

#: The coding-workflow palette. ``shape`` is the structural test a harness can
#: apply to its own problem before reaching for a model at all.
PATTERNS: dict[str, dict[str, Any]] = {
    "gate": {
        "shape": "one boolean about one text",
        "why": "You need a yes/no before doing something expensive.",
        "example": "Does this diff introduce a public API change?",
        "layers": "two-stage: cheap signals, then a code-combined policy",
    },
    "triage": {
        "shape": "many items -> one bounded category each",
        "why": "Routing work to the right handler without reading it all.",
        "example": "Which module owns this failing test?",
        "layers": "two-stage: coarse bucket, then fine bucket",
    },
    "reduce": {
        "shape": "a large sequence -> a small shortlist",
        "why": "Cut what the expensive model has to read.",
        "example": "Which 8 of these 900 log lines matter?",
        "layers": "two-stage: chunk-score, then final question over survivors",
    },
    "rank": {
        "shape": "several items -> an order (one Score each)",
        "why": "Priority without a pairwise tournament.",
        "example": "Rank these 12 lint findings by likely user impact.",
        "layers": "two-stage: score-all, then re-score the top-k with more context",
    },
    "route": {
        "shape": "one task -> one tier of effort",
        "why": "Spend frontier tokens only where they pay.",
        "example": "Is this fix a one-line change or an architectural one?",
        "layers": "two-stage: route, then verify the route was right",
    },
    "verify": {
        "shape": "one artifact -> a rubric score",
        "why": "Check your own output before a human sees it.",
        "example": "Does this test actually exercise the reported bug?",
        "layers": "two-stage: several atomic Noul checks, combined in code",
    },
    "guard": {
        "shape": "one proposed action -> safe / unsafe",
        "why": "A cheap tripwire in front of an irreversible step.",
        "example": "Does this command delete data outside the repo?",
        "layers": "two-stage: guard, then require human confirmation",
    },
    "shortlist": {
        "shape": "a close decision -> ask again, narrower",
        "why": "Confidence is a distribution; use it.",
        "example": "Top two modules at 0.51/0.44 -> re-ask with only those two.",
        "layers": "iterative: loop until confident or the budget runs out",
    },
    "extract": {
        "shape": "unstructured text -> fixed fields",
        "why": "Turn a messy report into code-readable values.",
        "example": "From this stack trace: language, exception type, failing frame.",
        "layers": "two-stage: presence gate per field, then a Choice over candidates",
    },
}


def choose_pattern(problem: str) -> tuple[str, dict[str, Any]]:
    """Pick a pattern from a plain-language description of the problem.

    Keyword-based on purpose: it is instant, explainable and free, and it gives
    the harness a *default* it can override. It is not a model call, and it does
    not need to be.

    **Order matters, and it is not alphabetical.** Earlier tests win, so the
    distinctive patterns come first and the generic ones last. ``triage`` is
    checked before ``gate`` because "which" collides with "whether", and ``route``
    is checked late because as a substring it matches "troubleshoot", "reroute"
    and "trout". Getting this order wrong silently mislabels decisions in the
    ledger, so the ordering is treated as part of the contract and covered by
    tests.
    """
    text = problem.lower()
    tests: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("guard", ("safe", "danger", "destructive", "delete", "permission",
                   "allow", "block", "irreversible")),
        ("verify", ("verify", "review", "grade", "quality", "rubric")),
        ("extract", ("extract", "parse", "fields", "structured", "stack trace", "error message")),
        ("rank", ("rank", "priorit", "sort", "most important", "severity")),
        ("reduce", ("too many", "too much", "large", "big ", "thousand", "million",
                    "log", "dump", "raw data", "trim", "shortlist of", "filter down",
                    "which of these", "reduce", "shrink")),
        ("shortlist", ("narrow", "close call", "coin flip", "iterat", "ambiguous",
                       "not sure", "unsure", "low confidence", "re-ask")),
        ("triage", ("categor", "classif", "which module", "which team", "which file",
                    "which subsystem", "triage", "label", "bucket", "own")),
        ("gate", ("whether", "is it", "does it", "yes/no", "boolean", "break", "safe to")),
        ("route", ("which model", "route", "tier", "effort", "simple or complex", "escalat")),
    )
    for name, keywords in tests:
        if any(word in text for word in keywords):
            return name, PATTERNS[name]
    return "triage", PATTERNS["triage"]


# --------------------------------------------------------------------------- #
# Iteration
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Round:
    """What to do next, given the answer we just got."""

    action: str  # "accept" | "narrow" | "escalate" | "stop"
    reason: str
    shortlist: list[tuple[str, float]]
    next_options: dict[str, Any] | None = None
    extra_state_hint: str = ""

    @property
    def done(self) -> bool:
        return self.action in ("accept", "escalate", "stop")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "shortlist": self.shortlist,
            "next_options": list((self.next_options or {}).keys()),
            "extra_state_hint": self.extra_state_hint,
        }


def next_round(
    probabilities: Mapping[str, float],
    *,
    confidence: float | None = None,
    accept_confidence: float = 0.7,
    margin: float = 0.15,
    keep: int = 3,
    rounds_left: int = 2,
) -> Round:
    """Decide whether to accept, narrow, escalate, or stop after a Choice.

    The rule set is intentionally simple and inspectable:

    * **accept** — confidence at or above ``accept_confidence``, *or* the winner
      leads the runner-up by more than ``margin`` of probability mass.
    * **narrow** — the leaders are within ``margin``: re-ask over only the top
      ``keep``, with more context. This is the highest-value iteration, because
      a genuine coin-flip becomes decidable once the distracting options are
      removed.
    * **escalate** — no clear leader even after narrowing, or the round budget is
      exhausted. Hand it to the reasoning model or a human; do not guess.
    """
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return Round("escalate", "no probabilities returned", [], extra_state_hint="")

    top_key, top_p = ranked[0]
    runner_p = ranked[1][1] if len(ranked) > 1 else 0.0
    gap = top_p - runner_p

    if confidence is not None and confidence >= accept_confidence:
        return Round("accept", f"confidence {confidence:.2f} >= {accept_confidence}", ranked[:keep])
    if gap > margin:
        return Round("accept", f"{top_key} leads by {gap:.2f} (> {margin})", ranked[:keep])
    if rounds_left <= 0:
        return Round(
            "escalate",
            f"still ambiguous after narrowing ({top_key} {top_p:.2f} vs {ranked[1][0]} {runner_p:.2f})",
            ranked[:keep],
        )
    next_options = dict(ranked[:keep])
    return Round(
        "narrow",
        f"{top_key} {top_p:.2f} vs {ranked[1][0]} {runner_p:.2f} — gap {gap:.2f} <= {margin}",
        ranked[:keep],
        next_options=next_options,
        extra_state_hint=f"discriminating context between {top_key} and {ranked[1][0]}",
    )


def combine_weighted(signals: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Weighted sum of Noul signals — the composite-scoring pattern.

    Several cheap atomic gates, combined in code, beat one broad question. This
    is a direct implementation of TypeSafe's documented recommendation and of the
    independent phishing study, where five signals plus logistic regression
    reached 95.1% against a single verdict that was statistically worse.
    """
    total = 0.0
    for name, weight in weights.items():
        total += float(weight) * float(signals.get(name, 0.0) or 0.0)
    return round(total, 4)


@dataclass(slots=True)
class Plan:
    """A ready-to-run, costed plan for one Jev usage."""

    pattern: str
    pattern_info: dict[str, Any]
    shape: Shape
    calls: int
    note: str

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "shape": self.shape.to_dict(),
            "calls": self.calls,
            "note": self.note,
            "why": self.pattern_info.get("why", ""),
            "layers": self.pattern_info.get("layers", ""),
        }


def plan_for(problem: str, data: Any = None, *, max_state_tokens: int = 8000) -> Plan:
    """End-to-end: what pattern, how many calls, and why.

    This is the function a harness should call *before* spending anything. It
    answers "is Jev even the right tool here, and what will it take?" in
    microseconds.
    """
    pattern, info = choose_pattern(problem)
    shape = profile(data, max_state_tokens=max_state_tokens) if data is not None else Shape(0, 0, False, True, "no data supplied")
    calls = 1
    note = shape.note
    if not shape.fits and shape.is_sequence:
        chunks = max(1, -(-shape.tokens // 6000))
        calls = chunks + 1
        note = f"{chunks} chunk call(s) + 1 final call over the shortlist"
    elif not shape.fits:
        note = "reduce the state with code first — see the note in Shape"
    if info.get("layers", "").startswith("iterative"):
        calls = max(calls, 2)
    return Plan(pattern, info, shape, calls, note)


def estimate_saving(
    *,
    state: Any,
    questions: int = 3,
    jev_input_price: float = 0.042,
    llm_input_price: float = 3.00,
    llm_output_price: float = 15.00,
    llm_output_tokens: int = 60,
) -> dict:
    """What the same judgement costs as a Jev call versus as an LLM call.

    Prices are arguments, not constants, precisely so the reader can substitute
    their own provider's rates and check the arithmetic. The default LLM price is
    a mainstream frontier chat model's list rate.
    """
    state_tokens = count_tokens(state)
    # Question text is real input and is billed; roughly 60 tokens per question.
    jev_tokens = state_tokens + questions * 60
    jev_cost = jev_tokens / 1_000_000 * jev_input_price
    llm_cost = (
        jev_tokens / 1_000_000 * llm_input_price
        + llm_output_tokens / 1_000_000 * llm_output_price
    )
    return {
        "state_tokens": state_tokens,
        "questions": questions,
        "jev_tokens": jev_tokens,
        "jev_cost_usd": round(jev_cost, 8),
        "llm_cost_usd": round(llm_cost, 6),
        "ratio": round(llm_cost / jev_cost, 1) if jev_cost else None,
        "saved_usd": round(llm_cost - jev_cost, 6),
    }


def should_use_jev(problem: str, data: Any = None) -> dict:
    """The go/no-go check, with the reasoning attached.

    Jev is the wrong tool when the answer is prose, code, or an open-ended set
    that cannot be enumerated before the request. Saying so explicitly — and
    saying it *first* — is what keeps a harness from reaching for it out of
    novelty.
    """
    text = problem.lower()
    disqualifiers = {
        "prose": ("write", "summar", "explain", "draft", "compose", "document", "comment"),
        "codegen": ("generate code", "write the code", "implement the function", "refactor the file"),
        "open_ended": ("extract all", "list every", "find all possible", "brainstorm", "ideate"),
        "reasoning": ("debug why", "prove", "derive", "step by step", "reason about"),
    }
    for reason, keywords in disqualifiers.items():
        if any(word in text for word in keywords):
            return {
                "use_jev": False,
                "reason": reason,
                "why": (
                    "The answer is text, code, or an unbounded set. Jev emits no text and can only "
                    "choose from options you enumerate up front — use the LLM for this, and consider "
                    "a Jev gate in front of it if the call is expensive."
                ),
            }
    plan = plan_for(problem, data)
    return {
        "use_jev": True,
        "reason": "bounded decision",
        "why": f"Pattern '{plan.pattern}': {plan.pattern_info.get('why', '')}",
        "plan": plan.to_dict(),
    }


def llm_only_context(
    records: Iterable[dict],
    *,
    max_items: int = 10,
    fields: Sequence[str] = ("ts", "which", "intent", "latency_ms", "cost_usd", "baseline_tokens", "outcome"),
) -> str:
    """Render a compact ledger digest for a *reporting* LLM pass.

    Intentionally the same shape as the REDUCE pattern this skill recommends:
    never hand the raw ledger to a model. Project to a few fields, cap the rows,
    and let the LLM narrate the summary.
    """
    rows = []
    for record in list(records)[-max_items:]:
        rows.append({field: record.get(field) for field in fields})
    return json.dumps(rows, ensure_ascii=False, indent=1)
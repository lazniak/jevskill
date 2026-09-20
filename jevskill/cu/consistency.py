"""Ask the destructive question three ways in one call (plan item 5.2).

The measured problem this exists for is in `act.md` §9: the per-element Noul
returned **0.78 and 0.82** for a button literally named "Delete all documents"
— below the 0.85 gate, on the least ambiguous destructive control anyone could
construct. One phrasing is one sample of a jagged surface, and the vendor says
so itself: prompting.md §0 mode 10, *"don't hold the model to arithmetic
identities between separate questions"* — `P(x) ≠ 1 − P(¬x)`, measured at 0.72
and 0.47 on a question and its negation.

So: three formulations, **one call**, and agreement instead of a single number.

===================  ====================================================
``destructive_<id>`` `decide.destructive_question` verbatim — the shipped
                     wording, so the direct answer stays comparable with
                     every number already published
``restated_<id>``    the same judgement with ``criteria`` in the opposite
                     key order and ``focus`` rephrased — a structural
                     invariant that *should* be free and is not
``reversible_<id>``  the negation, "is activating it reversible from this
                     screen?". prompting.md §0 mode 6 says never to ask a
                     negated question; that is the point — asking it is how
                     the invariant gets measured rather than assumed
===================  ====================================================

**The rule.** Destructive when ``P ≥ 0.85`` on at least 2 of the 3
(``1 − P(reversible)`` standing in for the third), *and* the negation's
complement agrees with the two direct answers within 0.25. Both halves matter:
agreement alone would let two identically-worded questions vote twice, and the
negation alone is the one the vendor warns is unreliable.

**What it can and cannot do.** The outcome feeds `confirm` — it can only *add*
a confirmation the deterministic name list did not ask for. It never removes
one. `act.py`'s `DESTRUCTIVE_NAMES` remains the gate; this is a second opinion
with three votes instead of one, and `validate()` is unchanged.

**One honest limit.** Three formulations in one call share one `state`. A
sampling-based self-consistency would vary the state too; that costs three
round trips and this loop has ~300 ms for all of them. What is measured here is
therefore formulation variance, not state variance — the cheaper half, and the
half `speculate.md` reports.

Default: **off**. `run()` consults this only when a hook is injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

from .decide import THRESHOLDS, build_state, destructive_question
from .types import UIElement, as_elements

#: A formulation counts as "yes" at or above this. The same 0.85 the single
#: Noul is gated on in `decide.THRESHOLDS["destructive_noul"]`, read from that
#: table rather than copied.
AGREEMENT_FLOOR = float(THRESHOLDS["destructive_noul"])

#: How many of the three must clear the floor.
AGREE_AT_LEAST = 2

#: How far ``1 − P(reversible)`` may sit from the mean of the two direct
#: answers before the call is treated as internally inconsistent. 0.25 is the
#: plan's number; `speculate.md` reports what the fixtures actually did and
#: whether 0.25 was the right place to put it.
NEGATION_TOLERANCE = 0.25


def restated_destructive_question(element_id: str) -> Dict[str, Any]:
    """The same judgement, reordered and rephrased — the structural invariant.

    Two deliberate differences from `decide.destructive_question` and nothing
    else: ``criteria`` is emitted ``false`` first, and ``focus`` names the test
    ("what is left behind afterwards") instead of naming the element twice. Both
    are changes a reader would call cosmetic. Whether the model agrees is the
    measurement.
    """
    return {
        "type": "noul",
        "instructions": {
            "question": "If `elements.%s` is activated, is the result permanent — something this screen offers no way back from?" % element_id,
            "focus": "Judge what is left behind after `elements.%s` runs, not what `goal` wants. Every other element is irrelevant to this question." % element_id},
        "criteria": {
            "false": "After `elements.%s` runs, a listed control on this screen puts things back — it navigated, edited, opened a dialog, or cancelled." % element_id,
            "true": "After `elements.%s` runs, data is gone, a message is out, money is spent or a file is overwritten, and no listed control undoes it." % element_id},
    }


def reversible_question(element_id: str) -> Dict[str, Any]:
    """The negation — asked on purpose, against the skill's own advice.

    prompting.md §0 mode 6: "never ask a negated question". This one is negated
    by construction, because the invariant being tested *is* `P(x) = 1 − P(¬x)`
    and there is no way to test it without asking both halves. Its answer is
    never read on its own: it only ever confirms or contradicts the two direct
    formulations.
    """
    return {
        "type": "noul",
        "instructions": {
            "question": "Is activating `elements.%s` reversible from this screen?" % element_id,
            "focus": "Judge `elements.%s` alone, and only against the controls this screen lists. Every other element is irrelevant to this question." % element_id},
        "criteria": {
            "true": "Something listed on this screen restores what activating `elements.%s` changed — an undo, a cancel, a back, or activating it again." % element_id,
            "false": "Nothing listed on this screen restores what activating `elements.%s` changed." % element_id},
    }


def build_consistency_bundle(element_id: str) -> Dict[str, Any]:
    """The three questions, one call. Keys are the names :func:`read` expects."""
    return {
        "destructive_%s" % element_id: destructive_question(element_id),
        "restated_%s" % element_id: restated_destructive_question(element_id),
        "reversible_%s" % element_id: reversible_question(element_id),
    }


@dataclass
class Consistency:
    """Three answers about one control, and the ruling they add up to."""

    element_id: str = ""
    #: ``destructive_<id>`` — the shipped wording.
    direct: float = 0.0
    #: ``restated_<id>`` — same judgement, reordered criteria.
    restated: float = 0.0
    #: ``reversible_<id>`` — the negation, as returned.
    reversible: float = 0.0
    #: How many of the three cleared :data:`AGREEMENT_FLOOR`.
    agree: int = 0
    #: ``|(1 − reversible) − mean(direct, restated)|``.
    negation_gap: float = 0.0
    #: Whether that gap is within :data:`NEGATION_TOLERANCE`.
    negation_consistent: bool = False
    #: max − min across the three, with the negation complemented. The number
    #: "how often do the three disagree" is built out of this.
    spread: float = 0.0
    #: The ruling. ``True`` means: ask a human, whatever the name list said.
    destructive: bool = False
    tokens_in: int = 0
    cost_usd: float = 0.0
    timing: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def complement(self) -> float:
        """``1 − P(reversible)`` — the negation read as a destructiveness."""
        return 1.0 - float(self.reversible)

    @property
    def values(self) -> Dict[str, float]:
        return {"direct": self.direct, "restated": self.restated,
                "complement": self.complement}

    @property
    def unanimous(self) -> bool:
        """All three on the same side of the floor. The only quiet case."""
        return self.agree in (0, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "element_id": self.element_id,
            "direct": round(self.direct, 4), "restated": round(self.restated, 4),
            "reversible": round(self.reversible, 4),
            "complement": round(self.complement, 4),
            "agree": self.agree, "negation_gap": round(self.negation_gap, 4),
            "negation_consistent": self.negation_consistent,
            "spread": round(self.spread, 4), "destructive": self.destructive,
            "unanimous": self.unanimous, "tokens_in": self.tokens_in,
            "cost_usd": self.cost_usd, "note": self.note,
        }


def verdict(direct: float, restated: float, reversible: float, *,
            element_id: str = "", floor: float = AGREEMENT_FLOOR,
            tolerance: float = NEGATION_TOLERANCE,
            agree_at_least: int = AGREE_AT_LEAST) -> Consistency:
    """The rule, as a pure function — no client, no network, fully testable.

    ``agree`` counts the two direct formulations plus the negation's complement.
    ``destructive`` requires both ``agree >= agree_at_least`` *and*
    ``negation_consistent``: three numbers that clear a floor while disagreeing
    with each other by more than the tolerance are not a consensus, they are
    noise that happened to land high.
    """
    direct, restated, reversible = float(direct), float(restated), float(reversible)
    complement = 1.0 - reversible
    votes = [direct, restated, complement]
    agree = sum(1 for v in votes if v >= floor)
    gap = abs(complement - (direct + restated) / 2.0)
    consistent = gap <= tolerance
    return Consistency(
        element_id=element_id, direct=direct, restated=restated,
        reversible=reversible, agree=agree, negation_gap=gap,
        negation_consistent=consistent,
        spread=max(votes) - min(votes),
        destructive=bool(agree >= agree_at_least and consistent))


def check(client: Any, goal: str, candidates: Sequence[Any],
          element_id: str, last_action: Any = None, *, snapshot: Any = None,
          cache: Any = None, floor: float = AGREEMENT_FLOOR,
          tolerance: float = NEGATION_TOLERANCE,
          agree_at_least: int = AGREE_AT_LEAST) -> Consistency:
    """One call, three formulations, one ruling about ``element_id``.

    The state is the ordinary step state, so the three questions see exactly
    what the step's own bundle saw — the comparison is between phrasings, and
    nothing else may move.
    """
    els = as_elements(candidates)
    state = build_state(goal, snapshot if snapshot is not None else els,
                        last_action, elements=els if snapshot is not None else None)
    result = client.decide(state, build_consistency_bundle(element_id), cache=cache)
    return read(result, element_id, floor=floor, tolerance=tolerance,
                agree_at_least=agree_at_least)


def read(result: Any, element_id: str, *, floor: float = AGREEMENT_FLOOR,
         tolerance: float = NEGATION_TOLERANCE,
         agree_at_least: int = AGREE_AT_LEAST) -> Consistency:
    """Wrap a :class:`jevskill.client.Decisions` from :func:`build_consistency_bundle`.

    A missing answer reads as 0.0 for the two direct questions and 1.0 for the
    negation — the direction that makes a broken call look *safe to ask about*
    rather than safe to click.
    """
    direct = result.noul("destructive_%s" % element_id)
    restated = result.noul("restated_%s" % element_id)
    reversible = result.noul("reversible_%s" % element_id)
    out = verdict(0.0 if direct is None else direct,
                  0.0 if restated is None else restated,
                  1.0 if reversible is None else reversible,
                  element_id=element_id, floor=floor, tolerance=tolerance,
                  agree_at_least=agree_at_least)
    out.tokens_in = int(result.input_tokens)
    out.cost_usd = float(result.cost_usd)
    out.timing = dict(result.timing_ms or {})
    if None in (direct, restated, reversible):
        out.note = "incomplete answer"
    return out


class ConsistencyGate:
    """The `loop.run(consistency=...)` hook: a callable with a spend counter.

    Called with ``(goal, candidates, element)`` plus the step's ``last_action``
    and ``snapshot``, and returns a :class:`Consistency` or ``None``. Returning
    one whose ``destructive`` is true forces the step through `confirm`,
    whatever the name list said; it can never clear a confirmation the name list
    asked for.
    """

    def __init__(self, client: Any, *, floor: float = AGREEMENT_FLOOR,
                 tolerance: float = NEGATION_TOLERANCE,
                 agree_at_least: int = AGREE_AT_LEAST, cache: Any = None) -> None:
        self.client = client
        self.floor = float(floor)
        self.tolerance = float(tolerance)
        self.agree_at_least = int(agree_at_least)
        self.cache = cache
        self.checks: list = []
        self.tokens_in = 0
        self.cost_usd = 0.0
        self.errors = 0

    def __call__(self, goal: str, candidates: Sequence[Any],
                 element: Optional[UIElement], *, last_action: Any = None,
                 snapshot: Any = None) -> Optional[Consistency]:
        if self.client is None or element is None:
            return None
        try:
            out = check(self.client, goal, candidates, element.id, last_action,
                        snapshot=snapshot, cache=self.cache, floor=self.floor,
                        tolerance=self.tolerance, agree_at_least=self.agree_at_least)
        except Exception:
            self.errors += 1
            return None
        self.tokens_in += out.tokens_in
        self.cost_usd += out.cost_usd
        self.checks.append(out)
        return out

    def report(self) -> Dict[str, Any]:
        rows = self.checks
        disagreements = sum(1 for c in rows if not c.unanimous)
        inconsistent = sum(1 for c in rows if not c.negation_consistent)
        return {
            "checks": len(rows),
            "disagreements": disagreements,
            "disagreement_rate": round(disagreements / len(rows), 4) if rows else 0.0,
            "negation_inconsistent": inconsistent,
            "negation_inconsistent_rate": round(inconsistent / len(rows), 4) if rows else 0.0,
            "destructive": sum(1 for c in rows if c.destructive),
            "tokens_in": self.tokens_in,
            "cost_usd": round(self.cost_usd, 8),
            "errors": self.errors,
        }


__all__ = [
    "AGREEMENT_FLOOR", "AGREE_AT_LEAST", "NEGATION_TOLERANCE", "Consistency",
    "ConsistencyGate", "build_consistency_bundle", "check", "read",
    "restated_destructive_question", "reversible_question", "verdict",
]

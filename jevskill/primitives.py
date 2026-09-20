"""The three primitives, plus safeguards that stop a harness using them badly.

Jev evaluates every question independently and in parallel against one shared
state, so the marginal cost of the 2nd..Nth question is a few input tokens and
almost no latency. The single most valuable behaviour this module encourages is
therefore *fan-out*: ask many narrow questions in one call instead of looping.

The constructors validate aggressively, because the documented 400 causes are
almost always a question built with an empty or single-option criteria set.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .errors import JevQuestionError

__all__ = ["noul", "choice", "score", "Q", "MAX_CHOICES", "MAX_SCORE_LEVELS"]

#: Above this, split the options or push a coarse Choice in front of a fine one.
#: Modeled on the measured Wikirace case (48 options, ~1100 tokens, still fast),
#: but accuracy on adjacent options degrades well before cost does.
MAX_CHOICES = 40
#: Score is a weighted mean over levels; beyond ~7 the axis stops being
#: meaningful to a human reading the output.
MAX_SCORE_LEVELS = 7


def noul(instructions: str | Mapping[str, Any], true: Any = None, false: Any = None) -> dict:
    """A yes/no gate. Returns ``P(true)`` as a float in ``[0, 1]``.

    Use for filters, guardrails and verification. The threshold is applied by
    *your* code, never by the model, so one answer can drive several policies.

    ``criteria`` is optional but sharply improves accuracy: describing what
    counts as true *and* what counts as false is what stops a gate drifting.
    """
    question: dict = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        criteria: dict[str, Any] = {}
        if true is not None:
            criteria["true"] = true
        if false is not None:
            criteria["false"] = false
        question["criteria"] = criteria
    return question


def choice(instructions: str | Mapping[str, Any], criteria: Mapping[str, Any]) -> dict:
    """Pick exactly one option from a fixed set. Returns the key plus a full
    probability distribution and a confidence value.

    Option keys are the strings your code branches on, so name them the way the
    code wants to read them (``"payments"``, ``"e7"``, ``"rewrite"``), not the
    way a human would phrase them. Always include an explicit escape hatch such
    as ``"unclear"`` — without one, the model is forced to pick a wrong answer
    when no option fits.
    """
    if not criteria:
        raise JevQuestionError("choice requires at least one option")
    if len(criteria) < 2:
        raise JevQuestionError(
            "choice with a single option is a no-op — use noul() if the answer is binary"
        )
    if len(criteria) > MAX_CHOICES:
        raise JevQuestionError(
            f"choice has {len(criteria)} options (max {MAX_CHOICES}). "
            "Either narrow with code, or run a coarse Choice first, then a fine one "
            "over only the shortlisted options."
        )
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str | Mapping[str, Any], criteria: Sequence[Any]) -> dict:
    """Grade the state on an ordered rubric.

    Returns the probability-weighted mean of the level indices (so ``1.4`` is a
    legitimate answer, not just integers), the legend, and the distribution.
    Order defines the scale, and it must be monotonic — put the lowest level
    first. Score grades *one* thing on *one* axis; to compare N items against
    each other, use one ``score`` per item in a fan-out, then sort in code.
    """
    if not criteria:
        raise JevQuestionError("score requires at least one level")
    if len(criteria) < 2:
        raise JevQuestionError("score with a single level cannot vary — use noul() instead")
    if len(criteria) > MAX_SCORE_LEVELS:
        raise JevQuestionError(
            f"score has {len(criteria)} levels (max {MAX_SCORE_LEVELS}). "
            "Coarser axes are better calibrated; collapse adjacent levels."
        )
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


class Q:
    """Namespace for named, self-documenting question bundles.

    Build one bundle per *intent*, then reuse it. Keeping bundles as data means a
    harness can cache, diff and tune them across runs instead of re-inventing
    prompt wording every session.
    """

    noul = staticmethod(noul)
    choice = staticmethod(choice)
    score = staticmethod(score)


def validate_questions(questions: Mapping[str, dict]) -> None:
    """Fail fast on the shapes the API rejects, before spending a round trip."""
    if not questions:
        raise JevQuestionError("at least one question is required")
    for name, question in questions.items():
        if not isinstance(question, dict):
            raise JevQuestionError(f"question {name!r} must be a dict")
        kind = question.get("type")
        if kind not in ("noul", "choice", "score"):
            raise JevQuestionError(
                f"question {name!r} has type {kind!r}; must be 'noul', 'choice' or 'score'"
            )
        if not question.get("instructions"):
            raise JevQuestionError(f"question {name!r} is missing 'instructions'")
        if kind == "choice":
            criteria = question.get("criteria") or {}
            if len(criteria) < 2:
                raise JevQuestionError(f"choice {name!r} needs at least 2 options")
        if kind == "score":
            criteria = question.get("criteria") or []
            if len(criteria) < 2:
                raise JevQuestionError(f"score {name!r} needs at least 2 levels")
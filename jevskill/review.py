"""Which answers should not be acted on unattended.

Jev returns calibrated distributions, so the useful question is not "what did it
say" but "is this confident enough to automate". One number is not enough: the
vendor's ``confidence`` is a *concentration* measure, not the probability of being
correct — measured and documented in ``references/benchmarks.md`` — so a `choice`
also needs the margin between the top two options, and a `noul` needs to sit near
an extreme rather than in the middle of the range.

**The thresholds are illustrative heuristics, not calibrated guarantees.** The
defaults (0.75 / 0.10) are a starting point; tune them on held-out data for your
own workload. That is what ``jevskill outcome`` and ``jevskill advice`` exist to
measure, and why :data:`DEFAULT_REVIEW_BELOW` is exposed as a flag rather than
buried as a constant.

Exit code 2 is the contract: "at least one answer needs review". It is deliberately
distinct from 1 (error) so a harness can tell "the model hesitated" apart from
"the call failed" without parsing output.
"""

from __future__ import annotations

#: Below this, an answer is not safe to automate unattended.
DEFAULT_REVIEW_BELOW = 0.75

#: Below this gap between the top two options, a `choice` is too close to call.
DEFAULT_REVIEW_MARGIN = 0.10


def needs_review(
    answer,
    *,
    below: float = DEFAULT_REVIEW_BELOW,
    margin: float = DEFAULT_REVIEW_MARGIN,
) -> bool:
    """True when one answer is too close to call for unattended use."""
    if answer.kind == "noul":
        value = answer.value
        if value is None:
            return True
        # Expressed through the same confidence floor so one flag tunes every
        # primitive: p below `below` and above `1 - below` is the band where a
        # rephrase could flip the answer.
        return (1.0 - below) < float(value) < below

    if answer.kind == "choice":
        ranked = answer.top(2)
        if not ranked:
            return True
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        return top < below or (top - second) < margin

    if answer.kind == "score":
        confidence = answer.confidence
        if confidence is not None:
            return confidence < below
        ranked = answer.top(2)
        if not ranked:
            return True
        return ranked[0][1] < below

    # An unknown kind is reported rather than silently trusted.
    return True


def review_report(
    answers,
    *,
    below: float = DEFAULT_REVIEW_BELOW,
    margin: float = DEFAULT_REVIEW_MARGIN,
) -> dict[str, bool]:
    """Per-answer flags, in the order the questions were asked."""
    return {
        name: needs_review(answer, below=below, margin=margin)
        for name, answer in answers.items()
    }


def flagged(report: dict[str, bool]) -> list[str]:
    """The names that need a human (or a second model) before acting."""
    return [name for name, flag in report.items() if flag]
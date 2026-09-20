"""Tests for the review contract: which answers are safe to automate unattended.

The thresholds are heuristics, so these tests pin the *behaviour of each rule* and
the exit-code contract, not the tuned numbers — retune the defaults and the rule
tests should still hold.
"""

from __future__ import annotations

import pytest

from jevskill.client import Answer
from jevskill.review import (
    DEFAULT_REVIEW_BELOW,
    DEFAULT_REVIEW_MARGIN,
    flagged,
    needs_review,
    review_report,
)


def noul(p) -> Answer:
    return Answer("noul", "q", {"type": "noul", "noul": p})


def choice(top: float, second: float) -> Answer:
    return Answer("choice", "q", {
        "type": "choice", "choice": "a", "confidence": top,
        "probabilities": {"a": top, "b": second},
    })


def score(confidence) -> Answer:
    payload = {"type": "score", "score": 1.2, "probabilities": {"1": 0.9, "2": 0.1}}
    if confidence is not None:
        payload["confidence"] = confidence
    return Answer("score", "q", payload)


class TestNoul:
    @pytest.mark.parametrize("p", [0.0, 0.04, 0.96, 1.0])
    def test_extremes_are_actionable(self, p):
        assert needs_review(noul(p)) is False

    @pytest.mark.parametrize("p", [0.3, 0.5, 0.72, 0.74])
    def test_the_middle_band_needs_review(self, p):
        assert needs_review(noul(p)) is True

    def test_the_band_follows_the_threshold(self):
        # A higher floor is stricter: it widens the band that counts as unclear.
        assert needs_review(noul(0.6), below=0.9) is True
        assert needs_review(noul(0.6), below=0.5) is False

    def test_a_missing_value_is_not_trusted(self):
        assert needs_review(Answer("noul", "q", {"type": "noul"})) is True


class TestChoice:
    def test_a_lopsided_choice_is_actionable(self):
        assert needs_review(choice(0.9, 0.05)) is False

    def test_a_low_top_probability_needs_review(self):
        assert needs_review(choice(0.6, 0.3)) is True

    def test_a_narrow_margin_needs_review_even_when_top_is_high(self):
        # 0.82 clears the floor, but 0.82 vs 0.79 is a coin toss dressed up.
        assert needs_review(choice(0.82, 0.79)) is True

    def test_the_margin_threshold_is_tunable(self):
        assert needs_review(choice(0.82, 0.79), margin=0.01) is False

    def test_a_single_option_cannot_be_close_to_a_second(self):
        assert needs_review(Answer("choice", "q", {
            "type": "choice", "choice": "a", "confidence": 0.9,
            "probabilities": {"a": 0.9}})) is False


class TestScore:
    def test_reported_confidence_is_used_when_present(self):
        assert needs_review(score(0.9)) is False
        assert needs_review(score(0.5)) is True

    def test_it_falls_back_to_the_distribution_without_confidence(self):
        assert needs_review(score(None)) is False
        assert needs_review(Answer("score", "q", {
            "type": "score", "score": 0.5, "probabilities": {"1": 0.55, "2": 0.45}})) is True


def test_unknown_kinds_are_reported_not_trusted():
    assert needs_review(Answer("mystery", "q", {"type": "mystery"})) is True


def test_report_and_flagged_keep_question_order():
    report = review_report({
        "certain": noul(0.99),
        "coin_toss": noul(0.51),
        "also_certain": choice(0.95, 0.01),
    })
    assert report == {"certain": False, "coin_toss": True, "also_certain": False}
    assert flagged(report) == ["coin_toss"]


def test_defaults_are_documented_and_tunable():
    assert 0 < DEFAULT_REVIEW_MARGIN < DEFAULT_REVIEW_BELOW <= 1
    assert DEFAULT_REVIEW_BELOW == 0.75


class TestBundledScriptStaysInStep:
    """The zero-install script carries its own copy of the review rules, so the
    two implementations must be shown to agree rather than assumed to."""

    def test_the_thresholds_match_the_package(self, jev_query):
        assert jev_query.DEFAULT_REVIEW_BELOW == DEFAULT_REVIEW_BELOW
        assert jev_query.DEFAULT_REVIEW_MARGIN == DEFAULT_REVIEW_MARGIN

    @pytest.mark.parametrize("p", [0.0, 0.1, 0.36, 0.5, 0.74, 0.9, 1.0])
    def test_noul_verdicts_agree_with_the_package(self, jev_query, p):
        raw = {"type": "noul", "noul": p}
        assert jev_query.needs_review(raw) is needs_review(noul(p))

    def test_choice_verdicts_agree_on_a_narrow_margin(self, jev_query):
        raw = {"type": "choice", "choice": "a", "confidence": 0.82,
               "probabilities": {"a": 0.82, "b": 0.79}}
        assert jev_query.needs_review(raw) is needs_review(choice(0.82, 0.79)) is True

    def test_score_uses_reported_confidence_like_the_package(self, jev_query):
        assert jev_query.needs_review(
            {"type": "score", "score": 1.0, "confidence": 0.5}) is True
        assert jev_query.needs_review(
            {"type": "score", "score": 1.0, "confidence": 0.95}) is False

    def test_an_unknown_kind_is_reported_not_trusted(self, jev_query):
        assert jev_query.needs_review({"type": "future"}) is True

    def test_review_flags_keep_question_order(self, jev_query):
        answers = {"a": {"type": "noul", "noul": 0.99},
                   "b": {"type": "noul", "noul": 0.5}}
        assert jev_query.review_flags(answers, DEFAULT_REVIEW_BELOW,
                                      DEFAULT_REVIEW_MARGIN) == ["b"]
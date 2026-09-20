"""Tests for the advice layer — turning the ledger into "should we keep doing this?"

This is the part of the objective that asks the model to *learn when using Jev pays
off*, rather than only to use it. The verdicts are policy, so the tests pin the
policy boundaries explicitly: a saving too small to matter must be reported as not
worth it even when accuracy is perfect, and an unmeasured intent must never be
reported as a win.
"""

from __future__ import annotations

import pytest

from jevskill.stats import (
    ADVICE,
    advise,
    format_advice,
    record_decision,
    record_outcome,
    _verdict_for,
)


def entry(*, n=10, input_tokens=20_000, comparable_tokens=200_000,
          baseline_tokens=200_000, baseline_cost=3.0, data_cost=2.1,
          comparable_cost=None, cost=0.01, correct=0, incorrect=0, p50_ms=350.0):
    """An aggregated bucket shaped like the real ones `summarize` produces.

    ``accuracy`` is derived from the counts, not passed in — an earlier version of
    this helper set it to the function object, which is always truthy and so
    silently skipped every accuracy branch.

    The worth-it verdict compares ``input_tokens`` (what Jev actually billed)
    against ``comparable_tokens`` (the read it replaced). Both are tokens, so no
    price and no scaffolding can distort the ratio.
    """
    judged = correct + incorrect
    if comparable_cost is None:
        comparable_cost = data_cost
    return {
        "n": n,
        "p50_ms": p50_ms,
        "cost_usd": cost,
        "baseline_cost_usd": baseline_cost,
        "baseline_data_cost_usd": data_cost,
        "data_cost_at_jev_rates_usd": comparable_cost,
        "input_tokens": input_tokens,
        "comparable_tokens": comparable_tokens,
        "baseline_tokens": baseline_tokens,
        "outcomes": {"correct": correct, "incorrect": incorrect},
        "accuracy": (correct / judged) if judged else None,
    }


def summary(**overrides):
    """A report summary shaped like `advise` returns, for format_advice tests."""
    base = {"decisions": 0, "judged": 0, "saved_usd": 0.0,
            "saved_pct": None, "overall_accuracy": None}
    base.update(overrides)
    return base


class TestVerdicts:
    def test_not_worth_it_when_the_state_is_too_small(self):
        """The finding that matters most: replacing almost nothing.

        Jev reads 120 tokens to replace 40 — the question text a decision needs
        outweighs a tiny state. Two earlier versions of this check passed
        everything: one priced the state at a frontier model's rate (~71x Jev's),
        the other added an absolute dollar floor on top of a still-mismatched
        ratio.
        """
        verdict = _verdict_for(entry(n=10, input_tokens=1_200, comparable_tokens=400,
                                     correct=10))
        assert verdict["verdict"] == "not_worth_it"
        assert "to replace" in verdict["why"]

    def test_not_worth_it_even_at_perfect_accuracy(self):
        # Ordering is deliberate: "we replace nothing" outranks "we are accurate".
        verdict = _verdict_for(entry(n=100, input_tokens=13_000, comparable_tokens=4_000,
                                     correct=100))
        assert verdict["verdict"] == "not_worth_it"

    def test_a_large_state_is_worth_doing(self):
        """The other side, so the threshold is not always-true.

        ~20k tokens replaced by ~4k read: 80% fewer tokens.
        """
        verdict = _verdict_for(entry(n=10, input_tokens=40_000, comparable_tokens=200_000))
        assert verdict["verdict"] == "unproven"
        assert verdict["facts"]["saved_pct"] == pytest.approx(80.0)

    def test_worth_it_needs_both_saving_and_measured_accuracy(self):
        verdict = _verdict_for(entry(n=50, input_tokens=20_000, comparable_tokens=200_000,
                                     correct=48, incorrect=2))
        assert verdict["verdict"] == "worth_it"
        assert "96%" in verdict["why"]

    def test_escalate_when_cheap_but_too_often_wrong(self):
        verdict = _verdict_for(entry(n=50, input_tokens=20_000, comparable_tokens=200_000,
                                     correct=30, incorrect=20))
        assert verdict["verdict"] == "escalate"
        assert "60%" in verdict["why"]
        assert "escalate" in verdict["why"].lower()

    def test_unproven_when_no_outcomes_are_paired(self):
        verdict = _verdict_for(entry(n=100, input_tokens=20_000, comparable_tokens=200_000))
        assert verdict["verdict"] == "unproven"
        assert "outcome" in verdict["why"]

    def test_unproven_below_the_judged_threshold(self):
        few = ADVICE["min_judged_for_accuracy"] - 1
        verdict = _verdict_for(entry(n=100, input_tokens=20_000,
                                     comparable_tokens=200_000, correct=few))
        assert verdict["verdict"] == "unproven"

    def test_accuracy_exactly_at_the_threshold_counts_as_worth_it(self):
        judged = 100
        correct = int(ADVICE["min_accuracy"] * judged)
        verdict = _verdict_for(entry(n=judged, input_tokens=20_000,
                                     comparable_tokens=200_000,
                                     correct=correct, incorrect=judged - correct))
        assert verdict["verdict"] == "worth_it"

    def test_no_baseline_when_nothing_was_sized(self):
        verdict = _verdict_for(entry(n=10, baseline_tokens=0, baseline_cost=0.0,
                                     data_cost=0.0, comparable_cost=0.0,
                                     input_tokens=0, comparable_tokens=0, cost=0.0))
        assert verdict["verdict"] == "no_baseline"
        assert "--state" in verdict["why"]

    def test_no_baseline_when_only_the_cost_is_known(self):
        # Tokens never sized: the ratio cannot be trusted either way.
        verdict = _verdict_for(entry(n=10, comparable_tokens=0, input_tokens=1_000,
                                     baseline_tokens=0, baseline_cost=1.0))
        assert verdict["verdict"] == "no_baseline"

    def test_facts_carry_the_numbers_behind_the_verdict(self):
        verdict = _verdict_for(entry(n=10, input_tokens=20_000, comparable_tokens=200_000,
                                     baseline_tokens=500_000, correct=9, incorrect=1))
        facts = verdict["facts"]
        assert facts["calls"] == 10
        assert facts["jev_tokens"] == 20_000
        assert facts["tokens_replaced"] == 200_000
        assert facts["saved_tokens_per_call"] == 18_000
        assert facts["accuracy"] == pytest.approx(0.9)
        assert facts["judged"] == 10
        assert facts["p50_ms"] == 350.0
        assert facts["saved_pct"] == pytest.approx(90.0)


class TestAdvise:
    def _ledger(self, tmp_path):
        return tmp_path / ".jevskill" / "ledger.jsonl"

    def test_empty_ledger_gives_no_verdicts(self):
        report = advise([])
        assert report["verdicts"] == []
        assert report["summary"]["decisions"] == 0

    def test_groups_by_both_pattern_and_intent(self, tmp_path):
        ledger = self._ledger(tmp_path)
        root = ledger.parent.parent
        for _ in range(6):
            did = record_decision(which="gate", intent="pre-commit", root=root,
                                  cost_usd=0.00002, baseline_tokens=50_000)
            record_outcome(did, "correct", root=root)
        report = advise(__import__("jevskill.stats", fromlist=["x"]).load_records([ledger]))
        scopes = {(v["scope"], v["name"]) for v in report["verdicts"]}
        assert ("pattern", "gate") in scopes
        assert ("intent", "pre-commit") in scopes

    def test_worth_it_sorts_before_unproven(self, tmp_path):
        """The positive finding must lead; burying it was a real bug."""
        ledger = self._ledger(tmp_path)
        root = ledger.parent.parent
        # a proven-winner pattern
        for _ in range(6):
            did = record_decision(which="gate", root=root, cost_usd=0.00002,
                                  baseline_tokens=90_000)
            record_outcome(did, "correct", root=root)
        # an unmeasured pattern with far more calls
        for _ in range(200):
            record_decision(which="reduce", root=root, cost_usd=0.00002,
                            baseline_tokens=90_000)
        report = advise(__import__("jevskill.stats", fromlist=["x"]).load_records([ledger]))
        first = report["verdicts"][0]
        assert first["verdict"] == "worth_it", [v["verdict"] for v in report["verdicts"][:4]]

    def test_actionable_and_keep_lists(self, tmp_path):
        ledger = self._ledger(tmp_path)
        root = ledger.parent.parent
        for _ in range(6):
            did = record_decision(which="gate", root=root, cost_usd=0.00001,
                                  baseline_tokens=80_000)
            record_outcome(did, "incorrect", root=root)
        report = advise(__import__("jevskill.stats", fromlist=["x"]).load_records([ledger]))
        assert any(v["verdict"] == "escalate" for v in report["actionable"])
        assert report["keep_using"] == []

    def test_thresholds_are_reported_so_they_can_be_argued_with(self, tmp_path):
        ledger = self._ledger(tmp_path)
        record_decision(which="gate", root=ledger.parent.parent, cost_usd=0.00002,
                        baseline_tokens=80_000)
        report = advise(__import__("jevskill.stats", fromlist=["x"]).load_records([ledger]))
        assert report["thresholds"] == ADVICE
        assert "min_accuracy" in report["thresholds"]

    def test_escalated_outcomes_do_not_count_as_accuracy(self, tmp_path):
        ledger = self._ledger(tmp_path)
        root = ledger.parent.parent
        for _ in range(6):
            did = record_decision(which="gate", root=root, cost_usd=0.00002,
                                  baseline_tokens=80_000)
            record_outcome(did, "escalated", root=root)
        report = advise(__import__("jevskill.stats", fromlist=["x"]).load_records([ledger]))
        gate = next(v for v in report["verdicts"] if v["name"] == "gate")
        assert gate["verdict"] == "unproven"


class TestFormatAdvice:
    def test_empty_report_explains_how_to_start(self):
        text = format_advice({"verdicts": []})
        assert "Nothing to advise yet" in text
        assert "jevskill outcome" in text

    def test_renders_labels_and_reasons(self):
        report = {"summary": summary(decisions=50, judged=50, saved_usd=1.0,
                                      saved_pct=90.0, overall_accuracy=0.96),
                  "thresholds": ADVICE, "verdicts": [
            {"scope": "pattern", "name": "gate", "verdict": "worth_it",
             "why": "Saves 90% at 96% accuracy over 50 judged decisions.",
             "facts": {"calls": 50, "saved_pct": 90.0, "accuracy": 0.96,
                       "judged": 50, "p50_ms": 300.0}},
        ]}
        text = format_advice(report)
        assert "KEEP" in text
        assert "pattern:gate" in text
        assert "96%" in text

    def test_unproven_output_is_capped(self):
        report = {"summary": summary(decisions=20), "thresholds": ADVICE, "verdicts": [
            {"scope": "intent", "name": f"i{i}", "verdict": "unproven",
             "why": "no outcomes", "facts": {"calls": 1, "saved_pct": 50.0,
                                             "accuracy": None, "judged": 0, "p50_ms": 1.0}}
            for i in range(20)
        ]}
        text = format_advice(report, limit_unproven=3)
        assert "and 17 more unproven group(s)" in text
        # Only three rendered.
        assert text.count("UNPROVEN") == 3

    def test_all_unproven_shown_when_under_the_cap(self):
        report = {"summary": summary(decisions=1), "thresholds": ADVICE, "verdicts": [
            {"scope": "intent", "name": "a", "verdict": "unproven", "why": "x",
             "facts": {"calls": 1, "saved_pct": 50.0, "accuracy": None,
                       "judged": 0, "p50_ms": 1.0}}
        ]}
        text = format_advice(report, limit_unproven=5)
        assert "more unproven" not in text

    def test_thresholds_printed_so_the_reader_can_disagree(self):
        report = {"summary": summary(decisions=5, judged=5, saved_usd=0.1,
                                      saved_pct=90.0, overall_accuracy=1.0),
                  "thresholds": ADVICE, "verdicts": [
            {"scope": "pattern", "name": "gate", "verdict": "worth_it", "why": "x",
             "facts": {"calls": 5, "saved_pct": 90.0, "accuracy": 1.0,
                       "judged": 5, "p50_ms": 1.0}}
        ]}
        text = format_advice(report)
        assert "thresholds:" in text
        assert "policy, not fact" in text


class TestAdviceThresholds:
    def test_accuracy_threshold_is_believable(self):
        assert 0.5 <= ADVICE["min_accuracy"] <= 0.99

    def test_min_saved_pct_is_not_trivial(self):
        # A 5% saving is not worth a network round trip.
        assert ADVICE["min_saved_pct"] >= 10

    def test_judged_threshold_is_at_least_a_handful(self):
        assert ADVICE["min_judged_for_accuracy"] >= 3

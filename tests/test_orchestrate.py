"""Tests for planning, profiling, chunking and the iteration rule."""

from __future__ import annotations

import pytest

from jevskill.orchestrate import (
    PATTERNS,
    chunk,
    choose_pattern,
    combine_weighted,
    count_tokens,
    estimate_saving,
    llm_only_context,
    next_round,
    plan_for,
    profile,
    should_use_jev,
)


class TestCountTokens:
    def test_scales_with_length(self):
        # Derived, not hard-coded: the constant is calibrated against the live
        # API and will be re-tuned, and these tests should track it rather than
        # pin it.
        from jevskill.config import CHARS_PER_TOKEN

        assert count_tokens("x" * 10_000) == int(10_000 / CHARS_PER_TOKEN)

    def test_handles_structures(self):
        from jevskill.config import CHARS_PER_TOKEN

        assert count_tokens(["a" * 10_000]) >= int(10_000 / CHARS_PER_TOKEN)

    def test_handles_bytes(self):
        assert count_tokens(b"x" * 360) == count_tokens("x" * 360)

    def test_never_returns_zero(self):
        assert count_tokens("") == 1

    def test_calibration_is_conservative_for_logs(self):
        """The estimate must not under-count log-like content.

        Measured against the live API, the naive 3.6 chars/token rule
        under-counted synthetic log lines by 2.15x. The corrected constant is
        derived from that measurement, so a state that looks like it fits really
        does fit.
        """
        from jevskill.config import CHARS_PER_TOKEN

        assert CHARS_PER_TOKEN <= 2.0, (
            "chars-per-token above 2.0 risks under-counting code and log content"
        )


class TestProfile:
    def test_small_state_fits(self):
        shape = profile({"a": "b"})
        assert shape.fits is True
        assert "one call" in shape.note

    def test_large_sequence_is_chunked_with_advice(self):
        shape = profile([f"line {i} " + "x" * 60 for i in range(400)])
        assert shape.fits is False
        assert shape.is_sequence is True
        assert "REDUCE" in shape.note
        # The advice must steer away from simply raising the budget.
        assert "Do not raise the budget" in shape.note

    def test_large_scalar_state_tells_you_to_cut_with_code(self):
        shape = profile("x" * 200_000)
        assert shape.fits is False
        assert shape.is_sequence is False
        assert "grep" in shape.note or "Cut it with code" in shape.note

    def test_respects_a_custom_budget(self):
        assert profile({"a": "b"}, max_state_tokens=1).fits is False

    def test_shape_is_serialisable(self):
        assert set(profile({"a": "b"}).to_dict()) == {"tokens", "items", "is_sequence", "fits", "note"}


class TestChunk:
    def test_splits_a_long_sequence(self):
        items = [f"line {i}" + "y" * 100 for i in range(60)]
        parts = chunk(items, chunk_tokens=1000)
        assert len(parts) > 1
        assert sum(len(p) for p in parts) == len(items)

    def test_preserves_order(self):
        items = [f"item-{i}" for i in range(50)]
        flat = [x for part in chunk(items, chunk_tokens=200) for x in part]
        assert flat == items

    def test_empty_input(self):
        assert chunk([], chunk_tokens=100) == []

    def test_single_oversized_item_still_yields_a_chunk(self):
        parts = chunk(["z" * 50_000], chunk_tokens=100)
        assert len(parts) == 1

    def test_budget_caps_the_chunk_size(self):
        items = [f"i{i}" + "y" * 100 for i in range(40)]
        parts = chunk(items, chunk_tokens=5000, budget_tokens=500)
        assert parts
        # Each chunk must respect the tighter of the two limits.
        assert all(count_tokens(part) <= 700 for part in parts)


class TestChoosePattern:
    @pytest.mark.parametrize(
        "problem,expected",
        [
            ("there are too many log lines, keep the important ones", "reduce"),
            ("this dump is large, shrink it to a shortlist", "reduce"),
            ("is it safe to run this destructive command", "guard"),
            ("can we allow this command to delete files", "guard"),
            ("rank these findings by severity", "rank"),
            ("prioritize the open findings", "rank"),
            ("verify the test actually covers the bug", "verify"),
            ("review this against a rubric", "verify"),
            ("extract fields from this stack trace", "extract"),
            ("parse the error message into structured values", "extract"),
            ("which model tier should handle this task", "route"),
            ("how much effort does this change need", "route"),
            ("categorize which module owns this failure", "triage"),
            ("classify this ticket into a bucket", "triage"),
            ("does this diff break the api", "gate"),
            ("whether the tests pass", "gate"),
            ("this decision is a close call, narrow it down", "shortlist"),
            ("low confidence, re-ask over the leaders", "shortlist"),
        ],
    )
    def test_keyword_routing(self, problem, expected):
        name, info = choose_pattern(problem)
        assert name == expected, f"{problem!r} routed to {name}, expected {expected}"
        assert info is PATTERNS[expected]

    def test_unrecognised_problem_defaults_to_triage(self):
        name, _ = choose_pattern("something entirely unrelated")
        assert name == "triage"

    def test_matching_is_case_insensitive(self):
        assert choose_pattern("RANK these")[0] == "rank"

    def test_every_pattern_is_documented(self):
        for name, info in PATTERNS.items():
            assert info["shape"] and info["why"] and info["example"] and info["layers"], name

    def test_every_pattern_is_reachable(self):
        # A pattern no keyword can select is dead code in the palette.
        samples = {
            "gate": "does this break the api",
            "triage": "categorize this ticket",
            "reduce": "too many log lines",
            "rank": "rank by severity",
            "route": "which model tier",
            "verify": "verify the output",
            "guard": "is this safe",
            "shortlist": "this is a close call",
            "extract": "extract fields from this",
        }
        for expected, sample in samples.items():
            assert choose_pattern(sample)[0] == expected, sample

    def test_route_does_not_hijack_unrelated_words(self):
        # 'route' is a substring of 'troubleshoot'; the ordering must prevent it
        # from swallowing unrelated problems.
        assert choose_pattern("troubleshoot this failure")[0] != "route"


class TestNextRound:
    def test_accepts_on_high_confidence(self):
        verdict = next_round({"a": 0.55, "b": 0.45}, confidence=0.95)
        assert verdict.action == "accept"
        assert verdict.done is True

    def test_accepts_on_a_clear_margin_even_without_confidence(self):
        verdict = next_round({"a": 0.9, "b": 0.1})
        assert verdict.action == "accept"

    def test_narrows_on_a_close_call(self):
        verdict = next_round({"a": 0.51, "b": 0.44, "c": 0.05}, confidence=0.5)
        assert verdict.action == "narrow"
        assert verdict.done is False
        assert set(verdict.next_options) == {"a", "b", "c"}

    def test_narrow_carries_the_leaders_and_a_hint(self):
        verdict = next_round({"a": 0.51, "b": 0.49}, keep=2)
        assert list(verdict.next_options) == ["a", "b"]
        assert "a" in verdict.extra_state_hint and "b" in verdict.extra_state_hint

    def test_escalates_when_rounds_are_exhausted(self):
        verdict = next_round({"a": 0.51, "b": 0.49}, rounds_left=0)
        assert verdict.action == "escalate"
        assert verdict.done is True

    def test_empty_probabilities_escalate(self):
        assert next_round({}).action == "escalate"

    def test_single_option_is_accepted(self):
        assert next_round({"only": 0.6}).action == "accept"

    def test_to_dict_lists_next_option_keys_only(self):
        verdict = next_round({"a": 0.51, "b": 0.49}, keep=2)
        assert verdict.to_dict()["next_options"] == ["a", "b"]


class TestCombineWeighted:
    def test_weighted_sum(self):
        result = combine_weighted({"a": 1.0, "b": 0.0}, {"a": 0.7, "b": 0.3})
        assert result == pytest.approx(0.7)

    def test_missing_signals_count_as_zero(self):
        assert combine_weighted({"a": 1.0}, {"a": 0.5, "b": 0.5}) == pytest.approx(0.5)

    def test_rounds_to_four_places(self):
        assert combine_weighted({"a": 0.333333}, {"a": 1.0}) == 0.3333


class TestPlanFor:
    def test_reports_a_single_call_when_data_fits(self):
        plan = plan_for("categorize this ticket", {"a": 1})
        assert plan.pattern == "triage"
        assert plan.calls == 1

    def test_reports_chunk_calls_when_data_is_large(self):
        plan = plan_for("keep the important log lines", [f"l{i}" + "x" * 200 for i in range(300)])
        assert plan.pattern == "reduce"
        assert plan.calls > 2
        assert "chunk" in plan.note

    def test_iterative_patterns_ask_for_at_least_two_calls(self):
        plan = plan_for("the decision is close, narrow it", {"a": 1})
        assert plan.calls >= 2

    def test_works_without_data(self):
        plan = plan_for("categorize this")
        assert plan.shape.tokens == 0
        assert plan.calls == 1

    def test_to_dict_is_serialisable(self):
        payload = plan_for("route this", {"a": 1}).to_dict()
        assert {"pattern", "shape", "calls", "note", "why", "layers"} <= set(payload)


class TestShouldUseJev:
    @pytest.mark.parametrize(
        "problem,reason",
        [
            ("write a summary of this file", "prose"),
            ("generate code for the parser", "codegen"),
            ("extract all entities from the text", "open_ended"),
            ("debug why this fails step by step", "reasoning"),
        ],
    )
    def test_rejects_what_jev_cannot_do(self, problem, reason):
        verdict = should_use_jev(problem)
        assert verdict["use_jev"] is False
        assert verdict["reason"] == reason
        assert "no text" in verdict["why"]

    def test_accepts_a_bounded_decision(self):
        verdict = should_use_jev("categorize which module owns this")
        assert verdict["use_jev"] is True
        assert verdict["plan"]["pattern"] == "triage"

    def test_rejection_is_decided_before_planning(self):
        # A prose request must not be costed as if it were a Jev call.
        assert "plan" not in should_use_jev("write documentation for this module")


class TestEstimateSaving:
    def test_jev_is_far_cheaper_than_a_frontier_llm(self):
        result = estimate_saving(state={"log": "x" * 4000})
        assert result["jev_cost_usd"] < result["llm_cost_usd"]
        assert result["ratio"] > 1

    def test_prices_are_overridable(self):
        cheap = estimate_saving(state={"a": "b"}, llm_input_price=0.05, llm_output_price=0.05)
        assert cheap["ratio"] < 100

    def test_more_questions_cost_more(self):
        one = estimate_saving(state={"a": "b"}, questions=1)
        many = estimate_saving(state={"a": "b"}, questions=10)
        assert many["jev_tokens"] > one["jev_tokens"]

    def test_reports_the_arithmetic_inputs(self):
        result = estimate_saving(state={"a": "b"})
        assert {"state_tokens", "jev_tokens", "jev_cost_usd", "llm_cost_usd"} <= set(result)


class TestLlmOnlyContext:
    def test_caps_rows_and_projects_fields(self):
        records = [{"ts": f"t{i}", "which": "gate", "secret": "nope"} for i in range(50)]
        rendered = llm_only_context(records, max_items=5)
        assert rendered.count('"ts"') == 5
        assert "secret" not in rendered

    def test_empty_input(self):
        assert llm_only_context([]) == "[]"

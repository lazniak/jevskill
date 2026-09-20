"""The package's public surface is a published promise; test it as one.

``SKILL.md`` §4 tells an agent to write ``noul(...)``, ``choice(...)``,
``next_round(...)`` and ``combine_weighted(...)`` and to call
``jev.decide(state, questions)``. Until 0.11.1 ``jevskill/__init__.py`` exported
``__version__`` alone, so an agent following the skill hit ``ImportError`` before
its first decision — a documented defect (research §2.1, F8). These tests pin the
import path *and* the shapes the snippets depend on, because a name that imports
but returns a different dict is the same broken promise with a slower failure.

The behavioural assertions below are the ones the snippets actually rely on; the
full behaviour of ``next_round`` and the builders lives in ``test_orchestrate.py``
and ``test_primitives.py``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import jevskill

#: Exactly the names SKILL.md's Python snippets need to run.
PROMISED = (
    "JevClient",
    "Config",
    "Decisions",
    "Answer",
    "noul",
    "choice",
    "score",
    "next_round",
    "combine_weighted",
    "JevApiError",
    "JevConfigError",
    "JevQuestionError",
)


class TestPublicSurface:
    @pytest.mark.parametrize("name", PROMISED)
    def test_the_name_is_importable(self, name):
        assert getattr(jevskill, name, None) is not None, (
            f"SKILL.md promises `from jevskill import {name}`")

    def test_the_from_import_form_works(self):
        # The attribute test above would pass on a module __getattr__ that the
        # `from ... import ...` machinery could not see; this statement is the
        # form an agent copying SKILL.md actually types.
        from jevskill import (  # noqa: F401
            Answer,
            Config,
            Decisions,
            JevApiError,
            JevClient,
            JevConfigError,
            JevQuestionError,
            choice,
            combine_weighted,
            next_round,
            noul,
            score,
        )

    def test_all_is_declared_and_resolvable(self):
        assert set(PROMISED) <= set(jevskill.__all__)
        for name in jevskill.__all__:
            assert hasattr(jevskill, name), f"__all__ lists {name!r}, which does not resolve"

    def test_dir_matches_all(self):
        assert dir(jevskill) == sorted(jevskill.__all__)

    def test_an_unknown_name_still_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            jevskill.not_a_real_export

    def test_importing_the_package_does_not_pull_httpx(self):
        """Reading ``__version__`` must not cost the HTTP stack.

        Measured on this machine: ``import jevskill.client`` costs ~525 ms
        because ``httpx`` imports ``httpx._main`` (click, rich). The client names
        are therefore resolved lazily, and this is the assertion that keeps an
        innocent-looking top-level import from undoing it.
        """
        proc = subprocess.run(
            [sys.executable, "-c", "import jevskill, sys; print('httpx' in sys.modules)"],
            capture_output=True, text=True, check=True,
        )
        assert proc.stdout.strip() == "False", "importing jevskill now drags in httpx"

    def test_the_lazy_names_still_come_from_the_client_module(self):
        from jevskill import client

        assert jevskill.JevClient is client.JevClient
        assert jevskill.Decisions is client.Decisions
        assert jevskill.Answer is client.Answer


class TestQuestionBuildersProduceTheDocumentedShapes:
    """The dicts below are what the Decisions API is documented to accept."""

    def test_noul_without_criteria(self):
        assert jevskill.noul("Does the diff touch a public API?") == {
            "type": "noul",
            "instructions": "Does the diff touch a public API?",
        }

    def test_noul_with_criteria(self):
        question = jevskill.noul("Is `L7` an error?", true="a failure", false="anything else")
        assert question == {
            "type": "noul",
            "instructions": "Is `L7` an error?",
            "criteria": {"true": "a failure", "false": "anything else"},
        }

    def test_choice_keeps_the_option_keys_code_branches_on(self):
        options = {"backend": "server code", "frontend": "UI code", "unclear": "cannot tell"}
        question = jevskill.choice("Which module owns `file`?", options)
        assert question["type"] == "choice"
        assert question["criteria"] == options
        assert question["criteria"] is not options, "criteria must be copied, not aliased"

    def test_score_keeps_the_levels_in_order(self):
        levels = ["no risk", "some risk", "high risk"]
        question = jevskill.score("How risky is `patch`?", levels)
        assert question == {
            "type": "score",
            "instructions": "How risky is `patch`?",
            "criteria": levels,
        }

    def test_the_builders_reject_what_the_api_rejects(self):
        with pytest.raises(jevskill.JevQuestionError):
            jevskill.choice("one option is a no-op", {"only": "x"})
        with pytest.raises(jevskill.JevQuestionError):
            jevskill.score("one level cannot vary", ["only"])

    def test_the_built_questions_pass_the_client_side_validator(self):
        from jevskill.primitives import validate_questions

        validate_questions({
            "gate": jevskill.noul("Does `L7` report an error?"),
            "owner": jevskill.choice("Who owns `L7`?", {"net": "networking", "unclear": "none"}),
            "risk": jevskill.score("How risky is `L7`?", ["low", "high"]),
        })


class TestNextRound:
    """SKILL.md §4 Rule 4 branches on ``action``; these are its real semantics."""

    def test_a_close_pair_narrows_to_the_leaders(self):
        # The distribution printed in SKILL.md's worked example.
        verdict = jevskill.next_round(
            {"net/client.py": 0.51, "net/retry.py": 0.44, "net/timeout.py": 0.05, "net/pool.py": 0.0}
        )
        assert verdict.action == "narrow"
        assert not verdict.done
        assert {"net/client.py", "net/retry.py"} <= set(verdict.next_options)
        assert [name for name, _ in verdict.shortlist][:2] == ["net/client.py", "net/retry.py"]

    def test_keep_bounds_the_shortlist_to_exactly_the_two_leaders(self):
        verdict = jevskill.next_round(
            {"net/client.py": 0.51, "net/retry.py": 0.44, "net/timeout.py": 0.05}, keep=2
        )
        assert verdict.action == "narrow"
        assert set(verdict.next_options) == {"net/client.py", "net/retry.py"}

    def test_a_clear_winner_is_accepted(self):
        verdict = jevskill.next_round({"net/retry.py": 0.91, "net/client.py": 0.09})
        assert verdict.action == "accept"
        assert verdict.done
        assert verdict.next_options is None

    def test_a_flat_distribution_narrows_first_and_escalates_only_when_the_budget_is_out(self):
        """A flat distribution does **not** escalate on its own.

        The rule is about the *gap*, not about the shape: with rounds left, a
        four-way tie is exactly the case narrowing exists for. Escalation is what
        happens when narrowing has already been spent — asserting anything else
        would pin behaviour the code does not have.
        """
        flat = {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}
        assert jevskill.next_round(flat).action == "narrow"
        assert jevskill.next_round(flat, rounds_left=0).action == "escalate"

    def test_no_probabilities_escalates_rather_than_guessing(self):
        assert jevskill.next_round({}).action == "escalate"

    def test_high_confidence_accepts_even_when_the_gap_is_small(self):
        verdict = jevskill.next_round({"a": 0.51, "b": 0.44}, confidence=0.95)
        assert verdict.action == "accept"


class TestCombineWeighted:
    def test_the_documented_half_and_half_case(self):
        assert jevskill.combine_weighted({"a": 1.0, "b": 0.0}, {"a": 0.5, "b": 0.5}) == 0.5

    def test_a_missing_signal_counts_as_zero_not_as_an_error(self):
        # A question the API did not answer must not take the whole composite
        # down with it; the weight simply contributes nothing.
        assert jevskill.combine_weighted({"a": 1.0}, {"a": 0.5, "b": 0.5}) == 0.5

    def test_it_is_the_weighted_sum_skill_md_rule_2_prints(self):
        signals = {"claims_tests_pass": 1.0, "cite_diff_line": 0.0, "contradicts_diff": 1.0}
        weights = {"claims_tests_pass": 0.45, "cite_diff_line": 0.30, "contradicts_diff": 0.25}
        assert jevskill.combine_weighted(signals, weights) == pytest.approx(0.70)

"""Tests for question construction and validation. No network required."""

from __future__ import annotations

import pytest

from jevskill.errors import JevQuestionError
from jevskill.primitives import MAX_CHOICES, choice, noul, score, validate_questions


class TestNoul:
    def test_minimal(self):
        assert noul("Is it broken?") == {"type": "noul", "instructions": "Is it broken?"}

    def test_with_both_criteria(self):
        question = noul("Is it broken?", "Yes it is.", "No it is not.")
        assert question["criteria"] == {"true": "Yes it is.", "false": "No it is not."}

    def test_single_sided_criteria_is_allowed(self):
        # A one-sided gate is weaker but not invalid; the API accepts it.
        assert noul("q", "only true")["criteria"] == {"true": "only true"}

    def test_structured_instructions_pass_through(self):
        question = noul({"question": "Is `a.b` set?", "focus": "Ignore defaults."})
        assert question["instructions"]["focus"] == "Ignore defaults."


class TestChoice:
    def test_basic(self):
        question = choice("Which?", {"a": "First.", "b": "Second."})
        assert question["type"] == "choice"
        assert list(question["criteria"]) == ["a", "b"]

    def test_rejects_empty(self):
        with pytest.raises(JevQuestionError, match="at least one option"):
            choice("Which?", {})

    def test_rejects_single_option(self):
        with pytest.raises(JevQuestionError, match="no-op"):
            choice("Which?", {"only": "one"})

    def test_rejects_too_many_options(self):
        criteria = {f"o{i}": f"Option {i}" for i in range(MAX_CHOICES + 1)}
        with pytest.raises(JevQuestionError, match="max"):
            choice("Which?", criteria)

    def test_accepts_structured_criteria(self):
        question = choice("Which?", {"a": {"what": "x", "not_for": "y"}, "b": {"what": "z"}})
        assert question["criteria"]["a"]["not_for"] == "y"

    def test_copies_criteria(self):
        original = {"a": "x", "b": "y"}
        choice("Which?", original)
        assert original == {"a": "x", "b": "y"}


class TestScore:
    def test_basic(self):
        question = score("How bad?", ["Low", "Medium", "High"])
        assert question["criteria"] == ["Low", "Medium", "High"]

    def test_rejects_empty(self):
        with pytest.raises(JevQuestionError, match="at least one level"):
            score("How bad?", [])

    def test_rejects_single_level(self):
        with pytest.raises(JevQuestionError, match="cannot vary"):
            score("How bad?", ["Only"])

    def test_rejects_absurd_axis(self):
        with pytest.raises(JevQuestionError, match="max"):
            score("How bad?", [str(i) for i in range(9)])

    def test_preserves_order(self):
        levels = ["Z", "A", "M"]
        assert score("?", levels)["criteria"] == ["Z", "A", "M"]


class TestValidateQuestions:
    def test_accepts_a_normal_bundle(self):
        validate_questions({
            "gate": noul("Is it?", "y", "n"),
            "route": choice("Which?", {"a": "A", "b": "B"}),
            "grade": score("How?", ["L", "M", "H"]),
        })

    def test_rejects_empty_bundle(self):
        with pytest.raises(JevQuestionError, match="at least one question"):
            validate_questions({})

    def test_rejects_unknown_type(self):
        with pytest.raises(JevQuestionError, match="must be 'noul', 'choice' or 'score'"):
            validate_questions({"q": {"type": "ranking", "instructions": "x"}})

    def test_rejects_missing_instructions(self):
        with pytest.raises(JevQuestionError, match="missing 'instructions'"):
            validate_questions({"q": {"type": "noul"}})

    def test_rejects_degenerate_choice_built_by_hand(self):
        # A caller can bypass the constructor by loading JSON, so validation
        # has to stand on its own.
        with pytest.raises(JevQuestionError, match="at least 2 options"):
            validate_questions({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}})

    def test_rejects_degenerate_score_built_by_hand(self):
        with pytest.raises(JevQuestionError, match="at least 2 levels"):
            validate_questions({"q": {"type": "score", "instructions": "x", "criteria": ["only"]}})

    def test_rejects_non_dict_question(self):
        with pytest.raises(JevQuestionError, match="must be a dict"):
            validate_questions({"q": "just a string"})
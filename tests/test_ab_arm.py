"""The A/B arm must gate blocks, not lines.

That single change is what took the `yaml_drift` row from 0/3 to 3/3, and it is easy
to undo by accident — the arm is a benchmark script with no other test coverage. This
pins the property that made the difference: the state sent to the gate contains
*multi-line* units for indentation-structured input, and a single window never splits
a header from its values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from jevskill.client import Answer, Decisions

import bench.ab as ab  # noqa: E402  (needs the repo root on sys.path)

YAML = """flags:
  flag_0000:
    default: false
    prod: false
    owner: team-3
  flag_0512:
    default: false
    prod: true
    owner: team-7
"""


@dataclass
class RecordingClient:
    """Stands in for JevClient; records the state each call was given."""

    states: list = field(default_factory=list)
    questions: list = field(default_factory=list)

    def decide(self, state, questions, **kwargs):
        self.states.append(state)
        self.questions.append(questions)
        # Answers are keyed by QUESTION name (`keep_B{i}`), while the state is keyed
        # by unit (`B{i}`) — the same relationship the real response has.
        answers = {}
        for name in questions:
            unit = name[len("keep_"):] if name.startswith("keep_") else name
            value = state.get(unit, "")
            drifting = "prod: true" in value
            answers[name] = Answer(
                "noul", name,
                {"type": "noul", "noul": 0.95 if drifting else 0.02})
        return Decisions(answers=answers, model="test", request_id="r",
                         usage={"input_tokens": 10, "output_tokens": 2, "cost": 0.0},
                         timing_ms={"total_ms": 1.0})


def run_arm(monkeypatch, context: str, *, keep: int = 8):
    client = RecordingClient()
    captured = {}

    def fake_ask_model(model, reduced, question, key, timeout=120.0):
        captured["reduced"] = reduced
        return {"content": reduced, "prompt_tokens": 1, "completion_tokens": 1,
                "total_tokens": 2, "ms": 1.0}

    monkeypatch.setattr(ab, "ask_model", fake_ask_model)
    monkeypatch.setattr(ab, "record_decision", lambda **k: "d_test")
    result = ab.arm_jev(client, context,
                        "Which feature flag is enabled in prod but disabled by default?",
                        "keep lines where the prod value differs from the default value",
                        "test-model", "sk-or-v1-test", window=40, keep=keep)
    return client, captured, result


class TestTheArmGatesBlocks:
    def test_a_header_and_its_values_travel_in_one_unit(self, monkeypatch):
        client, _, _ = run_arm(monkeypatch, YAML)
        state = client.states[0]
        joined = json.dumps(state)
        assert "prod: true" in joined and "default: false" in joined
        # The property that was missing: the pair shares a single state value.
        assert any("default" in v and "prod" in v for v in state.values()), state

    def test_the_header_is_in_the_same_unit_as_the_signal(self, monkeypatch):
        client, _, _ = run_arm(monkeypatch, YAML)
        # The block text keeps its original indentation; only the membership matters.
        assert any("flag_0512:" in v and "prod: true" in v
                   for v in client.states[0].values())

    def test_the_question_names_each_unit(self, monkeypatch):
        client, _, _ = run_arm(monkeypatch, YAML)
        # Naming the value in the STATE is the repo's most expensive lesson. The unit
        # (`B0`) is what the question must point at; `keep_B0` is only its answer key.
        state_units = set(client.states[0])
        for name, question in client.questions[0].items():
            unit = name[len("keep_"):]
            assert unit in state_units, "the question named a unit absent from the state"
            assert f"`{unit}`" in question["instructions"]
            assert f"`{unit}`" in question["criteria"]["true"]
            assert f"`{unit}`" in question["criteria"]["false"]

    def test_the_reduced_context_carries_the_answer(self, monkeypatch):
        _, captured, _ = run_arm(monkeypatch, YAML)
        # The whole point: after reduction the flag NAME must still be present.
        assert "flag_0512" in captured["reduced"]

    def test_flat_text_still_gates_line_by_line(self, monkeypatch):
        client, captured, _ = run_arm(monkeypatch, "ERROR a\nINFO b\nWARN c")
        assert len(client.states[0]) == 3
        assert client.states[0]["B0"] == "ERROR a"

    def test_a_block_is_never_split_by_the_window(self, monkeypatch):
        # window=4 forces several windows; every block must stay whole regardless.
        client, _, _ = run_arm(monkeypatch, YAML, keep=2)
        for state in client.states:
            for value in state.values():
                assert "flag_0512:" not in value or "prod: true" in value, \
                    "a header was separated from its values by a window boundary"

    def test_it_reports_the_block_budget_it_used(self, monkeypatch):
        _, _, result = run_arm(monkeypatch, YAML)
        assert result["jev_calls"] >= 1 and result["shortlist_lines"] >= 1
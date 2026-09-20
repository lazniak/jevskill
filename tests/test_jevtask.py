"""Tests for batching: the template rewrite, the strategies, and failure handling.

The rewrite is the part that matters most. Batching puts many items in one state,
which makes the "question does not name its value" failure *more* likely — and that
failure is silent, returning a flat plausible number for every candidate. These
tests pin that the backticked `item` reference is retargeted per item.
"""

from __future__ import annotations

import json

import pytest

from jevskill.client import Answer, Decisions
from jevskill.jevtask import (
    BatchItem,
    build_bundle,
    estimate_separate_tokens,
    load_items,
    run_batch,
    save_results,
    summarize_outcomes,
    _retarget,
)
from jevskill.primitives import choice, noul


def make_answer(name: str, value, confidence=None):
    if isinstance(value, str):
        return Answer("choice", name, {"type": "choice", "choice": value,
                                       "confidence": confidence or 0.9,
                                       "probabilities": {value: confidence or 0.9}})
    return Answer("noul", name, {"type": "noul", "noul": value})


class FakeClient:
    """Records the calls it receives and answers by generated question id."""

    def __init__(self, *, value_for=None, fail_on=None):
        self.calls: list[dict] = []
        self.value_for = value_for or (lambda gen, state: "routine")
        self.fail_on = fail_on

    def decide(self, state, questions, session_id=None, timeout_s=None):
        self.calls.append({"state": state, "questions": questions,
                           "session_id": session_id, "timeout_s": timeout_s})
        if self.fail_on is not None and len(self.calls) - 1 == self.fail_on:
            raise RuntimeError("simulated provider failure")
        answers = {}
        for gen in questions:
            answers[gen] = make_answer(gen, self.value_for(gen, state))
        return Decisions(
            answers=answers, model="fake", request_id="r1",
            usage={"input_tokens": 100, "output_tokens": 5, "cost": 0.000004},
            timing_ms={"total_ms": 1.0}, provider="openrouter",
        )


def items_from(texts):
    return [BatchItem(index=i, data=t, label=f"L{i}") for i, t in enumerate(texts)]


class TestRetarget:
    def test_backticked_item_is_rewritten_to_the_state_key(self):
        q = noul("Does `item` report an error?")
        out = _retarget(q, "item_7")
        assert "`item_7`" in out["instructions"]
        assert "`item`" not in out["instructions"]

    def test_nested_path_is_preserved(self):
        q = noul("Is `item.message` urgent?")
        assert "`item_3.message`" in _retarget(q, "item_3")["instructions"]

    def test_criteria_are_rewritten_too(self):
        q = noul("Is `item` bad?", true="`item` failed.", false="`item` is fine.")
        out = _retarget(q, "item_2")
        assert "`item_2`" in out["criteria"]["true"]
        assert "`item_2`" in out["criteria"]["false"]

    def test_structured_instructions_are_rewritten(self):
        q = noul({"question": "Is `item` urgent?", "focus": "Ignore `item.id`."})
        out = _retarget(q, "item_5")
        assert out["instructions"]["question"] == "Is `item_5` urgent?"
        assert out["instructions"]["focus"] == "Ignore `item_5.id`."

    def test_the_original_template_is_not_mutated(self):
        q = noul("Does `item` report an error?")
        _retarget(q, "item_9")
        assert "`item`" in q["instructions"]

    def test_unrelated_words_are_untouched(self):
        q = noul("Is this item-like text a problem?")
        assert _retarget(q, "item_1")["instructions"] == "Is this item-like text a problem?"


class TestBuildBundle:
    def test_one_state_key_per_item(self):
        items = items_from(["a", "b", "c"])
        calls = build_bundle({"kind": noul("Is `item` bad?")}, [items])
        state, questions, mapping = calls[0]
        assert set(state) == {"item_0", "item_1", "item_2"}
        assert len(questions) == 3
        assert mapping["kind__1"] == (1, "kind")

    def test_question_ids_are_namespaced(self):
        items = items_from(["a", "b"])
        _state, questions, _mapping = build_bundle(
            {"kind": noul("x"), "risk": noul("y")}, [items])[0]
        assert set(questions) == {"kind__0", "risk__0", "kind__1", "risk__1"}

    def test_each_item_is_independently_named(self):
        items = items_from(["a", "b"])
        _state, questions, _m = build_bundle({"k": noul("Is `item` bad?")}, [items])[0]
        assert "`item_0`" in questions["k__0"]["instructions"]
        assert "`item_1`" in questions["k__1"]["instructions"]

    def test_empty_window_list_yields_no_calls(self):
        assert build_bundle({"k": noul("x")}, []) == []


class TestLoadItems:
    def test_jsonl(self, tmp_path):
        path = tmp_path / "items.jsonl"
        path.write_text('{"line": "a"}\n{"line": "b"}\n', encoding="utf-8")
        items = load_items(path, text_key="line")
        assert [i.data for i in items] == ["a", "b"]

    def test_json_array(self, tmp_path):
        path = tmp_path / "items.json"
        path.write_text('["a", "b", "c"]', encoding="utf-8")
        assert len(load_items(path)) == 3

    def test_plain_lines_fall_back_to_strings(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_text("ERROR one\nINFO two\n", encoding="utf-8")
        items = load_items(path)
        assert [i.data for i in items] == ["ERROR one", "INFO two"]

    def test_labels_are_detected(self, tmp_path):
        path = tmp_path / "items.jsonl"
        path.write_text('{"id": "L7", "line": "x"}\n', encoding="utf-8")
        assert load_items(path, text_key="line")[0].label == "L7"

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "items.jsonl"
        path.write_text('{"a":1}\n\n\n{"a":2}\n', encoding="utf-8")
        assert len(load_items(path)) == 2

    def test_missing_text_key_names_the_available_keys(self, tmp_path):
        path = tmp_path / "items.jsonl"
        path.write_text('{"body": "x"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="available keys"):
            load_items(path, text_key="line")

    def test_empty_file_is_an_error(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="no items"):
            load_items(path)


class TestRunBatch:
    def test_windowed_makes_fewer_calls(self):
        client = FakeClient()
        items = items_from([f"line {i}" for i in range(20)])
        result = run_batch(client, items, {"k": noul("Is `item` bad?")},
                           strategy="windowed", window_size=8)
        assert result.calls == 3
        assert len(client.calls) == 3

    def test_per_item_makes_one_call_each(self):
        client = FakeClient()
        items = items_from([f"line {i}" for i in range(5)])
        result = run_batch(client, items, {"k": noul("x")}, strategy="per-item")
        assert result.calls == 5

    def test_every_item_gets_an_outcome_in_order(self):
        client = FakeClient()
        items = items_from([f"line {i}" for i in range(20)])
        result = run_batch(client, items, {"k": noul("x")}, window_size=7)
        assert [o.index for o in result.outcomes] == list(range(20))

    def test_values_are_attributed_to_the_right_item(self):
        # Answer depends on the item's own text, so a mis-mapped id shows up.
        client = FakeClient(value_for=lambda gen, state: "problem"
                            if "ERROR" in str(state) else "routine")
        items = items_from(["ERROR a", "INFO b", "ERROR c"])
        result = run_batch(client, items, {"k": choice("x", {"problem": "p", "routine": "r"})},
                           strategy="per-item")
        assert [o.values["k"] for o in result.outcomes] == ["problem", "routine", "problem"]

    def test_usage_is_totalled_across_calls(self):
        client = FakeClient()
        items = items_from([f"l{i}" for i in range(20)])
        result = run_batch(client, items, {"k": noul("x")}, window_size=8)
        assert result.input_tokens == 300   # 3 calls x 100
        assert result.cost_usd == pytest.approx(0.000012)

    def test_a_failed_call_loses_only_its_own_items(self):
        client = FakeClient(fail_on=1)
        items = items_from([f"l{i}" for i in range(20)])
        result = run_batch(client, items, {"k": noul("x")}, window_size=8,
                           concurrency=1)
        assert result.errors == 8              # the whole second window
        assert sum(1 for o in result.outcomes if o.ok) == 12
        errored = [o for o in result.outcomes if not o.ok]
        assert "simulated provider failure" in errored[0].error

    def test_labels_are_carried_through(self):
        client = FakeClient()
        items = items_from(["a", "b"])
        result = run_batch(client, items, {"k": noul("x")}, strategy="per-item")
        assert [o.label for o in result.outcomes] == ["L0", "L1"]

    def test_probabilities_are_preserved(self):
        client = FakeClient(value_for=lambda gen, state: "routine")
        items = items_from(["a"])
        result = run_batch(client, items,
                           {"k": choice("x", {"routine": "r", "problem": "p"})},
                           strategy="per-item")
        assert result.outcomes[0].probabilities["k"]["routine"] > 0

    def test_empty_input_returns_empty(self):
        result = run_batch(FakeClient(), [], {"k": noul("x")})
        assert result.outcomes == [] and result.calls == 0

    def test_unknown_strategy_is_rejected(self):
        with pytest.raises(ValueError, match="unknown strategy"):
            run_batch(FakeClient(), items_from(["a"]), {"k": noul("x")},
                      strategy="magic")

    def test_timeout_is_passed_through(self):
        client = FakeClient()
        run_batch(client, items_from(["a"]), {"k": noul("x")}, timeout_s=99.0,
                  strategy="per-item")
        assert client.calls[0]["timeout_s"] == 99.0

    def test_concurrency_does_not_lose_items(self):
        client = FakeClient()
        items = items_from([f"l{i}" for i in range(12)])
        result = run_batch(client, items, {"k": noul("x")}, strategy="per-item",
                           concurrency=6)
        assert len(result.outcomes) == 12
        assert all(o.ok for o in result.outcomes)


class TestResultReporting:
    def test_token_saving_uses_the_supplied_baseline(self):
        client = FakeClient()
        items = items_from([f"l{i}" for i in range(10)])
        result = run_batch(client, items, {"k": noul("x")}, strategy="per-item")
        result.separate_tokens = result.input_tokens * 2
        assert result.token_saving_pct == pytest.approx(50.0)

    def test_no_baseline_means_no_percentage(self):
        result = run_batch(FakeClient(), items_from(["a"]), {"k": noul("x")})
        assert result.token_saving_pct is None

    def test_json_shape(self):
        result = run_batch(FakeClient(), items_from(["a"]), {"k": noul("x")})
        payload = result.to_dict()
        assert payload["items"] == 1
        assert payload["results"][0]["values"]

    def test_save_results_writes_one_json_object_per_line(self, tmp_path):
        result = run_batch(FakeClient(), items_from(["a", "b"]), {"k": noul("x")})
        out = tmp_path / "out.jsonl"
        save_results(result, out)
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["index"] == 0

    def test_tally_counts_answers(self):
        client = FakeClient(value_for=lambda gen, state: "routine")
        result = run_batch(client, items_from(["a", "b", "c"]), {"k": noul("x")},
                           strategy="per-item")
        tally = summarize_outcomes(result)
        assert tally["k"]["routine"] == 3

    def test_tally_can_filter_to_one_question(self):
        client = FakeClient(value_for=lambda gen, state: "routine")
        result = run_batch(client, items_from(["a"]), {"k": noul("x")},
                           strategy="per-item")
        assert summarize_outcomes(result, question="nope") == {}

    def test_estimate_separate_tokens_counts_item_text(self):
        items = items_from(["x" * 1_000, "y" * 1_000])
        assert estimate_separate_tokens(items) > 1_000

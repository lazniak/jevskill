"""REDUCE must never drop something it could not evaluate.

Found by auditing the reduction path against a competing implementation's explicit
rule that "timeouts and provider failures conservatively keep records for
analysis". The script did the opposite for a subtler reason: it coerced a *missing*
verdict to 0.0, which is indistinguishable from a confident "irrelevant". An item
the model never judged was filed as rejected and counted in the reduction.
"""

from __future__ import annotations

import json

import pytest

ITEMS = [
    "INFO  healthcheck ok",
    "ERROR payment capture failed",
    "INFO  cache hit",
    "WARN  db pool at 94%",
]

QUESTIONS = {"type": "noul"}


def outcome(index: int, noul) -> dict:
    answer = {"type": "noul"}
    if noul is not None:
        answer["noul"] = noul
    return {f"keep_L{index}": answer}


def verdicts(overrides: dict | None = None) -> dict:
    """A complete answer set for ITEMS: every index judged 0.0 unless overridden.

    Complete on purpose — a partial set would itself create unjudged items and
    make the assertions measure the fixture instead of the behaviour.
    """
    overrides = overrides or {}
    answers: dict = {}
    for i in range(len(ITEMS)):
        answers.update(outcome(i, overrides.get(i, 0.0)))
    return answers


def missing(index: int, overrides: dict | None = None) -> dict:
    """As above, but `index` gets a response with no verdict at all."""
    answers = verdicts(overrides)
    answers[f"keep_L{index}"] = {"type": "noul"}
    return answers


def run_reduce(jev_query, tmp_path, monkeypatch, responses):
    """Drive reduce_state with scripted per-chunk responses."""
    payloads = iter(responses)

    def fake_post(body, key, base_url, endpoint, retries, timeout, provider):
        return next(payloads)

    monkeypatch.setattr(jev_query, "post", fake_post)

    class Args:
        window = 20
        keep = 1
        instructions = None
        state_file = None
        no_recovery = False
        recovery_dir = str(tmp_path / "recovery")
        retries = 0
        timeout = 5.0

    return jev_query.reduce_state(list(ITEMS), Args(), "sk-or-v1-test",
                                  "openrouter", "typesafe/jev-1.13")


class TestUnjudgedItemsAreKept:
    def test_a_missing_verdict_keeps_the_item_rather_than_rejecting_it(
            self, jev_query, tmp_path, monkeypatch):
        # Item 1 is judged true; item 2's verdict is missing entirely.
        response = {
            "answers": missing(2, {1: 0.97}),
            "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0},
        }
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        kept_texts = result["lines"]  # already the kept texts
        assert ITEMS[2] in kept_texts, "an unjudged item must not be dropped"
        assert result["unjudged_count"] == 1

    def test_the_unjudged_item_is_not_counted_as_rejected(
            self, jev_query, tmp_path, monkeypatch):
        response = {
            "answers": missing(2, {1: 0.97}),
            "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0},
        }
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        record = json.loads(
            (tmp_path / "recovery" / f"{result['handle']}.json").read_text(encoding="utf-8"))
        rejected_indexes = {row["index"] for row in record["rejected"]}
        assert 2 not in rejected_indexes, "it was never judged, so it is not rejected"
        assert [row["index"] for row in record["unjudged"]] == [2]

    def test_the_partition_still_accounts_for_every_item(
            self, jev_query, tmp_path, monkeypatch):
        response = {
            "answers": missing(1, {0: 0.9}),
            "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0},
        }
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        record = json.loads(
            (tmp_path / "recovery" / f"{result['handle']}.json").read_text(encoding="utf-8"))
        seen = ({row["index"] for row in record["kept"]}
                | {row["index"] for row in record["rejected"]}
                | {row["index"] for row in record["unjudged"]})
        assert seen == set(range(len(ITEMS)))

    def test_an_unjudged_item_survives_a_tight_keep_budget(
            self, jev_query, tmp_path, monkeypatch):
        # keep=1, so a judged hit and an unjudged item compete. The unjudged item
        # must not be the one that loses: it was never weighed.
        response = {
            "answers": missing(2, {0: 0.9, 1: 0.8}),
            "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0},
        }
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        kept_texts = result["lines"]  # already the kept texts
        assert ITEMS[2] in kept_texts

    def test_a_genuine_zero_is_still_a_rejection(self, jev_query, tmp_path, monkeypatch):
        # The distinction that matters: 0.0 is a verdict, missing is not.
        response = {
            "answers": verdicts({0: 0.92}),
            "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0},
        }
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        assert result["unjudged_count"] == 0
        assert result["kept"] == 1

    def test_the_count_is_reported_so_the_caller_can_act_on_it(
            self, jev_query, tmp_path, monkeypatch):
        answers = verdicts({0: 0.92})
        answers["keep_L1"] = {"type": "noul"}
        answers["keep_L2"] = {"type": "noul"}
        response = {"answers": answers,
                    "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0}}
        result = run_reduce(jev_query, tmp_path, monkeypatch, [response])
        assert result["unjudged_count"] == 2

    def test_a_failing_chunk_says_what_it_spent_before_dying(
            self, jev_query, tmp_path, monkeypatch, capsys):
        def boom(*a, **k):
            raise SystemExit("simulated transport failure")

        monkeypatch.setattr(jev_query, "post", boom)

        class Args:
            window = 20
            keep = 1
            instructions = None
            state_file = None
            no_recovery = True
            recovery_dir = str(tmp_path / "recovery")
            retries = 0
            timeout = 5.0

        with pytest.raises(SystemExit):
            jev_query.reduce_state(list(ITEMS), Args(), "sk-or-v1-test",
                                   "openrouter", "typesafe/jev-1.13")
        err = capsys.readouterr().err
        assert "nothing was written" in err and "chunk 1" in err
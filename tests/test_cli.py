"""Tests for the command line. No network: the client is monkeypatched."""

from __future__ import annotations

import json

import pytest

from jevskill import cli
from jevskill.client import Answer, Decisions


def fake_decisions(**overrides) -> Decisions:
    answers = {
        "owner": Answer("choice", "owner", {
            "type": "choice", "choice": "billing", "confidence": 0.88,
            "probabilities": {"billing": 0.88, "api": 0.12},
        }),
        "risk": Answer("score", "risk", {
            "type": "score", "score": 1.4, "confidence": 0.7,
            "legend": {"0": "Low", "1": "Med", "2": "High"},
            "probabilities": {"0": 0.1, "1": 0.5, "2": 0.4},
        }),
        "needs_test": Answer("noul", "needs_test", {"type": "noul", "noul": 0.91}),
    }
    defaults = dict(
        answers=answers, model="typesafe/jev-1.13-20260917", request_id="gen-1",
        usage={"input_tokens": 500, "output_tokens": 40, "cost": 0.000021},
        timing_ms={"serialize_ms": 0.2, "http_ms": 310.5, "parse_ms": 0.1, "total_ms": 311.0},
    )
    defaults.update(overrides)
    return Decisions(**defaults)


class FakeClient:
    """Stands in for JevClient; records what it was asked."""

    calls: list = []

    def __init__(self, config=None, **kwargs):
        self.config = config
        self.warmed = 0

    def warm(self):
        self.warmed += 1
        return 90.0

    def decide(self, state, questions, session_id=None, timeout_s=None):
        FakeClient.calls.append({"state": state, "questions": questions,
                                 "session_id": session_id, "timeout_s": timeout_s})
        return fake_decisions()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture(autouse=True)
def patch_client(monkeypatch, tmp_path):
    FakeClient.calls = []
    # Replace only the client. Config resolution runs for real so the commands
    # exercise their actual configuration path; a dummy key satisfies the
    # "key present" guard without touching the network.
    monkeypatch.setattr(cli, "JevClient", FakeClient)
    monkeypatch.setenv("JEVSKILL_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path))
    yield


def run(capsys, argv):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestPatternsCommand:
    def test_lists_the_palette(self, capsys):
        code, out, _ = run(capsys, ["patterns"])
        assert code == 0
        for name in ("gate", "triage", "reduce", "shortlist"):
            assert name in out

    def test_json_output_is_valid(self, capsys):
        code, out, _ = run(capsys, ["patterns", "--json"])
        payload = json.loads(out)
        assert payload["patterns"]["gate"]["shape"] == "one boolean about one text"


class TestPlanCommand:
    def test_accepts_a_bounded_decision(self, capsys):
        code, out, _ = run(capsys, ["plan", "categorize which module owns this failure"])
        assert code == 0
        assert "USE JEV" in out
        assert "triage" in out

    def test_refuses_a_prose_task(self, capsys):
        code, out, _ = run(capsys, ["plan", "write a summary of the module"])
        assert code == 0
        assert "DO NOT USE JEV" in out

    def test_json_includes_the_plan_and_saving(self, capsys):
        code, out, _ = run(capsys, ["plan", "categorize this", "--state", "a short note", "--json"])
        payload = json.loads(out)
        assert payload["use_jev"] is True
        assert payload["plan"]["pattern"] == "triage"
        assert "saving" in payload

    def test_spends_nothing(self, capsys):
        run(capsys, ["plan", "categorize this"])
        assert FakeClient.calls == []


class TestAskCommand:
    def test_single_noul_question(self, capsys):
        code, out, _ = run(capsys, [
            "ask", "--state", "some text", "--question-type", "noul",
            "--name", "gate", "--instructions", "Is it broken?", "--json",
        ])
        assert code == 0
        body = FakeClient.calls[0]["questions"]["gate"]
        assert body["type"] == "noul"
        assert body["instructions"] == "Is it broken?"

    def test_choice_from_options(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "choice", "--name", "owner",
                     "--options", "billing", "api", "--json"])
        body = FakeClient.calls[0]["questions"]["owner"]
        assert list(body["criteria"]) == ["billing", "api"]

    def test_score_from_levels(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "score", "--name", "risk",
                     "--levels", "Low", "Med", "High", "--json"])
        assert FakeClient.calls[0]["questions"]["risk"]["criteria"] == ["Low", "Med", "High"]

    def test_choice_needs_two_options(self, capsys):
        code, _, err = run(capsys, ["ask", "--state", "x", "--question-type", "choice",
                                    "--name", "o", "--options", "only"])
        assert code == 1
        assert "at least 2" in err

    def test_full_questions_json(self, capsys):
        questions = json.dumps({
            "a": {"type": "noul", "instructions": "Is it?"},
            "b": {"type": "choice", "instructions": "Which?", "criteria": {"x": "X", "y": "Y"}},
        })
        code, _, _ = run(capsys, ["ask", "--state", "x", "--questions", questions, "--json"])
        assert code == 0
        assert set(FakeClient.calls[0]["questions"]) == {"a", "b"}

    def test_invalid_questions_json_is_reported(self, capsys):
        code, _, err = run(capsys, ["ask", "--state", "x", "--questions", "{not json"])
        assert code == 1
        assert "not valid JSON" in err

    def test_missing_questions_is_reported(self, capsys):
        code, _, err = run(capsys, ["ask", "--state", "x"])
        assert code == 1
        assert "no questions given" in err

    def test_missing_state_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        code, _, err = run(capsys, ["ask", "--question-type", "noul", "--name", "q"])
        assert code == 1
        assert "no state supplied" in err

    def test_state_file_missing_is_reported(self, capsys):
        code, _, err = run(capsys, ["ask", "--state-file", "does-not-exist.txt",
                                    "--question-type", "noul", "--name", "q"])
        assert code == 1
        assert "state file not found" in err

    def test_state_file_json_is_parsed(self, capsys, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"file": "a.py"}', encoding="utf-8")
        run(capsys, ["ask", "--state-file", str(path), "--question-type", "noul", "--name", "q", "--json"])
        assert FakeClient.calls[0]["state"] == {"file": "a.py"}

    def test_oversized_state_is_refused_with_advice(self, capsys):
        code, out, _ = run(capsys, ["ask", "--state", "x" * 200_000,
                                    "--question-type", "noul", "--name", "q", "--json"])
        assert code == 3
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "advice" in payload
        assert FakeClient.calls == []  # nothing was spent

    def test_force_sends_an_oversized_state(self, capsys):
        code, _, _ = run(capsys, ["ask", "--state", "x" * 200_000, "--force",
                                  "--question-type", "noul", "--name", "q", "--json"])
        assert code == 0
        assert len(FakeClient.calls) == 1

    def test_json_reports_answers_values_and_usage(self, capsys):
        code, out, _ = run(capsys, ["ask", "--state", "x", "--questions",
                                    json.dumps({"o": {"type": "noul", "instructions": "q"}}), "--json"])
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["values"]["owner"] == "billing"
        assert payload["values"]["risk"] == 1.4
        assert payload["usage"]["input_tokens"] == 500

    def test_human_output_renders_answers_and_stages(self, capsys):
        code, out, _ = run(capsys, ["ask", "--state", "x", "--questions",
                                    json.dumps({"o": {"type": "noul", "instructions": "q"}})])
        assert "JEV decided" in out
        assert "billing" in out
        assert "stages:" in out
        assert "http" in out

    def test_session_id_is_forwarded(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--session-id", "s-1", "--json"])
        assert FakeClient.calls[0]["session_id"] == "s-1"

    def test_json_error_is_machine_readable(self, capsys):
        code, out, _ = run(capsys, ["ask", "--state", "x", "--json"])
        assert code == 1
        assert json.loads(out)["ok"] is False


class TestLedgerIntegration:
    def test_ask_writes_a_ledger_row(self, capsys, tmp_path):
        code, out, _ = run(capsys, [
            "ask", "--state", "some text", "--question-type", "noul", "--name", "q",
            "--pattern", "gate", "--intent", "unit-test", "--json",
        ])
        payload = json.loads(out)
        assert payload["decision_id"].startswith("d_")
        ledger = tmp_path / "ledger.jsonl"
        assert ledger.exists()
        row = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[0])
        assert row["which"] == "gate"
        assert row["intent"] == "unit-test"
        assert row["tokens_in"] == 500
        assert row["latency_ms"] == 311.0

    def test_no_ledger_flag_skips_the_write(self, capsys, tmp_path):
        code, out, _ = run(capsys, ["ask", "--state", "x", "--question-type", "noul",
                                    "--name", "q", "--no-ledger", "--json"])
        assert "decision_id" not in json.loads(out)
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_baseline_tokens_are_recorded(self, capsys, tmp_path):
        run(capsys, ["ask", "--state", "x" * 3600, "--question-type", "noul", "--name", "q", "--json"])
        row = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip())
        # The state, plus the question text an LLM would also have had to read.
        assert 2000 <= row["baseline_tokens"] <= 2300

    def test_baseline_grows_with_the_state(self, capsys, tmp_path):
        # Both sizes must stay inside the state budget or the larger one is
        # refused (and correctly writes no ledger row), so size them from the
        # constant rather than guessing character counts.
        from jevskill.config import CHARS_PER_TOKEN

        small_chars = 720
        big_chars = int(7000 * CHARS_PER_TOKEN)
        run(capsys, ["ask", "--state", "x" * small_chars, "--question-type", "noul",
                     "--name", "q", "--json"])
        run(capsys, ["ask", "--state", "x" * big_chars, "--question-type", "noul",
                     "--name", "q", "--json"])
        rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text(
            encoding="utf-8").strip().splitlines()]
        assert len(rows) == 2
        assert rows[1]["baseline_tokens"] > rows[0]["baseline_tokens"] * 10

    def test_a_refused_state_writes_no_ledger_row(self, capsys, tmp_path):
        code, _, _ = run(capsys, ["ask", "--state", "x" * 200_000,
                                  "--question-type", "noul", "--name", "q", "--json"])
        assert code == 3
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_pattern_is_inferred_when_not_given(self, capsys, tmp_path):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--intent", "there are too many log lines", "--json"])
        row = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip())
        assert row["which"] == "reduce"


class TestOutcomeCommand:
    def test_records_an_outcome(self, capsys, tmp_path):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q", "--json"])
        code, out, _ = run(capsys, ["outcome", "d_test123", "correct", "--detail", "ok", "--json"])
        assert code == 0
        assert json.loads(out)["outcome"] == "correct"
        lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
        patch = json.loads(lines[-1])
        assert patch["kind"] == "outcome"
        assert patch["decision_id"] == "d_test123"

    def test_rejects_an_unknown_outcome(self, capsys):
        with pytest.raises(SystemExit):
            run(capsys, ["outcome", "d_1", "maybe"])


class TestStatsCommand:
    def test_empty_ledger_is_friendly(self, capsys):
        code, out, _ = run(capsys, ["stats"])
        assert code == 0
        assert "No decisions recorded yet" in out

    def test_reports_measured_values(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--pattern", "gate", "--json"])
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--pattern", "gate", "--json"])
        code, out, _ = run(capsys, ["stats"])
        assert code == 0
        assert "2 decisions" in out
        assert "gate" in out
        assert "p50" in out

    def test_notes_when_no_outcomes_are_paired(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q", "--json"])
        _, out, _ = run(capsys, ["stats"])
        assert "no outcomes paired" in out

    def test_accuracy_appears_once_outcomes_exist(self, capsys, tmp_path):
        code, out, _ = run(capsys, ["ask", "--state", "x", "--question-type", "noul",
                                    "--name", "q", "--json"])
        decision_id = json.loads(out)["decision_id"]
        run(capsys, ["outcome", decision_id, "correct", "--json"])
        _, out, _ = run(capsys, ["stats"])
        assert "accuracy" in out
        assert "100.0%" in out

    def test_json_output_is_valid(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--pattern", "gate", "--json"])
        code, out, _ = run(capsys, ["stats", "--json"])
        payload = json.loads(out)
        assert payload["decisions"] == 1
        assert payload["by_pattern"]["gate"]["n"] == 1


class TestAdviceCommand:
    def test_empty_ledger_explains_how_to_start(self, capsys):
        code, out, _ = run(capsys, ["advice"])
        assert code == 0
        assert "Nothing to advise yet" in out
        assert "outcome" in out

    def test_advises_on_real_decisions(self, capsys):
        run(capsys, ["ask", "--state", "x", "--question-type", "noul", "--name", "q",
                     "--pattern", "gate", "--json"])
        code, out, _ = run(capsys, ["advice"])
        assert code == 0
        assert "JEV advice" in out
        assert "gate" in out

    def test_a_small_state_is_reported_as_not_worth_it(self, capsys):
        """A 500-char state costs ~$0.000014 to read.

        Jev costs about the same to decide it, so the round trip buys nothing. The
        floor is calibrated around 2,400 tokens of state (roughly 4,000 chars);
        below that the `not_worth_it` verdict fires.
        """
        code, out, _ = run(capsys, ["ask", "--state", "x" * 500, "--question-type",
                                    "noul", "--name", "q", "--pattern", "gate", "--json"])
        assert code == 0
        _, out, _ = run(capsys, ["advice"])
        assert "STOP" in out
        assert "to replace" in out

    def test_a_large_state_is_not_reported_as_not_worth_it(self, capsys):
        """The other side of the floor, so the threshold is not always-true.

        10,000 chars (~6,000 tokens) costs $0.00025 at Jev's own rate against a
        $0.000013 decision, so reduction is worth doing. No outcomes are paired,
        so it is UNPROVEN rather than KEEP.
        """
        run(capsys, ["ask", "--state", "x" * 10_000, "--question-type", "noul",
                     "--name", "q", "--pattern", "gate", "--json"])
        _, out, _ = run(capsys, ["advice"])
        assert "STOP" not in out
        assert "UNPROVEN" in out

    def test_json_output_is_valid(self, capsys):
        run(capsys, ["ask", "--state", "x" * 10_000, "--question-type", "noul",
                     "--name", "q", "--pattern", "gate", "--json"])
        code, out, _ = run(capsys, ["advice", "--json"])
        payload = json.loads(out)
        assert payload["summary"]["decisions"] == 1
        assert payload["verdicts"]
        assert "thresholds" in payload

    def test_limit_unproven_is_accepted(self, capsys):
        run(capsys, ["ask", "--state", "x" * 10_000, "--question-type", "noul",
                     "--name", "q", "--pattern", "gate", "--json"])
        code, out, _ = run(capsys, ["advice", "--limit-unproven", "1"])
        assert code == 0
        assert "UNPROVEN" in out


class TestBatchCommand:
    def _fixture(self, tmp_path, n=6):
        path = tmp_path / "items.jsonl"
        path.write_text("".join(
            json.dumps({"id": f"L{i}", "line": f"ERROR line {i}"}) + "\n"
            for i in range(n)), encoding="utf-8")
        return path

    def test_batch_runs_and_reports(self, capsys, tmp_path):
        path = self._fixture(tmp_path)
        code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                    "--question-type", "choice", "--name", "kind",
                                    "--options", "routine", "problem"])
        assert code == 0
        assert "Jev batch" in out
        assert "reading" in out

    def test_json_output_has_results_and_tally(self, capsys, tmp_path):
        path = self._fixture(tmp_path)
        code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                    "--question-type", "choice", "--name", "kind",
                                    "--options", "routine", "problem", "--json"])
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["items"] == 6
        assert len(payload["results"]) == 6
        assert "tally" in payload

    def test_out_file_is_written(self, capsys, tmp_path):
        path = self._fixture(tmp_path)
        out_file = tmp_path / "results.jsonl"
        run(capsys, ["batch", str(path), "--text-key", "line",
                     "--question-type", "noul", "--name", "q",
                     "--out", str(out_file), "--json"])
        lines = out_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 6

    def test_windowed_uses_fewer_calls_than_items(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=16)
        _code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                     "--question-type", "noul", "--name", "q",
                                     "--window", "8", "--json"])
        payload = json.loads(out)
        assert payload["calls"] == 2
        assert payload["items"] == 16

    def test_per_item_strategy_makes_one_call_each(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=4)
        _code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                     "--question-type", "noul", "--name", "q",
                                     "--strategy", "per-item", "--json"])
        assert json.loads(out)["calls"] == 4

    def test_limit_caps_items(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=10)
        _code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                     "--question-type", "noul", "--name", "q",
                                     "--limit", "3", "--json"])
        assert json.loads(out)["items"] == 3

    def test_ledger_rows_are_written_per_call(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=8)
        run(capsys, ["batch", str(path), "--text-key", "line",
                     "--question-type", "noul", "--name", "q", "--intent", "my-batch",
                     "--json"])
        rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text(
            encoding="utf-8").strip().splitlines()]
        assert rows
        assert all(r["intent"] == "my-batch" for r in rows)
        assert all(r["which"] == "triage" for r in rows)

    def test_no_ledger_flag_skips_rows(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=4)
        run(capsys, ["batch", str(path), "--text-key", "line",
                     "--question-type", "noul", "--name", "q", "--no-ledger", "--json"])
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_missing_file_is_reported(self, capsys, tmp_path):
        code, _, err = run(capsys, ["batch", str(tmp_path / "nope.jsonl"),
                                    "--question-type", "noul", "--name", "q"])
        assert code == 1
        assert "could not read items" in err

    def test_bad_text_key_is_reported(self, capsys, tmp_path):
        path = self._fixture(tmp_path)
        code, _, err = run(capsys, ["batch", str(path), "--text-key", "absent",
                                    "--question-type", "noul", "--name", "q"])
        assert code == 1
        assert "available keys" in err

    def test_measured_baseline_reports_a_saving(self, capsys, tmp_path):
        path = self._fixture(tmp_path, n=8)
        _code, out, _ = run(capsys, ["batch", str(path), "--text-key", "line",
                                     "--question-type", "noul", "--name", "q",
                                     "--json"])
        payload = json.loads(out)
        assert payload["baseline_measured"] is True
        assert payload["separate_tokens"] > 0


class TestArgumentParsing:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as info:
            cli.main(["--version"])
        assert info.value.code == 0

    def test_requires_a_subcommand(self, capsys):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_unknown_subcommand(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["frobnicate"])


class TestRenderAnswer:
    def test_noul_shows_probability(self):
        assert "0.91" in cli._render_answer(Answer("noul", "n", {"type": "noul", "noul": 0.91}))

    def test_choice_shows_winner_and_distribution(self):
        rendered = cli._render_answer(Answer("choice", "c", {
            "type": "choice", "choice": "a", "confidence": 0.9,
            "probabilities": {"a": 0.9, "b": 0.1},
        }))
        assert "'a'" in rendered and "conf 0.9" in rendered and "a=0.90" in rendered

    def test_score_shows_value(self):
        rendered = cli._render_answer(Answer("score", "s", {
            "type": "score", "score": 1.4, "confidence": 0.7,
        }))
        assert "1.4" in rendered

    def test_unknown_kind_falls_back_to_json(self):
        assert "raw" in cli._render_answer(Answer("future", "f", {"raw": True}))


class TestBaselineTokens:
    def test_counts_state_and_questions(self):
        from jevskill.primitives import noul

        baseline = cli._baseline_tokens("x" * 3600, {"q": noul("Is it?")})
        assert baseline > 1000

    def test_handles_structured_state(self):
        from jevskill.primitives import noul

        assert cli._baseline_tokens({"a": "b" * 3600}, {"q": noul("Is it?")}) > 1000

    def test_empty_state_still_counts_questions(self):
        from jevskill.primitives import noul

        assert cli._baseline_tokens("", {"q": noul("Is it?")}) >= 0
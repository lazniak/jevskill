"""The agent/harness contract: derived properties and the pure aggregation.

These are the functions `bench/cu_report.py` turns into a published table, so they
are tested against hand-built results with medians chosen to be checkable by eye.
The load-bearing test is `TestSuccessComesFromTheOracle`: a `RunResult` that
stopped with `done` must never be counted as a pass, because the agent's own
verdict is not the benchmark's success signal (`bench/cu_tasks.md`).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _contract():
    try:
        return importlib.import_module("jevskill.cu.contract")
    except ImportError:  # no package init yet: the integrator adds it
        cached = sys.modules.get("jevskill_cu_contract")
        if cached is not None:
            return cached
        path = ROOT / "jevskill" / "cu" / "contract.py"
        spec = importlib.util.spec_from_file_location("jevskill_cu_contract", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["jevskill_cu_contract"] = module
        spec.loader.exec_module(module)
        return module


cu = _contract()
StepRecord = cu.StepRecord
RunResult = cu.RunResult
RunOptions = cu.RunOptions
TaskRun = cu.TaskRun


def step(index: int, *, decide_ms: float = 300.0, t_ms: float = 400.0,
         decided_by: str = "jev", tokens_in: int = 1000,
         cost_usd: float = 0.0001) -> StepRecord:
    return StepRecord(
        index=index, t_ms=t_ms,
        stages_ms={"observe": 10.0, "reduce": 1.0, "decide": decide_ms,
                   "validate": 0.1, "act": 5.0, "settle": 50.0},
        candidates=12, target="e1", op="click", confidence=0.93, margin=0.6,
        goal_reached=0.04, needs_text=0.2, is_destructive=0.1,
        decided_by=decided_by, executed=True, tree_changed=True,
        tokens_in=tokens_in, cost_usd=cost_usd)


def result(task_id: str, run: int, *, steps, wall_ms: float, decisions=None,
           stop_reason: str = "done", escalations=None, tokens_in: int = 0,
           cost_usd: float = 0.0, destructive_gates: int = 0) -> RunResult:
    escalated = sum(1 for s in steps if s.decided_by == "escalation")
    return RunResult(
        task_id=task_id, run=run, stop_reason=stop_reason, steps=list(steps),
        wall_ms=wall_ms,
        decisions=len(steps) if decisions is None else decisions,
        escalations=escalated if escalations is None else escalations,
        destructive_gates=destructive_gates,
        tokens_in=tokens_in or sum(s.tokens_in for s in steps),
        cost_usd=cost_usd or sum(s.cost_usd for s in steps))


class TestDerivedProperties:
    def test_steps_count_is_the_list_length(self):
        assert result("a", 1, steps=[step(0), step(1), step(2)], wall_ms=1.0).steps_count == 3

    def test_decide_p50_is_the_median_decide_stage(self):
        steps = [step(0, decide_ms=100.0), step(1, decide_ms=300.0), step(2, decide_ms=200.0)]
        assert result("a", 1, steps=steps, wall_ms=1.0).decide_ms_p50 == 200.0

    def test_step_p50_is_the_median_whole_step(self):
        steps = [step(0, t_ms=100.0), step(1, t_ms=300.0), step(2, t_ms=200.0)]
        assert result("a", 1, steps=steps, wall_ms=1.0).step_ms_p50 == 200.0

    def test_a_missing_stage_reads_as_zero_not_a_keyerror(self):
        bare = step(0)
        bare.stages_ms = {}
        assert bare.stage_ms("decide") == 0.0
        assert result("a", 1, steps=[bare], wall_ms=1.0).decide_ms_p50 == 0.0

    def test_jev_share_is_decisions_over_steps(self):
        run = result("a", 1, steps=[step(i) for i in range(4)], wall_ms=1.0, decisions=2)
        assert run.jev_share == 0.5

    def test_jev_share_of_an_all_macro_replay_is_zero(self):
        """Plan item 4.6's acceptance bar: a repeat spends 0 calls and still steps."""
        steps = [step(i, decided_by="macro") for i in range(3)]
        run = result("a", 1, steps=steps, wall_ms=1.0, decisions=0)
        assert run.jev_share == 0.0
        assert run.steps_count == 3

    def test_no_steps_does_not_divide_by_zero(self):
        run = result("a", 1, steps=[], wall_ms=0.0, decisions=0)
        assert run.jev_share == 0.0
        assert run.decide_ms_p50 == 0.0
        assert run.step_ms_p50 == 0.0

    def test_escalation_steps_and_escalated_are_different_questions(self):
        steps = [step(0), step(1, decided_by="escalation"), step(2)]
        mid = result("a", 1, steps=steps, wall_ms=1.0)
        assert mid.escalation_steps == 1 and mid.escalated is False
        ended = result("a", 2, steps=[step(0)], wall_ms=1.0, stop_reason="escalated")
        assert ended.escalation_steps == 0 and ended.escalated is True


class TestProblems:
    def test_a_clean_run_has_no_problems(self):
        assert result("a", 1, steps=[step(0), step(1)], wall_ms=1.0).problems() == []

    def test_under_reported_escalations_are_named(self):
        steps = [step(0, decided_by="escalation")]
        run = result("a", 1, steps=steps, wall_ms=1.0, escalations=0)
        assert any("escalations=0" in p for p in run.problems())

    def test_unknown_stop_reason_and_decided_by(self):
        run = result("a", 1, steps=[step(0, decided_by="vibes")], wall_ms=1.0,
                     stop_reason="finished")
        text = " ".join(run.problems())
        assert "stop_reason" in text and "decided_by" in text

    def test_step_indexes_must_be_dense(self):
        steps = [step(0), step(7)]
        assert any("step indexes" in p for p in result("a", 1, steps=steps, wall_ms=1.0).problems())

    def test_more_decisions_than_steps_is_flagged(self):
        run = result("a", 1, steps=[step(0)], wall_ms=1.0, decisions=4)
        assert any("jev_share" in p for p in run.problems())


class TestJsonRoundTrip:
    def test_step_and_run_survive_a_round_trip(self):
        run = result("a", 3, steps=[step(0), step(1, decided_by="escalation")], wall_ms=42.0)
        back = RunResult.from_dict(run.to_dict())
        assert back.task_id == "a" and back.run == 3
        assert back.steps_count == 2
        assert back.steps[1].decided_by == "escalation"
        assert back.jev_share == run.jev_share

    def test_derived_values_are_written_so_a_reader_agrees_with_the_writer(self):
        run = result("a", 1, steps=[step(0, decide_ms=123.0)], wall_ms=1.0)
        data = run.to_dict()
        assert data["decide_ms_p50"] == 123.0
        assert data["steps_count"] == 1
        assert data["escalation_steps"] == 0

    def test_a_task_run_round_trips_with_its_oracle_verdict(self):
        row = TaskRun(result=result("a", 1, steps=[step(0)], wall_ms=1.0), success=False,
                      mode="dry-run", provider="typesafe", agent="fake",
                      oracle_detail="nope", notes=["x"])
        back = TaskRun.from_dict(row.to_dict())
        assert back.success is False and back.mode == "dry-run"
        assert back.key() == ("a", 1, "dry-run", "typesafe")
        assert back.notes == ["x"]

    def test_a_dry_run_row_is_marked_synthetic_in_json(self):
        row = TaskRun(result=result("a", 1, steps=[step(0)], wall_ms=1.0), success=True,
                      mode="dry-run")
        assert row.to_dict()["synthetic"] is True
        live = TaskRun(result=result("a", 1, steps=[step(0)], wall_ms=1.0), success=True)
        assert live.to_dict()["synthetic"] is False

    def test_from_dict_tolerates_a_field_it_does_not_know(self):
        data = result("a", 1, steps=[step(0)], wall_ms=1.0).to_dict()
        data["invented_by_a_later_loop"] = 7
        data["steps"][0]["also_invented"] = "x"
        assert RunResult.from_dict(data).steps_count == 1


class TestRunOptions:
    def test_defaults_are_the_documented_caps(self):
        opts = RunOptions()
        assert (opts.max_steps, opts.budget_s, opts.dry_run, opts.provider) == (25, 90.0, False, None)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def three_runs_of(task_id: str, *, walls, step_counts, successes, escalate=(0, 0, 0)):
    rows = []
    for i, (wall, count, ok, esc) in enumerate(zip(walls, step_counts, successes, escalate), 1):
        steps = [step(j, decided_by="escalation" if j < esc else "jev") for j in range(count)]
        rows.append(TaskRun(result=result(task_id, i, steps=steps, wall_ms=wall),
                            success=ok, mode="dry-run"))
    return rows


class TestSummarise:
    def setup_method(self):
        # alpha: walls 100/300/200 -> median 200; steps 1/3/2 -> median 2;
        # escalated steps 0/1/2 = 3 of 6.  beta: every run fails.
        self.rows = (
            three_runs_of("alpha", walls=(100.0, 300.0, 200.0), step_counts=(1, 3, 2),
                          successes=(True, False, True), escalate=(0, 1, 2))
            + three_runs_of("beta", walls=(50.0, 50.0, 50.0), step_counts=(2, 2, 2),
                            successes=(False, False, False))
        )
        self.report = cu.summarise(self.rows)

    def test_task_order_is_first_seen_not_alphabetical(self):
        assert self.report["task_order"] == ["alpha", "beta"]

    def test_medians_are_over_the_runs_of_one_task(self):
        alpha = self.report["tasks"]["alpha"]
        assert alpha["wall_ms"]["median"] == 200.0
        assert alpha["wall_ms"]["values"] == [100.0, 300.0, 200.0]
        assert alpha["steps"]["median"] == 2
        assert alpha["escalations"]["median"] == 1

    def test_every_raw_value_is_kept_because_n_is_three(self):
        """cu_tasks.md: report all 3 raw values per cell, not just a mean."""
        assert self.report["tasks"]["alpha"]["steps"]["values"] == [1, 3, 2]

    def test_counts_are_ints_not_floats_in_an_integer_column(self):
        alpha = self.report["tasks"]["alpha"]
        assert isinstance(alpha["steps"]["median"], int)
        assert isinstance(alpha["decisions"]["sum"], int)

    def test_success_is_counted_per_task_and_in_total(self):
        assert self.report["tasks"]["alpha"]["success"] == {
            "passes": 2, "graded": 3, "unknown": 0, "rate": 2 / 3,
            "values": [True, False, True]}
        assert self.report["totals"]["passes"] == 2
        assert self.report["totals"]["graded"] == 6
        assert self.report["totals"]["success_rate"] == 2 / 6

    def test_a_task_that_failed_every_run_is_named(self):
        assert self.report["totals"]["failed_all_runs"] == ["beta"]

    def test_escalation_rate_is_over_steps(self):
        # alpha 3 escalated of 6 steps, beta 0 of 6 -> 3/12
        assert self.report["totals"]["escalation_rate"] == pytest.approx(0.25)
        assert self.report["tasks"]["alpha"]["escalation_rate"] == pytest.approx(0.5)
        assert self.report["tasks"]["beta"]["escalation_rate"] == 0.0

    def test_both_sums_are_reported_and_named_differently(self):
        totals = self.report["totals"]
        assert totals["wall_ms_sum_of_medians"] == 250.0        # 200 + 50
        assert totals["wall_ms_sum_all_runs"] == 750.0          # 600 + 150

    def test_stop_reasons_are_tallied(self):
        assert self.report["totals"]["stop_reasons"] == {"done": 6}

    def test_an_empty_result_set_does_not_explode(self):
        report = cu.summarise([])
        assert report["tasks"] == {}
        assert report["totals"]["runs"] == 0
        assert report["totals"]["success_rate"] is None


class TestSuccessComesFromTheOracle:
    """The rule the whole benchmark rests on, made structural."""

    def test_a_bare_run_result_is_ungraded_even_when_it_stopped_with_done(self):
        rows = [result("alpha", i, steps=[step(0)], wall_ms=1.0, stop_reason="done")
                for i in (1, 2, 3)]
        report = cu.summarise(rows)
        assert report["tasks"]["alpha"]["success"]["passes"] == 0
        assert report["tasks"]["alpha"]["success"]["graded"] == 0
        assert report["tasks"]["alpha"]["success"]["unknown"] == 3
        # None, not 0.0: nothing was graded, which is not the same as 0% passing.
        assert report["tasks"]["alpha"]["success"]["rate"] is None
        assert report["totals"]["success_rate"] is None

    def test_an_ungraded_task_is_not_listed_as_failing_every_run(self):
        rows = [result("alpha", 1, steps=[step(0)], wall_ms=1.0)]
        assert cu.summarise(rows)["totals"]["failed_all_runs"] == []

    def test_an_errored_run_can_still_be_graded_a_pass(self):
        """The agent crashed on the way out; the file on disk is still correct."""
        crashed = result("alpha", 1, steps=[step(0)], wall_ms=1.0, stop_reason="error")
        report = cu.summarise([TaskRun(result=crashed, success=True)])
        assert report["totals"]["passes"] == 1

    def test_mixing_graded_and_ungraded_rows_keeps_them_apart(self):
        rows = [TaskRun(result=result("a", 1, steps=[step(0)], wall_ms=1.0), success=True),
                result("a", 2, steps=[step(0)], wall_ms=1.0)]
        totals = cu.summarise(rows)["totals"]
        assert (totals["passes"], totals["graded"], totals["unknown"]) == (1, 1, 1)


class TestEscalationFunctions:
    def test_escalation_rate_counts_steps_not_runs(self):
        rows = [result("a", 1, steps=[step(0, decided_by="escalation"), step(1)], wall_ms=1.0),
                result("a", 2, steps=[step(0), step(1)], wall_ms=1.0)]
        assert cu.escalation_rate(rows) == 0.25

    def test_escalation_rate_of_nothing_is_zero(self):
        assert cu.escalation_rate([]) == 0.0
        assert cu.escalation_rate([result("a", 1, steps=[], wall_ms=0.0)]) == 0.0

    def test_escalations_per_task_keeps_zero_tasks_visible(self):
        rows = [result("a", 1, steps=[step(0, decided_by="escalation")], wall_ms=1.0),
                result("b", 1, steps=[step(0)], wall_ms=1.0)]
        assert cu.escalations_per_task(rows) == {"a": 1, "b": 0}

    def test_a_run_ending_escalated_is_counted_separately(self):
        rows = [TaskRun(result=result("a", 1, steps=[step(0)], wall_ms=1.0,
                                      stop_reason="escalated"), success=False)]
        totals = cu.summarise(rows)["totals"]
        assert totals["escalated_runs"] == 1
        assert totals["escalated_run_rate"] == 1.0
        assert totals["escalation_rate"] == 0.0     # no step said escalation

    def test_summarise_reports_the_same_rate_as_the_standalone_function(self):
        rows = [result("a", 1, steps=[step(0, decided_by="escalation"), step(1)], wall_ms=1.0)]
        assert cu.summarise(rows)["totals"]["escalation_rate"] == cu.escalation_rate(rows)


class TestConstants:
    def test_the_six_stages_are_the_act_md_loop(self):
        assert cu.STAGES == ("observe", "reduce", "decide", "validate", "act", "settle")

    def test_decided_by_keeps_macro_and_escalation_distinct(self):
        assert set(cu.DECIDED_BY) == {"jev", "macro", "code", "escalation"}

    def test_stop_reasons_include_blocked_and_escalated_separately(self):
        # act.md: folding `blocked` into `none` loses the escalation counter,
        # "which is the metric that decides the average".
        assert {"blocked", "escalated"} <= set(cu.STOP_REASONS)

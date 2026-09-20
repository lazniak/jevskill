"""The computer-use benchmark harness: task schema, ordering, oracles, report.

Everything here is offline. Nothing opens a window, types a key, or executes a
PowerShell snippet; the one test that shells out runs `[Parser]::ParseInput`,
which builds a syntax tree and returns errors without running anything, and it
skips itself when `pwsh` is not installed.

The tests that matter most are the interlocks: `--live` without
`--i-am-not-streaming` must refuse, `make_powershell_phase` must refuse to exist
before live mode is armed, and `run_task` must run teardown after every way a run
can go wrong. Those are the parts nobody can exercise on a machine that is being
filmed, so they are the parts that need a test instead.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    """Load a `bench/` script by path: they are scripts, not an importable package."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cu_run = _load("cu_run_under_test", "bench/cu_run.py")
cu_report = _load("cu_report_under_test", "bench/cu_report.py")
contract = cu_run.contract

TASKS_FILE = ROOT / "bench" / "cu_tasks.json"
TASKS = cu_run.load_tasks(TASKS_FILE)

try:  # CI runs on Linux; the one registry oracle is unevaluable there.
    import winreg  # noqa: F401
    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False


# --------------------------------------------------------------------------- #
# The task list on disk
# --------------------------------------------------------------------------- #

class TestTaskFileSchema:
    """`bench/cu_tasks.json` is the benchmark's definition; pin its shape here."""

    def test_the_real_file_validates(self):
        assert cu_run.validate_tasks(TASKS) == []

    def test_ten_tasks_with_unique_ids(self):
        ids = [task["id"] for task in TASKS]
        assert len(ids) == 10
        assert len(set(ids)) == len(ids)

    def test_every_task_has_every_declared_field_and_no_others(self):
        for task in TASKS:
            assert set(task) == set(cu_run.TASK_SCHEMA), task["id"]

    def test_restores_state_is_only_the_theme_task(self):
        """The one task that repaints the desktop of whoever is sitting there."""
        flagged = [task["id"] for task in TASKS if task["restores_state"]]
        assert flagged == ["settings_dark_mode"]

    def test_nothing_is_irreversible(self):
        # The harness has no confirmation prompt: it runs setup, the agent and
        # teardown unattended. A task marked irreversible would need one, so
        # adding one has to break this test rather than slip through.
        assert [task["id"] for task in TASKS if task["irreversible"]] == []

    def test_every_oracle_type_is_one_the_harness_knows(self):
        def types(spec):
            yield spec["type"]
            for sub in spec.get("all_of", []) + spec.get("any_of", []):
                yield from types(sub)

        for task in TASKS:
            for kind in types(task["oracle"]):
                assert kind in cu_run.ORACLE_TYPES, f"{task['id']}: {kind}"

    def test_every_filesystem_oracle_points_inside_the_scratch_directory(self):
        """An oracle that could name a real user file could also grade one."""
        def paths(spec):
            if "path" in spec and spec["type"] in ("file_exists", "file_contains"):
                yield spec["path"]
            for sub in spec.get("all_of", []) + spec.get("any_of", []):
                yield from paths(sub)

        for task in TASKS:
            for path in paths(task["oracle"]):
                assert path.upper().startswith("%TEMP%\\JEVCU\\"), f"{task['id']}: {path}"

    def test_an_agent_is_never_expected_to_beat_a_human_on_steps(self):
        for task in TASKS:
            assert task["expected_agent_steps_min"] > task["human_steps"], task["id"]

    def test_the_apps_are_the_five_the_method_names(self):
        assert {task["app"] for task in TASKS} == {
            "notepad", "explorer", "settings", "chrome", "calc"}

    def test_every_task_carries_notes(self):
        # cu_tasks.md's "threats to validity" section points at these per task.
        for task in TASKS:
            assert task["notes"].strip(), task["id"]


class TestValidateTasks:
    """The validator has to catch the mistakes, not just accept the good file."""

    def one(self, **overrides):
        task = json.loads(json.dumps(TASKS[0]))
        task.update(overrides)
        return task

    def test_a_duplicate_id_is_caught(self):
        problems = cu_run.validate_tasks([self.one(), self.one()])
        assert any("duplicate id" in p for p in problems)

    def test_a_missing_field_is_caught(self):
        task = self.one()
        del task["oracle"]
        assert any("missing required field 'oracle'" in p
                   for p in cu_run.validate_tasks([task]))

    def test_a_wrong_type_is_caught(self):
        assert any("human_steps is str" in p
                   for p in cu_run.validate_tasks([self.one(human_steps="four")]))

    def test_a_bool_is_not_accepted_where_an_int_is_required(self):
        assert any("human_steps is a bool" in p
                   for p in cu_run.validate_tasks([self.one(human_steps=True)]))

    def test_an_unknown_field_is_caught(self):
        assert any("unknown field" in p
                   for p in cu_run.validate_tasks([self.one(retries=3)]))

    def test_an_empty_setup_is_caught(self):
        assert any("setup is empty" in p for p in cu_run.validate_tasks([self.one(setup=[])]))

    def test_an_unknown_oracle_type_is_caught(self):
        assert any("oracle type" in p
                   for p in cu_run.validate_tasks([self.one(oracle={"type": "vibes"})]))

    def test_a_file_contains_with_neither_predicate_is_caught(self):
        oracle = {"type": "file_contains", "path": "%TEMP%\\jevcu\\x\\a.txt"}
        assert any("needs 'contains' or 'not_contains'" in p
                   for p in cu_run.validate_tasks([self.one(oracle=oracle)]))

    def test_a_nested_oracle_is_validated_too(self):
        oracle = {"type": "all_of", "all_of": [{"type": "file_exists"}]}
        assert any("missing 'path'" in p
                   for p in cu_run.validate_tasks([self.one(oracle=oracle)]))

    def test_an_agent_minimum_at_or_below_the_human_count_is_caught(self):
        assert any("is not above" in p
                   for p in cu_run.validate_tasks([self.one(human_steps=5,
                                                            expected_agent_steps_min=5)]))

    def test_a_non_list_file_is_rejected(self):
        assert cu_run.validate_tasks({"id": "x"})
        assert cu_run.validate_tasks([])


class TestOrderTasks:
    def test_state_changing_tasks_go_last_whatever_the_shuffle(self):
        import random
        for seed in range(5):
            ordered = cu_run.order_tasks(TASKS, random.Random(seed))
            assert ordered[-1]["id"] == "settings_dark_mode"
            assert len(ordered) == len(TASKS)

    def test_no_shuffle_keeps_file_order_apart_from_that(self):
        ordered = [t["id"] for t in cu_run.order_tasks(TASKS, None)]
        expected = ([t["id"] for t in TASKS if not t["restores_state"]]
                    + [t["id"] for t in TASKS if t["restores_state"]])
        assert ordered == expected

    def test_the_seed_makes_the_order_reproducible(self):
        import random
        first = [t["id"] for t in cu_run.order_tasks(TASKS, random.Random(7))]
        second = [t["id"] for t in cu_run.order_tasks(TASKS, random.Random(7))]
        assert first == second


# --------------------------------------------------------------------------- #
# PowerShell: parsed, never executed
# --------------------------------------------------------------------------- #

class TestPowerShellSyntax:
    def test_every_snippet_parses_and_a_broken_one_does_not(self):
        """One `pwsh` process for the real snippets plus a deliberately broken one.

        Both directions in a single invocation because process start dominates:
        a second call would double the cost of this file for no extra coverage.
        """
        if not cu_run.powershell_path():
            pytest.skip("pwsh not on PATH; the snippet syntax check cannot run here")
        broken = dict(TASKS[0], id="SYNTHETIC_broken",
                      setup=["if ($x { 'no closing paren'"], teardown=[])
        report = cu_run.check_powershell_syntax(list(TASKS) + [broken])
        assert report["available"] is True
        if report["reason"]:
            # The probe itself could not run. That is "not checked", not "the
            # snippets are broken" - skip with the reason rather than fail.
            pytest.skip(f"pwsh parse probe unavailable: {report['reason']}")
        failed = {f["ref"] for f in report["failures"]}
        assert failed == {"SYNTHETIC_broken.setup[0]"}, failed
        assert report["checked"] == sum(
            len(t["setup"]) + len(t["teardown"]) for t in list(TASKS) + [broken])

    def test_a_missing_pwsh_is_reported_not_raised(self, monkeypatch):
        """No PowerShell 7 must not fail a dry run; it must say the check was skipped."""
        monkeypatch.setattr(cu_run, "powershell_path", lambda: None)
        report = cu_run.check_powershell_syntax(TASKS)
        assert report["available"] is False
        assert report["failures"] == []
        assert "not found" in report["reason"]

    def test_a_dry_run_without_pwsh_still_succeeds(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cu_run, "powershell_path", lambda: None)
        assert cu_run.main(["--runs", "1", "--task", "calc_multiply",
                            "--out", str(tmp_path / "runs.json")]) == 0
        assert "SKIPPED" in capsys.readouterr().out

    def test_an_empty_task_list_needs_no_process(self):
        report = cu_run.check_powershell_syntax([])
        assert report["checked"] == 0 and report["failures"] == []


class TestLiveInterlock:
    """Nothing but a human typing `--i-am-not-streaming` may execute a snippet."""

    def test_building_a_live_phase_is_refused_until_armed(self):
        assert cu_run._LIVE_ARMED is False
        with pytest.raises(cu_run.PhaseError, match="not armed"):
            cu_run.make_powershell_phase("setup")
        with pytest.raises(cu_run.PhaseError, match="not armed"):
            cu_run.make_powershell_phase("teardown")

    def test_live_without_the_assertion_refuses_and_writes_nothing(self, tmp_path, capsys):
        out = tmp_path / "runs.json"
        assert cu_run.main(["--live", "--out", str(out)]) == 2
        assert not out.exists()
        assert "REFUSED" in capsys.readouterr().err
        assert cu_run._LIVE_ARMED is False

    def test_live_and_dry_run_together_are_refused(self, tmp_path):
        assert cu_run.main(["--live", "--i-am-not-streaming", "--dry-run",
                            "--out", str(tmp_path / "runs.json")]) == 2
        assert cu_run._LIVE_ARMED is False


# --------------------------------------------------------------------------- #
# Oracles
# --------------------------------------------------------------------------- #

class TestOracles:
    def test_file_exists_both_ways(self, tmp_path):
        target = tmp_path / "a.txt"
        spec = {"type": "file_exists", "path": str(target), "kind": "file", "expect": True}
        assert cu_run.evaluate_oracle(spec)[0] is False
        target.write_text("hi", encoding="utf-8")
        assert cu_run.evaluate_oracle(spec)[0] is True
        gone = {"type": "file_exists", "path": str(target), "kind": "file", "expect": False}
        assert cu_run.evaluate_oracle(gone)[0] is False

    def test_file_exists_distinguishes_a_directory(self, tmp_path):
        spec = {"type": "file_exists", "path": str(tmp_path / "d"), "kind": "dir", "expect": True}
        assert cu_run.evaluate_oracle(spec)[0] is False
        (tmp_path / "d").mkdir()
        assert cu_run.evaluate_oracle(spec)[0] is True

    def test_file_contains_and_not_contains_must_both_hold(self, tmp_path):
        doc = tmp_path / "doc.txt"
        doc.write_text("foo and bar", encoding="utf-8")
        spec = {"type": "file_contains", "path": str(doc),
                "contains": "bar", "not_contains": "foo"}
        assert cu_run.evaluate_oracle(spec)[0] is False       # foo is still there
        doc.write_text("bar and bar", encoding="utf-8")
        assert cu_run.evaluate_oracle(spec)[0] is True

    def test_file_contains_on_a_missing_file_fails_with_a_reason(self, tmp_path):
        passed, detail = cu_run.evaluate_oracle(
            {"type": "file_contains", "path": str(tmp_path / "nope.txt"), "contains": "x"})
        assert passed is False and "does not exist" in detail

    def test_all_of_needs_every_clause(self, tmp_path):
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        spec = {"type": "all_of", "all_of": [
            {"type": "file_exists", "path": str(tmp_path / "b.txt"), "expect": True},
            {"type": "file_exists", "path": str(tmp_path / "a.txt"), "expect": False},
        ]}
        assert cu_run.evaluate_oracle(spec)[0] is True
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        assert cu_run.evaluate_oracle(spec)[0] is False

    def test_any_of_needs_one_clause(self, tmp_path):
        spec = {"type": "any_of", "any_of": [
            {"type": "file_exists", "path": str(tmp_path / "a.txt"), "expect": True},
            {"type": "file_exists", "path": str(tmp_path / "b.txt"), "expect": True},
        ]}
        assert cu_run.evaluate_oracle(spec)[0] is False
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        assert cu_run.evaluate_oracle(spec)[0] is True

    @pytest.mark.parametrize("kind", cu_run.ORACLE_NEEDS_DESKTOP)
    def test_a_desktop_oracle_refuses_rather_than_guessing(self, kind):
        with pytest.raises(cu_run.OracleUnsupported, match="live desktop"):
            cu_run.evaluate_oracle({"type": kind})

    def test_an_unknown_type_refuses(self):
        with pytest.raises(cu_run.OracleUnsupported):
            cu_run.evaluate_oracle({"type": "haruspicy"})

    def test_task_oracle_reads_the_task_s_own_spec(self, tmp_path, monkeypatch):
        task = {"oracle": {"type": "file_exists", "path": str(tmp_path / "x"),
                           "expect": False}}
        assert cu_run.task_oracle(task)[0] is True

    def test_the_fake_oracle_says_it_grades_nothing(self):
        passed, detail = cu_run.fake_oracle(TASKS[0])
        assert passed is True and "grades nothing" in detail

    def test_exactly_which_tasks_this_harness_can_grade(self, tmp_path):
        """`bench/cu_tasks.md` names both halves of this split; pin it here.

        Five tasks grade from the filesystem and the registry. The other five need
        a UIA / window-title / clipboard reader, which is `cu/observe.py` (Phase
        4.1), and until it lands they raise rather than being guessed at.
        """
        gradable, needs_desktop = [], []
        for task in TASKS:
            try:
                # Read-only: a filesystem oracle whose %TEMP% path is absent
                # returns False, and the one registry oracle reads HKCU without
                # writing. Anything that raises something other than
                # OracleUnsupported is a harness bug and should surface here.
                cu_run.evaluate_oracle(task["oracle"])
            except cu_run.OracleUnsupported:
                needs_desktop.append(task["id"])
            else:
                gradable.append(task["id"])

        expected_gradable = ["explorer_new_folder", "explorer_rename",
                             "notepad_replace", "notepad_save_as", "settings_dark_mode"]
        expected_ungradable = ["calc_multiply", "calc_scientific_power",
                               "chrome_find_continue", "chrome_open_url",
                               "explorer_view_details"]
        if not _HAS_WINREG:
            # CI is Linux: the registry oracle is unevaluable there too, which is
            # the same honest "not graded" and not a different bug.
            expected_gradable.remove("settings_dark_mode")
            expected_ungradable = sorted(expected_ungradable + ["settings_dark_mode"])
        assert sorted(gradable) == expected_gradable
        assert sorted(needs_desktop) == expected_ungradable

    @pytest.mark.skipif(not _HAS_WINREG, reason="registry oracles need Windows")
    def test_a_missing_registry_value_fails_with_a_reason(self):
        """Read-only: a key that does not exist. Nothing is written."""
        passed, detail = cu_run.evaluate_oracle({
            "type": "registry_value", "hive": "HKCU",
            "path": "Software\\jevskill-no-such-key", "name": "nope", "expect": 0})
        assert passed is False and "does not exist" in detail

    def test_an_unknown_registry_hive_refuses(self):
        spec = {"type": "registry_value", "hive": "HKWAT", "path": "x", "name": "y"}
        with pytest.raises(cu_run.OracleUnsupported):
            cu_run.evaluate_oracle(spec)


# --------------------------------------------------------------------------- #
# run_task, with fakes for all four collaborators
# --------------------------------------------------------------------------- #

class Spy:
    """Records which phases ran, so ordering can be asserted rather than hoped for."""

    def __init__(self, *, setup_raises=None, teardown_raises=None):
        self.calls = []
        self.setup_raises = setup_raises
        self.teardown_raises = teardown_raises

    def setup(self, task):
        self.calls.append("setup")
        if self.setup_raises:
            raise self.setup_raises

    def teardown(self, task):
        self.calls.append("teardown")
        if self.teardown_raises:
            raise self.teardown_raises


def opts(**kw):
    return contract.RunOptions(**{"dry_run": True, **kw})


class TestRunTask:
    def test_the_happy_path_runs_all_four_phases_in_order(self):
        spy = Spy()
        agent = cu_run.make_fake_agent(TASKS[0], 1)
        oracle_calls = []

        def oracle(task):
            oracle_calls.append(task["id"])
            spy.calls.append("oracle")
            return True, "ok"

        row = cu_run.run_task(TASKS[0], agent, spy.setup, oracle, spy.teardown, opts(),
                              run=1, mode="dry-run")
        assert spy.calls == ["setup", "oracle", "teardown"]
        assert oracle_calls == [TASKS[0]["id"]]
        assert row.success is True and row.setup_ok and row.teardown_ok
        assert row.result.stop_reason == "done"
        assert row.result.steps_count == TASKS[0]["expected_agent_steps_min"]

    def test_a_failing_oracle_fails_the_run_without_touching_the_result(self):
        spy = Spy()
        agent = cu_run.make_fake_agent(TASKS[0], 1)
        row = cu_run.run_task(TASKS[0], agent, spy.setup,
                              lambda task: (False, "file never appeared"),
                              spy.teardown, opts(), run=1, mode="dry-run")
        assert row.success is False
        assert row.oracle_detail == "file never appeared"
        # The agent still believes it finished; that belief is not the verdict.
        assert row.result.stop_reason == "done"
        assert "teardown" in spy.calls

    def test_an_agent_that_raises_still_gets_teardown_and_still_gets_graded(self):
        spy = Spy()
        graded = []

        def agent(goal, options):
            raise RuntimeError("UIA provider died")

        def oracle(task):
            graded.append(task["id"])
            return True, "the file was written before the crash"

        row = cu_run.run_task(TASKS[0], agent, spy.setup, oracle, spy.teardown,
                              opts(), run=2, mode="dry-run")
        assert spy.calls == ["setup", "teardown"]
        assert graded == [TASKS[0]["id"]]        # a crash on the way out is not a failure
        assert row.success is True
        assert row.result.stop_reason == "error"
        assert "UIA provider died" in row.result.error
        assert any("agent raised" in note for note in row.notes)

    def test_teardown_runs_even_when_the_oracle_raises(self):
        spy = Spy()

        def oracle(task):
            raise ValueError("registry hive locked")

        row = cu_run.run_task(TASKS[0], cu_run.make_fake_agent(TASKS[0], 1), spy.setup,
                              oracle, spy.teardown, opts(), run=1, mode="dry-run")
        assert spy.calls == ["setup", "teardown"]
        assert row.success is False and "registry hive locked" in row.oracle_detail

    def test_a_failing_teardown_is_recorded_not_swallowed(self):
        spy = Spy(teardown_raises=RuntimeError("could not stop notepad"))
        row = cu_run.run_task(TASKS[0], cu_run.make_fake_agent(TASKS[0], 1), spy.setup,
                              lambda task: (True, "ok"), spy.teardown, opts(),
                              run=1, mode="dry-run")
        assert row.teardown_ok is False
        assert any("teardown failed" in note for note in row.notes)

    def test_a_failing_setup_skips_the_agent_and_still_tears_down(self):
        spy = Spy(setup_raises=cu_run.PhaseError("setup[1] exited 1"))
        ran = []

        def agent(goal, options):
            ran.append(goal)
            raise AssertionError("the agent must not run after setup failed")

        row = cu_run.run_task(TASKS[0], agent, spy.setup, lambda t: (True, "ok"),
                              spy.teardown, opts(), run=1, mode="dry-run")
        assert ran == []
        assert spy.calls == ["setup", "teardown"]
        assert row.setup_ok is False and row.success is False
        assert row.result.stop_reason == "error"
        assert "setup failed" in row.oracle_detail

    def test_an_unsupported_oracle_is_a_failure_with_a_reason(self):
        spy = Spy()

        def oracle(task):
            raise cu_run.OracleUnsupported("needs a live desktop reader")

        row = cu_run.run_task(TASKS[0], cu_run.make_fake_agent(TASKS[0], 1), spy.setup,
                              oracle, spy.teardown, opts(), run=1, mode="dry-run")
        assert row.success is False
        assert "live desktop" in row.oracle_detail

    def test_an_agent_returning_the_wrong_type_is_an_error_row(self):
        spy = Spy()
        row = cu_run.run_task(TASKS[0], lambda goal, options: {"done": True}, spy.setup,
                              lambda t: (True, "ok"), spy.teardown, opts(),
                              run=1, mode="dry-run")
        assert row.result.stop_reason == "error"
        assert any("not RunResult" in note for note in row.notes)

    def test_the_agent_does_not_get_to_name_its_own_cell(self):
        def agent(goal, options):
            return cu_run.error_result("some-other-task", 99, "mislabelled")

        row = cu_run.run_task(TASKS[0], agent, lambda t: None, lambda t: (True, "ok"),
                              lambda t: None, opts(), run=7, mode="dry-run")
        assert row.result.task_id == TASKS[0]["id"] and row.result.run == 7
        assert row.key() == (TASKS[0]["id"], 7, "dry-run", "")

    def test_a_budget_overrun_is_reported_not_enforced(self):
        row = cu_run.run_task(TASKS[0], cu_run.make_fake_agent(TASKS[0], 1),
                              lambda t: None, lambda t: (True, "ok"), lambda t: None,
                              opts(budget_s=0.0), run=1, mode="dry-run")
        assert any("budget overrun" in note for note in row.notes)
        assert row.result.stop_reason == "done"      # the harness did not kill it

    def test_max_steps_caps_the_fake_agent(self):
        row = cu_run.run_task(TASKS[9], cu_run.make_fake_agent(TASKS[9], 1),
                              lambda t: None, lambda t: (True, "ok"), lambda t: None,
                              opts(max_steps=2), run=1, mode="dry-run")
        assert row.result.steps_count == 2


class TestFakeAgent:
    def test_it_is_deterministic(self):
        a = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        b = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        assert a.to_dict() == b.to_dict()

    def test_different_runs_differ(self):
        a = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        b = cu_run.make_fake_agent(TASKS[0], 2)("goal", opts())
        assert a.wall_ms != b.wall_ms

    def test_it_never_escalates_unless_asked(self):
        plain = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        assert plain.escalation_steps == 0 and plain.escalations == 0
        every_two = cu_run.make_fake_agent(TASKS[0], 1, escalate_every=2)("goal", opts())
        assert every_two.escalation_steps == every_two.steps_count // 2
        assert every_two.escalations == every_two.escalation_steps
        assert every_two.problems() == []

    def test_its_rows_carry_no_accounting_problems(self):
        run = cu_run.make_fake_agent(TASKS[3], 2)("goal", opts())
        assert run.problems() == []


# --------------------------------------------------------------------------- #
# End to end, and the results file
# --------------------------------------------------------------------------- #

class TestDryRunEndToEnd:
    def test_the_default_is_a_dry_run_over_the_real_task_list(self, tmp_path):
        out = tmp_path / "runs.json"
        assert cu_run.main(["--skip-syntax", "--runs", "2", "--out", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["mode"] == "dry-run"
        assert data["modes"] == ["dry-run"]
        assert data["dry_run_rows"] == len(data["runs"]) == 2 * len(TASKS)
        assert all(row["synthetic"] is True for row in data["runs"])
        assert all(row["mode"] == "dry-run" for row in data["runs"])
        assert "SYNTHETIC" in data["about"]

    def test_rows_are_keyed_by_task_run_mode_provider(self, tmp_path):
        out = tmp_path / "runs.json"
        cu_run.main(["--skip-syntax", "--runs", "2", "--task", "calc_multiply",
                     "--out", str(out)])
        cu_run.main(["--skip-syntax", "--runs", "2", "--task", "calc_multiply",
                     "--out", str(out)])
        rows = json.loads(out.read_text(encoding="utf-8"))["runs"]
        assert len(rows) == 2                     # rewritten in place, not appended
        cu_run.main(["--skip-syntax", "--runs", "2", "--task", "calc_multiply",
                     "--provider", "typesafe", "--out", str(out)])
        rows = json.loads(out.read_text(encoding="utf-8"))["runs"]
        assert len(rows) == 4                     # a different provider is a new cell

    def test_escalation_accounting_reaches_the_file(self, tmp_path):
        out = tmp_path / "runs.json"
        cu_run.main(["--skip-syntax", "--runs", "1", "--fake-escalate-every", "2",
                     "--out", str(out)])
        rows, _ = cu_report.load_rows(out)
        report = contract.summarise(rows)
        assert report["totals"]["escalated_steps"] > 0
        assert report["totals"]["escalation_rate"] > 0
        assert set(report["escalations_per_task"]) == {t["id"] for t in TASKS}

    def test_an_unknown_task_id_is_refused(self, tmp_path):
        assert cu_run.main(["--skip-syntax", "--task", "no_such_task",
                            "--out", str(tmp_path / "x.json")]) == 2

    def test_a_task_file_that_does_not_validate_stops_the_run(self, tmp_path):
        broken = tmp_path / "tasks.json"
        broken.write_text(json.dumps([{"id": "x"}]), encoding="utf-8")
        assert cu_run.main(["--skip-syntax", "--tasks", str(broken),
                            "--out", str(tmp_path / "x.json")]) == 2

    def test_no_write_leaves_the_file_alone(self, tmp_path):
        out = tmp_path / "runs.json"
        assert cu_run.main(["--skip-syntax", "--runs", "1", "--no-write",
                            "--out", str(out)]) == 0
        assert not out.exists()


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def dry_results(tmp_path_factory):
    out = tmp_path_factory.mktemp("cu") / "runs.json"
    cu_run.main(["--skip-syntax", "--runs", "3", "--out", str(out)])
    return out


class TestReport:
    def test_every_table_title_says_dry_run(self, dry_results):
        rows, meta = cu_report.load_rows(dry_results)
        text = cu_report.render(rows, source=dry_results, meta=meta)
        title = text.splitlines()[0]
        assert title.startswith("## ") and "DRY RUN" in title
        assert "synthetic" in text

    def test_live_rows_are_not_labelled_dry_run(self):
        rows, _ = self._rows_with_mode("live")
        assert cu_report.mode_label(rows) == "live"

    def test_a_mixed_file_says_so_and_counts_the_synthetic_rows(self):
        live, _ = self._rows_with_mode("live")
        dry, _ = self._rows_with_mode("dry-run")
        label = cu_report.mode_label(live + dry)
        assert label.startswith("MIXED") and "1 of 2" in label

    def _rows_with_mode(self, mode):
        run = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        row = contract.TaskRun(result=run, success=True, mode=mode, agent="x")
        return [row], None

    def test_the_numbers_printed_come_from_the_json(self, dry_results):
        rows, meta = cu_report.load_rows(dry_results)
        text = cu_report.render(rows, source=dry_results, meta=meta)
        report = contract.summarise(rows)
        for task_id, entry in report["tasks"].items():
            line = [ln for ln in text.splitlines() if ln.startswith(f"| {task_id} ")]
            assert line, task_id
            assert str(entry["steps"]["median"]) in line[0]
            assert f"{entry['step_ms_p50']['median']:.1f}" in line[0]
            assert f"{entry['cost_usd']['median']:.6f}" in line[0]

    def test_the_escalation_line_is_printed_even_at_zero(self, dry_results):
        rows, meta = cu_report.load_rows(dry_results)
        text = cu_report.render(rows, source=dry_results, meta=meta)
        assert "**Escalations:** 0 of" in text
        assert "per task: none" in text

    def test_success_is_reported_as_passes_over_graded(self, dry_results):
        rows, meta = cu_report.load_rows(dry_results)
        report = contract.summarise(rows)
        text = cu_report.render(rows, source=dry_results, meta=meta)
        assert f"{report['totals']['passes']}/{report['totals']['graded']}" in text

    def test_ungraded_rows_are_marked_with_a_question_mark(self):
        run = cu_run.make_fake_agent(TASKS[0], 1)("goal", opts())
        rows = [contract.TaskRun(result=run, success=None, mode="live")]
        text = cu_report.render(rows, source=Path("x.json"), meta={})
        assert "?" in text and "Ungraded" in text

    def test_a_task_failed_on_every_run_is_called_out(self):
        rows = []
        for i in (1, 2, 3):
            run = cu_run.make_fake_agent(TASKS[0], i)("goal", opts())
            rows.append(contract.TaskRun(result=run, success=False, mode="live"))
        text = cu_report.render(rows, source=Path("x.json"), meta={})
        assert "Failed on every run" in text and TASKS[0]["id"] in text

    def test_compare_puts_both_agents_in_one_table(self, dry_results, tmp_path):
        other = tmp_path / "other.json"
        cu_run.main(["--skip-syntax", "--runs", "1", "--fake-escalate-every", "2",
                     "--out", str(other)])
        left, _ = cu_report.load_rows(dry_results)
        right, _ = cu_report.load_rows(other)
        text = cu_report.render_compare(left, right, left_source=dry_results,
                                        right_source=other, left_label="jev-cu",
                                        right_label="kofanlabs")
        assert "jev-cu vs kofanlabs" in text
        for task in TASKS:
            assert text.count(f"| {task['id']} ") == 2
        assert "same `bench/cu_tasks.json` oracles" in text

    def test_compare_keeps_a_task_only_one_side_ran(self, dry_results, tmp_path):
        other = tmp_path / "one.json"
        cu_run.main(["--skip-syntax", "--runs", "1", "--task", "calc_multiply",
                     "--out", str(other)])
        left, _ = cu_report.load_rows(dry_results)
        right, _ = cu_report.load_rows(other)
        text = cu_report.render_compare(left, right, left_source=dry_results,
                                        right_source=other)
        assert "notepad_save_as" in text        # present on the left, "-" on the right

    def test_main_renders_and_json_mode_round_trips(self, dry_results, capsys):
        assert cu_report.main([str(dry_results)]) == 0
        assert "DRY RUN" in capsys.readouterr().out
        assert cu_report.main([str(dry_results), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["totals"]["runs"] == 3 * len(TASKS)
        assert "DRY RUN" in payload["mode"]

    def test_main_on_a_missing_file_explains_itself(self, tmp_path, capsys):
        assert cu_report.main([str(tmp_path / "nope.json")]) == 2
        assert "cu_run.py --dry-run" in capsys.readouterr().err

    def test_no_rows_is_not_a_crash(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"runs": []}), encoding="utf-8")
        assert cu_report.main([str(empty)]) == 0

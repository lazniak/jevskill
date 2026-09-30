"""Regression tests for the review of the plan tree (2026-09-27).

Each test pins one confirmed finding of the adversarial review of the mission
driver: it failed on the code as first written and passes on the fix. The
finding's number is in the test's docstring, so a future change that brings a
failure back can be read against what the review saw.
"""

from __future__ import annotations

import os
import time

import pytest

from jevskill.cu import journal as journal_module
from jevskill.cu import runner as runner_module
from jevskill.cu.agenda import MAX_CHILDREN, MIN_PHASE_S, MTIME_SLACK_S, PlanNode
from jevskill.cu.experience import phase_key
from jevskill.cu.journal import RunStore
from jevskill.cu.runner import PlanStep, Run, RunNotResumable

from cu_sim import (NOTEPAD_HWND, SAVE_COMMAND, SAVE_PLAN, SimDesktop, scripted_loop)
from test_cu_memory import PathLLM, run_command, sim_operator
from test_cu_mission import (EXPANSIONS, THREE_PHASES, MissionLLM, ScriptLoop, kinds, leaf, make,
                             phase, run, wait_for, walk)


def store_of(tmp_path) -> RunStore:
    return RunStore(tmp_path / ".jevskill" / "cu_runs")


# --------------------------------------------------------------------------- #
# Plans are used whole, and the goal limit holds (#1, #4)
# --------------------------------------------------------------------------- #


class TestPlanSize:
    def test_a_user_plan_longer_than_a_model_reply_runs_every_goal(self, tmp_path):
        """#1: a user plan of 10 goals was cut to MAX_CHILDREN and reported done."""
        goals = ["Goal %d" % i for i in range(MAX_CHILDREN + 2)]
        operator, _desk, loop = make(tmp_path)
        status = run(operator, model="none", plan=[leaf(g) for g in goals])
        assert status["state"] == "done", status["error"]
        assert loop.calls == goals

    def test_a_remembered_flat_plan_longer_than_a_model_reply_is_used_whole(self, tmp_path):
        """#1: a remembered plan of 10 goals lost its last two (the Save, the Close)."""
        goals = ["Step %d" % i for i in range(MAX_CHILDREN + 2)]
        operator, _desk, loop = make(tmp_path)
        operator.experience.learn_plan("do the long thing", [leaf(g) for g in goals])
        status = run(operator, command="do the long thing", model="none", memory=True)
        assert status["state"] == "done", status["error"]
        assert status["memory"]["plan_source"] == "memory"
        assert loop.calls == goals

    def test_a_model_tree_over_the_goal_limit_is_broken_down_as_it_is_reached(self, tmp_path):
        """#4: 4 phases x 8 goals under max_leaves=10 ran all 32."""
        def goals(tag, n):
            return [leaf("%s %d" % (tag, i)) for i in range(n)]

        plan = {"steps": [phase("P1", goals("a", 7)), phase("P2", goals("b", MAX_CHILDREN)),
                          phase("P3", goals("c", MAX_CHILDREN)), phase("P4", goals("d", MAX_CHILDREN))]}
        expand = {g: {"steps": [leaf("%s once" % g)]} for g in ("P2", "P3", "P4")}
        llm = MissionLLM(plan, expand)
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator, max_leaves=10)
        assert status["state"] == "done", status["error"]
        assert len(status["plan"]) <= 10 and len(loop.calls) <= 10
        assert kinds(status, "plan_limit")
        assert llm.calls.count("expand") == 3, "the collapsed phases were broken down lazily"

    def test_a_tree_repair_cannot_add_goals_past_the_limit(self, tmp_path):
        """#4: room=1 let a repair add one phase carrying 8 goals."""
        plan = {"steps": [phase("P1", [leaf("Open Notepad", launch="notepad.exe"), leaf("x")]),
                          phase("P2", [leaf("y"), leaf("z")])]}
        wide = phase("Wide", [leaf("w%d" % i) for i in range(MAX_CHILDREN)])
        llm = MissionLLM(plan, repairs=[{"steps": [wide]}, {"steps": [leaf("narrow")]}])
        loop = ScriptLoop(outcomes={"x": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator, max_leaves=5)
        leaves = [n for n in walk(status["tree"]) if n["kind"] == "leaf" and not n["superseded"]]
        assert len(leaves) <= 5
        assert not any(c.startswith("w") and c[1:].isdigit() for c in loop.calls)


# --------------------------------------------------------------------------- #
# The total budget is active time, across pauses and resumes (#2, #6, #13, #10)
# --------------------------------------------------------------------------- #


class TestActiveBudget:
    def test_a_pause_longer_than_the_budget_does_not_spend_it(self, tmp_path):
        """#2/#6/#13: an 11-minute pause ended a 10-minute run on continue."""
        def gate(goal, _hooks):
            if goal == "Open Notepad":
                operator.run.total_budget_s = 1.5
                operator.pause(True)

        llm = MissionLLM({"steps": [leaf("Open Notepad", launch="notepad.exe"), leaf("Write")]})
        operator, _desk, loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: operator.status()["state"] == "paused")
        time.sleep(1.8)
        operator.pause(False)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", (status["stop_reason"], status["error"])
        assert loop.calls == ["Open Notepad", "Write"]

    def test_a_resume_with_its_budget_spent_is_refused_unless_raised(self, tmp_path):
        """#2/#13: every resume used to grant a fresh total budget."""
        def gate(goal, _hooks):
            if goal == "Fix the spelling":
                operator.stop("test")

        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(gate=gate))
        first = run(operator, total_budget_s=60)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        saved["active_s"] = 61.0
        store.checkpoint(first["run_id"], saved)
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(), desk=desk)
        with pytest.raises(RunNotResumable, match="total budget"):
            operator2.resume(first["run_id"])
        operator2.resume(first["run_id"], total_budget_s=600)
        operator2.wait(20)
        assert operator2.status()["state"] == "done", operator2.status()["error"]

    def test_a_running_phase_resumes_with_what_it_had_left(self, tmp_path):
        """#10: a phase that had used 190 of 200 s got 200 s again on resume."""
        operator, _desk, _ = make(tmp_path)
        for spent, left in ((50.0, 150.0), (190.0, MIN_PHASE_S)):
            root = PlanNode(id="0", goal="c", kind="phase", depth=0, expanded=True)
            ph = PlanNode(id="1", goal="p", kind="phase", depth=1, expanded=True)
            root.add_children([ph])
            ph.add_children([PlanNode(id="1.1", goal="g", kind="leaf", depth=2)])
            root.status = ph.status = "running"
            ph.budget_s, ph.spend["active_s"] = 200.0, spent
            run_ = Run(id="20260927-000000-abcdef", command="c", model=None)
            run_.tree = PlanNode.from_dict(root.to_dict(), step_loader=PlanStep.from_dict)
            operator._reopen(run_)
            phase_ = run_.tree.children[0]
            assert operator._remaining_s(run_, phase_) == pytest.approx(left, abs=1.0)

    def test_the_checkpoint_carries_a_running_phase_time(self, tmp_path):
        """#10: spend['active_s'] was only written when a phase closed."""
        def gate(goal, _hooks):
            if goal == "Fix the spelling":
                time.sleep(0.3)
                operator.stop("test")

        operator, _desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(gate=gate))
        first = run(operator)
        saved = store_of(tmp_path).load(first["run_id"])
        phase2 = next(n for n in walk(saved["tree"]) if n["id"] == "2")
        assert phase2["status"] == "running"
        assert phase2["spend"]["active_s"] >= 0.25


# --------------------------------------------------------------------------- #
# Heartbeat and event numbering across a crash (#7, #16, #9)
# --------------------------------------------------------------------------- #


class TestLiveness:
    def test_a_long_model_call_does_not_make_a_live_run_look_dead(self, tmp_path, monkeypatch):
        """#7/#16: no checkpoint during a 60 s model call; STALE_S is 30 s."""
        monkeypatch.setattr(runner_module, "HEARTBEAT_S", 0.05)
        monkeypatch.setattr(runner_module, "STALE_S", 0.3)
        monkeypatch.setattr(journal_module, "STALE_S", 0.3)

        class SlowExpand(MissionLLM):
            def chat(self, system, user, **kw):
                if "break one phase" in system:
                    time.sleep(1.2)
                return MissionLLM.chat(self, system, user, **kw)

        llm = SlowExpand(THREE_PHASES, EXPANSIONS)
        operator, desk, _ = make(tmp_path, llm)
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: "expand" in llm.calls)
        time.sleep(0.6)   # past STALE_S inside the model call
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), desk=desk)
        with pytest.raises(RunNotResumable):
            operator2.resume(operator.run.id)
        assert not operator2.runs(), "a live run is not offered for resume"
        operator.wait(20)
        assert operator.status()["state"] == "done"

    def test_a_resume_numbers_events_after_what_the_dead_segment_wrote(self, tmp_path):
        """#9: events_next is saved at checkpoints; the jsonl is written at once."""
        def gate(goal, _hooks):
            if goal == "Fix the spelling":
                operator.stop("test")

        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(gate=gate))
        first = run(operator)
        store = store_of(tmp_path)
        last = first["next"] - 1
        for extra in range(3):   # a crashed segment's events, after its last checkpoint
            store.append_event(first["run_id"], {"i": last + 1 + extra, "t": 0.0, "kind": "step",
                                                 "text": "late"})
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(), desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        second = operator2.status()
        assert second["events"][0]["i"] == last + 4
        indices = [e["i"] for e in store.events(first["run_id"])]
        assert len(indices) == len(set(indices)), "no index is written twice"


# --------------------------------------------------------------------------- #
# Verdicts: give_up ends the run, STOP stops it, a resume climbs (#3, #15, #5, #8)
# --------------------------------------------------------------------------- #


def _phase_with_a_failing_check(tmp_path):
    missing = tmp_path / "never.txt"
    return {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                            checks=[{"kind": "file_exists", "arg": str(missing)}]),
                      phase("Other", [leaf("b"), leaf("c")])]}


class TestVerdicts:
    def test_give_up_at_a_phase_acceptance_ends_the_run(self, tmp_path):
        """#3: the parent was asked again, re-planned and kept acting."""
        llm = MissionLLM(_phase_with_a_failing_check(tmp_path),
                         repairs=[{"give_up": "unsafe to continue"}, {"steps": [leaf("b2")]}])
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "failed"
        assert llm.calls.count("repair") == 1
        assert "unsafe" in status["error"]
        assert "b" not in loop.calls and "b2" not in loop.calls

    def test_a_denied_question_at_acceptance_ends_the_run(self, tmp_path):
        """#3: the user's 'no' was overridden by the level above."""
        llm = MissionLLM(_phase_with_a_failing_check(tmp_path),
                         repairs=[{"ask_user": "is the file somewhere else?"}, {"steps": [leaf("b2")]}])
        operator, _desk, loop = make(tmp_path, llm)
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: (operator.status().get("pending_confirm") or {}).get("op") == "continue")
        operator.confirm(False)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "failed"
        assert llm.calls.count("repair") == 1 and "b2" not in loop.calls

    def test_stop_during_a_repair_question_stops(self, tmp_path):
        """#15: STOP answered the question 'no' and the run ended failed."""
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")]),
                          phase("Other", [leaf("b"), leaf("c")])]}
        operator, _desk, _ = make(tmp_path, MissionLLM(plan, repairs=[{"ask_user": "log in first"}]),
                                  ScriptLoop(outcomes={"a": ["max_steps"]}))
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: (operator.status().get("pending_confirm") or {}).get("op") == "continue")
        operator.stop("test")
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "stopped", (status["state"], status["error"])

    def test_stop_during_the_resume_question_stops(self, tmp_path):
        """#15: the redo question behaved the same."""
        loop = ScriptLoop(acts={"Fix the spelling": 2})
        loop.gate = lambda goal, _h: goal == "Fix the spelling" and operator.stop("test")
        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop)
        first = run(operator)
        operator2, _d, _l = make(tmp_path, None, ScriptLoop(), desk=desk)
        operator2.resume(first["run_id"], model="none")
        assert wait_for(lambda: (operator2.status().get("pending_confirm") or {}).get("op") == "redo")
        operator2.stop("test")
        operator2.wait(10)
        assert operator2.status()["state"] == "stopped"

    def test_a_resume_climbs_when_the_phase_has_no_repair_left(self, tmp_path):
        """#5/#8: the scope alone decided, and anything else failed the run."""
        loop = ScriptLoop(acts={"Fix the spelling": 2})
        loop.gate = lambda goal, _h: goal == "Fix the spelling" and operator.stop("test")
        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop)
        first = run(operator)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        next(n for n in walk(saved["tree"]) if n["id"] == "2")["repairs"] = 2
        store.checkpoint(first["run_id"], saved)

        llm2 = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[{"steps": [leaf("Redo the edit")]}])
        loop2 = ScriptLoop()
        operator2, _d, _l = make(tmp_path, llm2, loop2, desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        status = operator2.status()
        assert status["state"] == "done", status["error"]
        assert llm2.payload("repair")["scope"]["id"] == "0", "the root was asked"
        assert "Redo the edit" in loop2.calls

    def test_a_resume_escalate_up_asks_the_level_above(self, tmp_path):
        """#8: {"escalate_up": true} for the phase failed the run at once."""
        loop = ScriptLoop(acts={"Fix the spelling": 2})
        loop.gate = lambda goal, _h: goal == "Fix the spelling" and operator.stop("test")
        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop)
        first = run(operator)
        llm2 = MissionLLM(THREE_PHASES, EXPANSIONS,
                          repairs=[{"escalate_up": True}, {"steps": [leaf("Redo the edit")]}])
        operator2, _d, _l = make(tmp_path, llm2, ScriptLoop(), desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        status = operator2.status()
        assert status["state"] == "done", status["error"]
        assert [p["scope"]["id"] for c, p in zip(llm2.calls, llm2.payloads) if c == "repair"] == \
            ["2", "0"]
        assert llm2.payload("repair")["interrupted"] is True


# --------------------------------------------------------------------------- #
# Windows: never another program's, never the user's other document (#11, #12)
# --------------------------------------------------------------------------- #


class TestWindows:
    def test_a_phase_in_a_program_with_no_window_does_not_borrow_the_pinned_one(
            self, tmp_path, monkeypatch):
        """#11: a Calculator goal ran in Notepad and the run finished done."""
        monkeypatch.setattr(runner_module, "APP_WAIT_S", 0.3)
        plan = {"steps": [leaf("Open Notepad", launch="notepad.exe"),
                          phase("Compute", [leaf("b"), leaf("c")], app="calc2.exe")]}
        operator, desk, loop = make(tmp_path, MissionLLM(plan))
        status = run(operator)
        assert status["state"] == "failed"
        assert "calc2.exe" in status["error"]
        assert loop.calls == ["Open Notepad"]
        assert desk.actions == []

    def test_an_allow_listed_program_with_no_window_is_opened(self, tmp_path):
        """#11: the creative half — a named, allow-listed program is launched."""
        desk = SimDesktop()

        def launcher(target):
            if "paint" in target.lower():
                desk.windows.add(300)
            else:
                desk.launch(target)

        plan = {"steps": [leaf("Open Notepad", launch="notepad.exe"),
                          phase("Draw", [leaf("b"), leaf("c")], app="mspaint.exe")]}
        operator, _d, loop = make(
            tmp_path, MissionLLM(plan), desk=desk, launcher=launcher,
            window_process=lambda h: {300: "mspaint.exe"}.get(
                h, "Notepad.exe" if h == NOTEPAD_HWND else "Discord.exe"),
            window_pid=lambda h: {300: 9}.get(h, desk.window_pid(h)))
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert [e["data"]["hwnd"] for e in kinds(status, "window")] == [NOTEPAD_HWND, 300]
        assert loop.calls == ["Open Notepad", "b", "c"]

    def test_several_windows_of_the_program_prefer_the_one_the_run_used(self, tmp_path):
        """#12: the first window of the process was taken, whoever's it was."""
        desk = SimDesktop()
        diary = NOTEPAD_HWND - 1   # sorts first: the old code took windows[0]
        desk.windows.update({NOTEPAD_HWND, diary, 300})
        titles = {diary: "diary.txt - Notatnik", 300: "Kalkulator"}
        plan = {"steps": [phase("Write", [leaf("a"), leaf("b")], app="notepad.exe"),
                          phase("Count", [leaf("c"), leaf("d")], app="calc.exe"),
                          phase("Back", [leaf("e"), leaf("f")], app="notepad.exe")]}
        procs = {NOTEPAD_HWND: "notepad.exe", diary: "notepad.exe", 300: "calc.exe"}
        operator, _d, _l = make(
            tmp_path, MissionLLM(plan), desk=desk,
            window_process=lambda h: procs.get(h, "Discord.exe"),
            window_pid=lambda h: {diary: 11, 300: 9}.get(h, desk.window_pid(h)),
            window_title=lambda h: titles.get(h) or desk.window_title(h))
        desk.front = NOTEPAD_HWND   # the user brings the run's Notepad up first
        status = run(operator)
        assert status["state"] == "done", status["error"]
        pinned = [e["data"]["hwnd"] for e in kinds(status, "window")]
        assert diary not in pinned, "the user's diary is never taken"
        assert pinned[-1] == NOTEPAD_HWND

    def test_a_resume_never_pins_another_document_of_the_program(self, tmp_path, monkeypatch):
        """#12: the run's window was closed; the user's diary was pinned instead."""
        monkeypatch.setattr(runner_module, "APP_WAIT_S", 0.3)

        def gate(goal, _hooks):
            if goal == "Fix the spelling":
                operator.stop("test")

        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(gate=gate))
        first = run(operator)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        saved["target"].update(hwnd=555, pid=424242, title="hello.txt - Notatnik")
        store.checkpoint(first["run_id"], saved)
        desk.actions.clear()
        loop2 = ScriptLoop(acts={"Fix the spelling": 1})
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop2, desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        status = operator2.status()
        assert status["state"] == "failed"
        assert "notepad" in status["error"].lower()
        assert desk.actions == [] and loop2.calls == []


# --------------------------------------------------------------------------- #
# Memory is written only from a real desktop, and only what is true (#14, #17, #20, #23)
# --------------------------------------------------------------------------- #


class TestMemoryWrites:
    def test_a_dry_run_never_demotes_a_remembered_break_down(self, tmp_path):
        """#14/#20: two dry runs demoted a working decomposition."""
        operator, _desk, _ = make(tmp_path)
        exp = operator.experience
        exp.learn_decomposition("notepad.exe", "Fill the form", [leaf("Type the name into the field"),
                                                                leaf("Look at it")])
        key = phase_key("notepad.exe", "Fill the form")
        before = (exp.plans[key].failures, exp.plans[key].streak_failed)
        status = run(operator, model="none", dry_run=True, memory=True,
                     plan=[phase("Fill the form", app="notepad.exe")])
        assert status["state"] == "failed"
        assert (exp.plans[key].failures, exp.plans[key].streak_failed) == before

    def test_a_file_from_before_the_run_does_not_rescue_a_failed_phase(self, tmp_path,
                                                                       monkeypatch):
        """#17: yesterday's hello.txt marked today's failed save completed."""
        old = tmp_path / "hello.txt"
        old.write_text("yesterday", encoding="utf-8")
        past = time.time() - 86400
        os.utime(old, (past, past))
        # A file's age is the newest of its times, creation included, and this
        # one was created a moment ago: the run is made to begin after it.
        monkeypatch.setattr(Run, "since", lambda self: self.started_at + MTIME_SLACK_S + 5.0)
        plan = [phase("Save", [leaf("Open Notepad", launch="notepad.exe"), leaf("Confirm the save")],
                      checks=[{"kind": "file_exists", "arg": str(old)}]),
                phase("Close", [leaf("x"), leaf("y")])]
        loop = ScriptLoop(outcomes={"Confirm the save": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, None, loop)
        status = run(operator, model="none", plan=plan)
        assert status["state"] == "failed"

    def test_a_rescued_phase_is_not_learned_as_a_tree(self, tmp_path):
        """#17: the tree was stored without the goal that failed."""
        target = tmp_path / "out.txt"

        def gate(goal, _hooks):
            if goal == "Confirm the save":
                target.write_text("now", encoding="utf-8")

        plan = {"steps": [phase("Save", [leaf("Open Notepad", launch="notepad.exe"),
                                         leaf("Confirm the save")],
                                checks=[{"kind": "file_exists", "arg": str(target)}]),
                          phase("Close", [leaf("x"), leaf("y")])]}
        llm = MissionLLM(plan, repairs=[{"escalate_up": True}])
        loop = ScriptLoop(outcomes={"Confirm the save": ["max_steps"]}, gate=gate)
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator, memory=True)
        assert status["state"] == "done", status["error"]
        assert operator.experience.tree_for("mission") is None
        assert any("not remembered" in e["text"] for e in kinds(status, "memory"))

    def test_an_unconfirmed_replay_that_then_fails_counts_against_the_recipe(self, tmp_path):
        """#23: a replay that did every step but missed the goal was never demoted."""
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"
        goal = SAVE_PLAN["steps"][1]["goal"]

        desk2 = SimDesktop()
        loop2 = ScriptLoop(outcomes={goal: ["max_steps"]})
        operator2 = sim_operator(tmp_path, desk2, loop2, None)
        real = operator2._code_verify
        operator2._code_verify = lambda run_, step, snap: (None, "") if step.goal == goal \
            else real(run_, step, snap)
        status = run_command(operator2, SAVE_COMMAND, model="none", memory=True)
        assert status["state"] == "failed"
        assert loop2.calls == [goal], "the replay ran, then Jev looked"
        recipe = operator2.experience.recipe_for("notepad.exe", goal, usable_only=False)
        assert recipe is not None and recipe.streak_failed == 1

    def test_a_break_down_with_the_commands_file_name_is_not_learned(self, tmp_path):
        """#19: 'Type hello.txt into the file name field' served the next mission."""
        plan = {"steps": [phase("Save the document on the Desktop",
                                [leaf("Open Notepad", launch="notepad.exe"),
                                 leaf("Type hello.txt into the file name field")],
                                app="notepad.exe"),
                          phase("Close", [leaf("x"), leaf("y")])]}
        operator, _desk, _ = make(tmp_path, MissionLLM(plan))
        status = run(operator, command="Zapisz dokument jako hello.txt", memory=True)
        assert status["state"] == "done", status["error"]
        assert operator.experience.decomposition_for("notepad.exe",
                                                     "Save the document on the Desktop") is None


# --------------------------------------------------------------------------- #
# Second review (of the fixes above)
# --------------------------------------------------------------------------- #


def calc_desk_kwargs(desk, launches):
    """A Calculator that launches as calc.exe and owns its window as
    CalculatorApp.exe — a new window per launch, as a second launch would."""
    handles = [300]

    def launcher(target):
        launches.append(target)
        if "calc" in target.lower():
            handles[0] += 1
            desk.windows.add(handles[0])
        else:
            desk.launch(target)

    return dict(
        launcher=launcher,
        window_process=lambda h: "CalculatorApp.exe" if h > 300 else (
            "Notepad.exe" if h == NOTEPAD_HWND else "Discord.exe"),
        window_pid=lambda h: 9 if h > 300 else desk.window_pid(h),
        window_title=lambda h: "Kalkulator" if h > 300 else desk.window_title(h))


def _stopped_in_phase_two(tmp_path):
    def gate(goal, _hooks):
        if goal == "Fix the spelling":
            operator.stop("test")

    operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(gate=gate))
    first = run(operator, usd_cap=0.5)
    assert first["state"] == "stopped"
    return first, desk


class TestReviewRound2:
    def test_a_program_whose_window_has_another_process_is_launched_once(self, tmp_path):
        """calc.exe owns its window as CalculatorApp.exe: every goal launched a
        new, blank Calculator (three windows for three goals)."""
        desk, launches = SimDesktop(), []
        plan = {"steps": [{"goal": "Open Calculator", "launch": "calc.exe", "app": "calc.exe"},
                          {"goal": "Compute 2+2", "app": "calc.exe"},
                          {"goal": "Copy the result", "app": "calc.exe"}]}
        operator, _d, loop = make(tmp_path, MissionLLM(plan), desk=desk,
                                  **calc_desk_kwargs(desk, launches))
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert launches == ["calc.exe"]
        assert [e["data"]["hwnd"] for e in kinds(status, "window")] == [301]

    def test_a_launch_leaf_pins_its_phase(self, tmp_path):
        """The phase's own launch leaf pinned a window; the next goal of the
        phase did not know it and launched again."""
        desk, launches = SimDesktop(), []
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                                app="notepad.exe"),
                          phase("Count", [leaf("Open Calculator", launch="calc.exe"), leaf("b"),
                                          leaf("c")], app="calc.exe")]}
        operator, _d, _loop = make(tmp_path, MissionLLM(plan), desk=desk,
                                   **calc_desk_kwargs(desk, launches))
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert launches == ["notepad.exe", "calc.exe"]

    def test_a_resume_keeps_the_program_it_launched(self, tmp_path):
        """On resume the alias was lost and the kept Calculator was dropped; a
        second one was launched, even while only looking at the interrupted goal."""
        desk, launches = SimDesktop(), []
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                                app="notepad.exe"),
                          phase("Count", [leaf("Open Calculator", launch="calc.exe"), leaf("b"),
                                          leaf("c")], app="calc.exe")]}
        loop = ScriptLoop(acts={"c": 1})
        loop.gate = lambda goal, _h: goal == "c" and operator.stop("test")
        kw = calc_desk_kwargs(desk, launches)
        operator, _d, _l = make(tmp_path, MissionLLM(plan), loop, desk=desk, **kw)
        first = run(operator)
        assert first["state"] == "stopped"
        del launches[:]
        operator2, _d2, _l2 = make(tmp_path, None, ScriptLoop(), desk=desk, **kw)
        operator2.resume(first["run_id"], model="none")
        assert wait_for(lambda: (operator2.status().get("pending_confirm") or {}).get("op") == "redo")
        operator2.confirm(True)
        operator2.wait(20)
        status = operator2.status()
        assert status["state"] == "done", status["error"]
        assert launches == [], "nothing is launched again on resume"

    def test_the_window_the_user_brings_up_is_taken_when_its_process_differs(
            self, tmp_path, monkeypatch):
        """wt.exe is WindowsTerminal.exe: the wait accepted only a window whose
        process matched the name, so the user could never satisfy it."""
        import threading as _threading
        monkeypatch.setattr(runner_module, "APP_WAIT_S", 5.0)
        desk = SimDesktop()
        desk.windows.add(500)
        plan = {"steps": [leaf("Open Notepad", launch="notepad.exe"),
                          phase("Shell", [leaf("b"), leaf("c")], app="wt.exe")]}
        operator, _d, loop = make(
            tmp_path, MissionLLM(plan), desk=desk,
            window_process=lambda h: {500: "WindowsTerminal.exe", NOTEPAD_HWND: "Notepad.exe"}.get(
                h, "Discord.exe"),
            window_pid=lambda h: {500: 12}.get(h, desk.window_pid(h)),
            window_title=lambda h: "Terminal" if h == 500 else desk.window_title(h))

        def user_clicks_terminal():
            wait_for(lambda: operator.run is not None and operator.run.state == "waiting_window")
            time.sleep(0.2)
            desk.front = 500

        _threading.Thread(target=user_clicks_terminal, daemon=True).start()
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert [e["data"]["hwnd"] for e in kinds(status, "window")][-1] == 500
        assert loop.calls == ["Open Notepad", "b", "c"]

    def test_a_damaged_checkpoint_is_refused_with_a_reason(self, tmp_path):
        """A damaged run.json was a 500, or a 400 that blamed the request body."""
        first, desk = _stopped_in_phase_two(tmp_path)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        saved["tree"] = {"id": "0", "kind": "phase", "children": [{"id": 1, "goal": None,
                                                                  "spend": "oops"}]}
        saved["segment"] = "x"
        store.checkpoint(first["run_id"], saved)
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(),
                                 desk=desk)
        try:
            operator2.resume(first["run_id"])
        except RunNotResumable as exc:
            assert "damaged" in str(exc)
        else:   # a tree the loader can make sense of resumes; it must not crash
            operator2.wait(20)
            assert operator2.status()["state"] in ("done", "failed")

    def test_a_damaged_limit_takes_its_default(self, tmp_path):
        first, desk = _stopped_in_phase_two(tmp_path)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        saved["limits"] = "oops"
        saved["heartbeat_at"] = None
        store.checkpoint(first["run_id"], saved)
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(),
                                 desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        assert operator2.status()["state"] == "done", operator2.status()["error"]

    def test_a_resume_with_its_spend_cap_reached_is_refused_unless_raised(self, tmp_path):
        """A cap-stopped run stopped again before its first goal, every try."""
        first, desk = _stopped_in_phase_two(tmp_path)
        store = store_of(tmp_path)
        saved = store.load(first["run_id"])
        saved["spend"]["usd"] = 0.51
        store.checkpoint(first["run_id"], saved)
        operator2, _d, _l = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), ScriptLoop(),
                                 desk=desk)
        with pytest.raises(RunNotResumable, match="cap"):
            operator2.resume(first["run_id"])
        operator2.resume(first["run_id"], usd_cap=2.0)
        operator2.wait(20)
        assert operator2.status()["state"] == "done", operator2.status()["error"]

    def test_a_user_phase_over_the_goal_limit_is_refused_not_cut(self, tmp_path):
        """max_children=max_leaves cut a 7-goal phase to 5 under max_leaves=5,
        and the count guard then passed."""
        operator, _desk, _ = make(tmp_path)
        with pytest.raises(ValueError, match="limit is 5"):
            operator.start("mission", None, plan=[phase("Big", [leaf("g%d" % i) for i in range(7)])],
                           max_leaves=5, memory=False)

"""The plan as a tree: a mission, its phases, and the leaves Jev drives.

Everything runs the real :class:`jevskill.cu.runner.Operator` offline — the
simulated desktop of ``tests/cu_sim.py``, a scripted stand-in for the Jev loop,
and a fake planning model that answers each prompt family by its marker — so
what is under test is the operator's own driver: when a phase is broken down,
which scope a failure is repaired in, what a pause, a STOP and a resume leave
behind, and that nothing but a leaf ever acts on the desktop.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from jevskill.cu import runner as runner_module
from jevskill.cu.act import Action
from jevskill.cu.agenda import MAX_REPAIRS_PER_NODE, MAX_SAME_GOAL_FAILS
from jevskill.cu.contract import RunResult, StepRecord
from jevskill.cu.journal import RunStore
from jevskill.cu.killswitch import KillSwitch
from jevskill.cu.llm import LLMReply
from jevskill.cu.runner import (MAX_LLM_CALLS, NotRunning, Operator, RunNotResumable,
                                UnknownRun, _GuardedBackend)

from cu_sim import (DIALOG_HWND, NOTEPAD_HWND, SimBackend, SimDesktop, scripted_loop,
                    sim_operator_kwargs)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def purpose_of(system: str) -> str:
    """The prompt family, from the marker each system prompt carries."""
    if "plan desktop tasks" in system:
        return "plan"
    if "break one phase" in system:
        return "expand"
    if "Part of a long desktop task" in system:
        return "repair"
    if "ended without" in system:
        return "replan"
    if "exact text" in system:
        return "compose"
    if "just failed" in system:
        return "escalate"
    return "verify"


class MissionLLM:
    """Answers by purpose; ``expand`` by phase goal, ``repair`` from a queue."""

    def __init__(self, plan: Dict[str, Any], expand: Optional[Dict[str, Any]] = None,
                 repairs: Optional[List[Dict[str, Any]]] = None, compose: str = "hello world",
                 cost: float = 0.0001, escalate: Optional[Dict[str, Any]] = None):
        self.plan = plan
        self.expand = expand or {}
        self.repairs = list(repairs or [])
        self.compose = compose
        self.cost = cost
        self.escalate = escalate or {"op": "none", "why": "nothing safe"}
        self.calls: List[str] = []
        self.payloads: List[Dict[str, Any]] = []
        self.systems: List[str] = []

    def chat(self, system, user, **_kw):
        purpose = purpose_of(system)
        self.calls.append(purpose)
        self.systems.append(system)
        try:
            payload = json.loads(user)
        except ValueError:
            payload = {}
        self.payloads.append(payload)
        if purpose == "plan":
            body = self.plan
        elif purpose == "expand":
            body = self.expand.get(payload.get("phase", {}).get("goal"), {"steps": []})
        elif purpose in ("repair", "replan"):
            body = self.repairs.pop(0) if self.repairs else {"give_up": "no more repairs"}
        elif purpose == "compose":
            body = {"text": self.compose}
        elif purpose == "escalate":
            body = self.escalate
        else:
            body = {"done": True, "why": "looks done"}
        return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=100,
                        tokens_out=20, cost_usd=self.cost, cost_source="provider",
                        latency_ms=1.0)

    def payload(self, purpose: str, n: int = 0) -> Dict[str, Any]:
        found = [p for c, p in zip(self.calls, self.payloads) if c == purpose]
        return found[n]


class ScriptLoop:
    """Stands in for the Jev loop: a scripted stop reason per goal.

    ``outcomes`` maps a goal to the stop reasons of its successive attempts
    (default ``done``); ``acts`` makes a goal type into the editor that many
    times through the operator's execute hook; ``gate`` is called with the goal
    before it returns, so a test can pause or STOP the run mid-goal.
    """

    def __init__(self, outcomes=None, acts=None, gate=None, sleep: float = 0.0):
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.acts = dict(acts or {})
        self.gate = gate
        self.sleep = sleep
        self.calls: List[str] = []

    def __call__(self, goal, opts=None, **hooks):
        self.calls.append(goal)
        try:
            snap = hooks["observe"]()
        except Exception as exc:  # the real loop records observe errors this way
            return RunResult(task_id="x", stop_reason="error",
                             error="%s: %s" % (type(exc).__name__, exc), steps=[])
        for _ in range(self.acts.get(goal, 0)):
            hooks["execute"](Action(op="type", target="e3", text="x"), snap)
        if self.sleep:
            time.sleep(self.sleep)
        if self.gate is not None:
            self.gate(goal, hooks)
        queue = self.outcomes.get(goal)
        reason = queue.pop(0) if queue else "done"
        return RunResult(task_id="x", stop_reason=reason, error="" if reason == "done" else reason,
                         steps=[StepRecord(index=0, t_ms=0.0, decided_by="jev", executed=True)])


def make(tmp_path, llm=None, loop=None, desk=None, **overrides):
    desk = desk or SimDesktop()
    loop = loop if loop is not None else ScriptLoop()
    switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False,
                        poll_s=0.01)
    kwargs = sim_operator_kwargs(desk, loop)
    kwargs.update(overrides)
    operator = Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                        llm_factory=(lambda _m: llm) if llm is not None else None,
                        kill_switch=switch, idle_seconds=lambda: 99.0, **kwargs)
    return operator, desk, loop


def run(operator, command="mission", model="fake/model", timeout=30, **kw):
    kw.setdefault("memory", False)
    operator.start(command, model, **kw)
    operator.wait(timeout)
    assert not operator.busy, "the run did not end"
    return operator.status()


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def leaf(goal, done_when="", launch=None):
    return {"goal": goal, "done_when": done_when, "launch": launch}


def phase(goal, children=None, **kw):
    item = dict(kind="phase", goal=goal, done_when=kw.pop("done_when", ""), **kw)
    if children is not None:
        item["children"] = children
    return item


def kinds(status, kind):
    return [e for e in status["events"] if e["kind"] == kind]


# Three phases, the first broken down inline, the other two when reached.
THREE_PHASES = {"steps": [
    phase("Write the draft", [leaf("Open Notepad", launch="notepad.exe"),
                              leaf("Write the first line")], app="notepad.exe"),
    phase("Edit the draft", app="notepad.exe"),
    phase("Finish the draft", app="notepad.exe"),
], "note": "three phases"}
EXPANSIONS = {
    "Edit the draft": {"steps": [leaf("Fix the spelling"), leaf("Add a title")]},
    "Finish the draft": {"steps": [leaf("Read it once more"), leaf("Close the menu")]},
}


# --------------------------------------------------------------------------- #
# Flat parity: a short command runs exactly as before the tree
# --------------------------------------------------------------------------- #


class TestFlatParity:
    FLAT = {"steps": [leaf("Open Notepad", launch="notepad.exe"), leaf("Write a line"),
                      leaf("Look at it")], "note": "three goals"}

    def test_a_flat_plan_is_a_root_with_leaves_and_the_same_calls(self, tmp_path):
        llm = MissionLLM(self.FLAT)
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert [s["goal"] for s in status["plan"]] == [s["goal"] for s in self.FLAT["steps"]]
        assert all(s["status"] == "done" and s["depth"] == 1 and s["phase"] == ""
                   for s in status["plan"])
        assert [c["kind"] for c in status["tree"]["children"]] == ["leaf"] * 3
        assert llm.calls == ["plan"]
        assert llm.systems[0].startswith("You plan desktop tasks")
        assert loop.calls == [s["goal"] for s in self.FLAT["steps"]]
        assert not kinds(status, "phase") and not kinds(status, "expand")
        assert status["limits"]["max_llm_calls"] == MAX_LLM_CALLS, "a flat run keeps the cap"

    def test_the_flat_prompt_is_the_old_prompt_and_the_tree_prompt_extends_it(self, tmp_path):
        operator, _desk, _loop = make(tmp_path)
        flat, tree = operator._gap_system(False), operator._gap_system(True)
        assert "phase" not in flat
        assert flat.endswith('Reply with JSON only: {"steps": [{"goal": "...", "done_when": '
                             '"...", "launch": null}], "note": "..."}')
        rules = flat.split("Reply with JSON only")[0]
        assert tree.startswith(rules) and '"kind": "leaf|phase"' in tree

    def test_hierarchical_false_sends_the_flat_prompt_and_flattens_phases(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator, hierarchical=False)
        assert status["state"] == "done", status["error"]
        assert llm.systems[0] == operator._gap_system(False)
        assert llm.calls == ["plan"], "a flat run never breaks anything down"
        assert [c["kind"] for c in status["tree"]["children"]] == ["leaf"] * 3
        assert loop.calls == ["Write the draft", "Edit the draft", "Finish the draft"]

    def test_a_stalled_goal_gets_the_old_replan_prompt(self, tmp_path):
        llm = MissionLLM(self.FLAT, repairs=[{"completed": False, "steps": [leaf("Look again")],
                                              "give_up": None}])
        loop = ScriptLoop(outcomes={"Write a line": ["max_steps"]})
        operator, _desk, _loop = make(tmp_path, llm, loop)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert llm.calls == ["plan", "replan"]
        payload = llm.payload("replan")
        assert payload["failed_goal"]["goal"] == "Write a line"
        assert [s["goal"] for s in payload["remaining"]] == ["Look at it"]
        assert [s["goal"] for s in status["plan"]] == ["Open Notepad", "Write a line", "Look again"]
        assert [s["status"] for s in status["plan"]] == ["done", "failed", "done"]
        assert status["tree"]["children"][2]["id"] == "r1.1"
        assert status["tree"]["children"][1]["superseded"] is True

    def test_a_stop_reports_skipped_goals_while_the_tree_keeps_them_pending(self, tmp_path):
        def gate(goal, _hooks):
            if goal == "Write a line":
                operator.stop("test")

        llm = MissionLLM(self.FLAT)
        operator, _desk, _loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        status = run(operator)
        assert status["state"] == "stopped"
        assert [s["status"] for s in status["plan"]] == ["done", "stopped", "skipped"]
        assert [c["status"] for c in status["tree"]["children"]] == ["done", "stopped", "pending"]


# --------------------------------------------------------------------------- #
# Phases: broken down when reached, from the screen the last goal left
# --------------------------------------------------------------------------- #


class TestHierarchy:
    def test_phases_are_broken_down_one_at_a_time_when_reached(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert llm.calls == ["plan", "expand", "expand"]
        assert loop.calls == ["Open Notepad", "Write the first line", "Fix the spelling",
                              "Add a title", "Read it once more", "Close the menu"]
        # Phase 3's break-down came after phase 2's last goal was done.
        order = [(e["kind"], e.get("data", {}).get("node") or e.get("data", {}).get("node_id"))
                 for e in status["events"] if e["kind"] in ("goal_done", "expand")]
        assert order.index(("expand", "3")) > order.index(("goal_done", "2.2"))
        assert order.index(("expand", "2")) > order.index(("goal_done", "1.2"))
        # The expansion saw the screen the previous goal left — no extra walk.
        assert llm.payload("expand")["screen"]["window_title"]
        assert llm.payload("expand")["depth"] == 2
        progress = status["progress"]
        assert progress["phases_done"] == progress["phases_total"] == 3
        assert progress["leaves_done"] == progress["leaves_known"] == 6
        assert progress["unexpanded_phases"] == 0
        assert [s["phase"] for s in status["plan"]][:2] == ["Write the draft"] * 2
        assert [s["node_id"] for s in status["plan"]] == ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2"]
        assert all(p["evidence"] == "children" for p in status["tree"]["children"])
        assert status["limits"]["max_llm_calls"] == MAX_LLM_CALLS + \
            runner_module.LLM_CAP_PER_PHASE * 3

    def test_the_flat_plan_view_grows_as_phases_are_broken_down(self, tmp_path):
        seen: List[int] = []

        def gate(_goal, _hooks):
            seen.append(len(operator.status()["plan"]))

        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, _desk, _loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        assert run(operator)["state"] == "done"
        assert seen == [2, 2, 4, 4, 6, 6]

    def test_a_phase_that_cannot_be_broken_down_is_one_goal(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, {})   # every expansion answers no steps
        operator, _desk, loop = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert loop.calls[2:] == ["Edit the draft", "Finish the draft"]
        assert all(e["data"]["source"] == "none" for e in kinds(status, "expand"))

    def test_a_jev_only_run_of_a_user_tree_runs_every_leaf(self, tmp_path):
        operator, _desk, loop = make(tmp_path)
        status = run(operator, model=None, plan=[
            phase("One", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")]),
            phase("Two", [leaf("b"), leaf("c")])])
        assert status["state"] == "done", status["error"]
        assert loop.calls == ["Open Notepad", "a", "b", "c"]
        assert status["memory"]["plan_source"] == "user"


# --------------------------------------------------------------------------- #
# Repair: in the scope that failed, then one level up
# --------------------------------------------------------------------------- #


class TestRepair:
    def test_a_failed_leaf_is_repaired_inside_its_phase(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"steps": [leaf("Fix the spelling with the menu")]}])
        loop = ScriptLoop(outcomes={"Add a title": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator, memory=True)
        assert status["state"] == "done", status["error"]
        assert llm.calls == ["plan", "expand", "repair", "expand"]
        assert llm.payload("repair")["scope"]["id"] == "2"
        assert llm.payload("repair")["failed"]["id"] == "2.2"
        assert loop.calls.count("Open Notepad") == 1 and loop.calls.count("Write the first line") == 1
        phase2 = status["tree"]["children"][1]
        new = [c for c in phase2["children"] if c["id"].startswith("2.r1.")]
        assert [c["id"] for c in new] == ["2.r1.1"] and new[0]["source"] == "repair"
        assert phase2["repairs"] == 1
        lessons = operator.experience.lessons_for("notepad.exe", "Edit the draft")
        assert any('sub-goal "add a title" failed: max_steps' in line.casefold()
                   for line in lessons), lessons

    def test_a_scope_that_cannot_be_repaired_asks_the_level_above(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"escalate_up": True},
            {"steps": [phase("Edit the draft another way", [leaf("Use the menu"), leaf("Save")]),
                       phase("Finish the draft", app="notepad.exe")]}])
        loop = ScriptLoop(outcomes={"Fix the spelling": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert llm.calls == ["plan", "expand", "repair", "repair", "expand"]
        assert llm.payload("repair", 0)["scope"]["id"] == "2"
        root_repair = llm.payload("repair", 1)
        assert root_repair["scope"]["id"] == "0" and root_repair["failed"]["id"] == "2"
        children = status["tree"]["children"]
        assert children[1]["status"] == "failed" and children[1]["superseded"] is True
        assert [c["id"] for c in children[2:]] == ["r1.1", "r1.2"], "phase 3 was replaced"
        assert "Use the menu" in loop.calls and "Read it once more" in loop.calls

    def test_a_scope_is_repaired_at_most_twice(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"steps": [leaf("Try one")]}, {"steps": [leaf("Try two")]},
            {"steps": [leaf("Start the edit over")]}])
        loop = ScriptLoop(outcomes={g: ["max_steps"] for g in
                                    ("Fix the spelling", "Try one", "Try two")})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator)
        assert llm.calls.count("repair") == MAX_REPAIRS_PER_NODE + 1, \
            "two repairs of phase 2, then one of the root — never a third of phase 2"
        assert [llm.payload("repair", n)["scope"]["id"] for n in range(3)] == ["2", "2", "0"]
        # The root's repair replaced what was left — phase 3 included.
        assert status["state"] == "done", status["error"]
        assert loop.calls[-1] == "Start the edit over"
        assert "Read it once more" not in loop.calls

    def test_the_run_wide_repair_cap_ends_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_module, "MAX_REPAIRS_TOTAL", 1)
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[{"steps": [leaf("Try one")]}])
        loop = ScriptLoop(outcomes={"Fix the spelling": ["max_steps"], "Try one": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator)
        assert status["state"] == "failed"
        assert llm.calls.count("repair") == 1

    def test_a_goal_that_failed_twice_is_never_proposed_again(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"steps": [leaf("Fix the spelling")]}, {"steps": [leaf("Fix the spelling")]},
            {"give_up": "enough"}])
        loop = ScriptLoop(outcomes={"Fix the spelling": ["max_steps"] * 5})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator)
        assert status["state"] == "failed"
        assert loop.calls.count("Fix the spelling") == MAX_SAME_GOAL_FAILS
        assert llm.payload("repair", 1)["failed_twice"] == ["Fix the spelling"]
        assert status["spend"]["llm_calls"] <= status["limits"]["max_llm_calls"]
        assert status["progress"]["leaves_known"] <= status["limits"]["max_leaves"]

    def test_completed_counts_only_with_a_quote_from_the_screen(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"completed": True, "quote": "no such text anywhere"},
            {"completed": True, "quote": "Edytor tekstu"}])
        loop = ScriptLoop(outcomes={"Fix the spelling": ["max_steps"],
                                    "Add a title": ["max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        status = run(operator)
        # The first "completed" had nothing to show: escalated to the root,
        # whose repair (the second answer) quotes the screen for phase 2.
        repairs = [e["data"]["verdict"] for e in kinds(status, "repair") if "verdict" in e.get("data", {})]
        assert repairs[0] == "escalate_up"
        assert "completed" in repairs
        assert status["tree"]["children"][1]["evidence"] == "model"

    def test_ask_user_waits_for_the_person(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[
            {"ask_user": "log in to the site first"}, {"ask_user": "still logged out"}])
        loop = ScriptLoop(outcomes={"Fix the spelling": ["max_steps", "max_steps"]})
        operator, _desk, _ = make(tmp_path, llm, loop)
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: (operator.status().get("pending_confirm") or {}).get("op") == "continue")
        assert operator.status()["pending_confirm"]["name"] == "log in to the site first"
        operator.confirm(True)   # allow: the goal is tried again
        assert wait_for(lambda: (operator.status().get("pending_confirm") or {}).get("name")
                        == "still logged out")
        operator.confirm(False)  # deny: the run ends with the question as the error
        operator.wait(10)
        status = operator.status()
        assert status["state"] == "failed" and status["error"] == "still logged out"
        assert loop.calls.count("Fix the spelling") == 2


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


class TestBudgets:
    def test_a_phase_over_its_seconds_is_repaired_and_the_run_goes_on(self, tmp_path, monkeypatch):
        carve = Operator._carve
        monkeypatch.setattr(Operator, "_carve", lambda self, run, node:
                            0.05 if node.id == "2" else carve(self, run, node))
        llm = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[{"steps": [leaf("Quick fix")]}])
        loop = ScriptLoop()
        loop.sleep = 0.0
        slow = ScriptLoop(sleep=0.1)
        operator, _desk, _ = make(tmp_path, llm, lambda goal, opts=None, **h: (
            slow if goal == "Fix the spelling" else loop)(goal, opts, **h))
        status = run(operator)
        assert status["state"] == "done", status["error"]
        phase2 = status["tree"]["children"][1]
        assert phase2["status"] == "failed" and phase2["stop_reason"] == "phase_budget"
        assert llm.payload("repair")["scope"]["id"] == "0"
        assert llm.payload("repair")["reason"] == "phase budget spent"

    def test_the_total_budget_counts_active_time_only(self):
        run_ = runner_module.Run(id="x", command="c", model=None)
        run_.started_at = 1000.0
        run_.paused_s = 30.0
        run_.active_s_before = 5.0
        assert run_.active_seconds(now=1100.0) == pytest.approx(75.0)
        run_.paused_since = 1090.0
        assert run_.active_seconds(now=1100.0) == pytest.approx(65.0)

    def test_the_total_budget_stops_the_run(self, tmp_path):
        def gate(goal, _hooks):
            operator.run.total_budget_s = 0.01
            time.sleep(0.05)

        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, _desk, _ = make(tmp_path, llm, ScriptLoop(gate=gate))
        status = run(operator)
        assert status["state"] == "stopped" and "total budget" in status["stop_reason"]

    def test_a_spend_cap_crossed_inside_an_escalation_stops_at_the_next_look(self, tmp_path):
        def gate(goal, hooks):
            if goal == "Write the first line":
                try:   # the real loop swallows what an escalation hook raises
                    hooks["escalate"]({"snapshot": None, "candidates": [], "reason": "unsure"})
                except Exception:
                    pass
                hooks["observe"]()

        llm = MissionLLM(THREE_PHASES, EXPANSIONS, cost=1.0)
        operator, _desk, _ = make(tmp_path, llm, ScriptLoop(gate=gate))
        operator.start("mission", "fake/model", memory=False, usd_cap=1.5)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "stopped" and "spend cap" in status["stop_reason"]
        assert "replan" not in llm.calls and "repair" not in llm.calls


# --------------------------------------------------------------------------- #
# Acceptance and pinning
# --------------------------------------------------------------------------- #


class TestAcceptanceAndPinning:
    def test_a_phase_check_that_holds_closes_it_in_code(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("done", encoding="utf-8")
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                                checks=[{"kind": "file_exists", "arg": str(target)}]),
                          phase("Other", [leaf("b"), leaf("c")])]}
        llm = MissionLLM(plan)
        operator, _desk, _ = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert status["tree"]["children"][0]["evidence"] == "code"
        assert llm.calls == ["plan"]

    def test_a_phase_check_that_fails_asks_for_a_repair(self, tmp_path):
        missing = tmp_path / "missing.txt"
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                                checks=[{"kind": "file_exists", "arg": str(missing)}]),
                          phase("Other", [leaf("b"), leaf("c")])]}

        def gate(goal, _hooks):
            if goal == "Write it to disk":
                missing.write_text("now", encoding="utf-8")

        llm = MissionLLM(plan, repairs=[{"steps": [leaf("Write it to disk")]}])
        operator, _desk, loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        status = run(operator)
        assert status["state"] == "done", status["error"]
        assert llm.payload("repair")["reason"].startswith("acceptance failed")
        assert llm.payload("repair")["failed"] is None
        assert status["tree"]["children"][0]["evidence"] == "code"
        assert "Write it to disk" in loop.calls

    def test_a_phase_in_another_program_pins_its_window_without_focus(self, tmp_path):
        brought: List[int] = []
        desk = SimDesktop()
        desk.windows.add(300)
        plan = {"steps": [phase("Write", [leaf("Open Notepad", launch="notepad.exe"), leaf("a")],
                                app="notepad.exe"),
                          phase("Count", [leaf("b"), leaf("c")], app="calc.exe")]}
        operator, _desk, _ = make(
            tmp_path, MissionLLM(plan), desk=desk,
            window_process=lambda h: {300: "CalculatorApp.exe", NOTEPAD_HWND: "Notepad.exe",
                                      DIALOG_HWND: "Notepad.exe"}.get(h, "Discord.exe"),
            window_pid=lambda h: {300: 9}.get(h, desk.window_pid(h)),
            bring_to_front=lambda h: brought.append(h) or True)
        # calc.exe's window belongs to CalculatorApp.exe on Windows 11; the
        # stem match is by executable, so name the process the plan names.
        operator._window_process = lambda h: {300: "calc.exe", NOTEPAD_HWND: "Notepad.exe",
                                              DIALOG_HWND: "Notepad.exe"}.get(h, "Discord.exe")
        status = run(operator)
        assert status["state"] == "done", status["error"]
        windows = [e["data"]["hwnd"] for e in kinds(status, "window")]
        assert windows == [NOTEPAD_HWND, 300]
        assert brought == [], "pinning never takes the foreground"


# --------------------------------------------------------------------------- #
# Pause, checkpoints, resume
# --------------------------------------------------------------------------- #


class TestPauseAndResume:
    def test_pause_holds_at_the_next_goal_and_continue_goes_on(self, tmp_path):
        paused = threading.Event()

        def gate(goal, _hooks):
            if goal == "Open Notepad":
                operator.pause(True)

        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, _desk, loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        with pytest.raises(NotRunning):
            operator.pause(True)
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: operator.status()["state"] == "paused")
        assert loop.calls == ["Open Notepad"], "the next goal must not start while paused"
        store = RunStore(tmp_path / ".jevskill" / "cu_runs")
        # The pause forces a checkpoint right after the state flips.
        assert wait_for(lambda: store.load(operator.run.id)["state"] == "paused")
        paused.set()
        time.sleep(0.25)
        operator.pause(False)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["error"]
        assert kinds(status, "pause") and kinds(status, "resume")
        assert status["progress"]["paused_s"] > 0

    def test_stop_while_paused_stops(self, tmp_path):
        def gate(goal, _hooks):
            if goal == "Open Notepad":
                operator.pause(True)

        operator, _desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS),
                                  ScriptLoop(gate=gate))
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: operator.status()["state"] == "paused")
        operator.stop("test")
        operator.wait(10)
        assert operator.status()["state"] == "stopped"

    def test_a_pause_that_outlasts_its_limit_stops_and_stays_resumable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_module, "PAUSE_MAX_S", 0.2)

        def gate(goal, _hooks):
            if goal == "Open Notepad":
                operator.pause(True)

        operator, _desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS),
                                  ScriptLoop(gate=gate))
        status = run(operator)
        assert status["state"] == "stopped" and "paused for more than" in status["stop_reason"]
        assert [r["run_id"] for r in operator.runs()] == [status["run_id"]]

    def test_the_checkpoint_is_the_tree_and_the_first_action_forces_one(self, tmp_path, monkeypatch):
        writes: List[Dict[str, Any]] = []
        real = RunStore.checkpoint

        def spy(self, run_id, payload):
            writes.append(json.loads(json.dumps(payload, default=str)))
            return real(self, run_id, payload)

        monkeypatch.setattr(RunStore, "checkpoint", spy)
        loop = ScriptLoop(acts={"Write the first line": 2})
        operator, _desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop)
        status = run(operator)
        assert status["state"] == "done", status["error"]
        saved = RunStore(tmp_path / ".jevskill" / "cu_runs").load(status["run_id"])
        assert saved["tree"] == json.loads(json.dumps(operator.run.tree.to_dict(), default=str))
        assert saved["state"] == "done" and saved["events_next"] == status["next"]

        def acted(payload, node_id):
            node = next(n for n in walk(payload["tree"]) if n["id"] == node_id)
            return node["acted"]
        assert any(acted(w, "1.2") == 1 and w["state"] == "running" for w in writes), \
            "the first executed action of a goal must force a checkpoint"
        lines = (tmp_path / ".jevskill" / "cu_runs" / status["run_id"] / "events.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == status["next"]

    def test_a_stopped_mission_resumes_where_it_stopped(self, tmp_path):
        def gate(goal, _hooks):
            if goal == "Fix the spelling" and operator.run.segment == 1:
                operator.stop("test")

        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, desk, loop = make(tmp_path, llm, ScriptLoop(gate=gate))
        first = run(operator)
        assert first["state"] == "stopped"
        tree = first["tree"]["children"]
        assert tree[1]["children"][0]["status"] == "stopped"
        assert tree[1]["children"][1]["status"] == "pending" and tree[2]["status"] == "pending"

        llm2 = MissionLLM(THREE_PHASES, EXPANSIONS)
        loop2 = ScriptLoop()
        operator2, _desk2, _ = make(tmp_path, llm2, loop2, desk=desk)
        assert operator2.resume(first["run_id"]) == first["run_id"]
        operator2.wait(20)
        second = operator2.status()
        assert second["state"] == "done", second["error"]
        assert "Open Notepad" not in loop2.calls and "Write the first line" not in loop2.calls
        assert loop2.calls[0] == "Fix the spelling", "no action was taken, so it simply runs again"
        assert llm2.calls == ["expand"], "no second plan, and phase 2 was already broken down"
        assert second["segment"] == 2
        assert second["events"][0]["i"] == first["next"], "the event numbering continues"

    def test_a_crashed_run_is_listed_and_a_proven_goal_is_not_redone(self, tmp_path):
        llm = MissionLLM(THREE_PHASES, EXPANSIONS)
        operator, desk, loop = make(tmp_path, llm)
        status = run(operator)
        assert status["state"] == "done"
        store = RunStore(tmp_path / ".jevskill" / "cu_runs")
        saved = store.load(status["run_id"])
        # Rewrite it as a run whose process died while goal 2.1 was running.
        node = next(n for n in walk(saved["tree"]) if n["id"] == "2.1")
        node["status"], node["done_when"], node["acted"] = "running", "the title reads 'Notatnik'", 1
        for n in walk(saved["tree"]):
            if n["id"] in ("2.2", "3", "3.1", "3.2"):
                n["status"] = "pending"
        for n in walk(saved["tree"]):
            if n["id"] in ("0", "2"):
                n["status"] = "running"
        saved.update(state="running", heartbeat_at=time.time() - 120, pid=424242)
        store.checkpoint(status["run_id"], saved)
        rows = operator.runs()
        assert rows and rows[0]["interrupted"] is True

        loop2 = ScriptLoop()
        operator2, _desk2, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS), loop2,
                                    desk=desk)
        operator2.resume(status["run_id"])
        operator2.wait(20)
        second = operator2.status()
        assert second["state"] == "done", second["error"]
        assert "Fix the spelling" not in loop2.calls, "the screen proved it done"
        redone = next(n for n in walk(second["tree"]) if n["id"] == "2.1")
        assert redone["evidence"] == "code"

    def _interrupted_after_acting(self, tmp_path, llm):
        loop = ScriptLoop(acts={"Fix the spelling": 2})
        loop.gate = lambda goal, _h: goal == "Fix the spelling" and operator.stop("test")
        operator, desk, _ = make(tmp_path, llm, loop)
        first = run(operator)
        assert first["state"] == "stopped"
        return first, desk

    def test_a_goal_that_acted_is_not_rerun_without_asking(self, tmp_path):
        first, desk = self._interrupted_after_acting(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS))
        loop2 = ScriptLoop()
        operator2, _desk2, _ = make(tmp_path, None, loop2, desk=desk)
        operator2.resume(first["run_id"], model="none")
        assert wait_for(lambda: (operator2.status().get("pending_confirm") or {}).get("op") == "redo")
        operator2.confirm(False)
        operator2.wait(10)
        assert operator2.status()["state"] == "failed"
        assert loop2.calls == []
        assert [r["run_id"] for r in operator2.runs()] == [first["run_id"]], "still resumable"

        loop3 = ScriptLoop()
        operator3, _desk3, _ = make(tmp_path, None, loop3, desk=desk)
        operator3.resume(first["run_id"], model="none")
        assert wait_for(lambda: (operator3.status().get("pending_confirm") or {}).get("op") == "redo")
        operator3.confirm(True)
        operator3.wait(10)
        assert operator3.status()["state"] == "done", operator3.status()["error"]
        assert loop3.calls[0] == "Fix the spelling"

    def test_with_a_model_the_repair_sees_how_far_the_goal_got(self, tmp_path):
        first, desk = self._interrupted_after_acting(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS))
        llm2 = MissionLLM(THREE_PHASES, EXPANSIONS, repairs=[{"steps": [leaf("Check the spelling")]}])
        operator2, _desk2, _ = make(tmp_path, llm2, ScriptLoop(), desk=desk)
        operator2.resume(first["run_id"])
        operator2.wait(20)
        assert operator2.status()["state"] == "done", operator2.status()["error"]
        payload = llm2.payload("repair")
        assert payload["interrupted"] is True and payload["acted"] == 2

    def test_what_cannot_be_resumed_says_why(self, tmp_path):
        operator, _desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS))
        done = run(operator)
        with pytest.raises(RunNotResumable):
            operator.resume(done["run_id"])
        with pytest.raises(UnknownRun):
            operator.resume("20260101-000000-abcdef")
        with pytest.raises(ValueError):
            operator.resume("../etc")
        store = RunStore(tmp_path / ".jevskill" / "cu_runs")
        saved = store.load(done["run_id"])
        saved.update(state="running", heartbeat_at=time.time())
        store.checkpoint(done["run_id"], saved)
        with pytest.raises(RunNotResumable):
            operator.resume(done["run_id"])

    def test_a_dry_run_resumes_dry(self, tmp_path):
        def stop_first(_goal, _hooks):
            pass

        llm = MissionLLM({"steps": [leaf("a"), leaf("b")]})
        operator, _desk, _ = make(tmp_path, llm)
        operator.start("mission", "fake/model", dry_run=True, memory=False)
        assert wait_for(lambda: len(operator.status()["events"]) > 3)
        operator.stop("test")
        operator.wait(10)
        run_id = operator.status()["run_id"]
        operator.resume(run_id)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done" and status["dry_run"] is True


def walk(node):
    yield node
    for child in node.get("children") or []:
        yield from walk(child)


# --------------------------------------------------------------------------- #
# Memory: a mission remembered as a tree, a phase remembered as a break-down
# --------------------------------------------------------------------------- #


MISSION = "Napisz „hello world” w Notatniku i zapisz jako hello.txt na pulpicie"
MISSION_PLAN = {"steps": [
    phase('Write "hello world" in Notepad', [
        leaf("Open Notepad", "Notepad window title contains 'Notatnik'", "notepad.exe"),
        leaf('Type "hello world" into the editor', 'The editor contains "hello world"')],
        app="notepad.exe"),
    phase("Save the document as hello.txt on the Desktop", app="notepad.exe",
          done_when="The Notepad title contains 'hello.txt'"),
]}
SAVE_EXPANSION = {"steps": [
    leaf("Open the Save As dialog", "A dialog titled 'Zapisz jako' is open"),
    leaf("Type the full file path into the file name field and save",
         "The Notepad title contains 'hello.txt'")]}


class PathMissionLLM(MissionLLM):
    def __init__(self):
        super().__init__(MISSION_PLAN, {MISSION_PLAN["steps"][1]["goal"]: SAVE_EXPANSION})

    def chat(self, system, user, **kw):
        if purpose_of(system) == "compose":
            payload = json.loads(user)
            name = [w for w in payload["command"].split() if w.endswith(".txt")][0]
            self.compose = "%USERPROFILE%\\Desktop\\" + name
        return super().chat(system, user, **kw)


class TestTreeMemory:
    def test_a_mission_is_remembered_as_a_tree_and_replays_without_a_call(self, tmp_path):
        llm = PathMissionLLM()
        desk = SimDesktop()
        calls: List[str] = []
        operator, _d, _l = make(tmp_path, llm, scripted_loop(desk, calls), desk=desk)
        first = run(operator, MISSION, memory=True)
        assert first["state"] == "done", first["error"]
        assert desk.saved == {"hello.txt": "hello world"}
        assert llm.calls[:2] == ["plan", "expand"]
        listing = operator.experience.listing()
        assert {p["kind"] for p in listing["plans"]} >= {"tree", "phase"}

        llm.calls.clear()
        desk2 = SimDesktop()
        calls2: List[str] = []
        operator2, _d2, _l2 = make(tmp_path, llm, scripted_loop(desk2, calls2), desk=desk2)
        second = run(operator2, "Napisz „lista” w Notatniku i zapisz jako zakupy.txt na pulpicie",
                     memory=True)
        assert second["state"] == "done", second["error"]
        assert desk2.saved == {"zakupy.txt": "lista"}
        assert llm.calls == [], "a remembered mission called the model"
        assert calls2 == [], "a remembered goal went to the loop"
        assert second["memory"]["plan_source"] == "memory"
        assert second["tree"]["children"][1]["expanded"] is True

    def test_a_remembered_phase_serves_another_mission(self, tmp_path):
        llm = PathMissionLLM()
        desk = SimDesktop()
        operator, _d, _l = make(tmp_path, llm, scripted_loop(desk, []), desk=desk)
        assert run(operator, MISSION, memory=True)["state"] == "done"

        other = {"steps": [
            phase("Open Notepad and write a note", [
                leaf("Open Notepad", "Notepad window title contains 'Notatnik'", "notepad.exe"),
                leaf('Type "note" into the editor', 'The editor contains "note"')],
                app="notepad.exe"),
            phase("Save the document as notes.txt on the Desktop", app="notepad.exe")]}
        llm2 = MissionLLM(other, compose="%USERPROFILE%\\Desktop\\notes.txt")
        desk2 = SimDesktop()
        operator2, _d2, _l2 = make(tmp_path, llm2, scripted_loop(desk2, []), desk=desk2)
        status = run(operator2, "Zapisz notatkę „note” jako notes.txt", memory=True)
        assert status["state"] == "done", status["error"]
        assert "expand" not in llm2.calls, "the save phase was broken down from memory"
        expand = [e for e in kinds(status, "expand") if e["data"]["node"] == "2"][0]
        assert expand["data"]["source"] == "memory"
        assert desk2.saved.get("notes.txt") == "note"

    def test_a_dry_run_teaches_nothing(self, tmp_path):
        llm = MissionLLM({"steps": [phase("A", [leaf("a"), leaf("b")]),
                                    phase("B", [leaf("c"), leaf("d")])]})
        operator, _desk, _ = make(tmp_path, llm)
        status = run(operator, dry_run=True, memory=True)
        assert status["state"] == "done", status["error"]
        stats = operator.experience.stats()
        assert stats["plans"] == stats["recipes"] == stats["lessons"] == 0


# --------------------------------------------------------------------------- #
# Safety: only leaves act, and only through the execute hook
# --------------------------------------------------------------------------- #


class RecordingBackend(SimBackend):
    """Every call records whether it came from inside ``_execute_hook``."""

    def __init__(self, desk, operator_ref):
        super().__init__(desk)
        self.operator_ref = operator_ref
        self.outside: List[str] = []
        self.inside = 0

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if callable(value) and name in ("set_value", "invoke", "send_keys", "expand", "select",
                                        "focus", "send_text"):
            operator = object.__getattribute__(self, "operator_ref")[0]

            def recorded(*args, **kwargs):
                if operator._acting > 0:
                    object.__setattr__(self, "inside", object.__getattribute__(self, "inside") + 1)
                else:
                    object.__getattribute__(self, "outside").append(name)
                return value(*args, **kwargs)
            return recorded
        return value


class TestSafety:
    def test_the_guarded_backend_refuses_outside_the_hook(self, tmp_path):
        operator, desk, _ = make(tmp_path)
        guarded = _GuardedBackend(SimBackend(desk), operator)
        assert hasattr(guarded, "invoke")
        with pytest.raises(RuntimeError, match="outside Operator._execute_hook"):
            guarded.invoke("e1")
        operator._acting = 1
        guarded.invoke("e1")
        assert desk.actions == ["invoke e1"]

    def test_every_desktop_call_happens_inside_the_hook(self, tmp_path):
        ref: List[Any] = [None]
        desk = SimDesktop()
        backend = RecordingBackend(desk, ref)
        launched: List[bool] = []

        def launcher(target):
            launched.append(ref[0]._acting > 0)
            desk.launch(target)

        llm = PathMissionLLM()
        operator, _d, _l = make(tmp_path, llm, scripted_loop(desk, []), desk=desk,
                                backend_factory=lambda: backend, launcher=launcher)
        ref[0] = operator
        assert run(operator, MISSION, memory=True)["state"] == "done"
        # Second run: every goal replays from memory.
        desk2 = SimDesktop()
        backend2 = RecordingBackend(desk2, ref)
        launched2: List[bool] = []
        operator2, _d2, _l2 = make(tmp_path, llm, scripted_loop(desk2, []), desk=desk2,
                                   backend_factory=lambda: backend2,
                                   launcher=lambda t: launched2.append(ref[0]._acting > 0)
                                   or desk2.launch(t))
        ref[0] = operator2
        assert run(operator2, MISSION, memory=True)["state"] == "done"
        assert backend.outside == [] and backend2.outside == []
        assert backend.inside > 0 and backend2.inside > 0
        assert launched == [True] and launched2 == [True]

    def test_a_destructive_goal_in_a_repaired_phase_still_asks(self, tmp_path):
        asked: List[str] = []

        def loop(goal, opts=None, **hooks):
            if "Delete" in goal:
                asked.append(goal)
                allowed = hooks["confirm"]("delete?", Action(op="click", target="e2"), None)
                return RunResult(task_id="x", stop_reason="done" if allowed else "blocked",
                                 steps=[])
            return ScriptLoop(outcomes={"Fix the spelling": ["max_steps"]})(goal, opts, **hooks)

        llm = MissionLLM(THREE_PHASES, EXPANSIONS,
                         repairs=[{"steps": [leaf("Delete the old draft")]}])
        operator, _desk, _ = make(tmp_path, llm, loop)
        operator.start("mission", "fake/model", memory=False)
        assert wait_for(lambda: operator.status().get("pending_confirm") is not None)
        operator.confirm(True)
        operator.wait(20)
        assert operator.status()["state"] == "done"
        assert asked == ["Delete the old draft"]

    def test_stop_before_a_launch_never_launches(self, tmp_path):
        launched: List[str] = []
        operator, desk, _ = make(tmp_path, MissionLLM(THREE_PHASES, EXPANSIONS),
                                 launcher=lambda t: launched.append(t))
        operator.kill.arm()
        operator.kill.trigger("test")
        with pytest.raises(Exception):
            operator._execute_hook(Action(op="launch", text="notepad.exe"), None)
        assert launched == []


class TestLiveBenchGuard:
    def test_the_live_learning_bench_refuses_without_live(self, capsys):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "bench" / "cu_learn_live.py"
        spec = importlib.util.spec_from_file_location("cu_learn_live", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main(["--model", "fake/model"]) == 2
        assert "refusing" in capsys.readouterr().err


class TestEndOfRunRace:
    def test_a_resume_the_moment_a_run_stops_is_not_refused(self, tmp_path):
        """``busy`` covers the thread's last checkpoint and kill-switch reset:
        before, a resume issued as soon as the state turned terminal read the
        previous checkpoint (active, fresh heartbeat) and was refused, and the
        old thread could reset the new run's armed switch."""
        for attempt in range(3):
            root = tmp_path / str(attempt)
            llm = MissionLLM({"steps": [leaf("a"), leaf("b"), leaf("c")]})
            operator, _desk, _ = make(root, llm)
            run_id = operator.start("walk", "fake/model", dry_run=True, memory=False)
            assert wait_for(lambda: any(e["kind"] == "goal" for e in operator.status()["events"]))
            operator.stop("race")
            while operator.busy:
                pass
            assert operator.resume(run_id) == run_id
            time.sleep(0.1)
            assert operator.kill.describe()["armed"], "the resumed run's switch was reset"
            operator.stop("cleanup")
            operator.wait(10)

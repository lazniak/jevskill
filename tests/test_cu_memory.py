"""The operator's memory (:mod:`jevskill.cu.experience`) and how the operator uses it.

The end-to-end tests drive the real :class:`jevskill.cu.runner.Operator` against
the simulated desktop in ``tests/cu_sim.py``: the first run of a command is
performed by a scripted stand-in for the Jev loop and a fake planning model;
every later run must come out of memory — no planning call, no loop, no model
call — and still end with the file saved. Nothing here touches a desktop or
the network.
"""

from __future__ import annotations

import json
import time

import pytest

from jevskill.cu.experience import (DEMOTE_AFTER, Experience, Trajectory, command_key,
                                    display_template, extract_slots, fill, goal_key,
                                    literal_check, make_template, recipe_check, resolve)
from jevskill.cu.killswitch import KillSwitch
from jevskill.cu.llm import LLMReply
from jevskill.cu.runner import Operator
from jevskill.cu.types import Snapshot, UIElement

from cu_sim import (NOTEPAD_HWND, SAVE_COMMAND, SAVE_PLAN, SimDesktop, scripted_loop,
                    sim_operator_kwargs)


# --------------------------------------------------------------------------- #
# Slots and templates
# --------------------------------------------------------------------------- #


class TestTemplates:
    def test_slots_are_quotes_then_unquoted_file_names(self):
        assert extract_slots(SAVE_COMMAND) == ["hello world", "hello.txt"]
        assert extract_slots('type "a.txt" then save b.docx') == ["a.txt", "b.docx"]
        assert extract_slots("open notepad") == []
        # A path's pieces are not file names the user typed.
        assert extract_slots(r"%USERPROFILE%\Desktop\x.txt") == []

    def test_a_template_round_trips_and_refuses_a_missing_value(self):
        goal_slots, command_slots = ["hello world"], ["hello world", "hello.txt"]
        template = make_template(r"%USERPROFILE%\Desktop\hello.txt", goal_slots, command_slots)
        assert template == "%USERPROFILE%\\Desktop\\⟦c1⟧"
        assert fill(template, [], ["x", "zakupy.txt"]) == "%USERPROFILE%\\Desktop\\zakupy.txt"
        assert fill(template, [], ["only one"]) is None
        assert fill(template, [], None) is None, "command slots withheld: must not fill"
        # Goal slots win over command slots for the same value.
        assert make_template('Type "hello world"', goal_slots, command_slots) == 'Type "⟦g0⟧"'
        assert display_template("save ⟦c1⟧ as ⟦g0⟧") == "save «2» as «1»"

    def test_the_longest_value_is_cut_first(self):
        assert make_template("hello world and hello", [], ["hello", "hello world"]) == \
            "⟦c1⟧ and ⟦c0⟧"

    def test_commands_that_differ_only_in_values_share_a_key(self):
        key_a, values_a = command_key(SAVE_COMMAND)
        key_b, values_b = command_key("Otwórz Notatnik, wpisz „lista” i zapisz jako zakupy.txt "
                                      "na pulpicie")
        key_c, _ = command_key("Otwórz Notatnik, wpisz „lista” i zapisz jako zakupy.txt "
                               "w dokumentach")
        assert key_a == key_b and values_b == ["lista", "zakupy.txt"]
        assert key_a != key_c, "a different word is a different command"
        assert goal_key('Type "a" into the editor')[0] == goal_key('type  "b" INTO the editor')[0]


# --------------------------------------------------------------------------- #
# Resolution and checks
# --------------------------------------------------------------------------- #


def el(id_, role, name="", **kw):
    kw.setdefault("bbox", (0, 0, 50, 20))
    return UIElement(id_, role, name, **kw)


class TestResolveAndChecks:
    def test_resolve_prefers_identity_then_automation_id_then_a_unique_name(self):
        els = [el("e1", "button", "Zapisz", automation_id="1", class_name="Button"),
               el("e2", "tabitem", "hello.txt", automation_id="tab", class_name="Tab"),
               el("e3", "button", "OK"), el("e4", "button", "OK")]
        assert resolve(["button", "Zapisz", "1", "Button"], els).id == "e1"
        # Renamed tab, same automation id.
        assert resolve(["tabitem", "Bez tytułu", "tab", "Tab"], els).id == "e2"
        # Two buttons named OK: a question for Jev, not a lookup.
        assert resolve(["button", "OK", "", ""], els) is None
        assert resolve(["tabitem", "⟦c1⟧", "", "Tab"], els, command_slots=["x", "hello.txt"]).id == "e2"

    def test_literal_check_reads_the_title_only_when_done_when_names_it(self):
        snap = Snapshot("hello.txt - Notatnik", "Notepad.exe", 5, 1, 0.0, 0.0,
                        [el("e1", "document", value="hello world")])
        assert literal_check("The title contains 'hello.txt'", snap) is True
        assert literal_check("The title contains 'other.txt'", snap) is None
        assert literal_check('The editor contains "hello world"', snap) is True
        assert literal_check("nothing quoted here", snap) is None

    def test_literal_check_does_not_believe_a_field_while_a_dialog_is_up(self):
        snap = Snapshot("Zapisz jako", "Notepad.exe", 5, 1, 0.0, 0.0,
                        [el("e0", "dialog", "Zapisz jako"),
                         el("e1", "edit", "Nazwa pliku:", value=r"C:\x\hello.txt")])
        assert literal_check("hello.txt is saved on the Desktop", snap) is None


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


def trajectory_for(steps, start="A - App", end="B - App"):
    """A Trajectory whose attempts all changed the screen."""
    from jevskill.cu.act import Action

    traj = Trajectory()
    before = Snapshot(start, "App.exe", 1, 1, 0.0, 0.0, [el("s", "button", "start")])
    traj.observed(before)
    last = before
    for index, (op, element, text, key) in enumerate(steps):
        snap = Snapshot(start, "App.exe", 1, 1, 0.0, 0.0,
                        [element] if element is not None else [el("x%d" % index, "button", "x")])
        traj.acted(Action(op=op, target=element.id if element else None, text=text, key=key),
                   snap, True, "", "literal" if op == "type" else "")
        last = Snapshot(end, "App.exe", 1, 1, 0.0, 0.0,
                        [el("changed%d" % index, "button", "moved %d" % index)] +
                        ([element] if element is not None else []))
        traj.observed(last)
    traj.close()
    return traj


class TestStore:
    def test_plans_are_filled_with_the_new_commands_values(self, tmp_path):
        store = Experience(tmp_path / "m.json")
        store.learn_plan(SAVE_COMMAND, SAVE_PLAN["steps"], model="fake/model")
        steps, memory = store.plan_for("Otwórz Notatnik, wpisz „lista zakupów” i zapisz jako "
                                       "zakupy.txt na pulpicie")
        assert memory.successes == 1
        assert steps[1]["goal"] == 'Type "lista zakupów" into the editor'
        assert steps[3]["done_when"] == "The Notepad title contains 'zakupy.txt'"
        assert steps[0]["launch"] == "notepad.exe"
        assert store.plan_for("zupełnie inne polecenie") is None

    def test_a_plan_that_fails_twice_is_not_offered_until_it_succeeds_again(self, tmp_path):
        store = Experience(tmp_path / "m.json")
        store.learn_plan(SAVE_COMMAND, SAVE_PLAN["steps"])
        for _ in range(DEMOTE_AFTER):
            store.plan_failed(SAVE_COMMAND)
        assert store.plan_for(SAVE_COMMAND) is None
        store.learn_plan(SAVE_COMMAND, SAVE_PLAN["steps"])
        assert store.plan_for(SAVE_COMMAND) is not None

    def test_a_goal_finished_with_no_action_is_only_kept_after_a_launch(self, tmp_path):
        store = Experience(tmp_path / "m.json")
        empty = trajectory_for([])
        assert store.learn_recipe("app.exe", "Type the text", "cmd", empty) is None
        recipe = store.learn_recipe("app.exe", "Open App", "cmd", empty, launched=True)
        assert recipe is not None and recipe.after_launch and recipe.steps == []

    def test_recipes_keep_only_what_moved_the_screen_and_lessons_keep_the_rest(self, tmp_path):
        from jevskill.cu.act import Action

        store = Experience(tmp_path / "m.json")
        help_button = el("e2", "button", "Pomoc", automation_id="Help", class_name="Button")
        save = el("e5", "button", "Zapisz", automation_id="1", class_name="Button")
        traj = Trajectory()
        screen = Snapshot("X - App", "App.exe", 1, 1, 0.0, 0.0, [help_button, save])
        traj.observed(screen)
        traj.acted(Action(op="click", target="e2"), screen, True)
        traj.observed(screen)                                     # nothing moved
        traj.acted(Action(op="click", target="e5"), screen, True)
        traj.observed(Snapshot("Y - App", "App.exe", 1, 1, 0.0, 0.0, [save]))
        traj.close()
        recipe = store.learn_recipe("App.exe", "Save it", "cmd", traj)
        assert [s.describe() for s in recipe.steps] == ["click button 'Zapisz'"]
        assert recipe.steps[0].outcome == "new_window"
        assert store.learn_lessons("App.exe", "Save it", traj) == 1
        lines = store.lessons_for("app.exe", "Save it")
        assert lines == ["click button 'Pomoc' changed nothing; what worked instead: "
                         "click button 'Zapisz'"]

    def test_save_merges_with_another_process_and_forget_is_not_undone(self, tmp_path):
        path = tmp_path / "m.json"
        first, second = Experience(path), Experience(path)
        first.learn_plan("command one", [{"goal": "Do one"}])
        second.learn_plan("command two", [{"goal": "Do two"}])
        assert Experience(path).plan_for("command one") is not None
        assert Experience(path).plan_for("command two") is not None
        key = command_key("command one")[0]
        second.load()
        assert second.forget("plan", key) is True
        # `first` still holds its copy in memory and saves after the forget:
        # the tombstone in the file wins over a copy not used since.
        first.save()
        assert Experience(path).plan_for("command one") is None
        assert Experience(path).plan_for("command two") is not None
        assert key not in first.plans
        # Relearning after the forget brings it back, from any process.
        time.sleep(0.01)
        first.learn_plan("command one", [{"goal": "Do one again"}])
        assert Experience(path).plan_for("command one")[0][0]["goal"] == "Do one again"

    def test_a_damaged_file_is_ignored(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text("{not json", encoding="utf-8")
        store = Experience(path)
        assert store.stats()["plans"] == 0
        store.learn_plan("a command", [{"goal": "Do it"}])
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


# --------------------------------------------------------------------------- #
# The operator, end to end on the simulated desktop
# --------------------------------------------------------------------------- #


class PathLLM:
    """Plans the save command and writes the file path; counts every call."""

    def __init__(self):
        self.calls = []

    def chat(self, system, user, **_kw):
        purpose = ("plan" if "plan desktop tasks" in system else
                   "compose" if "exact text" in system else
                   "replan" if "ended without" in system else
                   "escalate" if "just failed" in system else "verify")
        self.calls.append(purpose)
        if purpose == "plan":
            body = SAVE_PLAN
        elif purpose == "compose":
            payload = json.loads(user)
            name = [v for v in extract_slots(payload["command"]) if v.endswith(".txt")][0]
            body = {"text": "%USERPROFILE%\\Desktop\\" + name}
        elif purpose == "replan":
            body = {"completed": False, "steps": [], "give_up": "cannot continue"}
        elif purpose == "escalate":
            body = {"op": "none", "why": "nothing safe"}
        else:
            body = {"done": True, "why": "looks done"}
        return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=100,
                        tokens_out=20, cost_usd=0.001, cost_source="provider", latency_ms=1.0)


def sim_operator(tmp_path, desk, loop, llm=None):
    switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False,
                        poll_s=0.01)
    return Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                    llm_factory=(lambda _m: llm) if llm is not None else None,
                    kill_switch=switch, idle_seconds=lambda: 99.0,
                    **sim_operator_kwargs(desk, loop))


def run_command(operator, command, model="fake/model", **kw):
    operator.start(command, model, **kw)
    operator.wait(30)
    status = operator.status()
    assert not operator.busy
    return status


class TestOperatorLearns:
    def test_the_second_run_of_a_command_needs_no_plan_no_jev_and_no_model(self, tmp_path):
        llm = PathLLM()

        # Run 1: planned by the model, performed by the scripted "Jev" loop.
        desk = SimDesktop()
        calls = []
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, calls), llm)
        first = run_command(operator, SAVE_COMMAND)
        assert first["state"] == "done", first["error"]
        assert desk.saved == {"hello.txt": "hello world"}
        assert len(calls) == 4
        assert llm.calls == ["plan", "compose"], "verification should have been done in code"
        memory = first["memory"]
        assert memory["plan_source"] == "model"
        assert memory["recipes_learned"] == 4 and memory["lessons_learned"] == 1
        assert memory["code_verified"] >= 3
        store = operator.experience.listing()
        assert {r["goal"] for r in store["recipes"]} >= {"Open the Save As dialog"}
        assert any("Pomoc" in l["line"] and "ctrl+shift+s" in l["line"] for l in store["lessons"])

        # Run 2: a fresh desktop, a fresh operator on the same memory file.
        llm.calls.clear()
        desk2 = SimDesktop()
        calls2 = []
        operator2 = sim_operator(tmp_path, desk2, scripted_loop(desk2, calls2), llm)
        started = time.perf_counter()
        second = run_command(operator2, SAVE_COMMAND)
        elapsed = time.perf_counter() - started
        assert second["state"] == "done", second["error"]
        assert desk2.saved == {"hello.txt": "hello world"}
        assert calls2 == [], "a remembered goal went to the loop"
        assert llm.calls == [], "a remembered run called the model"
        assert second["memory"]["plan_source"] == "memory"
        assert second["memory"]["replayed_goals"] == 4
        assert second["spend"]["usd"] == 0.0
        # The replay acted only through UI Automation patterns, except the one
        # chord Notepad offers for Save As — and never clicked "Pomoc".
        assert "invoke e2" not in desk2.actions
        assert desk2.actions == ["setvalue e3 'hello world'", "keys ctrl+shift+s",
                                 "setvalue e70 '%USERPROFILE%\\\\Desktop\\\\hello.txt'",
                                 "invoke e71"]
        assert elapsed < 10

    def test_a_remembered_command_with_new_values_types_the_new_values(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"

        llm.calls.clear()
        desk2 = SimDesktop()
        calls = []
        operator2 = sim_operator(tmp_path, desk2, scripted_loop(desk2, calls), llm)
        status = run_command(operator2, "Otwórz Notatnik, wpisz „lista zakupów” i zapisz jako "
                                        "zakupy.txt na pulpicie")
        assert status["state"] == "done", status["error"]
        assert desk2.saved == {"zakupy.txt": "lista zakupów"}
        assert calls == [] and llm.calls == []

    def test_a_recipe_whose_control_is_gone_falls_back_to_jev_and_is_demoted(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"

        class NoSaveDialog(SimDesktop):
            """An update renamed the Save button's automation id and label."""

            def snapshot(self, hwnd):
                snap = SimDesktop.snapshot(self, hwnd)
                for element in snap.elements:
                    if element.name == "Zapisz":
                        element.name, element.automation_id = "Zapisz plik", "save2"
                return snap

        class RenamedBackend:
            pass

        for attempt in range(DEMOTE_AFTER):
            desk2 = NoSaveDialog()
            calls = []
            loop = scripted_loop(desk2, calls)

            def loop_with_renamed_button(goal, opts=None, _loop=loop, _desk=desk2, **hooks):
                if "file name" in goal.lower():
                    from jevskill.cu.act import Action

                    snap = hooks["observe"]()
                    target = next(e for e in snap.elements if e.name == "Zapisz plik")
                    if not _desk.file_field:
                        field = next(e for e in snap.elements if e.role == "edit")
                        hooks["execute"](Action(op="type", target=field.id,
                                                text=hooks["compose_text"](goal, field)), snap)
                        snap = hooks["observe"]()
                    hooks["execute"](Action(op="click", target=target.id), snap)
                    snap = hooks["observe"]()
                    from jevskill.cu.contract import RunResult

                    ok = hooks["verify"](goal, snap)
                    return RunResult(task_id="sim", stop_reason="done" if ok else "escalated",
                                     steps=[])
                return _loop(goal, opts, **hooks)

            op2 = sim_operator(tmp_path, desk2, loop_with_renamed_button, llm)
            status = run_command(op2, SAVE_COMMAND)
            assert status["state"] == "done", status["error"]
            assert desk2.saved == {"hello.txt": "hello world"}
            assert calls == [], "only the broken goal should reach the loop"
            events = [e["text"] for e in status["events"] if e["kind"] == "memory"]
            assert any("replay stopped" in t and "not on the screen" in t for t in events)
            # Jev finished the goal, and the goal was relearned with the new button.
            store = op2.experience
            recipe = store.recipe_for("notepad.exe", SAVE_PLAN["steps"][3]["goal"])
            assert recipe is not None
            assert any(s.identity and s.identity[1] == "Zapisz plik" for s in recipe.steps)
            break   # relearned on the first failure: the demotion never has to bite

    def test_a_dry_run_reads_memory_and_teaches_nothing(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        status = run_command(operator, SAVE_COMMAND, dry_run=True)
        assert status["state"] == "done"
        assert operator.experience.stats()["recipes"] == 0
        assert operator.experience.stats()["plans"] == 0

    def test_memory_off_neither_reads_nor_writes(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"
        before = operator.experience.stats()

        llm.calls.clear()
        desk2 = SimDesktop()
        calls = []
        operator2 = sim_operator(tmp_path, desk2, scripted_loop(desk2, calls), llm)
        status = run_command(operator2, SAVE_COMMAND, memory=False)
        assert status["state"] == "done"
        assert len(calls) == 4 and "plan" in llm.calls
        assert status["memory"]["enabled"] is False
        assert operator2.experience.stats()["plans"] == before["plans"]

    def test_the_foreground_goes_back_to_the_user_after_the_chord(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, [], fumble=False), llm)
        status = run_command(operator, SAVE_COMMAND)
        assert status["state"] == "done"
        assert desk.front == 999, "the user's window was not given back"
        focus = [e["text"] for e in status["events"] if e["kind"] == "focus"]
        assert focus and focus[0].startswith("gave back")

    def test_the_plan_button_answers_from_memory_without_a_model_key(self, tmp_path):
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"

        def no_key(_model):
            raise AssertionError("the model was built for a remembered plan")

        switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False)
        fresh = Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                         llm_factory=no_key, kill_switch=switch)
        plan = fresh.plan(SAVE_COMMAND, "fake/model")
        assert plan["source"] == "memory" and len(plan["steps"]) == 4
        assert plan["spend"]["llm_calls"] == 0

    def test_a_jev_only_run_is_verified_in_code_and_answered_from_memory(self, tmp_path):
        # Learn with a model once; then run with no model at all.
        llm = PathLLM()
        desk = SimDesktop()
        operator = sim_operator(tmp_path, desk, scripted_loop(desk, []), llm)
        assert run_command(operator, SAVE_COMMAND)["state"] == "done"

        desk2 = SimDesktop()
        calls = []
        operator2 = sim_operator(tmp_path, desk2, scripted_loop(desk2, calls), None)
        status = run_command(operator2, SAVE_COMMAND, model=None)
        assert status["state"] == "done", status["error"]
        assert status["memory"]["plan_source"] == "memory"
        assert desk2.saved == {"hello.txt": "hello world"}
        assert calls == []


# --------------------------------------------------------------------------- #
# The console routes and the CLI
# --------------------------------------------------------------------------- #


class TestMemorySurfaces:
    def test_the_console_lists_forgets_and_clears_only_on_confirm(self, tmp_path):
        from test_cu_runner import call, running

        with running(ledger_root=tmp_path) as (port, server):
            store = server.console.operator().experience
            store.learn_plan(SAVE_COMMAND, SAVE_PLAN["steps"])
            store.learn_plan("another command", [{"goal": "Do it"}])
            status, body = call(port, "GET", "/api/cu/memory")
            assert status == 200 and body["stats"]["plans"] == 2
            key = next(p["key"] for p in body["plans"] if p["command"] == "another command")
            status, body = call(port, "POST", "/api/cu/memory/forget",
                                {"kind": "plan", "key": key})
            assert status == 200 and body["forgot"] is True
            assert body["memory"]["stats"]["plans"] == 1
            status, body = call(port, "POST", "/api/cu/memory/forget", {"kind": "x", "key": "y"})
            assert status == 400
            status, body = call(port, "POST", "/api/cu/memory/clear", {})
            assert status == 400, "clear without confirm must refuse"
            status, body = call(port, "POST", "/api/cu/memory/clear", {"confirm": True})
            assert status == 200 and body["memory"]["stats"]["plans"] == 0

    def test_the_cli_lists_and_forgets(self, tmp_path, capsys):
        from jevskill import cli

        store = Experience.for_ledger_root(tmp_path)
        store.learn_plan(SAVE_COMMAND, SAVE_PLAN["steps"])
        assert cli.main(["cu", "memory", "--ledger-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "1 plans" in out and "Type \"«1»\" into the editor" in out
        assert cli.main(["cu", "memory", "--json", "--ledger-dir", str(tmp_path)]) == 0
        key = json.loads(capsys.readouterr().out)["plans"][0]["key"]
        assert cli.main(["cu", "memory", "--forget", "plan", key,
                         "--ledger-dir", str(tmp_path)]) == 0
        assert "forgot" in capsys.readouterr().out
        assert Experience.for_ledger_root(tmp_path).stats()["plans"] == 0

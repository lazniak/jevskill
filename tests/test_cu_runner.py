"""The computer-use operator, its kill switch and the console routes that drive it.

Everything here is offline and touches no desktop: the operator runs in
``dry_run`` (the planner is a fake, the steps are simulated) and the kill
switch is built without its key and mouse checks. What is under test is the
contract the panel relies on — states, events, the confirm handshake, the
four stop paths, the HTTP status codes — not Windows.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time

import pytest

from jevskill import cli
from jevskill.cu import llm as llm_module
from jevskill.cu.killswitch import KillSwitch, Stopped, touch_stop_file
from jevskill.cu.llm import LLMError, LLMReply, extract_json, recommended
from jevskill.cu.runner import Operator, OperatorBusy, OperatorUnavailable, quoted_text
from jevskill.web.server import make_server

MARKED_KEY = "sk-or-v1-TESTKEY123456789"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLLM:
    """Answers each prompt family with well-formed JSON; records the calls."""

    def __init__(self, plan=None):
        self.calls = []
        self.plan = plan or {"steps": [
            {"goal": "Open Notepad", "done_when": "title has Notepad", "launch": "notepad.exe"},
            {"goal": 'Type "hello world" into the document', "done_when": "value is hello world"},
            {"goal": "Delete the draft", "done_when": "gone"},
        ], "note": "three goals"}

    def chat(self, system, user, **_kw):
        self.calls.append(system[:40])
        if "plan desktop tasks" in system:
            body = self.plan
        elif "exact text" in system:
            body = {"text": "hello world"}
        elif "ended without" in system:
            body = {"completed": False, "steps": [], "give_up": "cannot continue"}
        else:
            body = {"done": True, "why": "looks done"}
        return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=100,
                        tokens_out=20, cost_usd=0.0001, cost_source="provider", latency_ms=5.0)


class RunResultStub:
    """The few fields the operator reads from a loop result."""

    def __init__(self, stop_reason, error=""):
        self.stop_reason = stop_reason
        self.error = error
        self.steps = []
        self.wall_ms = 1.0
        self.tokens_in = 0
        self.cost_usd = 0.0


def make_operator(tmp_path, llm=None, **kwargs):
    switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False,
                        poll_s=0.01)
    kwargs.setdefault("idle_seconds", lambda: 99.0)   # the user's hands are still
    return Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                    llm_factory=(lambda _m: llm) if llm is not None else None,
                    kill_switch=switch, **kwargs)


def answer_when_asked(operator, allow, timeout=10.0):
    def worker():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if operator.status().get("pending_confirm"):
                operator.confirm(allow)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class TestHelpers:
    def test_extract_json_finds_fenced_embedded_or_nothing(self):
        assert extract_json('ok:\n```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('noise {"x": [1, 2]} tail') == {"x": [1, 2]}
        assert extract_json("[1, 2]") == [1, 2]
        assert extract_json("no json here") is None
        assert extract_json("") is None

    def test_quoted_text_accepts_ascii_and_polish_quotes_only_when_unambiguous(self):
        assert quoted_text('type "hello world" here') == "hello world"
        assert quoted_text("wpisz „cześć” w polu") == "cześć"
        assert quoted_text('a "x" then "y"') is None
        assert quoted_text("nothing quoted") is None

    def test_recommended_picks_the_newest_plain_model_per_family(self):
        models = [
            {"id": "anthropic/claude-sonnet-4.5", "created": 1},
            {"id": "anthropic/claude-sonnet-5", "created": 2},
            {"id": "anthropic/claude-sonnet-5:thinking", "created": 3},
            {"id": "google/gemini-2.5-flash", "created": 1},
            {"id": "google/gemini-2.5-flash-image", "created": 9},
            {"id": "openai/gpt-5-mini", "created": 1},
        ]
        ids = [m["id"] for m in recommended(models)]
        assert ids[0] == "anthropic/claude-sonnet-5"
        assert "google/gemini-2.5-flash" in ids
        assert "google/gemini-2.5-flash-image" not in ids
        assert not any(":" in i for i in ids)

    def test_model_list_falls_back_when_openrouter_is_unreachable(self):
        def transport(*_a, **_k):
            raise OSError("offline")
        llm_module._models_cache["models"] = None
        models, source = llm_module.cached_models(transport=transport)
        assert source == "fallback"
        assert {m["id"] for m in models} == {m["id"] for m in llm_module.FALLBACK_MODELS}


# --------------------------------------------------------------------------- #
# The kill switch
# --------------------------------------------------------------------------- #


class TestKillSwitch:
    def test_stop_file_trips_once_and_is_consumed(self, tmp_path):
        switch = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
        switch.arm()
        try:
            assert switch.check() is None
            touch_stop_file(tmp_path / "cu.stop")
            deadline = time.time() + 2
            while not switch.event.is_set() and time.time() < deadline:
                time.sleep(0.01)
            assert switch.event.is_set()
            assert "stop file" in switch.reason
            assert not (tmp_path / "cu.stop").exists()
            with pytest.raises(Stopped):
                switch.raise_if_tripped()
        finally:
            switch.disarm()

    def test_arming_removes_a_stale_stop_file(self, tmp_path):
        touch_stop_file(tmp_path / "cu.stop")
        switch = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
        switch.arm()
        try:
            assert not (tmp_path / "cu.stop").exists()
            assert switch.check() is None
        finally:
            switch.disarm()

    def test_describe_names_every_way_to_stop(self, tmp_path):
        info = KillSwitch(tmp_path / "cu.stop").describe()
        assert info["button"] is True
        assert info["cli"] == "jevskill cu stop"
        assert info["stop_file"].endswith("cu.stop")


# --------------------------------------------------------------------------- #
# The operator, dry run
# --------------------------------------------------------------------------- #


class TestOperatorDryRun:
    def test_plans_runs_every_goal_and_asks_before_the_destructive_one(self, tmp_path):
        fake = FakeLLM()
        operator = make_operator(tmp_path, fake)
        operator.start("Otwórz Notatnik, wpisz hello i usuń szkic", "fake/model", dry_run=True)
        answer_when_asked(operator, allow=True)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["events"]
        assert [s["status"] for s in status["plan"]] == ["done", "done", "done"]
        kinds = [e["kind"] for e in status["events"]]
        assert "kill_switch" in kinds
        assert "confirm" in kinds and "confirm_result" in kinds
        assert status["spend"]["llm_calls"] == 1
        assert status["spend"]["jev_calls"] == 0  # simulated steps are not Jev calls
        assert fake.calls[0].startswith("You plan desktop tasks")
        rows = [json.loads(line) for line in
                (tmp_path / ".jevskill" / "ledger.jsonl").read_text("utf-8").splitlines() if line]
        assert [r["which"] for r in rows] == ["cu_llm"]
        assert "sk-or" not in json.dumps(status)

    def test_without_a_model_the_command_is_the_goal_and_quoted_text_types(self, tmp_path):
        operator = make_operator(tmp_path)
        operator.start('Type "abc" into the box', None, dry_run=True)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done"
        assert len(status["plan"]) == 1 and status["plan"][0]["goal"] == 'Type "abc" into the box'
        texts = [e["text"] for e in status["events"] if e["kind"] == "text"]
        assert texts == ["typing the quoted text from the goal"]

    def test_a_denied_confirmation_blocks_the_goal_and_the_run_fails(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("delete everything", "fake/model", dry_run=True)
        answer_when_asked(operator, allow=False)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "failed"
        assert status["plan"][2]["status"] == "failed"
        assert status["plan"][2]["stop_reason"] == "blocked"

    def test_stop_ends_the_run_and_marks_the_rest_skipped(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("three goals", "fake/model", dry_run=True)
        time.sleep(0.15)
        operator.stop("test STOP")
        operator.wait(10)
        status = operator.status()
        assert status["state"] == "stopped"
        assert status["stop_reason"] == "test STOP"
        assert status["plan"][-1]["status"] in ("skipped", "stopped")

    def test_stop_while_waiting_for_a_confirmation_is_stopped_not_failed(self, tmp_path):
        # Jev-only: no model, so nothing after the denied gate would have looked
        # at the switch — the run used to end as `failed`, hiding that the user
        # pressed STOP.
        operator = make_operator(tmp_path)
        operator.start("delete the file", None, dry_run=True)
        deadline = time.time() + 10
        while time.time() < deadline and not operator.status().get("pending_confirm"):
            time.sleep(0.02)
        assert operator.status()["state"] == "waiting_confirm"
        operator.stop("panel STOP")
        operator.wait(10)
        status = operator.status()
        assert status["state"] == "stopped"
        assert status["stop_reason"] == "panel STOP"
        assert status["plan"][0]["stop_reason"] == "blocked"

    def test_the_stop_file_stops_a_run_too(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("three goals", "fake/model", dry_run=True)
        time.sleep(0.1)
        touch_stop_file(tmp_path / ".jevskill" / "cu.stop")
        operator.wait(10)
        assert operator.status()["state"] == "stopped"
        assert "stop file" in operator.status()["stop_reason"]

    def test_one_run_at_a_time(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("first", "fake/model", dry_run=True)
        with pytest.raises(OperatorBusy):
            operator.start("second", "fake/model", dry_run=True)
        operator.stop("cleanup")
        operator.wait(10)

    def test_empty_command_is_refused_before_anything_starts(self, tmp_path):
        with pytest.raises(ValueError):
            make_operator(tmp_path).start("   ", None, dry_run=True)

    def test_a_live_run_is_refused_off_windows_but_a_dry_run_is_not(self, tmp_path):
        operator = make_operator(tmp_path, platform_check=lambda: {"ok": False, "reason": "linux"})
        with pytest.raises(OperatorUnavailable):
            operator.start("do it", None)
        operator.start('Type "x"', None, dry_run=True)
        operator.wait(10)
        assert operator.status()["state"] == "done"

    def test_plan_still_works_after_a_stopped_run_and_after_an_idle_stop(self, tmp_path):
        # The panel's plan button answered 500 "Stopped: STOP button": the
        # switch tripped by the previous run's STOP was still set.
        fake = FakeLLM()
        operator = make_operator(tmp_path, fake)
        operator.start("three goals", "fake/model", dry_run=True)
        time.sleep(0.1)
        operator.stop("panel STOP")
        operator.wait(10)
        assert operator.status()["state"] == "stopped"
        plan = operator.plan("open notepad", "fake/model")
        assert plan["steps"][0]["goal"] == "Open Notepad"
        operator.stop("idle STOP")  # nothing to stop; must not poison anything
        assert not operator.kill.event.is_set()
        assert operator.plan("open notepad", "fake/model")["steps"]

    def test_a_launched_app_that_stays_behind_fails_the_run_before_any_action(self, tmp_path):
        # A background process may be refused the foreground; acting on
        # "whatever is in front" would then act on the wrong window.
        launched = []
        acted = []

        def fake_loop(goal, opts=None, **hooks):
            acted.append(goal)
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            launcher=launched.append, foreground_hwnd=lambda: 4242,
            top_windows=lambda: {4242}, bring_to_front=lambda _h: False,
            foreground_title=lambda: "Some other window", run_loop=fake_loop,
            backend_factory=lambda: object())
        operator.start("open notepad", None,
                       plan=[{"goal": "Open Notepad", "launch": "notepad.exe"}])
        operator.wait(20)
        status = operator.status()
        assert launched == ["notepad.exe"]
        assert acted == [], "the loop ran although the launched window never came to the front"
        assert status["state"] == "failed"
        assert "did not come to the front" in status["error"]

    def test_a_launched_window_that_opens_behind_is_switched_to(self, tmp_path):
        # Measured live 2026-09-21: notepad.exe started by the console process
        # opened *behind* the user's window. The operator must find the new
        # top-level window and switch to it rather than act on the old one.
        desktop = {"front": 4242, "windows": {4242}}
        brought = []

        def launch(target):
            desktop["windows"] = {4242, 7777}

        def bring(hwnd):
            brought.append(hwnd)
            desktop["front"] = hwnd
            return True

        acted = []

        def fake_loop(goal, opts=None, **hooks):
            acted.append(goal)
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            launcher=launch, foreground_hwnd=lambda: desktop["front"],
            top_windows=lambda: set(desktop["windows"]), bring_to_front=bring,
            foreground_title=lambda: "Untitled - Notepad", run_loop=fake_loop,
            backend_factory=lambda: object())
        operator.start("open notepad", None,
                       plan=[{"goal": "Open Notepad", "launch": "notepad.exe"}])
        operator.wait(20)
        status = operator.status()
        assert brought == [7777]
        assert acted == ["Open Notepad"]
        assert status["state"] == "done", status["error"]
        assert any(e["kind"] == "window" and "Notepad" in e["text"] for e in status["events"])

    def test_the_loop_gets_the_operator_cap_and_the_pinned_hooks(self, tmp_path):
        from jevskill.cu.runner import OPERATOR_CAP

        seen = {}

        def fake_loop(goal, opts=None, **hooks):
            seen.update(hooks)
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=fake_loop,
            observe=lambda: object(), backend_factory=lambda: object())
        operator.start("do it", None, plan=[{"goal": "Open the dialog"}])
        operator.wait(20)
        assert operator.status()["state"] == "done"
        assert seen["cap"] == OPERATOR_CAP == 150
        assert callable(seen["observe"]) and callable(seen["execute"]) and callable(seen["confirm"])
        assert seen["escalate"] is None and seen["verify"] is None  # no planning model

    def test_a_single_instance_app_is_found_by_its_process_name(self, tmp_path):
        # Windows 11 Notepad answers a second notepad.exe with a new tab in its
        # existing window: no new top-level window, so the existing one must be
        # matched by process name and switched to.
        desktop = {"front": 4242}
        brought = []

        def bring(hwnd):
            brought.append(hwnd)
            desktop["front"] = hwnd
            return True

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            launcher=lambda _t: None, foreground_hwnd=lambda: desktop["front"],
            top_windows=lambda: {4242, 3131, 9999},
            window_process=lambda h: {3131: "Notepad.exe", 9999: "chrome.exe"}.get(h, ""),
            bring_to_front=bring, foreground_title=lambda: "Untitled - Notepad",
            run_loop=lambda goal, opts=None, **h: RunResultStub("done"),
            backend_factory=lambda: object())
        operator.start("open notepad", None,
                       plan=[{"goal": "Open Notepad", "launch": "notepad.exe"}])
        operator.wait(20)
        assert brought == [3131]
        assert operator.status()["state"] == "done", operator.status()["error"]

    def test_a_key_refuses_to_act_when_the_user_holds_the_foreground(self, tmp_path):
        # Measured live 2026-09-21: the user clicked Chrome mid-run and the loop,
        # observing "the foreground window", was handed Chrome and a model that
        # proposed the Windows key. The pinned window is now read by handle;
        # a key chord needs the foreground and refuses when Windows will not
        # give it, rather than type into the user's window.
        from jevskill.cu.act import Action

        desktop = {"front": 100, "pids": {100: 5, 200: 9}}
        brought, observed = [], []

        def bring(hwnd):
            brought.append(hwnd)
            return False  # Windows refused

        def loop_that_presses(goal, opts=None, **hooks):
            desktop["front"] = 200  # the user switched to another app
            snap = hooks["observe"]()   # read by handle, no screen needed
            try:
                hooks["execute"](Action(op="key", key="win"), snap)
            except RuntimeError as exc:
                return RunResultStub("error", error="RuntimeError: %s" % exc)
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: desktop["front"], window_pid=lambda h: desktop["pids"][h],
            bring_to_front=bring, foreground_title=lambda: "Chrome",
            run_loop=loop_that_presses,
            observe=lambda: (_ for _ in ()).throw(AssertionError("must not observe the foreground")),
            observe_window=lambda hwnd: observed.append(hwnd) or object(),
            active_popup=lambda hwnd: hwnd, backend_factory=lambda: object())
        operator.start("press it", None, plan=[{"goal": "Press the key"}])
        operator.wait(20)
        status = operator.status()
        assert observed == [100]
        assert brought == [100]
        assert status["state"] == "failed"
        assert "lost the foreground" in status["error"]

    def test_three_unexecuted_escalations_end_the_goal(self, tmp_path):
        # Measured live 2026-09-21: a broken key path made every escalation
        # answer "not executed", and the loop bought the same answer ten times.
        from jevskill.cu.contract import StepRecord

        def loop_that_escalates(goal, opts=None, **hooks):
            for index in range(3):
                hooks["on_step"](StepRecord(index=index, t_ms=0.0, decided_by="escalation",
                                            executed=False, op="key"))
            try:
                hooks["observe"]()
            except RuntimeError as exc:
                return RunResultStub("error", error="RuntimeError: %s" % exc)
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=loop_that_escalates,
            observe=lambda: object(), backend_factory=lambda: object())
        operator.start("do it", None, plan=[{"goal": "Open the dialog"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "failed"
        assert "3 consecutive escalation actions" in status["error"]

    def test_an_escalation_that_finds_done_when_satisfied_closes_the_goal(self, tmp_path):
        # Measured live 2026-09-21: Jev said `done` with a target (incoherent),
        # the model answered "nothing left to do", and the goal still ended
        # `escalated` and paid for a re-plan. `done` from the escalation now
        # closes the goal through the operator; the loop is unchanged.
        from types import SimpleNamespace

        class AgreeingLLM(FakeLLM):
            def chat(self, system, user, **kw):
                self.calls.append(system)
                if "just failed on this step" in system:
                    assert "done when `done_when` is already satisfied" in system
                    body = {"op": "done", "target": None, "why": "the editor already holds it"}
                    return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=50,
                                    tokens_out=20, cost_usd=0.0001, latency_ms=5.0)
                return FakeLLM.chat(self, system, user, **kw)

        fake = AgreeingLLM()
        returned = []

        def loop_that_escalates(goal, opts=None, **hooks):
            returned.append(hooks["escalate"]({
                "snapshot": None, "candidates": [], "reason": "incoherent_terminal",
                "decision": SimpleNamespace(op="done", target="e2", confidence=0.45),
                "step": 1, "last_action": {}}))
            return RunResultStub("escalated")

        operator = make_operator(
            tmp_path, fake, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=loop_that_escalates,
            observe=lambda: object(), backend_factory=lambda: object())
        operator.start("type it", "fake/model",
                       plan=[{"goal": 'Type "hello world"', "done_when": "the editor holds it"}])
        operator.wait(20)
        status = operator.status()
        assert returned == [None]
        assert status["state"] == "done"
        assert status["plan"][0]["status"] == "done"
        assert status["plan"][0]["stop_reason"] == "done"
        assert not any("ended without" in c for c in fake.calls), "no re-plan was needed"
        kinds = [e["kind"] for e in status["events"]]
        assert "goal_done" in kinds and "replan" not in kinds
        assert any("done_when already satisfied" in e["text"] for e in status["events"])

    def test_an_unusable_escalation_reply_is_retried_once_and_short(self, tmp_path):
        # Measured live 2026-09-21: Sonnet 5 in the Save As dialog answered an
        # escalation with 900 tokens of prose that never closed its JSON —
        # $0.015 and the goal was given up. One terse retry, capped at 300.

        class RamblingLLM(FakeLLM):
            def __init__(self):
                FakeLLM.__init__(self)
                self.kwargs = []

            def chat(self, system, user, **kw):
                self.calls.append(system)
                if "just failed on this step" in system:
                    self.kwargs.append(kw)
                    if "exactly one JSON object" not in system:
                        return LLMReply(text="Let me think through the dialog carefully. " * 60,
                                        model="fake/model", tokens_in=3000, tokens_out=900,
                                        cost_usd=0.015, latency_ms=13000.0)
                    body = {"op": "key", "target": None, "key": "ctrl+shift+s", "why": "Save As"}
                    return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=3000,
                                    tokens_out=30, cost_usd=0.003, latency_ms=2000.0)
                return FakeLLM.chat(self, system, user, **kw)

        fake = RamblingLLM()
        actions = []

        def loop_that_escalates(goal, opts=None, **hooks):
            actions.append(hooks["escalate"]({
                "snapshot": None, "candidates": [], "reason": "low_confidence",
                "decision": None, "step": 0, "last_action": {}}))
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, fake, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=loop_that_escalates,
            observe=lambda: object(), backend_factory=lambda: object())
        operator.start("save it", "fake/model", plan=[{"goal": "Open the Save As dialog"}])
        operator.wait(20)
        status = operator.status()
        assert len(actions) == 1 and actions[0].op == "key" and actions[0].key == "ctrl+shift+s"
        assert actions[0].source == "escalation"
        assert len(fake.kwargs) == 2 and fake.kwargs[0] == {} and fake.kwargs[1] == {"max_tokens": 300}
        texts = [e["text"] for e in status["events"] if e["kind"] == "escalate"]
        assert any("no usable answer (900 tokens out): Let me think" in t for t in texts)
        assert any("proposes key  ctrl+shift+s" in t for t in texts)
        assert status["spend"]["llm_calls"] == 2
        assert status["state"] == "done"

    def test_observation_reads_the_pinned_window_and_the_dialog_it_opened(self, tmp_path):
        # Measured live 2026-09-21: a run was stopped because the user was
        # reading the console — the loop had been observing "the foreground".
        # The pinned window is read by handle; the user keeps their screen.
        desktop = {"front": 300, "pids": {100: 5, 150: 5, 300: 7}, "popup": 100,
                   "windows": set()}
        brought, observed = [], []

        def bring(hwnd):
            brought.append(hwnd)
            desktop["front"] = hwnd
            return True

        def loop_that_observes(goal, opts=None, **hooks):
            desktop["front"] = 300             # the user went back to the console
            hooks["observe"]()                 # ... the window is read anyway
            desktop["popup"] = 150             # Notepad opened a Save As dialog
            hooks["observe"]()                 # ... the dialog, still not in front
            desktop["front"] = 150             # the dialog came to the front
            hooks["observe"]()
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            launcher=lambda _t: desktop["windows"].add(100),
            top_windows=lambda: set(desktop["windows"]),
            foreground_hwnd=lambda: desktop["front"],
            window_pid=lambda h: desktop["pids"][h],
            bring_to_front=bring,
            foreground_title=lambda: "jev · console", run_loop=loop_that_observes,
            observe=lambda: (_ for _ in ()).throw(AssertionError("must not observe the foreground")),
            observe_window=lambda hwnd: observed.append(hwnd) or object(),
            active_popup=lambda hwnd: desktop["popup"],
            backend_factory=lambda: object())
        operator.start("save it", None,
                       plan=[{"goal": "Open the Save As dialog", "launch": "notepad.exe"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["error"]
        assert observed == [100, 150, 150]
        assert brought == [100], "only the launch switched windows; no observation did"
        assert not [e for e in status["events"] if e["kind"] == "refocus"]

    def test_a_key_chord_waits_for_the_user_to_pause_before_taking_the_window(self, tmp_path):
        # Measured live 2026-09-21: three foreground steals in 40 s while the
        # user typed in Discord. A chord needs the window; it waits for the
        # user's hands to pause, then takes it once.
        from jevskill.cu.act import Action

        desktop = {"front": 100, "pids": {100: 5, 200: 9}}
        idle = iter([0.1, 0.2, 0.4, 5.0])
        brought, pressed = [], []

        def bring(hwnd):
            brought.append(hwnd)
            desktop["front"] = 100
            return True

        class KeyBackend:
            def send_keys(self, chord):
                pressed.append(chord)

        def loop_that_presses(goal, opts=None, **hooks):
            snap = hooks["observe"]()
            desktop["front"] = 200      # the user is typing in Discord
            result = hooks["execute"](Action(op="key", key="ctrl+shift+s"), snap)
            assert result.ok, result.error
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: desktop["front"], window_pid=lambda h: desktop["pids"][h],
            foreground_title=lambda: "Discord", run_loop=loop_that_presses,
            observe_window=lambda hwnd: object(), active_popup=lambda hwnd: hwnd,
            bring_to_front=bring, idle_seconds=lambda: next(idle, 99.0),
            backend_factory=lambda: KeyBackend())
        operator.start("save it", None, plan=[{"goal": "Open the Save As dialog"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["error"]
        assert brought == [100] and pressed == ["ctrl+shift+s"]
        refocus = [e["text"] for e in status["events"] if e["kind"] == "refocus"]
        assert len(refocus) == 1 and "brought back after waiting" in refocus[0]

    def test_a_uia_pattern_action_needs_no_foreground(self, tmp_path):
        # SetValue and Invoke act on a background window; only synthetic input
        # takes the screen. A file name typed into a Save As field while the
        # user reads the console must not pull the dialog in front of them.
        from types import SimpleNamespace

        from jevskill.cu.act import Action
        from jevskill.cu.runner import Operator

        field = SimpleNamespace(id="e73", role="edit", name="Nazwa pliku:", patterns=("value",))
        button = SimpleNamespace(id="e86", role="button", name="Zapisz", patterns=("invoke",))
        plain = SimpleNamespace(id="e9", role="button", name="Plain", patterns=())
        snap = SimpleNamespace(by_id=lambda i: {"e73": field, "e86": button, "e9": plain}.get(i))
        needs = Operator._needs_foreground
        assert needs(Action(op="type", target="e73", text="x"), snap) is False
        assert needs(Action(op="click", target="e86"), snap) is False
        assert needs(Action(op="click", target="e9"), snap) is True       # click by point
        assert needs(Action(op="key", key="ctrl+shift+s"), snap) is True
        assert needs(Action(op="done"), snap) is False

    def test_compose_asks_the_model_before_using_the_commands_quote(self, tmp_path):
        # Measured live 2026-09-21: goal 4 "Type the full file path into the
        # File name field" carried no quote of its own and the compose hook
        # typed the command's "hello world" into the Save As file-name field.
        from types import SimpleNamespace

        class PathLLM(FakeLLM):
            def chat(self, system, user, **kw):
                self.calls.append(system)
                if "exact text" in system:
                    payload = json.loads(user)
                    assert payload["field"]["name"] == "Nazwa pliku:"
                    assert payload["done_when"] == "hello.txt is on the Desktop"
                    assert kw == {"max_tokens": 300}
                    body = {"text": "%USERPROFILE%\\Desktop\\hello.txt"}
                    return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=50,
                                    tokens_out=20, cost_usd=0.0001, latency_ms=5.0)
                return FakeLLM.chat(self, system, user, **kw)

        fake = PathLLM()
        texts = []

        def loop_that_types(goal, opts=None, **hooks):
            field = SimpleNamespace(role="edit", name="Nazwa pliku:", value="")
            texts.append(hooks["compose_text"](goal, field))
            return RunResultStub("done")

        operator = make_operator(
            tmp_path, fake, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Zapisz jako", run_loop=loop_that_types,
            observe=lambda: object(), backend_factory=lambda: object())
        operator.start('Otwórz Notatnik, wpisz „hello world” i zapisz jako hello.txt na pulpicie',
                       "fake/model",
                       plan=[{"goal": "Type the full file path into the File name field and save",
                              "done_when": "hello.txt is on the Desktop"}])
        operator.wait(20)
        status = operator.status()
        assert texts == ["%USERPROFILE%\\Desktop\\hello.txt"]
        assert status["state"] == "done"
        events = [e["text"] for e in status["events"]]
        assert any("text from fake/model" in t for t in events)
        assert not any("quoted text from the command" in t for t in events)

    def test_the_model_sees_every_candidate_the_loop_saw(self, tmp_path):
        from jevskill.cu import runner as runner_module

        assert runner_module.ELEMENTS_FOR_LLM == runner_module.OPERATOR_CAP == 150

    def test_a_launch_waits_for_the_user_to_pause_before_switching_windows(self, tmp_path):
        # Measured live 2026-09-21: Notepad was switched to while the user typed
        # elsewhere and their next two keystrokes ("ac") landed in it.
        desktop = {"front": 300, "pids": {100: 5, 300: 7}, "windows": set()}
        idle = iter([0.1, 0.2, 0.3, 5.0])
        brought = []

        def bring(hwnd):
            brought.append(hwnd)
            desktop["front"] = hwnd
            return True

        operator = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            launcher=lambda _t: desktop["windows"].add(100),
            top_windows=lambda: set(desktop["windows"]),
            foreground_hwnd=lambda: desktop["front"], window_pid=lambda h: desktop["pids"][h],
            bring_to_front=bring, foreground_title=lambda: "Discord",
            run_loop=lambda goal, opts=None, **hooks: RunResultStub("done"),
            observe_window=lambda hwnd: object(), idle_seconds=lambda: next(idle, 99.0),
            backend_factory=lambda: object())
        operator.start("open it", None, plan=[{"goal": "Open Notepad", "launch": "notepad.exe"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["error"]
        assert brought == [100]
        launch = [e["text"] for e in status["events"] if e["kind"] == "launch"]
        assert any("waited" in text and "before switching windows" in text for text in launch)

    def test_text_the_model_composed_is_not_cleared_by_the_sanity_check(self, tmp_path):
        # Measured live 2026-09-21: Jev scored a correct %USERPROFILE% path at
        # 0.06 in the Save As file-name field and the loop cleared it.
        from jevskill.cu.decide import THRESHOLDS

        seen = {}

        def loop_that_records(goal, opts=None, **hooks):
            seen["with_model"] = hooks.get("thresholds")
            return RunResultStub("done")

        with_model = make_operator(
            tmp_path, FakeLLM(), platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=loop_that_records,
            observe_window=lambda hwnd: object(), backend_factory=lambda: object())
        with_model.start("save it", "fake/model", plan=[{"goal": "Save it"}])
        with_model.wait(20)
        assert with_model.status()["state"] == "done"
        assert seen["with_model"]["text_sanity"] == 0.0
        assert {k: v for k, v in seen["with_model"].items() if k != "text_sanity"} == \
            {k: v for k, v in THRESHOLDS.items() if k != "text_sanity"}

        def loop_jev_only(goal, opts=None, **hooks):
            seen["jev_only"] = "thresholds" in hooks
            return RunResultStub("done")

        jev_only = make_operator(
            tmp_path, platform_check=lambda: {"ok": True, "reason": ""},
            foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
            foreground_title=lambda: "Notepad", run_loop=loop_jev_only,
            observe_window=lambda hwnd: object(), backend_factory=lambda: object())
        jev_only.start("save it", None, plan=[{"goal": "Save it"}])
        jev_only.wait(20)
        assert seen["jev_only"] is False, "Jev only: the loop's own sanity check stays"

    def test_plan_only_calls_the_model_and_touches_nothing(self, tmp_path):
        fake = FakeLLM()
        operator = make_operator(tmp_path, fake)
        plan = operator.plan("open notepad and type hello", "fake/model")
        assert [s["goal"] for s in plan["steps"]][0] == "Open Notepad"
        assert plan["note"] == "three goals"
        assert operator.status()["state"] == "idle"


# --------------------------------------------------------------------------- #
# The console routes
# --------------------------------------------------------------------------- #


class FakeClient:
    def warm(self, mode=None):
        return 1.0

    def decide(self, *_a, **_k):  # pragma: no cover - never reached by these tests
        raise AssertionError("no decision expected")

    def close(self):
        pass


@contextlib.contextmanager
def running(**kwargs):
    server = make_server(port=0, client=FakeClient(), **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", "replace")
        return response.status, json.loads(text)
    finally:
        conn.close()


@pytest.fixture
def offline_models(monkeypatch):
    def fail(**_kw):
        raise LLMError("offline")
    monkeypatch.setattr(llm_module, "fetch_models", fail)
    llm_module._models_cache["models"] = None


@pytest.fixture
def keyed(monkeypatch):
    from jevskill.config import ALL_KEY_ENV_VARS

    for name in ALL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})
    monkeypatch.setenv("OPENROUTER_API_KEY", MARKED_KEY)


class TestRoutes:
    def test_status_is_idle_and_names_the_stop_paths(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, _server):
            status, body = call(port, "GET", "/api/cu/status")
            assert status == 200 and body["state"] == "idle"
            assert body["kill_switch"]["cli"] == "jevskill cu stop"
            assert "platform" in body and "ok" in body["platform"]

    def test_models_fall_back_offline_and_never_carry_the_key(self, tmp_path, offline_models, keyed):
        with running(ledger_root=tmp_path) as (port, _server):
            status, body = call(port, "GET", "/api/cu/models")
            assert status == 200 and body["source"] == "fallback"
            assert body["key_found"] is True
            assert body["default"] == "anthropic/claude-sonnet-5"
            assert MARKED_KEY not in json.dumps(body) and "TESTKEY" not in json.dumps(body)

    def test_start_validates_then_runs_a_dry_run_to_done(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = server.console.operator()
            operator.kill = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
            status, body = call(port, "POST", "/api/cu/start", {"command": " ", "dry_run": True})
            assert status == 400
            status, body = call(port, "POST", "/api/cu/confirm", {"allow": True})
            assert status == 409
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": 'Type "abc"', "model": None, "dry_run": True})
            assert status == 202 and body["run_id"]
            status, _ = call(port, "POST", "/api/cu/start", {"command": "again", "dry_run": True})
            assert status == 409
            operator.wait(20)
            status, body = call(port, "GET", "/api/cu/status?since=0")
            assert status == 200 and body["state"] == "done"
            assert body["next"] == len(body["events"]) > 0
            status, body = call(port, "POST", "/api/cu/stop", {})
            assert status == 200 and body["ok"] is True

    def test_stop_and_confirm_reach_the_operator(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = server.console.operator()
            operator.kill = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
            operator._llm_factory = lambda _m: FakeLLM()
            status, _ = call(port, "POST", "/api/cu/start",
                             {"command": "delete it", "model": "fake/model", "dry_run": True})
            assert status == 202
            deadline = time.time() + 10
            while time.time() < deadline:
                _, body = call(port, "GET", "/api/cu/status")
                if body.get("pending_confirm"):
                    break
                time.sleep(0.05)
            assert body["state"] == "waiting_confirm"
            assert body["pending_confirm"]["prompt"].startswith("click")
            status, _ = call(port, "POST", "/api/cu/stop", {"reason": "panel STOP"})
            assert status == 200
            operator.wait(10)
            _, body = call(port, "GET", "/api/cu/status")
            assert body["state"] == "stopped"
            assert body["stop_reason"] == "panel STOP"


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_cu_stop_touches_the_stop_file(self, tmp_path, capsys):
        assert cli.main(["cu", "stop", "--ledger-dir", str(tmp_path)]) == 0
        assert (tmp_path / ".jevskill" / "cu.stop").exists()
        assert "stop requested" in capsys.readouterr().out

    def test_cu_run_dry_run_prints_events_and_exits_zero(self, tmp_path, capsys):
        code = cli.main(["cu", "run", 'Type "abc"', "--dry-run", "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "DRY RUN" in out and "goal_done" in out

    def test_cu_help_names_every_stop_path(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["cu", "--help"])
        out = capsys.readouterr().out
        for phrase in ("Ctrl+C", "Ctrl+Alt+Esc", "top-left", "jevskill cu stop"):
            assert phrase in out


class TestPackageExports:
    def test_operator_and_switch_are_lazy_exports(self):
        import jevskill.cu as cu
        from jevskill.cu import killswitch, runner

        assert cu.Operator is runner.Operator
        assert cu.KillSwitch is killswitch.KillSwitch
        assert "Operator" in dir(cu)

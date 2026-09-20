"""The whole step loop, offline: every stop reason, every gate, every record.

The loop is the one module here that can do damage, so the tests are written
around the question "what would have to be true for this to click the wrong
thing". The answers are all in this file: an action executed without a verdict,
a destructive control passed without a human, a ``done`` believed without
evidence, a run with no budget.

Two frames of a real Notepad snapshot drive the happy path; everything that
needs a specific control (a Delete button, a text field) uses a hand-built
window instead, because the ranking inside ``reduce`` is being tuned and a test
of the *loop* must not fail when a candidate moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jevskill.client import Answer, Decisions
from jevskill.cu import loop as loop_mod
from jevskill.cu.act import ActResult
from jevskill.cu.contract import RunOptions
from jevskill.cu.loop import (DONE_WITHOUT_VERIFIER, ESCALATE_AFTER_INVALID,
                              STALL_LIMIT, run)
from jevskill.cu.macros import MacroCache
from jevskill.cu.reduce import candidates
from jevskill.cu.types import Snapshot, UIElement
from jevskill.stats import Ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"
GOAL = "Save the current document"


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #

def answer_set(target="e1", op="click", probs=None, op_probs=None, nouls=None,
               confidence=None):
    if probs is None:
        probs = {target: 0.97} if target == "none" else {target: 0.97, "none": 0.03}
    if op_probs is None:
        op_probs = {op: 0.95} if op == "key" else {op: 0.95, "key": 0.05}
    answers = {
        "target": Answer("choice", "target",
                         {"type": "choice", "choice": target,
                          "confidence": confidence if confidence is not None
                          else max(probs.values()), "probabilities": probs}),
        "op": Answer("choice", "op",
                     {"type": "choice", "choice": op,
                      "confidence": max(op_probs.values()),
                      "probabilities": op_probs}),
    }
    values = {"goal_reached": 0.04, "needs_text": 0.1, "is_destructive": 0.3}
    values.update(nouls or {})
    for name, value in values.items():
        answers[name] = Answer("noul", name, {"type": "noul", "noul": value})
    return answers


def response(**kwargs) -> Decisions:
    return Decisions(answers=answer_set(**kwargs), model="jev-1.13.0",
                     request_id="r", usage={"input_tokens": 1000, "cost": 4.2e-05},
                     timing_ms={"total_ms": 300.0}, provider="typesafe")


def text_response(value: float) -> Decisions:
    return Decisions(answers={"text_ok": Answer("noul", "text_ok",
                                                {"type": "noul", "noul": value})},
                     model="jev-1.13.0", request_id="t",
                     usage={"input_tokens": 200, "cost": 8e-06})


class ScriptedClient:
    """Canned answers in order; the last one repeats so a long run still runs."""

    def __init__(self, *responses, text_ok=0.95):
        self.responses = list(responses) or [response()]
        self.text_ok = text_ok
        self.calls = []
        self.warmed = False

    def warm(self, mode=None):
        self.warmed = True
        return 0.0

    def decide(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        if "text_ok" in questions:
            return text_response(self.text_ok)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class Frames:
    """Successive snapshots; the last one repeats (settle polls it)."""

    def __init__(self, *snapshots):
        self.queue = list(snapshots)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


class Executor:
    def __init__(self, ok=True, error=""):
        self.ok = ok
        self.error = error
        self.actions = []

    def __call__(self, action, snapshot, **kwargs):
        self.actions.append(action)
        return ActResult(ok=self.ok, method="fake", error=self.error,
                         dry_run=bool(kwargs.get("dry_run")))


def window(elements, title="Editor"):
    return Snapshot(window_title=title, app="editor.exe", pid=1, hwnd=2,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(elements),
                    _handles={el.id: "h:" + el.id for el in elements})


def simple(extra=()):
    return window([
        UIElement(id="e1", role="button", name="Save", bbox=(10, 10, 60, 30),
                  patterns=("invoke",)),
        UIElement(id="e2", role="button", name="Cancel", bbox=(80, 10, 60, 30),
                  patterns=("invoke",)),
    ] + list(extra))


def notepad_frames():
    """Two frames of a real snapshot: the second has lost its toolbar row."""
    data = json.loads((FIXTURES / "notepad.json").read_text(encoding="utf-8"))
    first = Snapshot.from_dict(data["snapshot"])
    first._handles = {el.id: "h:" + el.id for el in first.elements}
    keep = [el for el in first.elements if el.id not in {"e16", "e17", "e18"}]
    second = window(keep, title="notepad - saved")
    return first, second


def clickable(snapshot) -> str:
    """The id of a real button in a real snapshot.

    Not ``candidates(...)[0]``: on this fixture that is the focused *document*,
    and a ``click`` on a document is exactly the role/op mismatch
    :func:`jevskill.cu.decide.validate` exists to refuse. Picking by role keeps
    the test about the loop while the reduction's ranking is being tuned.
    """
    for element in candidates(snapshot.elements):
        if element.role == "button" and "invoke" in element.patterns:
            return element.id
    raise AssertionError("the fixture has no clickable button")


# --------------------------------------------------------------------------- #

class TestHappyPath:
    def test_a_run_over_two_real_frames_stops_on_a_verified_done(self):
        first, second = notepad_frames()
        target = clickable(first)
        client = ScriptedClient(response(target=target),
                                response(target="none", op="done",
                                         nouls={"goal_reached": 0.93}))
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=5), task_id="notepad", run=2,
                     observe=Frames(first, second), execute=executor,
                     client=client, settle_ms=1,
                     verify=lambda goal, snap: "saved" in snap.window_title)
        assert result.stop_reason == "done"
        assert result.task_id == "notepad" and result.run == 2
        assert [s.op for s in result.steps] == ["click", "done"]
        assert result.steps[0].executed and result.steps[0].tree_changed
        assert result.steps[1].executed is False        # `done` performs nothing
        assert result.decisions == 2 and result.escalations == 0
        assert result.tokens_in == 2000
        assert result.cost_usd == pytest.approx(8.4e-05)
        assert result.wall_ms > 0.0
        assert client.warmed

    def test_every_stage_is_timed(self):
        first, second = notepad_frames()
        result = run(GOAL, RunOptions(max_steps=1),
                     observe=Frames(first, second), execute=Executor(),
                     client=ScriptedClient(response(
                         target=clickable(first))), settle_ms=1)
        stages = result.steps[0].stages_ms
        assert set(stages) == {"observe", "reduce", "decide", "validate", "act",
                               "settle"}
        assert all(value >= 0.0 for value in stages.values())

    def test_the_step_record_carries_the_three_nouls_unthresholded(self):
        first, second = notepad_frames()
        client = ScriptedClient(response(target=clickable(first),
                                         nouls={"goal_reached": 0.11,
                                                "needs_text": 0.22,
                                                "is_destructive": 0.79}))
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(first, second),
                     execute=Executor(), client=client, settle_ms=1)
        step = result.steps[0]
        assert (step.goal_reached, step.needs_text, step.is_destructive) == (
            0.11, 0.22, 0.79)
        assert step.decided_by == "jev" and step.candidates > 0

    def test_the_goal_is_re_supplied_verbatim_every_step(self):
        first, second = notepad_frames()
        client = ScriptedClient(response(target=clickable(first)))
        run(GOAL, RunOptions(max_steps=2), observe=Frames(first, second),
            execute=Executor(), client=client, settle_ms=1)
        assert all(call["state"]["goal"] == GOAL for call in client.calls)

    def test_the_model_is_told_the_outcome_it_is_never_asked_for_it(self):
        first, second = notepad_frames()
        client = ScriptedClient(response(target=clickable(first)))
        run(GOAL, RunOptions(max_steps=2), observe=Frames(first, second),
            execute=Executor(), client=client, settle_ms=1)
        assert client.calls[0]["state"]["last_action"]["outcome"] is None
        assert client.calls[1]["state"]["last_action"]["outcome"] == "new_window"
        assert not any("stuck" in call["questions"] for call in client.calls)


class TestStopReasons:
    def test_max_steps(self):
        client = ScriptedClient(response())
        result = run(GOAL, RunOptions(max_steps=3), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1,
                     macros=None)
        # Every step acts on a tree that never changes, so the stall rule wins
        # before max_steps does — which is the point of having both.
        assert result.stop_reason == "blocked"
        assert len(result.steps) == STALL_LIMIT

    def test_max_steps_when_the_screen_keeps_moving(self):
        frames = [simple(), simple([UIElement(id="e3", role="button", name="A",
                                              bbox=(0, 60, 20, 20),
                                              patterns=("invoke",))]),
                  simple([UIElement(id="e4", role="button", name="B",
                                    bbox=(0, 90, 20, 20), patterns=("invoke",))])]
        index = {"n": 0}

        def observe():
            index["n"] += 1
            return frames[min(index["n"] // 2, len(frames) - 1)]

        result = run(GOAL, RunOptions(max_steps=2), observe=observe,
                     execute=Executor(), client=ScriptedClient(response()),
                     settle_ms=1)
        assert result.stop_reason == "max_steps" and len(result.steps) == 2

    def test_budget(self):
        result = run(GOAL, RunOptions(max_steps=10, budget_s=0.0),
                     observe=Frames(simple()), execute=Executor(),
                     client=ScriptedClient(response()))
        assert result.stop_reason == "budget" and result.steps == []

    def test_blocked_when_two_actions_leave_the_tree_unchanged(self):
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=9), observe=Frames(simple()),
                     execute=executor, client=ScriptedClient(response()),
                     settle_ms=1)
        assert result.stop_reason == "blocked"
        assert len(executor.actions) == STALL_LIMIT
        assert "unchanged" in result.steps[-1].note

    def test_escalated_when_nothing_answers_the_escalation(self):
        low = response(op_probs={"click": 0.51, "key": 0.49})
        result = run(GOAL, RunOptions(max_steps=5), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(low), settle_ms=1)
        assert result.stop_reason == "escalated" and result.escalations == 1
        assert "low_confidence" in result.steps[-1].note

    def test_error_when_a_stage_raises(self):
        def observe():
            raise OSError("no foreground window")

        result = run(GOAL, RunOptions(max_steps=2), observe=observe,
                     execute=Executor(), client=ScriptedClient(response()))
        assert result.stop_reason == "error" and "no foreground window" in result.error

    def test_a_step_with_no_client_and_no_macro_is_an_error_not_a_guess(self):
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(simple()),
                     execute=Executor(), client=None)
        assert result.stop_reason == "error"


class TestDone:
    def test_done_is_never_believed_without_evidence(self):
        client = ScriptedClient(response(target="none", op="done",
                                         nouls={"goal_reached": 0.99}))
        result = run(GOAL, RunOptions(max_steps=4), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1,
                     verify=lambda goal, snap: False)
        assert result.stop_reason == "escalated"

    def test_two_consecutive_done_proposals_stop_a_run_with_no_verifier(self):
        client = ScriptedClient(response(target="none", op="done",
                                         nouls={"goal_reached": 0.9}))
        result = run(GOAL, RunOptions(max_steps=6), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1)
        assert result.stop_reason == "done"
        assert len(result.steps) == DONE_WITHOUT_VERIFIER
        assert "unverified" in result.steps[-1].note

    def test_done_contradicted_by_its_own_goal_reached_is_blocked(self):
        client = ScriptedClient(response(target="none", op="done",
                                         nouls={"goal_reached": 0.05}))
        result = run(GOAL, RunOptions(max_steps=3), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1,
                     verify=lambda goal, snap: False)
        assert result.stop_reason == "blocked"

    def test_a_verifier_that_passes_stops_the_run_even_without_a_done(self):
        # act.md §3, row 3: `goal_reached` high, op not `done` — the verifier
        # decides, and it decides to stop.
        client = ScriptedClient(response(nouls={"goal_reached": 0.95}))
        result = run(GOAL, RunOptions(max_steps=3), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1,
                     verify=lambda goal, snap: True)
        assert result.stop_reason == "done"

    def test_a_verifier_that_raises_does_not_take_the_run_with_it(self):
        def verify(goal, snapshot):
            raise RuntimeError("the file went away")

        client = ScriptedClient(response(target="none", op="done",
                                         nouls={"goal_reached": 0.9}))
        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(simple()),
                     execute=Executor(), client=client, settle_ms=1, verify=verify)
        assert result.stop_reason == "escalated"


class TestDestructiveGate:
    def delete_screen(self):
        return simple([UIElement(id="e3", role="button", name="Delete all documents",
                                 bbox=(160, 10, 120, 30), patterns=("invoke",))])

    def test_a_deterministic_name_gates_even_at_0_99(self):
        asked = []
        client = ScriptedClient(response(target="e3",
                                         probs={"e3": 0.99, "none": 0.01},
                                         nouls={"is_destructive": 0.93}))
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=3), observe=Frames(self.delete_screen()),
                     execute=executor, client=client, settle_ms=1,
                     confirm=lambda prompt, action, element: asked.append(prompt) or False)
        assert result.stop_reason == "blocked"
        assert result.destructive_gates == 1
        assert executor.actions == []          # nothing was performed
        assert "Delete all documents" in asked[0]

    def test_the_default_gate_refuses_because_no_human_is_attached(self):
        client = ScriptedClient(response(target="e3", probs={"e3": 0.99, "none": 0.01}))
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(self.delete_screen()),
                     execute=executor, client=client, settle_ms=1)
        assert result.stop_reason == "blocked" and executor.actions == []

    def test_a_yes_lets_it_through_and_the_gate_is_still_counted(self):
        client = ScriptedClient(response(target="e3", probs={"e3": 0.99, "none": 0.01}))
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(self.delete_screen()),
                     execute=executor, client=client, settle_ms=1,
                     confirm=lambda *a: True)
        assert result.destructive_gates == 1
        assert [a.target for a in executor.actions] == ["e3"]

    def test_a_harmless_control_is_not_gated(self):
        client = ScriptedClient(response(target="e1"))
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(self.delete_screen()),
                     execute=Executor(), client=client, settle_ms=1)
        assert result.destructive_gates == 0

    def test_a_high_per_element_noul_gates_a_name_the_english_list_misses(self):
        screen = simple([UIElement(id="e3", role="button", name="Wyczyść pamięć",
                                   bbox=(160, 10, 120, 30), patterns=("invoke",))])
        answer = response(target="e3", probs={"e3": 0.95, "none": 0.05})
        answer.answers["destructive_e3"] = Answer("noul", "destructive_e3",
                                                  {"type": "noul", "noul": 0.9})
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(screen),
                     execute=Executor(), client=ScriptedClient(answer), settle_ms=1)
        assert result.destructive_gates == 1 and result.stop_reason == "blocked"


class TestText:
    def field(self):
        return simple([UIElement(id="e3", role="edit", name="File name",
                                 bbox=(10, 60, 200, 28), patterns=("value",),
                                 value="")])

    def test_a_step_that_needs_text_escalates_when_nothing_can_write_it(self):
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}))
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(self.field()),
                     execute=executor, client=client, settle_ms=1)
        assert result.stop_reason == "escalated" and result.escalations == 1
        assert executor.actions == []
        assert "needs_text" in result.steps[-1].note

    def test_an_injected_writer_fills_the_field(self):
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}))
        executor = Executor()
        run(GOAL, RunOptions(max_steps=1), observe=Frames(self.field()),
            execute=executor, client=client, settle_ms=1,
            compose_text=lambda goal, element: "draft.txt")
        assert executor.actions[0].text == "draft.txt"
        assert executor.actions[0].op == "type"

    def test_type_without_needs_text_is_a_contradiction_and_escalates(self):
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.05}))
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(self.field()),
                     execute=Executor(), client=client, settle_ms=1,
                     compose_text=lambda goal, element: "x")
        assert result.stop_reason == "escalated"
        assert "type_without_needs_text" in result.steps[-1].note

    def test_rejected_text_is_cleared_and_the_step_retried(self):
        typed = self.field()
        after = simple([UIElement(id="e3", role="edit", name="File name",
                                  bbox=(10, 60, 200, 28), patterns=("value",),
                                  value="qqqq")])
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}),
                                text_ok=0.12)
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(typed, after),
                     execute=executor, client=client, settle_ms=1,
                     compose_text=lambda goal, element: "draft.txt")
        assert [a.text for a in executor.actions] == ["draft.txt", ""]
        assert "text_ok=0.12" in result.steps[0].note
        # The second call is a cost the step was not budgeted for, so it is
        # recorded rather than absorbed.
        assert result.steps[0].tokens_in == 1200
        assert result.tokens_in == 1200

    def test_accepted_text_is_left_alone(self):
        typed = self.field()
        after = simple([UIElement(id="e3", role="edit", name="File name",
                                  bbox=(10, 60, 200, 28), patterns=("value",),
                                  value="draft.txt")])
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}),
                                text_ok=0.95)
        executor = Executor()
        run(GOAL, RunOptions(max_steps=1), observe=Frames(typed, after),
            execute=executor, client=client, settle_ms=1,
            compose_text=lambda goal, element: "draft.txt")
        assert [a.text for a in executor.actions] == ["draft.txt"]


class TestEscalation:
    def test_an_escalation_handler_can_answer_the_step(self):
        from jevskill.cu.act import Action

        low = response(op_probs={"click": 0.51, "key": 0.49})
        seen = {}

        def escalate(context):
            seen.update(context)
            return Action(op="key", key="ctrl+s", source="escalation")

        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(simple()),
                     execute=executor, client=ScriptedClient(low), settle_ms=1,
                     escalate=escalate)
        assert result.escalations == 1
        assert result.steps[0].decided_by == "escalation"
        assert executor.actions[0].key == "ctrl+s"
        assert seen["reason"] == "low_confidence"
        assert seen["goal"] == GOAL and seen["candidates"]

    def test_a_handler_that_declines_stops_the_run(self):
        low = response(op_probs={"click": 0.51, "key": 0.49})
        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(low), settle_ms=1,
                     escalate=lambda context: None)
        assert result.stop_reason == "escalated"

    def test_a_handler_that_raises_stops_the_run_rather_than_the_process(self):
        low = response(op_probs={"click": 0.51, "key": 0.49})

        def escalate(context):
            raise ValueError("the VLM is down")

        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(low), settle_ms=1,
                     escalate=escalate)
        assert result.stop_reason == "escalated"
        assert "ValueError" in result.steps[-1].note

    def test_a_stale_target_is_retried_before_it_is_escalated(self):
        # ~300 ms is long enough for a dialog to close; the first miss is a
        # perception failure, not a bad decision.
        gone = response(target="e9", probs={"e9": 0.98, "none": 0.02})
        result = run(GOAL, RunOptions(max_steps=4), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(gone), settle_ms=1)
        assert result.escalations == 1
        assert len(result.steps) == ESCALATE_AFTER_INVALID
        assert "unknown_target" in result.steps[0].note

    def test_an_execution_failure_is_retried_then_escalated(self):
        executor = Executor(ok=False, error="COM said no")
        result = run(GOAL, RunOptions(max_steps=5), observe=Frames(simple()),
                     execute=executor, client=ScriptedClient(response()), settle_ms=1)
        assert result.stop_reason == "escalated"
        assert len(executor.actions) == ESCALATE_AFTER_INVALID
        assert result.steps[0].executed is False


class TestEscalationIsNotAnAuthorisation:
    """An escalation handler's Action gets the same checks a model's does.

    It did not: a probe returned a click on "Delete all documents" and the loop
    performed it with ``destructive_gates == 0`` and ``confirm`` never called,
    while this module's docstring promises a run with no human attached cannot.
    """

    def delete_screen(self):
        return simple([UIElement(id="e3", role="button",
                                 name="Delete all documents",
                                 bbox=(160, 10, 120, 30), patterns=("invoke",))])

    def low(self):
        return ScriptedClient(response(op_probs={"click": 0.51, "key": 0.49}))

    def test_a_destructive_escalation_is_gated_counted_and_refused(self):
        from jevskill.cu.act import Action

        asked, executor = [], Executor()
        result = run(GOAL, RunOptions(max_steps=2),
                     observe=Frames(self.delete_screen()), execute=executor,
                     client=self.low(), settle_ms=1,
                     escalate=lambda ctx: Action(op="click", target="e3",
                                                 source="escalation"),
                     confirm=lambda prompt, a, e: asked.append(prompt) or False)
        assert result.stop_reason == "blocked"
        assert result.destructive_gates == 1
        assert executor.actions == []
        assert "Delete all documents" in asked[0]

    def test_the_default_gate_refuses_an_escalation_too(self):
        from jevskill.cu.act import Action

        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=2),
                     observe=Frames(self.delete_screen()), execute=executor,
                     client=self.low(), settle_ms=1,
                     escalate=lambda ctx: Action(op="click", target="e3",
                                                 source="escalation"))
        assert result.stop_reason == "blocked" and executor.actions == []

    def test_a_yes_lets_the_escalation_through(self):
        from jevskill.cu.act import Action

        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1),
                     observe=Frames(self.delete_screen()), execute=executor,
                     client=self.low(), settle_ms=1, confirm=lambda *a: True,
                     escalate=lambda ctx: Action(op="click", target="e3",
                                                 source="escalation"))
        assert result.destructive_gates == 1
        assert [a.target for a in executor.actions] == ["e3"]

    def test_an_incoherent_escalation_is_refused_rather_than_performed(self):
        from jevskill.cu.act import Action

        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=2), observe=Frames(simple()),
                     execute=executor, client=self.low(), settle_ms=1,
                     escalate=lambda ctx: Action(op="type", target="e1",
                                                 text="x", source="escalation"))
        assert result.stop_reason == "escalated"
        assert executor.actions == []                  # type on a button
        assert "role_op_mismatch" in result.steps[-1].note

    def test_enter_on_a_destructive_default_is_gated(self):
        from jevskill.cu.act import Action

        screen = simple([UIElement(id="e3", role="button",
                                   name="Delete all documents", focused=True,
                                   bbox=(160, 10, 120, 30), patterns=("invoke",))])
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(screen),
                     execute=executor, client=self.low(), settle_ms=1,
                     escalate=lambda ctx: Action(op="key", key="enter",
                                                 source="escalation"))
        assert result.stop_reason == "blocked" and executor.actions == []

    def test_an_escalated_action_claims_no_outcome_it_did_not_measure(self):
        """``ActResult.ok`` is "the call returned", never "the screen changed".

        The next step's observation is what measures it — the same two hashes
        every other step is judged by.
        """
        from jevskill.cu.act import Action

        first, second = notepad_frames()
        target = clickable(first)
        client = ScriptedClient(response(target=target,
                                         op_probs={"click": 0.51, "key": 0.49}),
                                response(target=clickable(second)))
        run(GOAL, RunOptions(max_steps=2), observe=Frames(first, second),
            execute=Executor(), client=client, settle_ms=1,
            escalate=lambda ctx: Action(op="click", target=target,
                                        source="escalation"))
        outcomes = [call["state"]["last_action"]["outcome"]
                    for call in client.calls]
        assert outcomes[0] is None
        # Step 2 was told what the hashes say, not what the executor returned.
        assert outcomes[1] == "new_window"


class TestKeyChord:
    """``op=key`` used to die in ``execute`` on "key without a chord"."""

    def dialog(self, extra=()):
        return window(list(extra) + [
            UIElement(id="e1", role="button", name="OK", focused=True,
                      bbox=(10, 10, 60, 30), patterns=("invoke",)),
            UIElement(id="d0", role="dialog", name="Save changes?")])

    def test_a_key_decision_becomes_a_real_keystroke(self):
        executor = Executor()
        result = run("Confirm the dialog", RunOptions(max_steps=1),
                     observe=Frames(self.dialog()), execute=executor,
                     client=ScriptedClient(response(target="none", op="key")),
                     settle_ms=1)
        assert [(a.op, a.key) for a in executor.actions] == [("key", "enter")]
        assert result.steps[0].executed
        assert "key enter" in result.steps[0].note

    def test_a_goal_that_cancels_gets_escape(self):
        executor = Executor()
        run("Cancel the dialog and discard the changes", RunOptions(max_steps=1),
            observe=Frames(self.dialog()), execute=executor,
            client=ScriptedClient(response(target="none", op="key")), settle_ms=1)
        assert executor.actions[0].key == "escape"

    def test_a_screen_with_no_row_escalates_instead_of_failing_twice(self):
        body = window([UIElement(id="e1", role="document", name="body",
                                 focused=True, bbox=(0, 0, 400, 300),
                                 patterns=("value",))])
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=3), observe=Frames(body),
                     execute=executor, client=ScriptedClient(
                         response(target="none", op="key")), settle_ms=1)
        assert result.stop_reason == "escalated" and result.escalations == 1
        assert executor.actions == []
        assert "no_chord" in result.steps[-1].note

    def test_enter_on_a_destructive_default_asks_the_human(self):
        """No focused default, so what Enter fires is unknown — and one of the
        dialog's buttons deletes."""
        screen = window([
            UIElement(id="e1", role="button", name="OK", bbox=(10, 10, 60, 30),
                      patterns=("invoke",)),
            UIElement(id="e2", role="button", name="Delete all documents",
                      bbox=(90, 10, 120, 30), patterns=("invoke",)),
            UIElement(id="d0", role="dialog", name="Delete these files?")])
        asked, executor = [], Executor()
        result = run("Confirm", RunOptions(max_steps=1), observe=Frames(screen),
                     execute=executor,
                     client=ScriptedClient(response(target="none", op="key")),
                     settle_ms=1,
                     confirm=lambda prompt, a, e: asked.append(prompt) or False)
        assert result.stop_reason == "blocked" and result.destructive_gates == 1
        assert executor.actions == [] and asked == ["key enter?"]


class TestSettleSeesWhatTheActionChanged:
    """A successful ``type`` changes only ``value``, which the default hash drops."""

    def field(self, value=""):
        return simple([UIElement(id="e3", role="edit", name="File name",
                                 bbox=(10, 60, 200, 28), patterns=("value",),
                                 value=value)])

    def typing_client(self):
        return ScriptedClient(response(target="e3", op="type",
                                       probs={"e3": 0.96, "none": 0.04},
                                       nouls={"needs_text": 0.9}),
                              text_ok=0.95)

    def test_a_type_that_only_fills_the_field_counts_as_a_change(self):
        result = run("Type a file name", RunOptions(max_steps=1),
                     observe=Frames(self.field(""), self.field("draft.txt")),
                     execute=Executor(), client=self.typing_client(),
                     settle_ms=5, compose_text=lambda g, e: "draft.txt")
        assert result.steps[0].tree_changed is True
        assert result.stop_reason == "max_steps"

    def test_the_next_step_is_told_changed_and_not_unchanged(self):
        client = self.typing_client()
        run("Type a file name", RunOptions(max_steps=2),
            observe=Frames(self.field(""), self.field("draft.txt")),
            execute=Executor(), client=client, settle_ms=5,
            compose_text=lambda g, e: "draft.txt")
        steps = [call for call in client.calls if "target" in call["questions"]]
        assert steps[1]["state"]["last_action"]["outcome"] == "changed"

    def test_a_macro_is_learned_from_a_type_that_worked(self, tmp_path):
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        run("Type a file name", RunOptions(max_steps=1),
            observe=Frames(self.field(""), self.field("draft.txt")),
            execute=Executor(), client=self.typing_client(), settle_ms=5,
            macros=cache, compose_text=lambda g, e: "draft.txt")
        assert len(cache) == 1

    def test_a_type_that_did_nothing_still_reads_unchanged(self):
        """The fix must not make every `type` look successful."""
        result = run("Type a file name", RunOptions(max_steps=1),
                     observe=Frames(self.field("")), execute=Executor(),
                     client=self.typing_client(), settle_ms=1,
                     compose_text=lambda g, e: "draft.txt")
        assert result.steps[0].tree_changed is False

    def test_a_click_still_settles_on_the_default_ignore(self):
        """A `value` that ticks on its own is not a click's effect."""
        before = simple([UIElement(id="e3", role="progressbar", name="Progress",
                                   bbox=(10, 60, 200, 10), value="10")])
        after = simple([UIElement(id="e3", role="progressbar", name="Progress",
                                  bbox=(10, 60, 200, 10), value="20")])
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(before, after),
                     execute=Executor(), client=ScriptedClient(response()),
                     settle_ms=1)
        assert result.steps[0].tree_changed is False

    def test_a_combobox_raises_a_short_ceiling_to_the_published_cap(self):
        """act.md §4's 200 ms, applied the only way a hash poll can: as a floor."""
        from jevskill.cu import act as act_mod

        seen = {}
        original = act_mod.settle

        def spy(observe, prev, **kwargs):
            seen.update(kwargs)
            return original(observe, prev, **kwargs)

        combo = simple([UIElement(id="e3", role="combobox", name="Save as type",
                                  bbox=(10, 60, 200, 28),
                                  patterns=("select", "expand"))])
        monkey = pytest.MonkeyPatch()
        monkey.setattr(act_mod, "settle", spy)
        try:
            run(GOAL, RunOptions(max_steps=1), observe=Frames(combo),
                execute=Executor(), settle_ms=1.0,
                client=ScriptedClient(response(target="e3", op="select",
                                               probs={"e3": 0.96, "none": 0.04})))
        finally:
            monkey.undo()
        assert seen["timeout_ms"] == act_mod.SETTLE_CAP_COMBOBOX_MS


class TestDecidedByCode:
    """``contract.DECIDED_BY`` documents ``code``; nothing ever produced it."""

    def field(self, value=""):
        return simple([UIElement(id="e3", role="edit", name="File name",
                                 bbox=(10, 60, 200, 28), patterns=("value",),
                                 value=value)])

    def combo(self, value=""):
        """An editable combobox: `click` is legal on it and so is `type`.

        The override only exists where both are — on a plain ``edit`` the
        role/op check refuses the `click` first, which is a different rule
        doing a different job.
        """
        return simple([UIElement(id="e3", role="combobox", name="File name",
                                 bbox=(10, 60, 200, 28),
                                 patterns=("expand", "value"), value=value)])

    def test_a_click_turned_into_a_type_is_the_codes_decision(self):
        client = ScriptedClient(response(target="e3", op="click",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}),
                                text_ok=0.95)
        executor = Executor()
        result = run("Type a file name", RunOptions(max_steps=1),
                     observe=Frames(self.combo(""), self.combo("draft.txt")),
                     execute=executor, client=client, settle_ms=5,
                     compose_text=lambda g, e: "draft.txt")
        assert executor.actions[0].op == "type"
        assert result.steps[0].decided_by == "code"
        assert executor.actions[0].source == "code"

    def test_a_type_the_model_chose_is_still_the_models(self):
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}),
                                text_ok=0.95)
        result = run("Type a file name", RunOptions(max_steps=1),
                     observe=Frames(self.field(""), self.field("draft.txt")),
                     execute=Executor(), client=client, settle_ms=5,
                     compose_text=lambda g, e: "draft.txt")
        assert result.steps[0].decided_by == "jev"

    def test_the_clear_and_retry_is_the_codes_too(self):
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}),
                                text_ok=0.12)
        result = run("Type a file name", RunOptions(max_steps=1),
                     observe=Frames(self.field(""), self.field("qqqq")),
                     execute=Executor(), client=client, settle_ms=5,
                     compose_text=lambda g, e: "draft.txt")
        assert result.steps[0].decided_by == "code"

    def test_every_value_the_loop_writes_is_in_the_contract(self):
        from jevskill.cu.contract import DECIDED_BY

        assert "code" in DECIDED_BY


class TestMacros:
    def test_a_hit_skips_the_call_and_is_marked_as_a_macro(self, tmp_path):
        screen = simple()
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        cache.store(GOAL, candidates(screen.elements), None, "e1", "click")
        client = ScriptedClient(response())
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(screen),
                     execute=executor, client=client, settle_ms=1, macros=cache)
        assert client.calls == []
        assert result.decisions == 0 and result.tokens_in == 0
        assert result.steps[0].decided_by == "macro"
        assert executor.actions[0].target == "e1"

    def test_a_macro_is_learned_from_a_step_that_moved_the_screen(self, tmp_path):
        first, second = notepad_frames()
        target = clickable(first)
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        run(GOAL, RunOptions(max_steps=1), observe=Frames(first, second),
            execute=Executor(), client=ScriptedClient(response(target=target)),
            settle_ms=1, macros=cache)
        assert cache.lookup(GOAL, candidates(first.elements), None) == (target,
                                                                        "click")

    def test_nothing_is_learned_from_a_step_that_changed_nothing(self, tmp_path):
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        run(GOAL, RunOptions(max_steps=1), observe=Frames(simple()),
            execute=Executor(), client=ScriptedClient(response()), settle_ms=1,
            macros=cache)
        assert len(cache) == 0

    def test_a_macro_that_stops_working_is_invalidated(self, tmp_path):
        screen = simple()
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        cache.store(GOAL, candidates(screen.elements), None, "e1", "click")
        run(GOAL, RunOptions(max_steps=1), observe=Frames(screen),
            execute=Executor(), client=ScriptedClient(response()), settle_ms=1,
            macros=cache)
        assert len(cache) == 0 and cache.invalidations == 1

    def test_a_macro_still_passes_every_structural_check(self, tmp_path):
        screen = simple()
        cache = MacroCache(tmp_path / "m.json", autosave=False)
        cache.store(GOAL, candidates(screen.elements), None, "e1", "type")
        executor = Executor()
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(screen),
                     execute=executor, client=ScriptedClient(response()),
                     settle_ms=1, macros=cache)
        assert executor.actions == []        # type on a button never happens
        assert result.stop_reason == "escalated"


class TestReporting:
    def test_on_step_sees_every_record_as_it_happens(self):
        seen = []
        run(GOAL, RunOptions(max_steps=2), observe=Frames(simple()),
            execute=Executor(), client=ScriptedClient(response()), settle_ms=1,
            on_step=seen.append)
        assert [record.index for record in seen] == [0, 1]

    def test_a_ledger_row_per_step_with_the_fields_a_report_reads(self, tmp_path):
        first, second = notepad_frames()
        target = clickable(first)
        ledger = Ledger(path=tmp_path / "ledger.jsonl", start_thread=False)
        run(GOAL, RunOptions(max_steps=1), task_id="task-7",
            observe=Frames(first, second), execute=Executor(),
            client=ScriptedClient(response(target=target)), settle_ms=1,
            ledger=ledger)
        ledger.flush()
        rows = [json.loads(line) for line in
                (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["which"] == "act"
        assert row["intent"] == GOAL.casefold()
        assert row["tokens_in"] == 1000
        assert row["cost_usd"] == pytest.approx(4.2e-05)
        assert row["questions"] > 5 and row["state_tokens"] > 0
        assert set(row["stages_ms"]) == {"observe", "reduce", "decide", "validate",
                                         "act", "settle"}
        assert row["latency_ms"] > 0
        assert row["session_id"] == "task-7"
        assert row["confidence"]["target"] > 0
        assert row["extra"]["op"] == "click" and row["extra"]["decided_by"] == "jev"
        assert row["extra"]["executed"] is True
        assert row["extra"]["tree_changed"] is True

    def test_a_ledger_that_fails_does_not_end_the_run(self):
        class Broken:
            def record(self, **fields):
                raise OSError("disk full")

        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(response()),
                     settle_ms=1, ledger=Broken())
        assert result.stop_reason in {"blocked", "max_steps"}

    def test_the_run_serialises_for_the_bench(self):
        result = run(GOAL, RunOptions(max_steps=1), observe=Frames(simple()),
                     execute=Executor(), client=ScriptedClient(response()),
                     settle_ms=1)
        data = result.to_dict()
        json.dumps(data)
        assert data["steps"][0]["op"] == "click"
        assert data["stop_reason"] in {"blocked", "max_steps"}


class TestDryRun:
    def test_a_dry_run_resolves_everything_and_performs_nothing(self):
        screen = simple()
        result = run(GOAL, RunOptions(max_steps=1, dry_run=True),
                     observe=Frames(screen), client=ScriptedClient(response()),
                     settle_ms=1)
        # No executor injected: the real one runs, in dry mode, against the real
        # handles — which is how this package is exercised end to end at all.
        assert result.steps[0].executed is True
        assert result.steps[0].tree_changed is False

    def test_the_text_check_is_skipped_in_a_dry_run(self):
        field = simple([UIElement(id="e3", role="edit", name="File name",
                                  bbox=(10, 60, 200, 28), patterns=("value",),
                                  value="")])
        client = ScriptedClient(response(target="e3", op="type",
                                         probs={"e3": 0.96, "none": 0.04},
                                         nouls={"needs_text": 0.9}))
        run(GOAL, RunOptions(max_steps=1, dry_run=True), observe=Frames(field),
            client=client, settle_ms=1, compose_text=lambda g, e: "draft.txt")
        assert not any("text_ok" in call["questions"] for call in client.calls)


class TestConstants:
    def test_the_loop_publishes_the_limits_it_enforces(self):
        assert loop_mod.CASCADE_CAP == 60
        assert STALL_LIMIT == 2 and ESCALATE_AFTER_INVALID == 2
        assert DONE_WITHOUT_VERIFIER == 2

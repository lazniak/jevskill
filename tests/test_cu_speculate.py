"""Speculative planning, offline: what it predicts, what it refuses, what it costs.

The module's whole claim is "a call you did not have to make", so the tests are
written around the two ways that claim can be false: a prediction applied to the
wrong element (matching by a positional id, an ambiguous name, a control the
name list flagged) and a prediction that *delayed* the step instead of saving
one.

Every element list here is hand-built. `reduce.candidates()` ordering on the big
fixtures is being tuned in a parallel change, and a test of speculation must not
fail when a candidate moves.
"""

from __future__ import annotations

import threading

import pytest

from jevskill.client import Answer, Decisions
from jevskill.cu import speculate as spec_mod
from jevskill.cu.act import Action
from jevskill.cu.contract import RunOptions
from jevskill.cu.decide import OPS
from jevskill.cu.loop import run
from jevskill.cu.speculate import (NEW_ELEMENT, REFUSED_OPS, Speculation,
                                   Speculator, as_decision,
                                   build_speculation_bundle, from_answer,
                                   identity_key, match, speculate)
from jevskill.cu.types import Snapshot, UIElement


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #

def button(element_id, name, **kw):
    kw.setdefault("bbox", (10, 10, 80, 30))
    kw.setdefault("patterns", ("invoke",))
    return UIElement(id=element_id, role="button", name=name, **kw)


def window(elements, title="Editor"):
    return Snapshot(window_title=title, app="editor.exe", pid=1, hwnd=2,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(elements),
                    _handles={el.id: "h:" + el.id for el in elements})


def spec_answer(target, op="click", probs=None, op_probs=None):
    if probs is None:
        probs = {target: 0.93, "none": 0.04, NEW_ELEMENT: 0.03}
    if op_probs is None:
        op_probs = {op: 0.95, "key": 0.05}
    return Decisions(
        answers={
            "next_target": Answer("choice", "next_target",
                                  {"type": "choice", "choice": target,
                                   "confidence": max(probs.values()),
                                   "probabilities": probs}),
            "next_op": Answer("choice", "next_op",
                              {"type": "choice", "choice": op,
                               "confidence": max(op_probs.values()),
                               "probabilities": op_probs}),
        },
        model="jev-1.13.0", request_id="s", provider="typesafe",
        usage={"input_tokens": 400, "cost": 1.68e-05},
        timing_ms={"total_ms": 280.0})


def step_answer(target="e1", op="click", nouls=None):
    """The ordinary step bundle, so a loop test can run without speculation."""
    probs = {target: 0.96, "none": 0.04}
    answers = {
        "target": Answer("choice", "target",
                         {"type": "choice", "choice": target, "confidence": 0.96,
                          "probabilities": probs}),
        "op": Answer("choice", "op",
                     {"type": "choice", "choice": op, "confidence": 0.95,
                      "probabilities": {op: 0.95, "key": 0.05}}),
    }
    values = {"goal_reached": 0.04, "needs_text": 0.05, "is_destructive": 0.2}
    values.update(nouls or {})
    for name, value in values.items():
        answers[name] = Answer("noul", name, {"type": "noul", "noul": value})
    return Decisions(answers=answers, model="jev-1.13.0", request_id="d",
                     provider="typesafe",
                     usage={"input_tokens": 1000, "cost": 4.2e-05},
                     timing_ms={"total_ms": 300.0})


class SplitClient:
    """Answers speculative bundles and step bundles from two separate scripts."""

    def __init__(self, steps=None, specs=None):
        self.steps = list(steps or [])
        self.specs = list(specs or [])
        self.step_calls = 0
        self.spec_calls = 0

    def warm(self, mode=None):
        return 0.0

    def decide(self, state, questions, **kwargs):
        if "next_target" in questions:
            self.spec_calls += 1
            return self.specs.pop(0) if len(self.specs) > 1 else self.specs[0]
        self.step_calls += 1
        return self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]


class BlockingClient:
    """A speculative call that never returns until the test lets it."""

    def __init__(self):
        self.gate = threading.Event()
        self.entered = threading.Event()

    def decide(self, state, questions, **kwargs):
        self.entered.set()
        self.gate.wait(5.0)
        return spec_answer("e1")


# --------------------------------------------------------------------------- #
# the bundle
# --------------------------------------------------------------------------- #

class TestBundle:
    def test_the_choice_carries_every_candidate_plus_two_escape_hatches(self):
        els = [button("e1", "Save"), button("e2", "Cancel")]
        bundle = build_speculation_bundle(els)
        options = set(bundle["next_target"]["criteria"])
        assert options == {"e1", "e2", "none", NEW_ELEMENT}

    def test_new_element_exists_so_a_dialog_is_not_forced_onto_a_wrong_button(self):
        # Without it the Choice has to put its mass on a listed control, and the
        # commonest real answer — "the thing I need appears after this click" —
        # would come back as a confident wrong element.
        bundle = build_speculation_bundle([button("e1", "Save")])
        assert NEW_ELEMENT in bundle["next_target"]["criteria"]
        assert "dialog" in bundle["next_target"]["criteria"][NEW_ELEMENT]["what"]

    def test_the_op_choice_is_the_nine_ops(self):
        bundle = build_speculation_bundle([button("e1", "Save")])
        assert set(bundle["next_op"]["criteria"]) == set(OPS)

    def test_there_are_no_nouls_because_the_screen_does_not_exist_yet(self):
        bundle = build_speculation_bundle([button("e1", "Save")])
        assert all(q["type"] == "choice" for q in bundle.values())
        assert len(bundle) == 2

    def test_the_wording_is_not_the_validated_step_wording(self):
        # act.md §9's numbers belong to `decide.element_criteria`. Sharing the
        # sentence would let this module's hit rate be read as those numbers.
        from jevskill.cu.decide import element_criteria

        el = button("e1", "Save")
        assert spec_mod.next_element_criteria(el) != element_criteria(el)
        assert "last_action" in spec_mod.next_element_criteria(el)["what"]


# --------------------------------------------------------------------------- #
# identity, not position
# --------------------------------------------------------------------------- #

class TestIdentity:
    def test_the_key_is_role_and_name_case_folded(self):
        assert identity_key(button("e1", "Save")) == identity_key(button("e9", "save"))

    def test_a_different_role_is_a_different_control(self):
        a = UIElement(id="e1", role="button", name="Save")
        b = UIElement(id="e1", role="menuitem", name="Save")
        assert identity_key(a) != identity_key(b)

    def test_an_unnamed_control_has_no_identity_and_can_never_match(self):
        nameless = UIElement(id="e1", role="button", bbox=(0, 0, 20, 20))
        assert identity_key(nameless) == ""
        spec = Speculation(target_key="", op="click", confidence=0.9)
        assert match(spec, [nameless]) == (None, "no_target")

    def test_the_automation_id_stands_in_for_a_missing_name(self):
        el = UIElement(id="e1", role="button", automation_id="SaveButton")
        assert identity_key(el) == "button\x1fsavebutton"


class TestMatch:
    def test_the_same_control_at_a_new_position_is_found(self):
        before = [button("e11", "Zapisz jako")]
        after = [button("e31", "Zapisz jako"), button("e32", "Anuluj")]
        spec = from_answer(spec_answer("e11"), before)
        found, reason = match(spec, after)
        assert reason == "ok"
        assert found.id == "e31"

    def test_two_controls_with_one_identity_are_refused_not_guessed(self):
        before = [button("e1", "Save")]
        spec = from_answer(spec_answer("e1"), before)
        twins = [button("e5", "Save"), button("e9", "Save")]
        assert match(spec, twins) == (None, "ambiguous")

    def test_a_control_that_is_gone_reports_absent(self):
        spec = from_answer(spec_answer("e1"), [button("e1", "Save")])
        assert match(spec, [button("e2", "Cancel")]) == (None, "absent")

    def test_predicting_new_element_is_not_a_target(self):
        spec = from_answer(spec_answer(NEW_ELEMENT), [button("e1", "Save")])
        assert spec.target_key is None
        assert match(spec, [button("e1", "Save")]) == (None, "no_target")


class TestDecisionShape:
    def test_the_distribution_is_remapped_not_invented(self):
        before = [button("e1", "Save"), button("e2", "Cancel")]
        spec = from_answer(spec_answer("e1", probs={"e1": 0.8, "e2": 0.15,
                                                    "none": 0.05}), before)
        after = [button("e7", "Cancel"), button("e8", "Save")]
        decision = as_decision(spec, after[1], after)
        assert decision.target == "e8"
        assert decision.probs["e8"] == pytest.approx(0.8)
        assert decision.probs["e7"] == pytest.approx(0.15)
        assert decision.source == "speculation"

    def test_confidence_is_the_min_of_the_two_choices(self):
        before = [button("e1", "Save")]
        answer = spec_answer("e1", probs={"e1": 0.99, "none": 0.01},
                             op_probs={"click": 0.71, "key": 0.29})
        spec = from_answer(answer, before)
        assert spec.confidence == pytest.approx(0.71)

    def test_none_and_new_element_mass_lands_on_none(self):
        before = [button("e1", "Save")]
        spec = from_answer(spec_answer("e1", probs={"e1": 0.7, "none": 0.2,
                                                    NEW_ELEMENT: 0.1}), before)
        decision = as_decision(spec, button("e4", "Save"), [button("e4", "Save")])
        assert decision.probs["none"] == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# the guards
# --------------------------------------------------------------------------- #

class TestGuards:
    def runner(self, answer, over=None, **kw):
        client = SplitClient(specs=[answer])
        runner = Speculator(client, join_ms=2000, **kw)
        runner.start("Save it", over or [button("e1", "Save")],
                     Action(op="click", target="e1"))
        return runner

    def test_a_clean_prediction_is_used(self):
        runner = self.runner(spec_answer("e1"))
        hit = runner.consume("Save it", [button("e4", "Save")])
        assert hit is not None
        assert hit.element.id == "e4"
        assert runner.stats["used"] == 1
        assert runner.hit_rate == pytest.approx(1.0)

    @pytest.mark.parametrize("op", sorted(REFUSED_OPS))
    def test_type_done_and_blocked_are_refused(self, op):
        # None of the three can be justified by a bundle this call never asked:
        # `type` needs `needs_text` and a composed string, `done` and `blocked`
        # are claims about evidence.
        runner = self.runner(spec_answer("e1", op=op))
        assert runner.consume("Save it", [button("e4", "Save")]) is None
        assert runner.last_reason == "refused_op"

    def test_a_target_on_the_destructive_name_list_is_refused(self):
        # The per-element destructive Nouls were never asked, so the one thing
        # speculation may not do is be the cheap path to an irreversible click.
        runner = self.runner(spec_answer("e1"),
                             over=[button("e1", "Delete all documents")])
        delete = button("e4", "Delete all documents")
        assert runner.consume("Save it", [delete], risky_ids=["e4"]) is None
        assert runner.last_reason == "risky"

    def test_a_low_confidence_prediction_is_refused(self):
        answer = spec_answer("e1", probs={"e1": 0.51, "none": 0.49},
                             op_probs={"click": 0.55, "key": 0.45})
        runner = self.runner(answer)
        assert runner.consume("Save it", [button("e4", "Save")]) is None
        assert runner.last_reason == "low_confidence"

    def test_a_narrow_margin_is_refused_the_same_as_a_real_decision(self):
        answer = spec_answer("e1", probs={"e1": 0.48, "e2": 0.45, "none": 0.07})
        runner = self.runner(answer)
        assert runner.consume("Save it", [button("e4", "Save"),
                                          button("e5", "Cancel")]) is None
        assert runner.last_reason == "low_confidence"

    def test_validate_still_runs_on_the_new_candidates(self):
        runner = self.runner(spec_answer("e1"))
        disabled = button("e4", "Save", enabled=False)
        assert runner.consume("Save it", [disabled]) is None
        assert runner.last_reason.startswith("invalid:")

    def test_an_illegal_op_for_the_role_does_not_survive(self):
        runner = self.runner(spec_answer("e1", op="select"))
        assert runner.consume("Save it", [button("e4", "Save")]) is None
        assert runner.last_reason == "invalid:role_op_mismatch"


# --------------------------------------------------------------------------- #
# the budget rule
# --------------------------------------------------------------------------- #

class TestBudget:
    def test_a_pending_prediction_is_abandoned_rather_than_waited_for(self):
        client = BlockingClient()
        runner = Speculator(client)          # join_ms=0: the hot-path default
        runner.start("Save it", [button("e1", "Save")], Action(op="click", target="e1"))
        assert client.entered.wait(5.0)
        assert runner.consume("Save it", [button("e4", "Save")]) is None
        assert runner.last_reason == "pending"
        assert runner.stats["wasted"] == 1
        client.gate.set()

    def test_an_abandoned_leg_is_still_billed(self):
        client = BlockingClient()
        runner = Speculator(client)
        runner.start("Save it", [button("e1", "Save")], Action(op="click", target="e1"))
        assert client.entered.wait(5.0)
        runner.consume("Save it", [button("e4", "Save")])
        client.gate.set()
        for _ in range(200):                       # the worker files it late
            tokens, cost = runner.drain_spend()
            if tokens:
                break
            threading.Event().wait(0.01)
        assert tokens == 400
        assert cost == pytest.approx(1.68e-05)

    def test_a_refused_prediction_is_billed_too(self):
        client = SplitClient(specs=[spec_answer("e1", op="done")])
        runner = Speculator(client, join_ms=2000)
        runner.start("Save it", [button("e1", "Save")], Action(op="click", target="e1"))
        assert runner.consume("Save it", [button("e4", "Save")]) is None
        assert runner.drain_spend() == (400, pytest.approx(1.68e-05))

    def test_starting_a_second_prediction_abandons_the_first(self):
        client = SplitClient(specs=[spec_answer("e1")])
        runner = Speculator(client, join_ms=2000)
        els = [button("e1", "Save")]
        runner.start("Save it", els, Action(op="click", target="e1"))
        runner.take()            # answered, held
        runner.start("Save it", els, Action(op="click", target="e1"))
        assert runner.stats["started"] == 2

    def test_a_client_error_never_escapes_the_thread(self):
        class Broken:
            def decide(self, *a, **kw):
                raise RuntimeError("no")

        runner = Speculator(Broken(), join_ms=2000)
        runner.start("Save it", [button("e1", "Save")], Action(op="click", target="e1"))
        assert runner.consume("Save it", [button("e1", "Save")]) is None
        assert runner.report()["errors"] == 1

    def test_speculate_asks_with_an_unknown_outcome(self):
        # "assuming it succeeded" is the question, not a measured hash
        # comparison; writing "changed" into last_action would be a guess
        # wearing the clothes of a fact.
        seen = {}

        class Recording:
            def decide(self, state, questions, **kw):
                seen.update(state)
                return spec_answer("e1")

        speculate(Recording(), "Save it", [button("e1", "Save")],
                  Action(op="click", target="e1"))
        assert seen["last_action"] == {"type": "click", "target": "e1",
                                       "outcome": None}


# --------------------------------------------------------------------------- #
# in the loop
# --------------------------------------------------------------------------- #

class TestInTheLoop:
    def frames(self):
        first = window([button("e1", "Zapisz jako"), button("e2", "Anuluj")])
        second = window([button("e7", "Anuluj"), button("e8", "Zapisz jako"),
                         button("e9", "Nowy")], title="Editor *")
        return first, second

    def test_the_second_step_costs_no_decide_call(self):
        first, second = self.frames()
        frames = [first, second, second, second, second]
        client = SplitClient(steps=[step_answer("e1")],
                             specs=[spec_answer("e1")])
        runner = Speculator(client, join_ms=2000)
        result = run("Zapisz dokument", RunOptions(max_steps=2),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: _ok(), client=client,
                     speculation=runner, settle_ms=5.0)
        assert [s.decided_by for s in result.steps] == ["jev", "speculation"]
        assert client.step_calls == 1
        assert client.spec_calls == 2      # one per executed step

    def test_the_speculated_step_still_records_what_it_cost(self):
        first, second = self.frames()
        frames = [first, second, second, second, second]
        client = SplitClient(steps=[step_answer("e1")], specs=[spec_answer("e1")])
        runner = Speculator(client, join_ms=2000)
        result = run("Zapisz dokument", RunOptions(max_steps=2),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: _ok(), client=client,
                     speculation=runner, settle_ms=5.0)
        assert result.steps[1].tokens_in >= 400
        # the second step's prediction was abandoned at close(): still billed
        assert result.tokens_in >= 1000 + 400

    def test_no_speculator_means_the_loop_is_what_it_was(self):
        first, second = self.frames()
        frames = [first, second, second, second]
        client = SplitClient(steps=[step_answer("e1")])
        result = run("Zapisz dokument", RunOptions(max_steps=2),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: _ok(), client=client,
                     settle_ms=5.0)
        assert {s.decided_by for s in result.steps} == {"jev"}
        assert client.spec_calls == 0


def _ok():
    from jevskill.cu.act import ActResult

    return ActResult(ok=True, method="fake")

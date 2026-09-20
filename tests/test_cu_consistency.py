"""Self-consistency for irreversible actions, offline.

The rule under test is not "the model said yes". It is: two independent
formulations *and* the negation's complement, agreeing with each other within a
tolerance. So the tests are mostly about the cases where those three come apart
— which is the case the vendor documents as normal
(prompting.md §0 mode 10: `P(x) ≠ 1 − P(¬x)`, measured at 0.72 and 0.47).

No fixture ordering, no hashes: one hand-built button is the whole state.
"""

from __future__ import annotations

import pytest

from jevskill.client import Answer, Decisions
from jevskill.cu.act import ActResult
from jevskill.cu.consistency import (AGREEMENT_FLOOR, NEGATION_TOLERANCE,
                                     Consistency, ConsistencyGate,
                                     build_consistency_bundle, check, read,
                                     restated_destructive_question,
                                     reversible_question, verdict)
from jevskill.cu.contract import RunOptions
from jevskill.cu.decide import destructive_question
from jevskill.cu.loop import run
from jevskill.cu.types import Snapshot, UIElement


def button(element_id, name, **kw):
    kw.setdefault("bbox", (10, 10, 80, 30))
    kw.setdefault("patterns", ("invoke",))
    return UIElement(id=element_id, role="button", name=name, **kw)


def window(elements, title="Editor"):
    return Snapshot(window_title=title, app="editor.exe", pid=1, hwnd=2,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(elements),
                    _handles={el.id: "h:" + el.id for el in elements})


def trio(element_id, direct, restated, reversible, tokens=520, cost=2.18e-05):
    names = {"destructive_%s" % element_id: direct,
             "restated_%s" % element_id: restated,
             "reversible_%s" % element_id: reversible}
    return Decisions(
        answers={n: Answer("noul", n, {"type": "noul", "noul": v})
                 for n, v in names.items()},
        model="jev-1.13.0", request_id="c", provider="typesafe",
        usage={"input_tokens": tokens, "cost": cost},
        timing_ms={"total_ms": 310.0})


def step_answer(target="e1", op="click", nouls=None):
    answers = {
        "target": Answer("choice", "target",
                         {"type": "choice", "choice": target, "confidence": 0.96,
                          "probabilities": {target: 0.96, "none": 0.04}}),
        "op": Answer("choice", "op",
                     {"type": "choice", "choice": op, "confidence": 0.95,
                      "probabilities": {op: 0.95, "key": 0.05}}),
    }
    values = {"goal_reached": 0.04, "needs_text": 0.05, "is_destructive": 0.4,
              "destructive_%s" % target: 0.78}
    values.update(nouls or {})
    for name, value in values.items():
        answers[name] = Answer("noul", name, {"type": "noul", "noul": value})
    return Decisions(answers=answers, model="jev-1.13.0", request_id="d",
                     provider="typesafe",
                     usage={"input_tokens": 900, "cost": 3.8e-05},
                     timing_ms={"total_ms": 300.0})


# --------------------------------------------------------------------------- #
# the bundle
# --------------------------------------------------------------------------- #

class TestBundle:
    def test_three_questions_in_one_call(self):
        bundle = build_consistency_bundle("e4")
        assert set(bundle) == {"destructive_e4", "restated_e4", "reversible_e4"}
        assert all(q["type"] == "noul" for q in bundle.values())

    def test_the_direct_question_is_the_shipped_wording_verbatim(self):
        # Changing it would make this measurement incomparable with act.md §9's
        # 0.78/0.82, which is the whole reason the module exists.
        assert build_consistency_bundle("e4")["destructive_e4"] == destructive_question("e4")

    def test_the_restated_question_reverses_the_criteria_order(self):
        assert list(restated_destructive_question("e4")["criteria"]) == ["false", "true"]
        assert list(destructive_question("e4")["criteria"]) == ["true", "false"]

    def test_every_question_names_the_element_with_a_backtick_path(self):
        # prompting.md §1: the one rule that fails silently when broken.
        for name, question in build_consistency_bundle("e4").items():
            blob = repr(question)
            assert "`elements.e4`" in blob, name

    def test_the_negation_is_asked_about_reversibility_not_destruction(self):
        question = reversible_question("e4")
        assert "reversible" in question["instructions"]["question"]
        assert "restores" in question["criteria"]["true"]


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #

class TestVerdict:
    def test_all_three_agreeing_high_is_destructive(self):
        out = verdict(0.95, 0.93, 0.04)
        assert out.agree == 3
        assert out.destructive
        assert out.unanimous

    def test_all_three_agreeing_low_is_not(self):
        out = verdict(0.08, 0.11, 0.95)
        assert out.agree == 0
        assert not out.destructive
        assert out.unanimous

    def test_two_of_three_is_enough_when_the_negation_agrees(self):
        # the act.md §9 case: the shipped wording under-reads at 0.78, the other
        # two formulations do not, and the gate finally fires.
        out = verdict(0.78, 0.91, 0.12)
        assert out.agree == 2
        assert out.negation_consistent
        assert out.destructive
        assert not out.unanimous

    def test_two_high_answers_with_a_contradicting_negation_are_not_a_consensus(self):
        # 0.95 and 0.92 say "cannot be undone"; the negation says it is 0.90
        # reversible. Three numbers that disagree are not two votes and a
        # rounding error — they are the jagged surface the vendor documents.
        out = verdict(0.95, 0.92, 0.90)
        assert out.agree == 2
        assert not out.negation_consistent
        assert not out.destructive

    def test_the_negation_gap_is_measured_against_the_mean_of_the_two_direct(self):
        near = verdict(0.90, 0.80, 0.30)         # mean 0.85, complement 0.70
        assert near.complement == pytest.approx(0.70)
        assert near.negation_gap == pytest.approx(0.15)
        assert near.negation_consistent
        far = verdict(0.90, 0.80, 0.45)          # mean 0.85, complement 0.55
        assert far.negation_gap == pytest.approx(0.30)
        assert not far.negation_consistent

    def test_spread_is_the_disagreement_number(self):
        out = verdict(0.90, 0.30, 0.50)
        assert out.spread == pytest.approx(0.60)

    def test_the_floor_is_the_same_0_85_the_single_noul_is_gated_on(self):
        from jevskill.cu.decide import THRESHOLDS

        assert AGREEMENT_FLOOR == THRESHOLDS["destructive_noul"]
        assert NEGATION_TOLERANCE == 0.25

    def test_thresholds_are_arguments_so_a_bench_can_sweep_them(self):
        assert verdict(0.78, 0.79, 0.20, floor=0.75).destructive
        assert not verdict(0.78, 0.79, 0.20, floor=0.85, agree_at_least=3).destructive


class TestRead:
    def test_a_full_answer_carries_its_spend(self):
        out = read(trio("e4", 0.91, 0.88, 0.05), "e4")
        assert out.destructive
        assert out.tokens_in == 520
        assert out.cost_usd == pytest.approx(2.18e-05)
        assert out.element_id == "e4"

    def test_a_missing_answer_fails_towards_asking_a_human(self):
        # direct reads 0.0 and reversible reads 1.0 — both push the ruling
        # towards "not proven destructive", which is *not* the same as "safe":
        # the name list is untouched and `validate()` still gates.
        broken = Decisions(answers={}, model="m", request_id="r",
                           usage={"input_tokens": 0, "cost": 0.0})
        out = read(broken, "e4")
        assert out.note == "incomplete answer"
        assert not out.destructive
        assert out.reversible == 1.0

    def test_check_sends_exactly_one_call(self):
        seen = []

        class Recording:
            def decide(self, state, questions, **kw):
                seen.append(questions)
                return trio("e4", 0.9, 0.9, 0.05)

        out = check(Recording(), "Delete it", [button("e4", "Delete")], "e4")
        assert len(seen) == 1
        assert out.agree == 3


class TestGate:
    def test_the_report_counts_disagreements_not_just_verdicts(self):
        class Client:
            def __init__(self):
                self.rows = [trio("e4", 0.95, 0.93, 0.03),
                             trio("e4", 0.95, 0.20, 0.90)]

            def decide(self, state, questions, **kw):
                return self.rows.pop(0)

        gate = ConsistencyGate(Client())
        el = button("e4", "Delete")
        gate("Delete it", [el], el)
        gate("Delete it", [el], el)
        report = gate.report()
        assert report["checks"] == 2
        assert report["disagreements"] == 1
        assert report["disagreement_rate"] == pytest.approx(0.5)
        assert report["destructive"] == 1
        assert report["tokens_in"] == 1040

    def test_a_failing_call_returns_none_rather_than_raising(self):
        class Broken:
            def decide(self, *a, **kw):
                raise RuntimeError("nope")

        gate = ConsistencyGate(Broken())
        el = button("e4", "Delete")
        assert gate("Delete it", [el], el) is None
        assert gate.report()["errors"] == 1


# --------------------------------------------------------------------------- #
# in the loop
# --------------------------------------------------------------------------- #

class TestInTheLoop:
    def frames(self):
        # "Purge archive" is not on act.py's deterministic name list, so
        # `validate()` asks for no confirmation and the single Noul comes back
        # at 0.78 — below the 0.85 gate. This is the whole gap being tested.
        first = window([button("e1", "Purge archive")])
        return [first, first, first, first]

    def test_an_agreeing_trio_raises_a_confirm_the_name_list_never_asked_for(self):
        frames = self.frames()
        client = _Client(step_answer("e1"))
        gate = ConsistencyGate(_Client(trio("e1", 0.78, 0.91, 0.08)))
        asked = []
        result = run("Purge the archive", RunOptions(max_steps=1),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: ActResult(ok=True, method="fake"),
                     client=client, consistency=gate, settle_ms=5.0,
                     confirm=lambda prompt, action, element: asked.append(prompt) or False)
        assert asked, "the consistency hook did not reach confirm"
        assert result.stop_reason == "blocked"
        assert result.destructive_gates == 1
        assert "consistency 2/3" in result.steps[0].note

    def test_a_disagreeing_trio_changes_nothing(self):
        frames = self.frames()
        client = _Client(step_answer("e1"))
        gate = ConsistencyGate(_Client(trio("e1", 0.95, 0.92, 0.90)))
        asked = []
        result = run("Purge the archive", RunOptions(max_steps=1),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: ActResult(ok=True, method="fake"),
                     client=client, consistency=gate, settle_ms=5.0,
                     confirm=lambda prompt, action, element: asked.append(prompt) or False)
        assert asked == []
        assert result.steps[0].executed

    def test_the_hook_can_never_clear_a_confirmation_the_name_list_asked_for(self):
        # "Delete all documents" is on the deterministic list. Even a trio that
        # votes 0/3 leaves `requires_confirm` alone: the list gates, the model
        # only adds.
        listed = window([button("e1", "Delete all documents")])
        frames = [listed, listed, listed, listed]
        gate = ConsistencyGate(_Client(trio("e1", 0.01, 0.02, 0.99)))
        asked = []
        result = run("Delete the documents", RunOptions(max_steps=1),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: ActResult(ok=True, method="fake"),
                     client=_Client(step_answer("e1")), consistency=gate,
                     settle_ms=5.0,
                     confirm=lambda prompt, action, element: asked.append(prompt) or False)
        assert len(asked) == 1
        assert result.stop_reason == "blocked"

    def test_no_hook_means_the_loop_is_what_it_was(self):
        frames = self.frames()
        asked = []
        result = run("Purge the archive", RunOptions(max_steps=1),
                     observe=lambda: frames.pop(0) if len(frames) > 1 else frames[0],
                     execute=lambda a, s, **kw: ActResult(ok=True, method="fake"),
                     client=_Client(step_answer("e1")), settle_ms=5.0,
                     confirm=lambda prompt, action, element: asked.append(prompt) or False)
        assert asked == []
        assert result.steps[0].executed


class _Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def warm(self, mode=None):
        return 0.0

    def decide(self, state, questions, **kwargs):
        self.calls += 1
        return self.answer

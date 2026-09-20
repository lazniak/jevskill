"""The bundle must stay the bundle that was measured, and the checks must bite.

Two different jobs live here and they fail for different reasons:

* **The wording tests** compare :mod:`jevskill.cu.decide` against
  ``bench/act_validate.py`` — the script that sent this bundle to the vendor on
  2026-09-20 and produced the numbers published in ``references/act.md`` §9. If
  a criterion is reworded, those numbers stop describing this code, and that is
  what these assertions are for. They are not style checks.
* **The validation matrix** is the code-side half of act.md §3 and §4. Every row
  is a way a plausible-looking answer gets a run into trouble: an op that is
  illegal for the role, a ``none`` that cannot be clicked, two candidates within
  the margin, a target that vanished while the model was thinking.

Everything is offline. The fake client returns canned :class:`Decisions`, built
the way ``tests/test_hot_client.py`` builds its fixture payloads.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from jevskill.client import Answer, Decisions
from jevskill.cu.decide import (ALLOWED_PATTERNS, ALLOWED_ROLES, COMMAND_ROLES,
                                DESTRUCTIVE_QUESTION_CAP, OPS, THRESHOLDS,
                                Decision, build_bundle, build_state, decide,
                                decide_cascade, destructive_ids,
                                element_criteria, from_decisions, goal_verdict,
                                noul_confidence, noul_margin,
                                text_sanity_question, validate)
from jevskill.cu.reduce import candidates
from jevskill.cu.types import Snapshot, UIElement

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"


@pytest.fixture(scope="module")
def act_validate():
    """The live-validated script, imported by path.

    ``bench/`` is not a package and ``act_validate`` imports ``cu_bench`` as a
    sibling, so the import mirrors ``conftest.py``'s treatment of the bundled
    zero-install script rather than adding a package just for a test.
    """
    for path in (str(REPO), str(REPO / "bench")):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(
        "act_validate_under_test", REPO / "bench" / "act_validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def screen():
    """A small, unambiguous window. Not a fixture reduction: the ranking rules
    in ``reduce`` are being tuned, and a test of *decide* must not fail because
    a candidate moved."""
    return [
        UIElement(id="e1", role="button", name="Save", bbox=(10, 10, 60, 30),
                  patterns=("invoke",)),
        UIElement(id="e2", role="button", name="Delete all documents",
                  bbox=(80, 10, 120, 30), patterns=("invoke",)),
        UIElement(id="e3", role="edit", name="File name", bbox=(10, 60, 200, 28),
                  patterns=("value",), value=""),
        UIElement(id="e4", role="combobox", name="Save as type",
                  bbox=(10, 100, 200, 28), patterns=("expand", "value")),
        UIElement(id="e5", role="button", name="", bbox=(220, 10, 30, 30),
                  automation_id="MoreButton", patterns=("invoke",)),
    ]


def snapshot(title="Untitled - Editor", elements=None):
    elements = screen() if elements is None else elements
    return Snapshot(window_title=title, app="editor.exe", pid=7, hwnd=9,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(elements),
                    _handles={el.id: object() for el in elements})


def answers(target="e1", op="click", probs=None, op_probs=None, nouls=None,
            confidence=None, op_confidence=None):
    """One canned response.

    ``confidence`` is settable independently of ``probabilities`` because the
    provider reports them independently and they do not have to agree: the live
    run in ``bench/cu_decide_results.json`` has ``confidence`` 0.500 against a
    top probability of 0.52. Code that assumed "confidence is the top
    probability" would make the margin check in :func:`validate` unreachable.
    """
    if probs is None:
        probs = {target: 0.97} if target == "none" else {target: 0.97, "none": 0.03}
    if op_probs is None:
        op_probs = {op: 0.95} if op == "key" else {op: 0.95, "key": 0.05}
    out = {
        "target": Answer("choice", "target",
                         {"type": "choice", "choice": target,
                          "confidence": max(probs.values()) if confidence is None
                          else confidence,
                          "probabilities": probs}),
        "op": Answer("choice", "op",
                     {"type": "choice", "choice": op,
                      "confidence": max(op_probs.values()) if op_confidence is None
                      else op_confidence,
                      "probabilities": op_probs}),
    }
    defaults = {"goal_reached": 0.03, "needs_text": 0.2, "is_destructive": 0.9}
    defaults.update(nouls or {})
    for name, value in defaults.items():
        out[name] = Answer("noul", name, {"type": "noul", "noul": value})
    return out


def decisions(**kwargs) -> Decisions:
    return Decisions(answers=answers(**kwargs), model="jev-1.13.0",
                     request_id="req", usage={"input_tokens": 1234, "cost": 5e-05},
                     timing_ms={"total_ms": 300.0}, provider="typesafe")


class FakeClient:
    """Replays canned responses and keeps every request for inspection."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def decide(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions})
        return self.responses.pop(0) if self.responses else decisions()


# --------------------------------------------------------------------------- #
# The wording is the measurement
# --------------------------------------------------------------------------- #

class TestBundleIsTheValidatedBundle:
    def test_the_nine_ops_are_byte_identical_to_the_validated_script(self, act_validate):
        assert OPS == act_validate.OPS

    def test_there_are_exactly_nine_ops(self):
        # jev-ultrafast's eight plus `key`. Splitting `key` into press_enter /
        # press_escape is the indirection failure mode act.md §2 refuses.
        assert len(OPS) == 9
        assert set(OPS) == {"click", "type", "select", "scroll_up", "scroll_down",
                            "key", "wait", "done", "blocked"}

    def test_every_op_says_what_it_is_not_for(self):
        for name, criteria in OPS.items():
            assert criteria["what"] and criteria["not_for"], name

    def test_target_criteria_match_the_validated_template(self, act_validate):
        el = UIElement(id="e11", role="button", name="Save")
        mine = element_criteria(el)
        theirs = act_validate.bundle(
            {"e11": {"role": "button", "name": "Save"}}, [])["target"]["criteria"]["e11"]
        assert mine == theirs

    def test_the_none_option_matches(self, act_validate):
        theirs = act_validate.bundle({}, [])["target"]["criteria"]["none"]
        assert build_bundle([])["target"]["criteria"]["none"] == theirs

    def test_the_fixed_nouls_match(self, act_validate):
        theirs = act_validate.bundle({}, [])
        mine = build_bundle([])
        for name in ("goal_reached", "needs_text", "is_destructive"):
            assert mine[name] == theirs[name], name

    def test_the_per_element_destructive_noul_matches(self, act_validate):
        theirs = act_validate.bundle(
            {"e28": {"role": "button", "name": "Delete all documents"}},
            ["e28"])["destructive_e28"]
        mine = build_bundle([UIElement(id="e28", role="button",
                                       name="Delete all documents")],
                            risky_ids=["e28"])["destructive_e28"]
        assert mine == theirs

    def test_the_instructions_match(self, act_validate):
        theirs = act_validate.bundle({}, [])
        mine = build_bundle([])
        assert mine["target"]["instructions"] == theirs["target"]["instructions"]
        assert mine["op"]["instructions"] == theirs["op"]["instructions"]

    def test_stuck_is_never_asked(self):
        # Measured 0.29-0.60 on screens that had plainly changed; the hash does
        # it exactly, for free. act.md §2's table of what is deliberately absent.
        bundle = build_bundle(screen(), risky_ids=["e2"])
        assert "stuck" not in bundle
        assert not any("chang" in json.dumps(q).lower() and name.startswith("stuck")
                       for name, q in bundle.items())


class TestActMdQuotesTheCodesRoleTable:
    """One table, three places that used to restate it — in three versions.

    act.md §4 said `type` went only into `textbox`/`combobox`. ``textbox`` is
    not a role :data:`jevskill.cu.types.CONTROL_TYPES` can emit, so the rule as
    published forbade typing into anything at all; §8's listing meanwhile
    allowed ``document`` and the code also allowed ``spinner``. The document's
    table is now generated from the constants here, so a change to either one
    fails until act.md is regenerated with it.
    """

    ACT_MD = (REPO / "skills" / "jev" / "references" / "act.md").read_text(
        encoding="utf-8")

    @staticmethod
    def expected_row(op):
        roles = ", ".join("`%s`" % role for role in sorted(ALLOWED_ROLES[op]))
        patterns = ", ".join("`%s`" % p for p in sorted(ALLOWED_PATTERNS[op]))
        return "| `%s` | %s | %s |" % (op, roles, patterns)

    @pytest.mark.parametrize("op", ["click", "type", "select"])
    def test_the_published_row_is_the_code_s_row(self, op):
        assert self.expected_row(op) in self.ACT_MD, (
            "act.md §4 no longer matches ALLOWED_ROLES/ALLOWED_PATTERNS; "
            "the row should read:\n%s" % self.expected_row(op))

    def test_no_rule_or_example_still_names_a_role_the_pipeline_cannot_emit(self):
        """The prose may *name* the superseded value; no rule may still use it.

        `AGENTS.md` asks for the replaced figure to be named rather than
        quietly dropped, so "said `textbox` until 2026-09-20" has to survive.
        What must not is a `"role": "textbox"` in an example or a `textbox` in
        a table row, both of which are read as instructions.
        """
        from jevskill.cu.types import CONTROL_TYPES

        assert "textbox" not in CONTROL_TYPES.values()
        assert '"role": "textbox"' not in self.ACT_MD
        offenders = [line for line in self.ACT_MD.splitlines()
                     if line.startswith("|") and "textbox" in line]
        assert offenders == []

    def test_section_8_imports_the_table_instead_of_restating_it(self):
        listing = self.ACT_MD.split("## 8. The loop")[1].split("```")[1]
        assert "ALLOWED_ROLES" in listing and "ALLOWED_PATTERNS" in listing
        assert 'ALLOWED = {' not in listing

    def test_the_example_state_uses_a_real_role(self):
        example = self.ACT_MD.split("## 1. State schema")[1].split("```")[1]
        from jevskill.cu.types import CONTROL_TYPES

        roles = {line.split('"role": "')[1].split('"')[0]
                 for line in example.splitlines() if '"role": "' in line}
        assert roles and roles <= set(CONTROL_TYPES.values())


class TestBundleShape:
    def test_every_candidate_plus_none_is_an_option(self):
        bundle = build_bundle(screen())
        assert set(bundle["target"]["criteria"]) == {"e1", "e2", "e3", "e4", "e5",
                                                     "none"}

    def test_an_unnamed_control_still_gets_an_option(self):
        # Dropping it would make an icon-only button unreachable; act.md §7 keeps
        # it and escalates when two of them are in the top band.
        criteria = build_bundle(screen())["target"]["criteria"]["e5"]
        assert "MoreButton" in criteria["what"]

    def test_a_nameless_control_with_nothing_at_all_says_so(self):
        el = UIElement(id="e9", role="button")
        assert "unnamed button" in element_criteria(el)["what"]

    def test_destructive_nouls_cover_command_controls_not_only_name_matches(self):
        # act.md §4 claims the Noul catches what the list misses ("Wipe device").
        # With §2's original scope that was impossible: it only asked about
        # elements the list had already flagged.
        bundle = build_bundle(screen(), risky_ids=["e2"])
        asked = {n[len("destructive_"):] for n in bundle if n.startswith("destructive_")}
        assert "e2" in asked          # the name match
        assert "e1" in asked          # a command control the list missed
        assert "e3" not in asked      # an edit box cannot be irreversible

    def test_the_name_match_comes_first_and_the_cap_holds(self):
        many = [UIElement(id="e%d" % i, role="button", name="B%d" % i)
                for i in range(40)]
        many.append(UIElement(id="risky", role="button", name="Delete everything"))
        ids = destructive_ids(many, risky_ids=["risky"])
        assert ids[0] == "risky"
        assert len(ids) == DESTRUCTIVE_QUESTION_CAP

    def test_names_scope_asks_only_about_the_list(self):
        bundle = build_bundle(screen(), risky_ids=["e2"], destructive_scope="names")
        assert [n for n in bundle if n.startswith("destructive_")] == ["destructive_e2"]

    def test_command_roles_are_the_ones_that_can_be_irreversible(self):
        assert COMMAND_ROLES == {"button", "menuitem", "hyperlink", "splitbutton"}


class TestState:
    def test_the_goal_is_the_callers_argument_verbatim(self):
        state = build_state("Save the document", snapshot(), None,
                            elements=screen())
        assert state["goal"] == "Save the document"
        assert state["window_title"] == "Untitled - Editor"
        assert set(state["elements"]) == {"e1", "e2", "e3", "e4", "e5"}

    def test_no_live_handle_ever_reaches_the_state(self):
        state = build_state("g", snapshot(), None, elements=screen())
        json.dumps(state)  # raises TypeError if a COM pointer got in

    def test_last_action_is_normalised_to_the_three_keys(self):
        state = build_state("g", snapshot(), {"type": "click", "target": "e4",
                                              "outcome": "new_window", "junk": 1})
        assert state["last_action"] == {"type": "click", "target": "e4",
                                        "outcome": "new_window"}

    def test_a_missing_last_action_is_three_nulls_not_an_absent_key(self):
        state = build_state("g", snapshot(), None)
        assert state["last_action"] == {"type": None, "target": None, "outcome": None}

    def test_a_state_dict_passes_through(self):
        state = build_state("g", {"window_title": "W", "app": "a.exe",
                                  "elements": {"e1": {"role": "button"}}}, None)
        assert state["elements"] == {"e1": {"role": "button"}}
        assert state["app"] == "a.exe"


class TestNoulArithmetic:
    def test_a_noul_has_no_choice_confidence(self):
        # The live confirmation of why these helpers exist: Decisions.confidence
        # returns None for a noul, and act.md §3 read confidence as universal.
        assert decisions().confidence("goal_reached") is None

    @pytest.mark.parametrize("p,conf,margin", [
        (0.95, 0.95, 0.45), (0.05, 0.95, 0.45), (0.5, 0.5, 0.0), (None, 0.0, 0.0)])
    def test_concentration_and_distance_from_a_coin_flip(self, p, conf, margin):
        assert noul_confidence(p) == pytest.approx(conf)
        assert noul_margin(p) == pytest.approx(margin)


class TestThresholds:
    @pytest.mark.parametrize("name,value", [
        ("floor", 0.60), ("margin", 0.10), ("auto", 0.85), ("needs_text", 0.60),
        ("goal_reached_stop", 0.85), ("goal_reached_contradiction", 0.50),
        ("destructive_noul", 0.85), ("text_sanity", 0.50), ("fits_gate", 0.30)])
    def test_the_table_is_act_mds(self, name, value):
        # Every one of these is published in references/act.md §3-§5. Changing a
        # number here means changing the document in the same commit.
        assert THRESHOLDS[name] == value


# --------------------------------------------------------------------------- #
# The validation matrix
# --------------------------------------------------------------------------- #

class TestValidate:
    def test_a_good_decision_passes(self):
        verdict = validate(from_decisions(decisions()), screen())
        assert verdict.ok and verdict.reason == "ok"
        assert not verdict.requires_confirm

    @pytest.mark.parametrize("kwargs,reason", [
        ({"target": "e99"}, "unknown_target"),
        ({"target": "none", "op": "click"}, "incoherent_none"),
        ({"target": "e1", "op": "type"}, "role_op_mismatch"),
        ({"target": "e1", "op": "select"}, "role_op_mismatch"),
        ({"target": "e1", "op": "done"}, "incoherent_terminal"),
        ({"target": "e1", "op": "blocked"}, "incoherent_terminal"),
        ({"target": "none", "op": "blocked"}, "blocked"),
    ])
    def test_the_invalid_combinations(self, kwargs, reason):
        verdict = validate(from_decisions(decisions(**kwargs)), screen())
        assert not verdict.ok
        assert verdict.reason == reason

    def test_none_with_a_viewport_or_keyboard_op_is_a_normal_step(self):
        for op in ("scroll_down", "scroll_up", "key", "wait"):
            verdict = validate(from_decisions(decisions(target="none", op=op)),
                               screen())
            assert verdict.ok, op

    def test_type_into_a_text_field_is_legal(self):
        verdict = validate(from_decisions(decisions(target="e3", op="type")), screen())
        assert verdict.ok and verdict.needs_text

    def test_select_needs_a_list_or_combo(self):
        assert validate(from_decisions(decisions(target="e4", op="select")),
                        screen()).ok

    def test_the_confidence_floor_is_the_vendors(self):
        low = decisions(op_probs={"click": 0.55, "key": 0.45})
        verdict = validate(from_decisions(low), screen())
        assert verdict.reason == "low_confidence" and verdict.escalate

    def test_two_controls_within_the_margin_do_not_act(self):
        # act.md §3: confidence is concentration, not correctness — two controls
        # named "Save" within ~0.10 of each other means narrow with new detail,
        # never re-roll. The confidence is set above the floor deliberately: the
        # margin has to be its own check or it never runs.
        close = decisions(probs={"e1": 0.40, "e2": 0.34, "none": 0.26},
                          confidence=0.72)
        verdict = validate(from_decisions(close), screen())
        assert verdict.reason == "narrow_margin"

    def test_a_disabled_target_is_a_liveness_failure_not_a_bad_answer(self):
        els = screen()
        els[0] = UIElement(id="e1", role="button", name="Save", enabled=False,
                           bbox=(10, 10, 60, 30), patterns=("invoke",))
        assert validate(from_decisions(decisions()), els).reason == "disabled_target"

    def test_an_icon_button_with_an_automation_id_is_not_nameless(self):
        # act.md §7: fill `name` from AutomationId, tooltip or HelpText first.
        # e5 has one, so it is a normal candidate.
        icon = decisions(target="e5", probs={"e5": 0.9, "none": 0.1})
        assert validate(from_decisions(icon), screen()).ok

    def test_two_nameless_candidates_in_the_top_band_escalate(self):
        # Nothing to put in either option's criteria, so the top-2 margin means
        # nothing: act.md §7 calls that a VLM escalation, not a guess.
        els = screen() + [
            UIElement(id="e6", role="button", bbox=(260, 10, 30, 30),
                      patterns=("invoke",)),
            UIElement(id="e7", role="button", bbox=(300, 10, 30, 30),
                      patterns=("invoke",))]
        nameless = decisions(target="e6", probs={"e6": 0.5, "e7": 0.35, "none": 0.15},
                             confidence=0.8)
        assert validate(from_decisions(nameless), els).reason == "nameless_ambiguous"

    def test_done_is_a_proposal_and_never_an_ok(self):
        verdict = validate(from_decisions(decisions(target="none", op="done")),
                           screen())
        assert not verdict.ok and verdict.stop and verdict.reason == "done_proposed"

    def test_the_name_list_gates_regardless_of_confidence(self):
        # 0.99 on "Delete all documents" still asks the human: the per-element
        # Noul measured 0.76-0.79 for exactly this button
        # (``bench/act_validate_out.json``), so no band earns the right to skip
        # the gate.
        sure = decisions(target="e2", probs={"e2": 0.99, "none": 0.01})
        verdict = validate(from_decisions(sure), screen(), risky_ids=["e2"])
        assert verdict.ok and verdict.requires_confirm

    def test_there_is_no_log_only_flag_to_act_above_0_85_on(self):
        """The rejected policy must not survive as a field nobody reads.

        ``Verdict.log_only`` encoded "act above 0.85 and log it", which is what
        act.md §3 and this module's docstring reject; nothing ever read it, so
        the only thing it could do was mislead the next person who did.
        """
        from dataclasses import fields

        from jevskill.cu.decide import Verdict

        assert "log_only" not in {f.name for f in fields(Verdict)}

    def test_a_command_control_the_question_never_reached_gates(self):
        """act.md §4's gate, applied to silence rather than to a number.

        The bundle caps the per-element Nouls; past the cap the chosen control
        has no measurement, and 0.0 used to be indistinguishable from a
        measured "safe".
        """
        answer = decisions(target="e1")
        answer.answers["destructive_e2"] = Answer(
            "noul", "destructive_e2", {"type": "noul", "noul": 0.10})
        verdict = validate(from_decisions(answer), screen(), risky_ids=[])
        assert verdict.ok and verdict.requires_confirm
        assert "never asked" in verdict.detail

    def test_a_measured_safe_command_control_does_not_gate(self):
        """The counterpart: asked and answered low is evidence, and it passes."""
        answer = decisions(target="e1")
        answer.answers["destructive_e1"] = Answer(
            "noul", "destructive_e1", {"type": "noul", "noul": 0.02})
        verdict = validate(from_decisions(answer), screen(), risky_ids=[])
        assert verdict.ok and not verdict.requires_confirm

    def test_a_macro_replay_is_left_to_the_name_list(self):
        """A path that never carries the Nouls must not gate on their absence.

        Otherwise every macro replay of a button — the whole point of the cache
        — would stop for a human, and the same for a speculation.
        """
        replay = Decision(target="e1", op="click", source="macro")
        verdict = validate(replay, screen(), require_confidence=False)
        assert verdict.ok and not verdict.requires_confirm
        gated = validate(Decision(target="e2", op="click", source="macro"),
                         screen(), require_confidence=False, risky_ids=["e2"])
        assert gated.requires_confirm

    def test_a_high_per_element_noul_gates_a_name_the_list_missed(self):
        answer = decisions(target="e1")
        answer.answers["destructive_e1"] = Answer(
            "noul", "destructive_e1", {"type": "noul", "noul": 0.93})
        verdict = validate(from_decisions(answer), screen(), risky_ids=[])
        assert verdict.requires_confirm

    def test_a_macro_replay_skips_the_floor_but_nothing_else(self):
        replay = Decision(target="e1", op="click", source="macro")
        assert validate(replay, screen(), require_confidence=False).ok
        assert validate(replay, screen()).reason == "low_confidence"
        bad = Decision(target="e1", op="type", source="macro")
        assert validate(bad, screen(), require_confidence=False).reason == "role_op_mismatch"


class TestGoalVerdict:
    @pytest.mark.parametrize("op,goal_reached,verified,expected", [
        ("done", 0.95, True, "stop"),
        ("done", 0.95, False, "escalate"),
        ("done", 0.10, False, "blocked"),
        ("done", 0.70, False, "escalate"),
        ("done", 0.95, None, "escalate"),
        ("click", 0.95, True, "stop"),
        ("click", 0.95, False, "continue"),
        ("click", 0.10, None, "continue"),
    ])
    def test_act_mds_disagreement_table(self, op, goal_reached, verified, expected):
        decision = Decision(target="none" if op == "done" else "e1", op=op,
                            goal_reached=goal_reached)
        assert goal_verdict(decision, verified) == expected


# --------------------------------------------------------------------------- #
# One call, and the cascade above the cap
# --------------------------------------------------------------------------- #

class TestDecide:
    def test_one_call_carries_every_question(self):
        client = FakeClient(decisions())
        decision = decide(client, "Save it", screen(), None, risky_ids=["e2"])
        assert len(client.calls) == 1
        assert decision.target == "e1" and decision.op == "click"
        assert decision.confidence == pytest.approx(0.95)   # min(target, op)
        assert decision.margin == pytest.approx(0.94)       # top1 - top2
        assert decision.tokens_in == 1234
        assert decision.questions == len(client.calls[0]["questions"])
        assert decision.scope_ids == ["e1", "e2", "e3", "e4", "e5"]

    def test_confidence_is_the_minimum_not_the_product(self):
        decision = from_decisions(decisions(probs={"e1": 0.9, "none": 0.1},
                                            op_probs={"click": 0.7, "key": 0.3}))
        assert decision.confidence == pytest.approx(0.7)

    def test_the_three_nouls_come_back_raw(self):
        decision = from_decisions(decisions(nouls={"goal_reached": 0.42}))
        assert decision.goal_reached == pytest.approx(0.42)

    def test_per_element_destructive_answers_are_collected(self):
        answer = decisions()
        answer.answers["destructive_e2"] = Answer("noul", "destructive_e2",
                                                  {"type": "noul", "noul": 0.79})
        decision = from_decisions(answer)
        assert decision.destructive_for("e2") == pytest.approx(0.79)

    def test_an_unasked_id_reads_none_and_never_zero(self):
        """0.0 is what a measured "safe" looks like; silence must not share it.

        ``DESTRUCTIVE_QUESTION_CAP`` guarantees the case: a screen with more
        command controls than the cap can have its chosen target fall outside
        the questions entirely.
        """
        answer = decisions()
        answer.answers["destructive_e2"] = Answer("noul", "destructive_e2",
                                                  {"type": "noul", "noul": 0.79})
        decision = from_decisions(answer)
        assert decision.destructive_for("e1") is None
        assert decision.asked_about("e1") is False
        assert decision.asked_about("e2") is True
        assert decision.asked_destructive == frozenset({"e2"})

    def test_a_decision_that_never_carried_the_question_says_so(self):
        """``None`` (macro, speculation) is not ``frozenset()`` (asked nothing)."""
        assert Decision(target="e1", op="click").asked_destructive is None
        assert from_decisions(decisions()).asked_destructive == frozenset()


class TestCascade:
    """Above the cap: region first, element second, both rejection points kept.

    Asserted structurally — which region wins depends on the reduction's
    ranking, and that ranking is being tuned in another change.
    """

    def region_answer(self, region="r0", applies=0.96):
        return Decisions(
            answers={"region": Answer("choice", "region",
                                      {"type": "choice", "choice": region,
                                       "confidence": 1.0,
                                       "probabilities": {region: 1.0}}),
                     "any_region_applies": Answer("noul", "any_region_applies",
                                                  {"type": "noul", "noul": applies})},
            model="m", request_id="r", usage={"input_tokens": 900, "cost": 4e-05})

    def load_big(self):
        data = json.loads((FIXTURES / "synthetic_500.json").read_text(encoding="utf-8"))
        return Snapshot.from_dict(data["snapshot"])

    def test_the_screen_is_over_the_cap_to_begin_with(self):
        assert len(candidates(self.load_big().elements)) == 60

    def test_stage_one_chooses_a_region_and_stage_two_stays_inside_it(self):
        big = self.load_big()
        from jevskill.cu.reduce import region_state

        first_region = sorted(region_state(big.elements, 60)["regions"])[0]
        members = region_state(big.elements, 60)["regions"][first_region]["members"]
        client = FakeClient(self.region_answer(first_region),
                            decisions(target=members[0]))
        decision = decide_cascade(client, "Save it", big.elements, None)
        assert len(client.calls) == 2
        stage1, stage2 = client.calls
        assert "regions" in stage1["state"] and "elements" not in stage1["state"]
        assert set(stage1["questions"]) == {"region", "any_region_applies"}
        assert decision.region == first_region
        assert set(decision.scope_ids).issubset(set(members))
        assert set(stage2["state"]["elements"]) == set(decision.scope_ids)

    def test_region_ids_are_region_states_r0_rn(self):
        big = self.load_big()
        client = FakeClient(self.region_answer("r0"), decisions(target="e0"))
        decide_cascade(client, "Save it", big.elements, None)
        assert all(key.startswith("r") and key[1:].isdigit()
                   for key in client.calls[0]["state"]["regions"])

    def test_stage_two_carries_a_per_candidate_rejection_point(self):
        big = self.load_big()
        from jevskill.cu.reduce import region_state

        region = sorted(region_state(big.elements, 60)["regions"])[0]
        client = FakeClient(self.region_answer(region), decisions(target="e0"))
        decide_cascade(client, "Save it", big.elements, None)
        assert any(name.startswith("fits_") for name in client.calls[1]["questions"])

    def test_the_gating_noul_stops_before_stage_two_is_paid_for(self):
        big = self.load_big()
        client = FakeClient(self.region_answer("r0", applies=0.12))
        decision = decide_cascade(client, "Save it", big.elements, None)
        assert len(client.calls) == 1        # stage 2 never happened
        assert decision.target is None and decision.source == "cascade"
        assert decision.tokens_in == 900

    def test_a_none_region_also_stops(self):
        big = self.load_big()
        client = FakeClient(self.region_answer("none"))
        assert decide_cascade(client, "Save it", big.elements, None).target is None

    def test_both_stages_are_billed_to_the_step(self):
        big = self.load_big()
        from jevskill.cu.reduce import region_state

        region = sorted(region_state(big.elements, 60)["regions"])[0]
        client = FakeClient(self.region_answer(region), decisions(target="e0"))
        decision = decide_cascade(client, "Save it", big.elements, None)
        assert decision.tokens_in == 900 + 1234
        assert decision.cost_usd == pytest.approx(4e-05 + 5e-05)


class TestTextSanity:
    def test_the_question_names_the_field_with_a_backtick_path(self):
        question = text_sanity_question("e3")["text_ok"]
        assert "`elements.e3.value`" in question["instructions"]["question"]
        assert "`elements.e3.value`" in question["criteria"]["true"]
        assert "`elements.e3.value`" in question["criteria"]["false"]

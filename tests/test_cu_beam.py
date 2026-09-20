"""Beam K=2 over the cascade, offline.

The greedy cascade's failure is structural: stage 1 commits to a region on three
sample names and a count, and stage 2 never sees the alternative. These tests
check that keeping the runner-up actually changes what can be chosen, that it is
only paid for when stage 1 was close, and that both rejection points survive.

Nothing here is keyed on ``r0`` or ``e7``: the fake client reads the region and
control *names* out of the state it is handed. `reduce.regions()` ordering is
being tuned in a parallel change and a test of the beam must not depend on it.
"""

from __future__ import annotations

import pytest

from jevskill.client import Answer, Decisions
from jevskill.cu.beam import BEAM_MARGIN, MAX_K, decide_beam, top_regions
from jevskill.cu.decide import decide_cascade
from jevskill.cu.types import Snapshot, UIElement


# --------------------------------------------------------------------------- #
# a synthetic screen with two clearly separate regions
# --------------------------------------------------------------------------- #

def screen():
    """A window holding two named groups, each with its own buttons.

    `regions()` groups on the nearest *named* ancestor with a region role, so
    two named groups are two regions whatever the candidate ranking does.
    """
    els = [UIElement(id="w", role="window", name="Editor", bbox=(0, 0, 900, 600)),
           UIElement(id="gA", role="group", name="Toolbar", parent="w",
                     bbox=(0, 0, 900, 60), depth=1),
           UIElement(id="gB", role="group", name="Save dialog", parent="w",
                     bbox=(200, 200, 400, 300), depth=1)]
    for index, name in enumerate(("Bold", "Italic", "Underline")):
        els.append(UIElement(id="a%d" % index, role="button", name=name,
                             parent="gA", depth=2, patterns=("invoke",),
                             bbox=(10 + 40 * index, 10, 36, 36)))
    for index, name in enumerate(("Save", "Discard", "Cancel")):
        els.append(UIElement(id="b%d" % index, role="button", name=name,
                             parent="gB", depth=2, patterns=("invoke",),
                             bbox=(210 + 90 * index, 420, 80, 30)))
    return els


def snapshot(els):
    return Snapshot(window_title="Editor", app="editor.exe", pid=1, hwnd=2,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(els),
                    _handles={el.id: "h:" + el.id for el in els})


#: Stage 1 cannot tell the two groups apart: both expand.
CLOSE = {"Toolbar": 0.46, "Save dialog": 0.42}
#: Stage 1 is certain: only the leader expands.
DECISIVE = {"Toolbar": 0.90, "Save dialog": 0.05}


def region_reply(ordered, *, any_applies=0.95):
    """A stage-1 answer over whatever region ids the bundle actually carried."""
    probabilities = dict(ordered)
    top = max(probabilities, key=lambda r: probabilities[r])
    return Decisions(
        answers={
            "region": Answer("choice", "region",
                             {"type": "choice", "choice": top,
                              "confidence": probabilities[top],
                              "probabilities": probabilities}),
            "any_region_applies": Answer("noul", "any_region_applies",
                                         {"type": "noul", "noul": any_applies}),
        },
        model="jev-1.13.0", request_id="r1", provider="typesafe",
        usage={"input_tokens": 300, "cost": 1.26e-05},
        timing_ms={"total_ms": 250.0})


def element_reply(probabilities, op="click"):
    top = max(probabilities, key=lambda k: probabilities[k])
    answers = {
        "target": Answer("choice", "target",
                         {"type": "choice", "choice": top,
                          "confidence": probabilities[top],
                          "probabilities": probabilities}),
        "op": Answer("choice", "op",
                     {"type": "choice", "choice": op, "confidence": 0.95,
                      "probabilities": {op: 0.95, "key": 0.05}}),
    }
    for name, value in (("goal_reached", 0.03), ("needs_text", 0.05),
                        ("is_destructive", 0.2)):
        answers[name] = Answer("noul", name, {"type": "noul", "noul": value})
    return Decisions(answers=answers, model="jev-1.13.0", request_id="r2",
                     provider="typesafe",
                     usage={"input_tokens": 700, "cost": 2.94e-05},
                     timing_ms={"total_ms": 300.0})


class CascadeClient:
    """Answers stage 1 from region *names*, stage 2 from control *names*.

    Both maps are keyed on names rather than on ``r0``/``e7``, because how
    `reduce` numbers and orders things is being tuned in a parallel change and
    none of these assertions is about that.
    """

    def __init__(self, regions, names, any_applies=0.95):
        self.regions = dict(regions)
        self.names = dict(names)
        self.any_applies = any_applies
        self.region_bundles = []
        self.element_rounds = []

    def decide(self, state, questions, **kwargs):
        if "region" in questions:
            ids = [r for r in questions["region"]["criteria"] if r != "none"]
            self.region_bundles.append(ids)
            ordered = {r: self.regions.get(state["regions"][r]["name"], 0.01)
                       for r in ids}
            return region_reply(ordered, any_applies=self.any_applies)
        offered = {element_id: state["elements"][element_id]["name"]
                   for element_id in questions["target"]["criteria"]
                   if element_id != "none"}
        self.element_rounds.append(sorted(offered.values()))
        probabilities = {element_id: self.names.get(name, 0.02)
                         for element_id, name in offered.items()}
        probabilities["none"] = 0.01
        return element_reply(probabilities)


# --------------------------------------------------------------------------- #
# the margin rule
# --------------------------------------------------------------------------- #

class TestTopRegions:
    def test_a_decisive_stage_one_expands_one_region(self):
        assert top_regions({"r0": 0.80, "r1": 0.15}, k=2) == [("r0", 0.80)]

    def test_a_close_runner_up_is_kept(self):
        picked = top_regions({"r0": 0.45, "r1": 0.40, "r2": 0.10}, k=2)
        assert [r for r, _ in picked] == ["r0", "r1"]

    def test_the_margin_is_the_knob(self):
        probabilities = {"r0": 0.50, "r1": 0.28}
        assert len(top_regions(probabilities, k=2, margin=0.10)) == 1
        assert len(top_regions(probabilities, k=2, margin=0.30)) == 2

    def test_none_is_never_expanded(self):
        # It is stage 1's rejection point, not a place controls live.
        picked = top_regions({"none": 0.60, "r0": 0.30, "r1": 0.10}, k=2,
                             margin=0.9)
        assert [r for r, _ in picked] == ["r0", "r1"]

    def test_k_is_capped_because_three_calls_lose_to_not_cascading(self):
        picked = top_regions({"r0": 0.3, "r1": 0.3, "r2": 0.3, "r3": 0.1},
                             k=9, margin=0.9)
        assert len(picked) == MAX_K

    def test_k_of_one_is_the_greedy_cascade(self):
        assert top_regions({"r0": 0.4, "r1": 0.39}, k=1) == [("r0", 0.4)]

    def test_the_published_margin_is_the_plan_s_number(self):
        assert BEAM_MARGIN == 0.25


# --------------------------------------------------------------------------- #
# the two element rounds
# --------------------------------------------------------------------------- #

class TestDecideBeam:
    def test_a_close_stage_one_pays_for_two_element_rounds(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Bold": 0.9, "Save": 0.9})
        decision = decide_beam(client, "Save the document", els,
                               cap=10, k=2, snapshot=snapshot(els))
        assert len(client.element_rounds) == 2
        assert decision.source == "beam"
        assert decision.timing["branches"] == 2

    def test_a_decisive_stage_one_pays_for_one(self):
        els = screen()
        client = CascadeClient(DECISIVE, {"Bold": 0.9, "Save": 0.9})
        decide_beam(client, "Save the document", els, cap=10, k=2,
                    snapshot=snapshot(els))
        assert len(client.element_rounds) == 1

    def test_the_winner_is_the_product_not_the_leading_region(self):
        # Stage 1 slightly prefers the first region; the second region holds a
        # control the element round is far more certain about. Greedy would have
        # taken the first. P(region) x P(element) takes the second.
        els = screen()
        client = CascadeClient(CLOSE,
                               {"Bold": 0.40, "Italic": 0.30, "Underline": 0.28,
                                "Save": 0.97, "Discard": 0.01, "Cancel": 0.01})
        decision = decide_beam(client, "Save the document", els, cap=10, k=2,
                               snapshot=snapshot(els))
        chosen = {el.id: el.name for el in els}[decision.target]
        assert chosen == "Save"
        assert "greedy would have taken" in decision.note

    def test_the_spend_of_every_branch_is_carried(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Bold": 0.9, "Save": 0.9})
        decision = decide_beam(client, "Save the document", els, cap=10, k=2,
                               snapshot=snapshot(els))
        assert decision.tokens_in == 300 + 700 + 700
        assert decision.cost_usd == pytest.approx(1.26e-05 + 2 * 2.94e-05)

    def test_stage_one_can_still_reject_the_whole_screen(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Save": 0.9}, any_applies=0.2)
        decision = decide_beam(client, "Print the document", els, cap=10, k=2,
                               snapshot=snapshot(els))
        assert decision.target is None
        assert client.element_rounds == []
        assert "no region holds the control" in decision.note

    def test_scope_ids_are_the_winning_branch_so_validate_runs_on_it(self):
        els = screen()
        client = CascadeClient(CLOSE,
                               {"Bold": 0.3, "Save": 0.97})
        decision = decide_beam(client, "Save the document", els, cap=10, k=2,
                               snapshot=snapshot(els))
        assert decision.target in decision.scope_ids


class TestFused:
    def test_one_element_round_over_the_union(self):
        els = screen()
        client = CascadeClient(CLOSE,
                               {"Bold": 0.3, "Italic": 0.05, "Underline": 0.05,
                                "Save": 0.5, "Discard": 0.05, "Cancel": 0.05})
        decision = decide_beam(client, "Save the document", els, cap=10, k=2,
                               mode="fused", snapshot=snapshot(els))
        assert len(client.element_rounds) == 1
        assert len(client.element_rounds[0]) == 6      # both regions' members
        assert decision.timing["mode"] == "fused"

    def test_the_union_is_re_ranked_by_the_product(self):
        els = screen()
        # The raw Choice prefers Bold; weighting by region flips it to Save.
        client = CascadeClient({"Toolbar": 0.35, "Save dialog": 0.50},
                               {"Bold": 0.40, "Save": 0.35})
        decision = decide_beam(client, "Save the document", els, cap=10, k=2,
                               mode="fused", snapshot=snapshot(els))
        chosen = {el.id: el.name for el in els}[decision.target]
        assert chosen == "Save"
        assert "the raw choice was" in decision.note

    def test_fused_costs_one_stage_two_instead_of_two(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Bold": 0.4, "Save": 0.5})
        fused = decide_beam(client, "Save the document", els, cap=10, k=2,
                            mode="fused", snapshot=snapshot(els))
        client2 = CascadeClient(CLOSE, {"Bold": 0.4, "Save": 0.5})
        calls = decide_beam(client2, "Save the document", els, cap=10, k=2,
                            mode="calls", snapshot=snapshot(els))
        assert fused.tokens_in < calls.tokens_in


# --------------------------------------------------------------------------- #
# the hook on decide_cascade
# --------------------------------------------------------------------------- #

class TestCascadeHook:
    def test_the_default_is_the_greedy_cascade_untouched(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Bold": 0.9, "Save": 0.9})
        decision = decide_cascade(client, "Save the document", els, cap=10,
                                  snapshot=snapshot(els))
        assert decision.source == "cascade"
        assert len(client.element_rounds) == 1

    def test_beam_k_two_routes_to_the_beam(self):
        els = screen()
        client = CascadeClient(CLOSE, {"Bold": 0.9, "Save": 0.9})
        decision = decide_cascade(client, "Save the document", els, cap=10,
                                  snapshot=snapshot(els), beam_k=2)
        assert decision.source == "beam"
        assert len(client.element_rounds) == 2

    def test_the_margin_can_be_overridden_through_the_cascade(self):
        els = screen()
        client = CascadeClient({"Toolbar": 0.50, "Save dialog": 0.28}, {"Bold": 0.9, "Save": 0.9})
        decide_cascade(client, "Save the document", els, cap=10,
                       snapshot=snapshot(els), beam_k=2, beam_margin=0.05)
        assert len(client.element_rounds) == 1

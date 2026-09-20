"""The reduction must be deterministic, cheap, and lossless where it matters.

Offline: every case here runs from a JSON fixture, including the two real
snapshots taken from Notepad and Calculator by ``bench/cu_observe_bench.py``.
Nothing in this file needs Windows, COM, or a network.

The property under test is not "the filter returns something plausible" but
"the filter returns the same thing twice and never returns a control that
cannot be clicked" — a computer-use loop that clicks a 2x2 layout artefact
fails in a way no assertion downstream will catch.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from jevskill.cu import (candidates, dedupe, interactive, prioritise,
                         region_state, regions, visible_enabled)
from jevskill.cu.reduce import ROW_TOLERANCE_PX
from jevskill.cu.types import MIN_SIDE_PX, Snapshot, UIElement

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"
SYNTHETIC = ("synthetic_50", "synthetic_500", "synthetic_2000")
REAL = ("notepad", "calculator")


def load(name: str) -> Snapshot:
    data = json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))
    return Snapshot.from_dict(data["snapshot"])


def screen() -> list:
    """A small hand-built window: a toolbar, a form, and a modal on top."""
    return [
        UIElement(id="e0", role="window", name="Editor", bbox=(0, 0, 800, 600)),
        UIElement(id="e1", role="toolbar", name="Main toolbar",
                  bbox=(0, 0, 800, 40), parent="e0", region="e0", depth=1),
        UIElement(id="e2", role="button", name="Save", bbox=(200, 4, 60, 32),
                  parent="e1", region="e1", depth=2, patterns=("invoke",)),
        UIElement(id="e3", role="button", name="Save", bbox=(10, 4, 60, 32),
                  parent="e1", region="e1", depth=2, patterns=("invoke",)),
        UIElement(id="e4", role="button", name="Disabled", bbox=(80, 4, 60, 32),
                  parent="e1", region="e1", depth=2, enabled=False,
                  patterns=("invoke",)),
        UIElement(id="e5", role="button", name="Scrolled away",
                  bbox=(600, 4, 60, 32), parent="e1", region="e1", depth=2,
                  offscreen=True, patterns=("invoke",)),
        UIElement(id="e6", role="separator", name="", bbox=(140, 4, 2, 32),
                  parent="e1", region="e1", depth=2),
        UIElement(id="e7", role="pane", name="Form", bbox=(0, 40, 800, 460),
                  parent="e0", region="e0", depth=1),
        UIElement(id="e8", role="edit", name="File name", bbox=(10, 60, 300, 28),
                  parent="e7", region="e7", depth=2, patterns=("value",),
                  value="draft.txt"),
        UIElement(id="e9", role="text", name="Label only", bbox=(10, 100, 300, 20),
                  parent="e7", region="e7", depth=2),
        UIElement(id="e10", role="dialog", name="Replace file?",
                  bbox=(200, 200, 400, 200), parent="e0", region="e0", depth=1),
        UIElement(id="e11", role="group", name="", bbox=(210, 300, 380, 60),
                  parent="e10", region="e10", depth=2),
        UIElement(id="e12", role="button", name="Replace", bbox=(380, 320, 90, 32),
                  parent="e11", region="e11", depth=3, patterns=("invoke",),
                  focused=True),
        UIElement(id="e13", role="button", name="Cancel", bbox=(480, 320, 90, 32),
                  parent="e11", region="e11", depth=3, patterns=("invoke",)),
    ]


class TestFilters:
    def test_visible_enabled_drops_what_cannot_be_clicked(self):
        kept = {el.id for el in visible_enabled(screen())}
        assert "e4" not in kept, "a disabled control survived"
        assert "e5" not in kept, "an offscreen control survived"
        assert "e6" not in kept, "a 2 px separator survived"
        assert {"e2", "e3", "e8", "e12", "e13"} <= kept

    def test_min_side_is_the_constant_not_a_literal(self):
        wide = UIElement(id="x", role="button", name="ok",
                         bbox=(0, 0, 100, MIN_SIDE_PX))
        thin = replace(wide, bbox=(0, 0, 100, MIN_SIDE_PX - 1))
        assert visible_enabled([wide]) == [wide]
        assert visible_enabled([thin]) == []

    def test_interactive_accepts_role_or_pattern(self):
        kept = {el.id for el in interactive(screen())}
        assert "e9" not in kept, "a static label is not a target"
        assert "e8" in kept, "an edit box is a target by role"
        pane_that_acts = UIElement(id="p", role="pane", name="Really a button",
                                   bbox=(0, 0, 40, 40), patterns=("invoke",))
        assert interactive([pane_that_acts]) == [pane_that_acts], (
            "WinUI ships buttons typed as panes; the pattern has to count")


class TestDedupe:
    def test_collapses_and_counts(self):
        kept = dedupe(interactive(visible_enabled(screen())))
        saves = [el for el in kept if el.name == "Save"]
        assert len(saves) == 1
        assert saves[0].duplicates == 1

    def test_keeps_the_one_nearest_focus(self):
        els = interactive(visible_enabled(screen()))
        near_left = dedupe(els, focus=(20, 20))
        near_right = dedupe(els, focus=(780, 20))
        assert [el.id for el in near_left if el.name == "Save"] == ["e3"]
        assert [el.id for el in near_right if el.name == "Save"] == ["e2"]

    def test_is_deterministic_without_focus(self):
        els = interactive(visible_enabled(screen()))
        assert [el.id for el in dedupe(els)] == [el.id for el in dedupe(els)]

    def test_does_not_mutate_its_input(self):
        els = interactive(visible_enabled(screen()))
        before = [el.duplicates for el in els]
        dedupe(els, focus=(20, 20))
        assert [el.duplicates for el in els] == before, (
            "dedupe wrote the duplicate count into the caller's snapshot")

    def test_same_name_in_two_regions_is_not_a_duplicate(self):
        els = [
            UIElement(id="a", role="button", name="Close", bbox=(0, 0, 40, 40),
                      region="r1", patterns=("invoke",)),
            UIElement(id="b", role="button", name="Close", bbox=(0, 60, 40, 40),
                      region="r2", patterns=("invoke",)),
        ]
        assert len(dedupe(els)) == 2


class TestPriority:
    def test_focus_first_then_dialog_then_the_rest(self):
        order = [el.id for el in prioritise(
            interactive(visible_enabled(screen())), "e12")]
        assert order[0] == "e12", "the focused control must come first"
        assert order[1] == "e13", "the rest of the modal comes next"
        assert order.index("e13") < order.index("e2")

    def test_reading_order_within_a_rank(self):
        row = [UIElement(id="r%d" % i, role="button", name="b%d" % i,
                         bbox=(left, 100 + jitter, 40, 30), patterns=("invoke",))
               for i, (left, jitter) in enumerate(
                   ((300, 0), (100, ROW_TOLERANCE_PX - 1), (200, 0)))]
        below = UIElement(id="below", role="button", name="b",
                          bbox=(0, 400, 40, 30), patterns=("invoke",))
        order = [el.id for el in prioritise(row + [below], None)]
        assert order == ["r1", "r2", "r0", "below"], (
            "a row must read left to right even when tops differ slightly")

    def test_nested_modal_content_still_ranks_as_dialog(self):
        deep = UIElement(id="deep", role="button", name="Deep",
                         bbox=(220, 360, 40, 30), parent="e11", region="e11",
                         depth=4, patterns=("invoke",))
        order = [el.id for el in prioritise(
            interactive(visible_enabled(screen() + [deep])), "e12")]
        assert order.index("deep") < order.index("e2")


class TestCandidates:
    def test_pipeline_result(self):
        picked = candidates(screen())
        ids = [el.id for el in picked]
        assert ids[0] == "e12"
        assert "e4" not in ids and "e5" not in ids and "e9" not in ids
        assert len(ids) == len(set(ids))

    def test_cap_is_honoured(self):
        snap = load("synthetic_2000")
        assert len(candidates(snap.elements, cap=17)) == 17

    def test_is_deterministic(self):
        snap = load("synthetic_500")
        first = [el.id for el in candidates(snap.elements)]
        second = [el.id for el in candidates(snap.elements)]
        assert first == second

    @pytest.mark.parametrize("name", SYNTHETIC + REAL)
    def test_every_candidate_is_actionable(self, name):
        for el in candidates(load(name).elements):
            assert el.enabled and not el.offscreen and el.is_sized()

    @pytest.mark.parametrize("name", REAL)
    def test_real_windows_reduce_to_a_short_list(self, name):
        snap = load(name)
        picked = candidates(snap.elements)
        assert 0 < len(picked) <= 60
        assert len(picked) < len(snap.elements)


class TestRegions:
    def test_groups_under_the_nearest_named_ancestor(self):
        grouped = {r.id: r for r in regions(screen())}
        assert grouped["e1"].name == "Main toolbar"
        assert "e2" in grouped["e1"].members
        # e11 is an unnamed group inside the modal: the named dialog owns it.
        assert "e12" in grouped["e10"].members

    def test_no_empty_regions(self):
        for name in SYNTHETIC + REAL:
            assert all(r.members for r in regions(load(name).elements))

    def test_region_state_shape(self):
        snap = load("synthetic_2000")
        state = region_state(snap.elements)
        assert set(state) == {"regions"}
        assert state["regions"], "a 2,000 node tree must produce regions"
        for row in state["regions"].values():
            assert row["name"] and row["members"]
            assert row["count"] >= len(row["members"]) or row["count"] > 0

    def test_cascade_is_itself_capped(self):
        """71 regions would be as unanswerable as 300 elements."""
        snap = load("synthetic_2000")
        assert len(regions(snap.elements)) > 60, "fixture no longer stresses this"
        assert len(region_state(snap.elements, cap=60)["regions"]) <= 60

    def test_cascade_reaches_past_the_flat_cap(self):
        snap = load("synthetic_2000")
        state = region_state(snap.elements, cap=60)
        reachable = sum(len(row["members"]) for row in state["regions"].values())
        assert reachable > len(candidates(snap.elements, cap=60)), (
            "the cascade exists to reach controls the flat cap truncated")

    def test_the_focused_region_survives_the_cap(self):
        snap = load("synthetic_2000")
        focus = next(el for el in snap.elements if el.focused)
        state = region_state(snap.elements, cap=3)
        assert any(focus.id in row["members"] for row in state["regions"].values())


class TestCost:
    def test_five_hundred_nodes_reduce_fast(self):
        """Target is <= 2 ms; the assertion is loose on purpose.

        CI runners are shared and a strict bound here would fail for reasons
        that have nothing to do with this code. The real number is measured by
        ``bench/cu_observe_bench.py`` (``reduce_ms_median``: 0.046-0.095 ms on
        the two real windows) — this test guards the order of magnitude.
        """
        elements = load("synthetic_500").elements
        candidates(elements)  # warm any import-time cost
        best = min(_time(lambda: candidates(elements)) for _ in range(5))
        assert best < 25.0, "reduce took %.2f ms on 500 nodes" % best

    def test_it_stays_linear_enough(self):
        small = load("synthetic_500").elements
        large = load("synthetic_2000").elements
        candidates(small), candidates(large)
        ratio = (min(_time(lambda: candidates(large)) for _ in range(5)) /
                 max(min(_time(lambda: candidates(small)) for _ in range(5)), 1e-6))
        assert ratio < 25, "4x the nodes cost %.1fx the time" % ratio


def _time(fn) -> float:
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1000.0

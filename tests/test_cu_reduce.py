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


def list_screen(rows: int) -> list:
    """One ``list`` region, ``rows`` rows, one "Delete" button per row.

    Every Delete shares ``(role, name, region)`` and differs only in ``parent``
    — the shape that made per-row controls unreachable.
    """
    els = [UIElement(id="e0", role="window", name="App", bbox=(0, 0, 800, 600)),
           UIElement(id="L", role="list", name="Files", bbox=(0, 40, 800, 500),
                     parent="e0", region="e0", depth=1)]
    for index in range(rows):
        top = 50 + index * 40
        els.append(UIElement(id="row%d" % index, role="listitem",
                             name="row %d" % index, bbox=(0, top, 600, 36),
                             parent="L", region="L", depth=2,
                             patterns=("select",)))
        els.append(UIElement(id="del%d" % index, role="button", name="Delete",
                             bbox=(620, top, 80, 36), parent="row%d" % index,
                             region="L", depth=3, patterns=("invoke",)))
    return els


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

    def test_per_row_controls_survive_one_list(self):
        """The regression: ten rows, ten Delete buttons, one region.

        Keyed on ``(role, name, region)`` this collapsed to a single survivor
        with ``duplicates=9`` and "delete row 7" became unreachable. The rows
        have different parents; that is what tells them apart.
        """
        kept = dedupe(interactive(visible_enabled(list_screen(10))))
        deletes = [el for el in kept if el.name == "Delete"]
        assert len(deletes) == 10
        assert "del7" in {el.id for el in kept}
        assert all(el.duplicates == 0 for el in deletes)

    def test_true_duplicates_under_one_parent_still_collapse(self):
        """The other half: nine "More options" in one toolbar is still noise."""
        els = [UIElement(id="bar", role="toolbar", name="Main",
                         bbox=(0, 0, 800, 40))]
        els += [UIElement(id="more%d" % i, role="button", name="More options",
                          bbox=(i * 60, 4, 50, 32), parent="bar", region="bar",
                          patterns=("invoke",)) for i in range(9)]
        kept = [el for el in dedupe(interactive(visible_enabled(els)))
                if el.name == "More options"]
        assert len(kept) == 1 and kept[0].duplicates == 8

    def test_two_hundred_rows_are_a_cap_problem_not_a_dedupe_problem(self):
        """Volume is handled by the cap and the ranking, never by deletion."""
        els = list_screen(200)
        kept = dedupe(interactive(visible_enabled(els)))
        assert len([el for el in kept if el.name == "Delete"]) == 200
        assert len(candidates(els, cap=60)) == 60


def modal_screen(n_background: int = 70, focused: bool = True) -> list:
    """A 72-control screen with a modal on top: 70 tools behind it, 2 in it.

    The shape that exposed the dead dialog prior. Everything in the background
    shares one region, so before the fix geometry decided the whole order and
    the modal's two buttons — the only controls that mattered — sorted last.
    """
    els = [UIElement(id="e0", role="window", name="App", bbox=(0, 0, 1200, 900)),
           UIElement(id="e1", role="pane", name="Main", bbox=(0, 40, 1200, 800),
                     parent="e0", region="e0", depth=1)]
    for index in range(n_background):
        els.append(UIElement(
            id="m%d" % index, role="button", name="Tool %d" % index,
            bbox=(10 + (index % 10) * 110, 50 + (index // 10) * 40, 100, 32),
            parent="e1", region="e1", depth=2, patterns=("invoke",),
            focused=(focused and index == 0)))
    els.append(UIElement(id="d0", role="dialog", name="Replace file?",
                         bbox=(300, 300, 500, 200), parent="e0", region="e0",
                         depth=1))
    els.append(UIElement(id="d1", role="group", name="", bbox=(310, 400, 480, 60),
                         parent="d0", region="d0", depth=2))
    els.append(UIElement(id="d2", role="button", name="Replace",
                         bbox=(560, 420, 100, 32), parent="d1", region="d1",
                         depth=3, patterns=("invoke",)))
    els.append(UIElement(id="d3", role="button", name="Cancel",
                         bbox=(670, 420, 100, 32), parent="d1", region="d1",
                         depth=3, patterns=("invoke",)))
    return els


class TestPriority:
    def test_focus_first_then_dialog_then_the_rest(self):
        order = [el.id for el in prioritise(
            interactive(visible_enabled(screen())), "e12", full=screen())]
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
        """Focus is in the *toolbar*, so only the dialog prior can rank this.

        With focus inside the modal this assertion passed through the
        focus-region path and said nothing about dialogs — the bug it was
        named for shipped underneath it.
        """
        deep = UIElement(id="deep", role="button", name="Deep",
                         bbox=(220, 360, 40, 30), parent="e11", region="e11",
                         depth=4, patterns=("invoke",))
        full = screen() + [deep]
        order = [el.id for el in prioritise(
            interactive(visible_enabled(full)), "e2", full=full)]
        assert order.index("deep") < order.index("e3"), (
            "a control two panes deep inside the modal must outrank the "
            "toolbar button next to the focused one")

    def test_the_modal_outranks_the_pane_holding_focus(self):
        """A modal is modal, wherever focus happens to sit."""
        full = screen()
        order = [el.id for el in prioritise(
            interactive(visible_enabled(full)), "e2", full=full)]
        assert order[0] == "e2"
        assert order.index("e12") < order.index("e3")
        assert order.index("e13") < order.index("e3")

    def test_the_dialog_node_is_not_in_the_reduced_list(self):
        """Why ``full`` exists: the filters delete the evidence.

        ``interactive()`` drops the ``dialog`` node and the unnamed ``group``
        between it and its buttons, so a parent chain walked over the reduced
        list can never reach a dialog. Passing ``full`` is not an optimisation.
        """
        reduced = interactive(visible_enabled(screen()))
        assert not any(el.role == "dialog" for el in reduced)
        assert "e11" not in {el.id for el in reduced}

    @pytest.mark.parametrize("focused", (True, False))
    def test_a_modal_beats_seventy_background_controls(self, focused):
        """Measured before the fix: ranks 71 and 72 of 72, outside a cap of 60."""
        els = modal_screen(focused=focused)
        assert len(interactive(visible_enabled(els))) == 72
        picked = [el.id for el in candidates(els, cap=60)]
        assert {"d2", "d3"} <= set(picked[:10]), (
            "the modal's buttons must be in the top 10, not the tail")


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

    def test_an_oversized_region_is_chunked_not_truncated(self):
        """254 members, cap 60: member 61 used to be unreachable in both rounds.

        ``members[:cap]`` hid it once in the flat list and again in the cascade,
        and there is no third level to recover it. Chunks give the second round
        something to land on.
        """
        els = [UIElement(id="e0", role="window", name="App",
                         bbox=(0, 0, 1600, 4000)),
               UIElement(id="G", role="group", name="Big panel",
                         bbox=(0, 40, 1600, 3900), parent="e0", region="e0",
                         depth=1)]
        els += [UIElement(id="b%d" % i, role="button", name="Item %d" % i,
                          bbox=(10 + (i % 8) * 190, 50 + (i // 8) * 40, 180, 36),
                          parent="G", region="G", depth=2, patterns=("invoke",))
                for i in range(254)]
        rows = region_state(els, cap=60)["regions"]
        assert set(rows) == {"r0a", "r0b", "r0c", "r0d", "r0e"}
        reachable = [member for row in rows.values() for member in row["members"]]
        assert len(reachable) == 254 and len(set(reachable)) == 254
        assert "b60" in reachable, "member 61 must be reachable in two rounds"
        for row in rows.values():
            assert len(row["members"]) <= 60, "a chunk is still one Choice"
            assert row["count"] == len(row["members"])
            assert row["part"].endswith("/5")

    def test_a_region_that_fits_keeps_its_plain_key(self):
        """No chunk suffix and no ``part`` when one Choice already covers it."""
        rows = region_state(screen(), cap=60)["regions"]
        assert all(key[1:].isdigit() for key in rows), sorted(rows)
        assert all("part" not in row for row in rows.values())


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

"""The question a model could not answer, answered by code.

``bench/cu_bench.py`` asks Jev a ``stuck`` noul — "does the last action plus the
current state show that the previous step had no effect?" — and it came back at
0.42-0.60 on both providers, run after run. That is not a prompting failure: the
model is shown one state and asked about two. This module is the replacement,
and these tests are the contract that lets ``stuck`` be deleted rather than
re-tuned.

Two properties matter and they pull in opposite directions:

* an unchanged screen must hash identically, including when a clock ticked, the
  window moved, or focus flickered — otherwise every step looks like progress;
* a changed screen must hash differently, including when only one button's
  label changed — otherwise a loop declares itself stuck and escalates.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from jevskill.cu import diff, normalise, tree_hash
from jevskill.cu.hashing import DEFAULT_IGNORE, VOLATILE_NAME_ROLES
from jevskill.cu.types import Snapshot, UIElement

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"
ALL_FIXTURES = ("synthetic_50", "synthetic_500", "synthetic_2000",
                "notepad", "calculator")


def load(name: str) -> Snapshot:
    data = json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))
    return Snapshot.from_dict(data["snapshot"])


def tree():
    return [
        UIElement(id="e0", role="window", name="Editor", bbox=(0, 0, 800, 600)),
        UIElement(id="e1", role="button", name="Save", bbox=(10, 10, 60, 30),
                  parent="e0", patterns=("invoke",)),
        UIElement(id="e2", role="edit", name="File name", bbox=(10, 50, 300, 28),
                  parent="e0", patterns=("value",), value="draft.txt"),
    ]


class TestStability:
    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_same_tree_same_hash(self, name):
        elements = load(name).elements
        assert tree_hash(elements) == tree_hash(elements)

    def test_dicts_and_dataclasses_agree(self):
        elements = tree()
        assert tree_hash(elements) == tree_hash([el.to_dict() for el in elements])

    def test_ids_are_not_hashed(self):
        """Ids are positional: one new node above must not change the rest."""
        base = tree()
        renumbered = [replace(el, id="x%d" % index)
                      for index, el in enumerate(base)]
        assert tree_hash(base) == tree_hash(renumbered)


class TestWhatCountsAsChange:
    def test_a_moved_window_is_not_a_change(self):
        moved = [replace(el, bbox=(el.bbox[0] + 7, el.bbox[1] + 3,
                                   el.bbox[2], el.bbox[3])) for el in tree()]
        assert tree_hash(moved) == tree_hash(tree())

    def test_focus_flicker_is_not_a_change(self):
        flickered = [replace(el, focused=not el.focused) for el in tree()]
        assert tree_hash(flickered) == tree_hash(tree())

    def test_a_ticking_value_is_not_a_change(self):
        typed = [replace(el, value="draft2.txt") if el.value else el
                 for el in tree()]
        assert tree_hash(typed) == tree_hash(tree())

    def test_but_it_is_when_you_ask_for_it(self):
        typed = [replace(el, value="draft2.txt") if el.value else el
                 for el in tree()]
        assert tree_hash(typed, ignore=()) != tree_hash(tree(), ignore=())

    def test_a_relabelled_button_is_a_change(self):
        relabelled = [replace(el, name="Save As...") if el.name == "Save" else el
                      for el in tree()]
        assert tree_hash(relabelled) != tree_hash(tree())

    def test_a_new_dialog_is_a_change(self):
        with_dialog = tree() + [UIElement(id="e3", role="dialog",
                                          name="Replace file?",
                                          bbox=(100, 100, 300, 150))]
        assert tree_hash(with_dialog) != tree_hash(tree())

    def test_a_disabled_button_is_a_change(self):
        greyed = [replace(el, enabled=False) if el.name == "Save" else el
                  for el in tree()]
        assert tree_hash(greyed) != tree_hash(tree())

    def test_ignore_and_default_agree_on_the_constant(self):
        assert set(DEFAULT_IGNORE) == {"bbox", "focused", "value"}
        assert tree_hash(tree()) == tree_hash(tree(), ignore=DEFAULT_IGNORE)

    def test_normalise_shows_the_offending_row(self):
        rows = normalise(tree())
        assert len(rows) == 3
        assert "Save" in rows[1] and "draft.txt" not in rows[2]


def rows(selected_index: int) -> list:
    """A three-row list with exactly one row selected."""
    return [UIElement(id="e0", role="list", name="Files", bbox=(0, 0, 400, 300))] + [
        UIElement(id="e%d" % (i + 1), role="listitem", name="row %d" % i,
                  bbox=(0, 10 + i * 40, 400, 30), parent="e0",
                  patterns=("select",), selected=(i == selected_index))
        for i in range(3)]


class TestStateIsHashed:
    """``patterns`` says a control *can* be selected, never that it *is*.

    Both cases below were verified equal before the fix, which is how a loop
    that pressed Down and then asked "did the screen change" was told no.
    """

    def test_an_arrow_key_move_is_a_change(self):
        assert tree_hash(rows(0)) != tree_hash(rows(1))

    def test_selection_survives_the_default_ignore_set(self):
        assert "selected" not in DEFAULT_IGNORE
        assert tree_hash(rows(0), ignore=DEFAULT_IGNORE) != tree_hash(rows(1))

    def test_a_ticked_checkbox_is_a_change(self):
        off = [UIElement(id="e0", role="checkbox", name="Word wrap",
                         bbox=(0, 0, 200, 24), patterns=("toggle",), toggled=0)]
        on = [replace(off[0], toggled=1)]
        indeterminate = [replace(off[0], toggled=2)]
        assert tree_hash(off) != tree_hash(on)
        assert tree_hash(on) != tree_hash(indeterminate), (
            "a tri-state checkbox has three states, not two")

    def test_diff_reports_the_row_that_moved(self):
        result = diff(rows(0), rows(1), ignore=("selected",))
        assert result.changed == ["e1", "e2"]

    def test_the_value_hole_is_real_and_has_a_documented_escape(self):
        """A rename dialog with a different filename hashes the same.

        Measured, and the reason ``ignore=()`` is documented rather than
        implied: ``value`` is ignored by default so a progress percentage does
        not read as progress.
        """
        before = [UIElement(id="e0", role="dialog", name="Rename",
                            bbox=(0, 0, 400, 200)),
                  UIElement(id="e1", role="edit", name="File name",
                            bbox=(10, 40, 300, 28), parent="e0",
                            patterns=("value",), value="notes.txt")]
        after = [before[0], replace(before[1], value="taxes-2026.txt")]
        assert tree_hash(before) == tree_hash(after)
        assert tree_hash(before, ignore=()) != tree_hash(after, ignore=())
        assert (tree_hash(before, ignore=("bbox", "focused"))
                != tree_hash(after, ignore=("bbox", "focused"))), (
            "the middle setting — keep value, tolerate a moved window")


def status(text: str) -> list:
    return [UIElement(id="e0", role="window", name="Editor", bbox=(0, 0, 800, 600)),
            UIElement(id="e1", role="statusbar", name=text,
                      bbox=(0, 570, 800, 30), parent="e0"),
            UIElement(id="e2", role="button", name="Save", bbox=(10, 10, 60, 30),
                      parent="e0", patterns=("invoke",))]


class TestVolatileText:
    """The module used to claim a ticking clock was not a change. It was.

    UIA puts clocks, "Ln 9, Col 42" and item counts in ``Name``, which is
    hashed. The committed Notepad fixture carries 7 such ``text``/``statusbar``
    nodes and Calculator 4 — measured, and the reason the claim needed fixing
    rather than deleting.
    """

    def test_by_default_a_ticking_status_line_is_a_change(self):
        assert tree_hash(status("Ln 1, Col 1")) != tree_hash(status("Ln 9, Col 42"))

    def test_volatile_text_silences_it(self):
        assert (tree_hash(status("Ln 1, Col 1"), volatile_text=True)
                == tree_hash(status("Ln 9, Col 42"), volatile_text=True))

    def test_but_a_dialog_still_registers(self):
        """The measurement that decides whether the option is safe to offer."""
        opened = status("Ln 1, Col 1") + [
            UIElement(id="e3", role="dialog", name="Replace file?",
                      bbox=(100, 100, 300, 150), parent="e0")]
        assert (tree_hash(status("Ln 1, Col 1"), volatile_text=True)
                != tree_hash(opened, volatile_text=True))

    def test_a_relabelled_button_still_registers(self):
        renamed = [replace(el, name="Save As...") if el.name == "Save" else el
                   for el in status("Ln 1, Col 1")]
        assert (tree_hash(status("Ln 1, Col 1"), volatile_text=True)
                != tree_hash(renamed, volatile_text=True))

    def test_diff_agrees_with_the_hash(self):
        assert not diff(status("a"), status("b"), volatile_text=True).changed_any
        assert diff(status("a"), status("b")).changed_any

    def test_the_roles_are_the_constant(self):
        assert VOLATILE_NAME_ROLES == {"text", "statusbar"}

    @pytest.mark.parametrize("name", ("notepad", "calculator"))
    def test_the_real_fixtures_still_hash_stably(self, name):
        elements = load(name).elements
        assert (tree_hash(elements, volatile_text=True)
                == tree_hash(elements, volatile_text=True))


class TestDiff:
    def test_no_change_is_no_change(self):
        result = diff(tree(), tree())
        assert not result.changed_any and not bool(result)
        assert result.to_dict() == {"added": [], "removed": [], "changed": [],
                                    "changed_any": False}

    def test_added_reports_current_ids(self):
        after = tree() + [UIElement(id="e3", role="button", name="Replace",
                                    bbox=(100, 100, 80, 30), patterns=("invoke",))]
        result = diff(tree(), after)
        assert result.added == ["e3"] and result.removed == []
        assert bool(result)

    def test_removed_reports_previous_ids(self):
        before = tree() + [UIElement(id="e3", role="button", name="Replace",
                                     bbox=(100, 100, 80, 30))]
        result = diff(before, tree())
        assert result.removed == ["e3"] and result.added == []

    def test_changed_is_the_volatile_fields(self):
        after = [replace(el, value="typed") if el.role == "edit" else el
                 for el in tree()]
        result = diff(tree(), after)
        assert result.changed == ["e2"]
        assert result.added == [] and result.removed == []

    def test_identity_survives_renumbering(self):
        """A dialog inserted at the top shifts every id; nothing was removed."""
        after = [UIElement(id="e0", role="dialog", name="Replace file?",
                           bbox=(100, 100, 300, 150))]
        after += [replace(el, id="e%d" % (index + 1))
                  for index, el in enumerate(tree())]
        result = diff(tree(), after)
        assert result.removed == []
        assert result.added == ["e0"]

    def test_two_identical_controls_match_one_each(self):
        twins = [UIElement(id="a", role="button", name="Tab", bbox=(0, 0, 40, 30)),
                 UIElement(id="b", role="button", name="Tab", bbox=(40, 0, 40, 30))]
        result = diff(twins, twins[:1])
        assert result.removed == ["b"], "a duplicate must not absorb its twin"

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_real_trees_diff_clean_against_themselves(self, name):
        elements = load(name).elements
        assert not diff(elements, elements).changed_any


def twins(count: int) -> list:
    """``count`` controls that are identical in every identity field.

    One virtualised list of one repeated row is the realistic case, and it puts
    every element in a single identity bucket — the worst case for matching.
    """
    return [UIElement(id="e%d" % index, role="button", name="Tab",
                      bbox=(0, 0, 40, 30), patterns=("invoke",))
            for index in range(count)]


def _best(fn, runs: int = 3) -> float:
    fn()
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - started) * 1000.0)
    return best


class TestDiffCost:
    """Matching used to re-scan each identity bucket from the start.

    Measured before the fix: 12.8 ms at 500 identical controls, 45.9 at 1,000,
    176 at 2,000, 747 at 4,000 — quadratic, inside a loop whose whole step
    budget is ~400 ms. A ``deque`` consumed from the left makes it linear.
    """

    def test_four_thousand_identical_controls_diff_fast(self):
        before, after = twins(4000), twins(4000)
        best = _best(lambda: diff(before, after))
        assert best < 50.0, "diff took %.1f ms on 4,000 identical controls" % best

    def test_cost_does_not_square(self):
        """Doubling the bucket must not quadruple the time."""
        small_a, small_b = twins(1000), twins(1000)
        large_a, large_b = twins(4000), twins(4000)
        small = max(_best(lambda: diff(small_a, small_b)), 1e-6)
        large = _best(lambda: diff(large_a, large_b))
        assert large / small < 10.0, (
            "4x the controls cost %.1fx the time" % (large / small))

    def test_matching_is_still_one_for_one(self):
        """Speed must not cost correctness: the deque pops, it does not reuse."""
        result = diff(twins(5), twins(3))
        assert result.removed == ["e3", "e4"]
        assert result.added == [] and result.changed == []
        assert diff(twins(3), twins(5)).added == ["e3", "e4"]

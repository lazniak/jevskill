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
from dataclasses import replace
from pathlib import Path

import pytest

from jevskill.cu import diff, normalise, tree_hash
from jevskill.cu.hashing import DEFAULT_IGNORE
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

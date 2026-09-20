"""The macro cache: a hit must be free, and a wrong hit must be impossible.

The interesting failures are not "does the dict work". They are:

* a hit on a screen that only *looks* the same (different goal, different way of
  getting there, different controls);
* a hit that returns an element id the walk has since given to another control —
  the reason an entry stores the target's identity next to its id;
* an entry that survives an action which stopped working.

The store is written to a tmp_path throughout; nothing here touches
``~/.jevskill``.
"""

from __future__ import annotations

import json

import pytest

from jevskill.cu.macros import (DEFAULT_PATH, SCHEMA, MacroCache, MacroEntry,
                                goal_fingerprint)
from jevskill.cu.types import UIElement

GOAL = "Save the current document and close the dialog"


def screen(save_name="Save"):
    return [
        UIElement(id="e1", role="button", name=save_name, bbox=(10, 10, 60, 30),
                  automation_id="SaveBtn", patterns=("invoke",)),
        UIElement(id="e2", role="button", name="Cancel", bbox=(80, 10, 60, 30),
                  patterns=("invoke",)),
    ]


@pytest.fixture
def cache(tmp_path):
    return MacroCache(tmp_path / "cu_macros.json")


class TestKeying:
    def test_the_goal_is_normalised_but_not_interpreted(self):
        assert goal_fingerprint("  Save   THE doc ") == goal_fingerprint("save the doc")
        assert goal_fingerprint("save the doc") != goal_fingerprint("close the doc")

    def test_a_different_goal_is_a_different_key(self, cache):
        assert cache.key(GOAL, screen(), "changed") != cache.key(
            "Print it", screen(), "changed")

    def test_a_different_screen_is_a_different_key(self, cache):
        assert cache.key(GOAL, screen(), "changed") != cache.key(
            GOAL, screen(save_name="Save as"), "changed")

    def test_how_the_screen_was_reached_is_part_of_the_key(self, cache):
        # The same dialog after a click that opened it wants a different next
        # step from the same dialog after an action that did nothing.
        assert cache.key(GOAL, screen(), "new_window") != cache.key(
            GOAL, screen(), "unchanged")

    def test_the_key_is_stable_across_instances(self, tmp_path):
        first = MacroCache(tmp_path / "a.json").key(GOAL, screen(), "changed")
        second = MacroCache(tmp_path / "b.json").key(GOAL, screen(), "changed")
        assert first == second


class TestHitAndMiss:
    def test_an_empty_cache_misses(self, cache):
        assert cache.lookup(GOAL, screen(), "changed") is None
        assert cache.stats()["misses"] == 1

    def test_a_stored_conclusion_comes_back(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        assert cache.lookup(GOAL, screen(), "changed") == ("e1", "click")
        assert cache.stats() == {"entries": 1, "hits": 1, "misses": 0, "stores": 1,
                                 "invalidations": 0, "hit_rate": 1.0}

    def test_a_hit_on_a_different_screen_never_happens(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        assert cache.lookup(GOAL, screen(save_name="Save as"), "changed") is None

    def test_the_id_is_re_resolved_against_the_current_candidates(self, cache):
        # Ids are positional. The same screen walked with one extra node above
        # the candidates numbers "Save" differently, and the tree hash — which
        # does not cover ids — still matches.
        cache.store(GOAL, screen(), "changed", "e1", "click")
        renumbered = [
            UIElement(id="e7", role="button", name="Save", bbox=(10, 10, 60, 30),
                      automation_id="SaveBtn", patterns=("invoke",)),
            UIElement(id="e8", role="button", name="Cancel", bbox=(80, 10, 60, 30),
                      patterns=("invoke",)),
        ]
        assert cache.lookup(GOAL, renumbered, "changed") == ("e7", "click")

    def test_an_identity_that_is_gone_is_a_miss_not_a_click(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        entry = next(iter(cache.entries.values()))
        entry.identity = ("button", "Vanished", "", "")
        assert cache.lookup(GOAL, screen(), "changed") is None

    def test_a_targetless_action_replays_as_none(self, cache):
        cache.store(GOAL, screen(), "changed", "none", "key")
        assert cache.lookup(GOAL, screen(), "changed") == ("none", "key")

    def test_hits_are_counted_per_entry_for_the_report(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        for _ in range(3):
            cache.lookup(GOAL, screen(), "changed")
        assert next(iter(cache.entries.values())).hits == 3
        assert cache.hit_rate == 1.0

    def test_hit_rate_is_zero_rather_than_a_zero_division(self, cache):
        assert cache.hit_rate == 0.0


class TestInvalidation:
    def test_an_entry_whose_replay_did_nothing_is_dropped(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        assert cache.invalidate(GOAL, screen(), "changed") is True
        assert cache.lookup(GOAL, screen(), "changed") is None
        assert cache.stats()["invalidations"] == 1

    def test_invalidating_something_absent_is_not_an_error(self, cache):
        assert cache.invalidate(GOAL, screen(), "changed") is False

    def test_clear_empties_the_store(self, cache):
        cache.store(GOAL, screen(), "changed", "e1", "click")
        assert cache.clear() == 1 and len(cache) == 0


class TestPersistence:
    def test_a_store_survives_a_new_instance(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        MacroCache(path).store(GOAL, screen(), "changed", "e1", "click")
        assert MacroCache(path).lookup(GOAL, screen(), "changed") == ("e1", "click")

    def test_a_damaged_file_is_ignored_rather_than_raised(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(MacroCache(path)) == 0

    def test_a_file_from_another_schema_is_ignored(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        path.write_text(json.dumps({"schema": SCHEMA + 1, "entries": {"k": {}}}),
                        encoding="utf-8")
        assert len(MacroCache(path)) == 0

    def test_an_unwritable_path_does_not_fail_the_loop_it_was_speeding_up(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        cache = MacroCache(blocker / "inner" / "cu_macros.json")
        cache.store(GOAL, screen(), "changed", "e1", "click")   # must not raise
        assert cache.lookup(GOAL, screen(), "changed") == ("e1", "click")

    def test_autosave_can_be_turned_off_for_a_hot_loop(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        cache = MacroCache(path, autosave=False)
        cache.store(GOAL, screen(), "changed", "e1", "click")
        assert not path.exists()
        assert cache.save() and path.exists()

    def test_the_default_path_is_the_users_home_not_the_repo(self):
        # A macro is a fact about an application's UI, not about a project.
        assert DEFAULT_PATH.name == "cu_macros.json"
        assert DEFAULT_PATH.parent.name == ".jevskill"

    def test_an_entry_round_trips_through_json(self):
        entry = MacroEntry(target="e1", op="click",
                           identity=("button", "Save", "SaveBtn", ""), goal="g")
        assert MacroEntry.from_dict(json.loads(json.dumps(entry.to_dict()))) == entry


class TestTwoWritersOnOneFile:
    """``DEFAULT_PATH`` is machine-wide, so this is the normal case.

    The old write used one fixed ``cu_macros.tmp`` beside the store and dumped
    the whole in-memory map over it, so the later ``os.replace`` discarded
    everything the other process had learned.
    """

    def test_neither_writer_discards_the_other(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        one, two = MacroCache(path), MacroCache(path)
        one.store("goal one", screen(), "changed", "e1", "click")
        two.store("goal two", screen("Open"), "changed", "e1", "click")

        fresh = MacroCache(path, autosave=False)
        assert len(fresh) == 2
        assert fresh.lookup("goal one", screen(), "changed") == ("e1", "click")
        assert fresh.lookup("goal two", screen("Open"), "changed") == ("e1", "click")

    def test_the_merge_does_not_resurrect_an_invalidated_entry(self, tmp_path):
        """An invalidated macro is wrong, not stale. Reading it back is a bug."""
        path = tmp_path / "cu_macros.json"
        one, two = MacroCache(path), MacroCache(path)
        one.store("goal one", screen(), "changed", "e1", "click")
        two.load()                                   # two now holds one's entry
        two.invalidate("goal one", screen(), "changed")
        assert MacroCache(path, autosave=False).lookup(
            "goal one", screen(), "changed") is None

    def test_clear_survives_the_merge_too(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        one = MacroCache(path)
        one.store("goal one", screen(), "changed", "e1", "click")
        one.clear()
        assert len(MacroCache(path, autosave=False)) == 0

    def test_the_newer_entry_wins_a_key_both_hold(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        one, two = MacroCache(path), MacroCache(path)
        one.store(GOAL, screen(), "changed", "e1", "click")
        two.store(GOAL, screen(), "changed", "e2", "click")
        assert MacroCache(path, autosave=False).lookup(
            GOAL, screen(), "changed") == ("e2", "click")

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        MacroCache(path).store(GOAL, screen(), "changed", "e1", "click")
        assert [p.name for p in tmp_path.iterdir()] == ["cu_macros.json"]

    def test_one_unreadable_row_does_not_lose_the_others(self, tmp_path):
        path = tmp_path / "cu_macros.json"
        cache = MacroCache(path)
        key = cache.store(GOAL, screen(), "changed", "e1", "click")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entries"]["junk"] = {"identity": 7}
        path.write_text(json.dumps(data), encoding="utf-8")
        assert MacroCache(path, autosave=False).entries.keys() >= {key}


class TestEviction:
    def test_the_least_recently_used_entry_goes_first(self, tmp_path):
        cache = MacroCache(tmp_path / "m.json", autosave=False, max_entries=2)
        for index in range(3):
            cache.store("goal %d" % index, screen(), "changed", "e1", "click")
        assert len(cache) == 2
        assert cache.lookup("goal 0", screen(), "changed") is None
        assert cache.lookup("goal 2", screen(), "changed") == ("e1", "click")

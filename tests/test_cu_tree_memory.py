"""Goal trees, phase decompositions and sub-goal lessons in the operator's memory.

These live as ordinary rows of the ``plans`` (and ``lessons``) section under
key prefixes, not in a new section, so an older build that knows only today's
``SECTIONS`` carries them through its own saves. The tests here are the
``Experience``-level half of the memory test (spec §9, test 25): learning,
filling with a new command's values, demotion, and survival across a save by a
store that only knows the old sections. The end-to-end operator half lives with
the runner's tests. Nothing here touches a desktop or the network.
"""

from __future__ import annotations

import json

from jevskill.cu.experience import (DEMOTE_AFTER, MAX_LESSONS, PHASE_PREFIX, SECTIONS,
                                    TREE_PREFIX, Experience, command_key, display_template,
                                    goal_key, phase_key, tree_key)

APP = "Notepad"
COMMAND = "wpisz „hello world” i zapisz jako hello.txt"
COMMAND_2 = "wpisz „lista” i zapisz jako zakupy.txt"
WRITE_PHASE = "Write „hello world” in Notepad"
SAVE_PHASE = "Save the document as hello.txt on the Desktop"
SAVE_PHASE_2 = "Save the document as zakupy.txt on the Desktop"


def save_children():
    """How the save phase split, as a finished tree hands it over."""
    return [
        {"goal": "Open the Save As dialog", "done_when": "a dialog titled 'Zapisz jako' is open",
         "launch": None, "kind": "leaf", "app": APP, "weight": 1.0,
         "checks": [{"kind": "dialog", "arg": "Zapisz jako", "text": None}]},
        {"goal": "Type hello.txt into the file name field",
         "done_when": "the file name field holds 'hello.txt'", "launch": None,
         "kind": "leaf", "app": APP, "weight": 1.0, "irreversible": False,
         "checks": [{"kind": "field", "arg": "hello.txt", "text": "hello.txt"}]},
        {"goal": "Confirm the save of hello.txt", "done_when": "the title reads 'hello.txt'",
         "launch": None, "kind": "leaf", "app": APP, "weight": 1.0, "irreversible": True,
         "checks": [{"kind": "title", "arg": "hello.txt", "text": None}]},
    ]


def tree_items():
    """The root's done children: two phases, the second one nested."""
    return [
        {"goal": WRITE_PHASE, "done_when": "the document holds „hello world”",
         "launch": "notepad.exe", "kind": "leaf", "app": APP, "weight": 1.0,
         "optional": False,
         "checks": [{"kind": "document", "arg": "hello world", "text": "hello world"}]},
        {"goal": SAVE_PHASE, "done_when": "the title reads 'hello.txt'", "launch": None,
         "kind": "phase", "app": APP, "weight": 2.0,
         "checks": [{"kind": "title", "arg": "hello.txt", "text": None}],
         "children": save_children()},
    ]


def store(tmp_path, name="m.json"):
    return Experience(tmp_path / name)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


class TestKeys:
    def test_keys_carry_their_prefix_and_a_plain_command_key_never_does(self):
        assert tree_key(COMMAND) == TREE_PREFIX + command_key(COMMAND)[0]
        assert tree_key(COMMAND) == tree_key(COMMAND_2)
        assert phase_key(APP, SAVE_PHASE) == (PHASE_PREFIX + APP.casefold() + "\x1f"
                                              + goal_key(SAVE_PHASE)[0])
        assert phase_key(APP, SAVE_PHASE) == phase_key(" notepad ", SAVE_PHASE_2)
        # The separator the prefixes end in is whitespace to the fingerprint,
        # so no command can ever collide with a tree or phase row.
        assert "\x1f" not in command_key("a\x1fb " + TREE_PREFIX + COMMAND)[0]


# --------------------------------------------------------------------------- #
# Trees
# --------------------------------------------------------------------------- #


class TestTree:
    def test_a_tree_serves_a_new_command_of_the_same_template_recursively(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items(), model="m1")
        hit = mem.tree_for(COMMAND_2)
        assert hit is not None
        items, memory = hit
        assert memory.example == ("tree: " + COMMAND)[:len("tree: ") + 290]
        assert memory.model == "m1" and memory.successes == 1
        write, save = items
        assert write["goal"] == "Write „lista” in Notepad"
        assert write["done_when"] == "the document holds „lista”"
        assert write["launch"] == "notepad.exe"
        assert write["checks"] == [{"kind": "document", "arg": "lista", "text": "lista"}]
        assert write["optional"] is False and write["weight"] == 1.0
        assert save["goal"] == SAVE_PHASE_2
        assert save["kind"] == "phase" and save["weight"] == 2.0
        assert save["checks"][0]["arg"] == "zakupy.txt"
        typed = save["children"][1]
        assert typed["goal"] == "Type zakupy.txt into the file name field"
        assert typed["done_when"] == "the file name field holds 'zakupy.txt'"
        assert typed["checks"] == [{"kind": "field", "arg": "zakupy.txt", "text": "zakupy.txt"}]
        assert save["children"][2]["irreversible"] is True
        # Nothing of the first run's values survives anywhere in the fill.
        dumped = json.dumps(items, ensure_ascii=False)
        assert "hello" not in dumped and "⟦" not in dumped

    def test_the_stored_tree_holds_command_markers_not_values(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        raw = json.dumps(mem.plans[tree_key(COMMAND)].steps, ensure_ascii=False)
        assert "hello" not in raw and "⟦c0⟧" in raw and "⟦c1⟧" in raw and "⟦g" not in raw

    def test_the_original_command_fills_back_to_what_was_learned(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        items, _ = mem.tree_for(COMMAND)
        assert items[1]["children"][1]["goal"] == "Type hello.txt into the file name field"

    def test_a_marker_that_cannot_be_filled_is_a_miss(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        steps = mem.plans[tree_key(COMMAND)].steps
        steps[1]["children"][0]["checks"][0]["arg"] = "⟦c7⟧"
        assert mem.tree_for(COMMAND_2) is None
        steps[1]["children"][0]["checks"][0]["arg"] = "⟦g0⟧"
        assert mem.tree_for(COMMAND_2) is None

    def test_a_goal_that_fills_empty_is_a_miss(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        mem.plans[tree_key(COMMAND)].steps[0]["goal"] = "   "
        assert mem.tree_for(COMMAND_2) is None

    def test_no_usable_item_learns_nothing(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, [{"goal": "  "}, {"done_when": "x"}, "junk"])
        assert tree_key(COMMAND) not in mem.plans
        assert mem.tree_for(COMMAND) is None

    def test_failures_demote_and_a_relearn_restores(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        for _ in range(DEMOTE_AFTER - 1):
            mem.tree_failed(COMMAND_2)
            assert mem.tree_for(COMMAND_2) is not None
        mem.tree_failed(COMMAND_2)
        assert mem.tree_for(COMMAND_2) is None
        memory = mem.plans[tree_key(COMMAND)]
        assert memory.failures == DEMOTE_AFTER and memory.streak_failed == DEMOTE_AFTER
        mem.learn_tree(COMMAND_2, tree_items())
        items, memory = mem.tree_for(COMMAND_2)
        assert memory.streak_failed == 0 and memory.successes == 2
        assert memory.failures == DEMOTE_AFTER

    def test_a_failure_on_nothing_learned_is_a_no_op(self, tmp_path):
        mem = store(tmp_path)
        mem.tree_failed(COMMAND)
        assert mem.plans == {}

    def test_plan_for_never_returns_a_tree(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        assert mem.plan_for(COMMAND) is None
        assert mem.plan_for(COMMAND_2) is None

    def test_a_flat_plan_is_untouched_by_a_tree_for_the_same_command(self, tmp_path):
        mem = store(tmp_path)
        flat = [{"goal": "Open Notepad", "done_when": "", "launch": "notepad.exe"},
                {"goal": "Type „hello world”", "done_when": "", "launch": None}]
        mem.learn_plan(COMMAND, flat)
        mem.learn_tree(COMMAND, tree_items())
        steps, _ = mem.plan_for(COMMAND_2)
        assert steps == [{"goal": "Open Notepad", "done_when": "", "launch": "notepad.exe"},
                         {"goal": "Type „lista”", "done_when": "", "launch": None}]
        assert mem.tree_for(COMMAND_2)[0][0]["goal"] == "Write „lista” in Notepad"


# --------------------------------------------------------------------------- #
# Decompositions
# --------------------------------------------------------------------------- #


class TestDecomposition:
    def test_a_decomposition_serves_another_file_name(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, save_children(), model="m1")
        hit = mem.decomposition_for(APP, SAVE_PHASE_2)
        assert hit is not None
        children, memory = hit
        assert memory.example == "phase: " + SAVE_PHASE
        assert memory.command == phase_key(APP, SAVE_PHASE)
        assert [c["goal"] for c in children] == [
            "Open the Save As dialog", "Type zakupy.txt into the file name field",
            "Confirm the save of zakupy.txt"]
        assert children[1]["checks"][0] == {"kind": "field", "arg": "zakupy.txt",
                                            "text": "zakupy.txt"}
        assert children[0]["checks"][0]["arg"] == "Zapisz jako"
        assert children[2]["done_when"] == "the title reads 'zakupy.txt'"
        raw = json.dumps(mem.plans[phase_key(APP, SAVE_PHASE)].steps, ensure_ascii=False)
        assert "hello.txt" not in raw and "⟦g0⟧" in raw and "⟦c" not in raw

    def test_it_serves_the_phase_inside_a_different_mission(self, tmp_path):
        """Learned while saving a note; asked for while saving a shopping list
        from a different command. The key is the phase, not the mission."""
        mem = store(tmp_path)
        mem.learn_tree(COMMAND, tree_items())
        mem.learn_decomposition(APP, SAVE_PHASE, save_children())
        other_mission = "otwórz notatnik, wklej schowek i zapisz jako zakupy.txt"
        assert mem.tree_for(other_mission) is None
        assert mem.plan_for(other_mission) is None
        children, _ = mem.decomposition_for(APP, SAVE_PHASE_2)
        assert children[1]["goal"] == "Type zakupy.txt into the file name field"

    def test_a_value_not_in_the_phase_goal_stays_literal(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, [
            {"goal": "Check the document still holds „hello world”", "done_when": ""},
            {"goal": "Save as hello.txt", "done_when": ""}])
        children, _ = mem.decomposition_for(APP, SAVE_PHASE_2)
        assert children[0]["goal"] == "Check the document still holds „hello world”"
        assert children[1]["goal"] == "Save as zakupy.txt"

    def test_a_different_phase_goal_or_app_misses(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, save_children())
        assert mem.decomposition_for(APP, "Save the document as zakupy.txt in Documents") is None
        assert mem.decomposition_for("wordpad", SAVE_PHASE_2) is None

    def test_a_blank_app_is_a_no_op(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition("  ", SAVE_PHASE, save_children())
        mem.learn_decomposition("", SAVE_PHASE, save_children())
        assert mem.plans == {}
        assert mem.decomposition_for("", SAVE_PHASE) is None
        mem.decomposition_failed("", SAVE_PHASE)
        assert mem.plans == {}

    def test_no_usable_child_learns_nothing(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, [{"goal": ""}])
        assert mem.plans == {}

    def test_a_command_marker_cannot_be_filled_from_a_phase(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, save_children())
        mem.plans[phase_key(APP, SAVE_PHASE)].steps[0]["done_when"] = "⟦c0⟧"
        assert mem.decomposition_for(APP, SAVE_PHASE_2) is None

    def test_failures_demote_and_a_relearn_restores(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_decomposition(APP, SAVE_PHASE, save_children())
        for _ in range(DEMOTE_AFTER):
            mem.decomposition_failed(APP, SAVE_PHASE_2)
        assert mem.decomposition_for(APP, SAVE_PHASE_2) is None
        mem.learn_decomposition(APP, SAVE_PHASE_2, save_children())
        _, memory = mem.decomposition_for(APP, SAVE_PHASE)
        assert memory.streak_failed == 0 and memory.successes == 2


# --------------------------------------------------------------------------- #
# Sub-goal lessons
# --------------------------------------------------------------------------- #


class TestSubgoalLessons:
    def test_the_line_names_the_child_and_the_reason(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s in the editor", "max_steps")
        key = Experience.lesson_key(APP, SAVE_PHASE, "subgoal", None,
                                    goal_key("Press ctrl+s in the editor")[0])
        lesson = mem.lessons[key]
        assert lesson.kind == "subgoal" and lesson.op == "subgoal" and lesson.count == 1
        assert lesson.goal == goal_key(SAVE_PHASE)[0]
        assert lesson.line() == 'sub-goal "%s" failed: max_steps' % display_template(
            goal_key("Press ctrl+s in the editor")[0])

    def test_a_repeat_counts_and_the_phase_lessons_come_first(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_subgoal_lesson(APP, "Some other phase", "Click File", "blocked")
        mem.learn_subgoal_lesson(APP, "Some other phase", "Click File", "blocked")
        mem.learn_subgoal_lesson(APP, "Some other phase", "Click File", "blocked")
        mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "max_steps")
        mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "max_steps")
        # Another file name, the same phase template: the same lesson.
        lines = mem.lessons_for(APP, SAVE_PHASE_2)
        assert lines[0].startswith('sub-goal "press ctrl+s" failed: max_steps')
        assert lines[0].endswith(" — 2 times")
        assert lines[1].startswith("(another goal) ")

    def test_no_reason_and_a_blank_app(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_subgoal_lesson(" ", SAVE_PHASE, "Press ctrl+s", "max_steps")
        assert mem.lessons == {}
        mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "")
        assert mem.lessons_for(APP, SAVE_PHASE)[0].endswith("failed: no reason")

    def test_lessons_stay_bounded(self, tmp_path):
        mem = Experience(tmp_path / "m.json", autosave=False)
        for i in range(MAX_LESSONS + 3):
            mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Child number %d" % i, "x")
        assert len(mem.lessons) == MAX_LESSONS


# --------------------------------------------------------------------------- #
# Listing, forgetting and survival across an older build's save
# --------------------------------------------------------------------------- #


class TestListingAndSurvival:
    def test_listing_names_the_kind_of_each_plan_row(self, tmp_path):
        mem = store(tmp_path)
        mem.learn_plan("open notepad", [{"goal": "Open Notepad", "launch": "notepad.exe"}])
        mem.learn_tree(COMMAND, tree_items())
        mem.learn_decomposition(APP, SAVE_PHASE, save_children())
        rows = {row["kind"]: row for row in mem.listing()["plans"]}
        assert set(rows) == {"plan", "tree", "phase"}
        assert rows["tree"]["key"] == tree_key(COMMAND)
        assert rows["tree"]["goals"] == [display_template(g) for g in
                                         (s["goal"] for s in mem.plans[tree_key(COMMAND)].steps)]
        assert rows["tree"]["goals"][1] == "Save the document as «2» on the Desktop"
        assert rows["phase"]["goals"][1] == "Type «1» into the file name field"
        assert rows["plan"]["goals"] == ["Open Notepad"]
        for row in rows.values():
            assert {"key", "command", "goals", "successes", "failures", "usable", "model",
                    "last_used"} <= set(row)

    def test_tree_and_phase_rows_survive_a_save_by_an_older_build(self, tmp_path):
        path = tmp_path / "m.json"
        # The older build: opened before the new rows existed, so its copy of
        # ``plans`` holds none of them, and it knows only today's sections.
        old = Experience(path)
        assert SECTIONS == ("recipes", "plans", "lessons")
        new = Experience(path)
        new.learn_tree(COMMAND, tree_items())
        new.learn_decomposition(APP, SAVE_PHASE, save_children())
        new.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "max_steps")
        old.learn_plan("open notepad", [{"goal": "Open Notepad", "launch": "notepad.exe"}])
        assert old.save()

        third = Experience(path)
        assert tree_key(COMMAND) in third.plans
        assert phase_key(APP, SAVE_PHASE) in third.plans
        assert third.plan_for("open notepad") is not None
        assert third.plan_for(COMMAND) is None
        assert third.tree_for(COMMAND_2)[0][1]["children"][2]["goal"] == \
            "Confirm the save of zakupy.txt"
        assert third.decomposition_for(APP, SAVE_PHASE_2) is not None
        assert third.lessons_for(APP, SAVE_PHASE)[0].startswith("sub-goal ")
        # The file itself holds them as plain ``plans`` rows, nested children
        # and all: nothing an older reader would have to understand.
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) == {"schema", "saved", "forgotten"} | set(SECTIONS)
        assert data["plans"][tree_key(COMMAND)]["steps"][1]["children"][1]["checks"]

    def test_forgetting_a_tree_row_is_durable(self, tmp_path):
        path = tmp_path / "m.json"
        stale = Experience(path)
        mem = Experience(path)
        mem.learn_tree(COMMAND, tree_items())
        stale.load()
        assert tree_key(COMMAND) in stale.plans
        assert mem.forget(kind="plan", key=tree_key(COMMAND)) is True
        # A stale copy in another process saving afterwards must not bring it back.
        stale.learn_plan("open notepad", [{"goal": "Open Notepad"}])
        reloaded = Experience(path)
        assert tree_key(COMMAND) not in reloaded.plans
        assert reloaded.tree_for(COMMAND_2) is None
        assert reloaded.plan_for("open notepad") is not None

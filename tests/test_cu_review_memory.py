"""Regression tests for the code-review findings on the operator's memory.

Each test names the finding it pins (#17-#25 of the cu-learn review) and fails
on the code as it was before the fix: an unsaved title taken as proof of a
save, a phase break-down that carried the first mission's file name into the
next, a "forget" or "clear" from another process that did not stick, a typed
value taken as proof of a goal whose commit never happened, a malformed row
that crashed verification, and bounds the save undid. Offline and
desktop-free; ``experience.py`` is pure Python.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from jevskill.cu import experience
from jevskill.cu.act import Action
from jevskill.cu.agenda import UNSAVED_DOTS
from jevskill.cu.experience import (Experience, PlanMemory, Recipe, RecipeStep, Trajectory,
                                    command_key, literal_check, phase_key, recipe_check,
                                    tree_key)
from jevskill.cu.types import Snapshot, UIElement

APP = "Notepad"
COMMAND = "wpisz „hello world” i zapisz jako hello.txt"
SAVE_PHASE = "Save the document as hello.txt on the Desktop"
SAVE_PHASE_2 = "Save the document as zakupy.txt on the Desktop"
#: The phase the model wrote in the review's case: it names no file.
BARE_PHASE = "Save the document on the Desktop"

FIELD = ["edit", "Znajdź", "find", "Edit"]
FIND_NEXT = ["button", "Znajdź następny", "next", "Button"]
SAVE_BUTTON = ["button", "Zapisz", "1", "Button"]


def el(id_, role, name="", **kw):
    kw.setdefault("bbox", (0, 0, 50, 20))
    return UIElement(id_, role, name, **kw)


def snap(title, elements=()):
    return Snapshot(title, "Notepad.exe", 1, 1, 0.0, 0.0, list(elements))


def find_screen(value="foo", title="Notatnik"):
    return snap(title, [el("e1", "edit", "Znajdź", value=value, automation_id="find",
                           class_name="Edit"),
                             el("e2", "button", "Znajdź następny", automation_id="next",
                                class_name="Button")])


def typed_trajectory(field_name):
    """One goal that typed into a field that stayed on the screen."""
    field = el("f", "edit", field_name, automation_id=field_name, class_name="Edit")
    traj = Trajectory()
    before = snap("A - App", [field])
    traj.observed(before)
    traj.acted(Action(op="type", target="f", text="x"), before, True, "", "literal")
    traj.observed(snap("A - App", [field, el("moved", "button", "moved")]))
    traj.close()
    return traj


def save_children(value="hello.txt"):
    return [
        {"goal": "Open the Save As dialog", "done_when": "a dialog titled 'Zapisz jako' is open",
         "app": APP},
        {"goal": "Type %s into the file name field" % value,
         "done_when": "the file name field holds '%s'" % value, "app": APP},
        {"goal": "Confirm the save", "done_when": "the title changes", "app": APP},
    ]


# --------------------------------------------------------------------------- #
# #18 an unsaved title is not a save
# --------------------------------------------------------------------------- #


class TestUnsavedTitle:
    def test_literal_check_does_not_take_an_unsaved_title_for_a_save(self):
        done_when = "The Notepad title contains 'hello.txt'"
        assert literal_check(done_when, snap("hello.txt - Notatnik")) is True
        assert literal_check(done_when, snap("*hello.txt - Notatnik")) is None
        for dot in UNSAVED_DOTS:
            assert literal_check(done_when, snap("%s hello.txt - Editor" % dot)) is None

    def test_recipe_check_requires_the_saved_state_it_learned(self):
        save = Recipe(app="notepad.exe", goal="save", steps=[RecipeStep("click", SAVE_BUTTON)],
                      start_title="Bez tytułu - Notatnik", end_title="hello.txt - Notatnik")
        assert recipe_check(save, snap("hello.txt - Notatnik")) is True
        assert recipe_check(save, snap("*hello.txt - Notatnik")) is None
        for dot in UNSAVED_DOTS:
            assert recipe_check(save, snap("%s hello.txt - Notatnik" % dot)) is None
        # Templated the way learn_recipe stores it, filled at check time.
        templated = Recipe(app="notepad.exe", goal="save", steps=[RecipeStep("click", SAVE_BUTTON)],
                           start_title="Bez tytułu - Notatnik", end_title="⟦g0⟧ - Notatnik")
        assert recipe_check(templated, snap("*zakupy.txt - Notatnik"),
                            goal_slots=["zakupy.txt"]) is None
        assert recipe_check(templated, snap("zakupy.txt - Notatnik"),
                            goal_slots=["zakupy.txt"]) is True
        # A goal that ended dirty wants it dirty: a clean title is the goal undone.
        typed = Recipe(app="notepad.exe", goal="new", steps=[RecipeStep("key", key="ctrl+n")],
                       start_title="Pulpit", end_title="*Bez tytułu - Notatnik")
        assert recipe_check(typed, snap("*Bez tytułu - Notatnik")) is True
        assert recipe_check(typed, snap("Bez tytułu - Notatnik")) is None

    def test_a_dot_marker_is_not_a_new_window(self):
        """The same normalisation decides "new window" in the trajectory: a
        dirty dot appearing after typing opened nothing."""
        field = el("f", "edit", "Editor", automation_id="ed", class_name="Edit")
        for dot in UNSAVED_DOTS:
            traj = Trajectory()
            before = snap("a.txt - Editor", [field])
            traj.observed(before)
            traj.acted(Action(op="type", target="f", text="x"), before, True)
            traj.observed(snap("%s a.txt - Editor" % dot, [field, el("m", "button", "moved")]))
            traj.close()
            assert traj.attempts[0].changed and not traj.attempts[0].new_window, dot

    def test_a_dot_used_as_a_separator_is_not_a_marker(self):
        """``Inbox • Slack`` is a saved title: a dot counted anywhere left
        every check on it undecided forever."""
        for dot in UNSAVED_DOTS:
            title = "Inbox %s Slack" % dot
            assert literal_check("The window title contains 'Inbox'", snap(title)) is True
            assert literal_check("The window title contains 'Slack'",
                                 snap("Instagram %s Photos - Slack" % dot)) is True
            chat = Recipe(app="slack.exe", goal="open", steps=[RecipeStep("click", SAVE_BUTTON)],
                          start_title="Slack", end_title=title)
            assert recipe_check(chat, snap(title)) is True

    def test_a_trailing_star_is_a_marker_and_opens_no_window(self):
        """``a.png* - Paint.NET``: the marker closes the document part. The
        normaliser strips it where the check reads it, so typing into the
        picture is not a new window either."""
        assert literal_check("The title contains 'a.png'", snap("a.png* - Paint.NET")) is None
        assert literal_check("The title contains 'a.png'", snap("a.png - Paint.NET")) is True
        field = el("f", "edit", "Canvas", automation_id="cv", class_name="Edit")
        traj = Trajectory()
        before = snap("a.png - Paint.NET", [field])
        traj.observed(before)
        traj.acted(Action(op="type", target="f", text="x"), before, True)
        traj.observed(snap("a.png* - Paint.NET", [field, el("m", "button", "moved")]))
        traj.close()
        assert traj.attempts[0].changed and not traj.attempts[0].new_window


# --------------------------------------------------------------------------- #
# #19 a phase break-down must not carry the command's values as literals
# --------------------------------------------------------------------------- #


class TestDecompositionLeak:
    def test_a_break_down_with_a_command_value_the_phase_lacks_is_refused(self, tmp_path):
        mem = Experience(tmp_path / "m.json")
        assert mem.learn_decomposition(APP, BARE_PHASE, save_children(), command=COMMAND) is False
        assert phase_key(APP, BARE_PHASE) not in mem.plans
        assert mem.decomposition_for(APP, BARE_PHASE) is None
        # Under a phase that names the file, the value is a slot: kept, templated.
        assert mem.learn_decomposition(APP, SAVE_PHASE, save_children(), command=COMMAND) is True
        children, _ = mem.decomposition_for(APP, SAVE_PHASE_2)
        assert children[1]["goal"] == "Type zakupy.txt into the file name field"

    @pytest.mark.parametrize("where", ["check", "launch", "nested", "case"])
    def test_the_value_is_found_wherever_a_replay_would_use_it(self, tmp_path, where):
        mem = Experience(tmp_path / "m.json")
        child = {"goal": "Confirm the save", "done_when": "", "app": APP}
        if where == "check":
            child["checks"] = [{"kind": "file_exists", "arg": "%USERPROFILE%\\Desktop\\hello.txt"}]
        elif where == "launch":
            child["launch"] = "notepad.exe hello.txt"
        elif where == "nested":
            child["kind"] = "phase"
            child["children"] = save_children()
        else:
            child["goal"] = "Type Hello.txt into the field"
        children = [{"goal": "Open the Save As dialog", "done_when": ""}, child]
        assert mem.learn_decomposition(APP, BARE_PHASE, children, command=COMMAND) is False

    def test_no_false_refusal_for_a_short_value_or_one_inside_a_goal_slot(self, tmp_path):
        mem = Experience(tmp_path / "m.json")
        # „a” is not inside "Save As"; „hello” is not left over inside hello.txt.
        assert mem.learn_decomposition(APP, BARE_PHASE,
                                       [{"goal": "Open the Save As dialog", "app": APP}],
                                       command="wpisz „a” i zapisz") is True
        assert mem.learn_decomposition(APP, SAVE_PHASE, save_children(),
                                       command="wpisz „hello” i zapisz jako hello.txt") is True

    def test_an_executable_the_command_names_does_not_refuse_its_own_app(self, tmp_path):
        """The command names the program. Every child carries it as its
        ``app`` and the first one launches it — neither can leak, since the
        app is part of the phase key — yet the scan refused every break-down
        of the mission."""
        mem = Experience(tmp_path / "m.json")
        command = "Otwórz notepad.exe i zapisz jako hello.txt"
        assert "notepad.exe" in command_key(command)[1], "premise: the program is a slot"
        children = [dict(child, app="notepad.exe") for child in save_children()]
        children[0]["launch"] = "notepad.exe"
        assert mem.learn_decomposition("notepad.exe", SAVE_PHASE, children,
                                       command=command) is True
        # The real leak of the same mission is still refused: the bare phase
        # names no file, its child types hello.txt.
        assert mem.learn_decomposition("notepad.exe", BARE_PHASE, children,
                                       command=command) is False
        # So is a launch that carries the document, and the program named in a goal.
        for child in ({"goal": "Open the document", "launch": "notepad.exe hello.txt"},
                      {"goal": "Start notepad.exe"}):
            assert mem.learn_decomposition("notepad.exe", BARE_PHASE,
                                           [dict(child, app="notepad.exe")],
                                           command=command) is False, child

    def test_without_a_command_the_old_behaviour_holds(self, tmp_path):
        mem = Experience(tmp_path / "m.json")
        assert mem.learn_decomposition(APP, BARE_PHASE, save_children()) is True
        assert mem.learn_decomposition("", BARE_PHASE, save_children()) is False
        assert mem.learn_decomposition(APP, BARE_PHASE, [{"goal": ""}]) is False


# --------------------------------------------------------------------------- #
# #21 forget and clear from another process
# --------------------------------------------------------------------------- #


def learned_everywhere(mem):
    """One row of every kind a lookup serves; returns ``(section, key, lookup)``."""
    mem.learn_plan(COMMAND, [{"goal": "Do it"}])
    mem.learn_tree(COMMAND, [{"goal": "Write „hello world”", "done_when": ""}])
    mem.learn_decomposition(APP, SAVE_PHASE, save_children())
    mem.learn_recipe(APP, "Type into the field", COMMAND, typed_trajectory("Name"))
    mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "max_steps")
    lesson_key = next(iter(mem.lessons))
    return [
        ("plans", command_key(COMMAND)[0], lambda m: m.plan_for(COMMAND)),
        ("plans", tree_key(COMMAND), lambda m: m.tree_for(COMMAND)),
        ("plans", phase_key(APP, SAVE_PHASE), lambda m: m.decomposition_for(APP, SAVE_PHASE)),
        ("recipes", Experience.recipe_key(APP, "Type into the field"),
         lambda m: m.recipe_for(APP, "Type into the field")),
        ("lessons", lesson_key, lambda m: m.lessons_for(APP, SAVE_PHASE) or None),
    ]


class TestForgetAcrossProcesses:
    def test_a_forget_elsewhere_stops_every_lookup_at_once(self, tmp_path):
        path = tmp_path / "m.json"
        console = Experience(path)
        rows = learned_everywhere(console)
        for _, _, lookup in rows:
            assert lookup(console) is not None
        cli = Experience(path)
        for section, key, _ in rows:
            assert cli.forget(section, key) is True
        for section, key, lookup in rows:
            assert lookup(console) is None, (section, key)
            assert key not in getattr(console, section)

    def test_a_copy_merely_used_after_the_forget_does_not_come_back(self, tmp_path):
        path = tmp_path / "m.json"
        console = Experience(path, autosave=False)
        console.learn_plan(COMMAND, [{"goal": "Do it"}])
        assert console.save()
        key = command_key(COMMAND)[0]
        Experience(path).forget("plan", key)
        tomb = json.loads(path.read_text(encoding="utf-8"))["forgotten"]["plans"][key]
        # The stale copy is used after the forget with no refresh in between,
        # unambiguously later than the tombstone. A direct bump: the failure
        # and replay paths refresh first now (see TestRelearnSurvivesAStaleCopy).
        console.plans[key].failures += 1
        console.plans[key].last_used = tomb + 1.0
        assert console.save()
        assert key not in json.loads(path.read_text(encoding="utf-8"))["plans"]
        assert key not in console.plans
        # Learning it anew is what brings a row back.
        console.learn_plan(COMMAND, [{"goal": "Do it again"}])
        console.plans[key].learned_at = tomb + 2.0
        assert console.save()
        assert Experience(path).plan_for(COMMAND)[0][0]["goal"] == "Do it again"

    def test_clear_forgets_what_another_process_learned_after_this_one_loaded(self, tmp_path):
        path = tmp_path / "m.json"
        console = Experience(path)
        cli = Experience(path)
        cli.learn_plan(COMMAND, [{"goal": "Do it"}])
        cli.learn_subgoal_lesson(APP, SAVE_PHASE, "Press ctrl+s", "max_steps")
        assert console.clear() == 2
        assert Experience(path).plan_for(COMMAND) is None
        # The CLI still holds its copies; its next save must not undo the clear.
        cli.learn_plan("another command", [{"goal": "Other"}])
        fresh = Experience(path)
        assert fresh.plan_for(COMMAND) is None and fresh.lessons == {}
        assert fresh.plan_for("another command") is not None
        assert cli.plan_for(COMMAND) is None

    def test_a_row_written_before_learned_at_existed_falls_back_to_last_used(self, tmp_path):
        assert PlanMemory.from_dict({"command": "c", "last_used": 12.5}).learned_at == 12.5
        assert PlanMemory.from_dict({"command": "c", "last_used": 12.5,
                                     "learned_at": 3.0}).learned_at == 3.0
        # Recent stamps: a tombstone older than FORGET_KEEP_S is expired.
        tomb = time.time() - 60.0
        path = tmp_path / "m.json"
        path.write_text(json.dumps({
            "schema": experience.SCHEMA, "saved": 0.0,
            "plans": {"kept": {"command": "kept", "steps": [{"goal": "a"}],
                               "last_used": tomb + 10.0},
                      "gone": {"command": "gone", "steps": [{"goal": "b"}],
                               "last_used": tomb - 10.0}},
            "recipes": {}, "lessons": {},
            "forgotten": {"plans": {"kept": tomb, "gone": tomb}},
        }), encoding="utf-8")
        mem = Experience(path)
        assert mem.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data["plans"]) == {"kept"}
        assert data["plans"]["kept"]["learned_at"] == tomb + 10.0


# --------------------------------------------------------------------------- #
# #21b a stale copy used after another process forgot *and relearned* the row
# --------------------------------------------------------------------------- #


GOAL = "Type into the field"


def on_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def ticking(monkeypatch):
    """Every ``time.time()`` experience.py reads is a second after the last,
    so learned < forgotten < relearned < used holds by construction, not by
    how fine the machine's clock happens to be."""
    now = [time.time()]

    def tick():
        now[0] += 1.0
        return now[0]

    monkeypatch.setattr(experience, "time", SimpleNamespace(time=tick))


def _goal_tag(row):
    return row["steps"][0]["goal"]


def _recipe_tag(row):
    return row["steps"][0]["identity"][1]


#: ``path -> (section, key, learn(mem, tag), use(mem), tag(row on disk))``:
#: every path that touches a held row without a lookup first.
STALE_USES = {
    "plan_failed": ("plans", command_key(COMMAND)[0],
                    lambda m, tag: m.learn_plan(COMMAND, [{"goal": tag}]),
                    lambda m: m.plan_failed(COMMAND), _goal_tag),
    "tree_failed": ("plans", tree_key(COMMAND),
                    lambda m, tag: m.learn_tree(COMMAND, [{"goal": tag, "done_when": ""}]),
                    lambda m: m.tree_failed(COMMAND), _goal_tag),
    "decomposition_failed": ("plans", phase_key(APP, SAVE_PHASE),
                             lambda m, tag: m.learn_decomposition(APP, SAVE_PHASE,
                                                                  [{"goal": tag}]),
                             lambda m: m.decomposition_failed(APP, SAVE_PHASE), _goal_tag),
    "recipe_failed": ("recipes", Experience.recipe_key(APP, GOAL),
                      lambda m, tag: m.learn_recipe(APP, GOAL, COMMAND, typed_trajectory(tag)),
                      lambda m: m.recipe_failed(APP, GOAL, "replay did not reach the goal"),
                      _recipe_tag),
    "replayed": ("recipes", Experience.recipe_key(APP, GOAL),
                 lambda m, tag: m.learn_recipe(APP, GOAL, COMMAND, typed_trajectory(tag)),
                 lambda m: m.learn_recipe(APP, GOAL, COMMAND, typed_trajectory("Old"),
                                          replayed=True),
                 _recipe_tag),
}


class TestRelearnSurvivesAStaleCopy:
    def test_the_tombstone_judges_each_copy_before_last_used_picks_one(self, tmp_path,
                                                                        ticking):
        """The console's stale copy, used later than the CLI's relearn, won on
        ``last_used``, was dropped by the tombstone — and the relearned row,
        never considered, was lost from the file."""
        path = tmp_path / "m.json"
        key = command_key(COMMAND)[0]
        console = Experience(path, autosave=False)
        console.learn_plan(COMMAND, [{"goal": "Old"}])
        assert console.save()
        cli = Experience(path)
        assert cli.forget("plan", key) is True
        cli.learn_plan(COMMAND, [{"goal": "New"}])
        relearned = on_disk(path)["plans"][key]
        tomb = on_disk(path)["forgotten"]["plans"][key]
        assert console.plans[key].learned_at <= tomb < relearned["learned_at"]
        # Used with no refresh in between, later than the relearn.
        console.plans[key].failures += 1
        console.plans[key].last_used = relearned["last_used"] + 1.0
        assert console.save()
        assert on_disk(path)["plans"].get(key) == relearned
        assert _goal_tag(console.plans[key].to_dict()) == "New"

    def test_a_tie_on_last_used_still_goes_to_this_process(self, tmp_path, ticking):
        path = tmp_path / "m.json"
        key = command_key(COMMAND)[0]
        one, two = Experience(path, autosave=False), Experience(path, autosave=False)
        one.learn_plan(COMMAND, [{"goal": "One"}])
        assert one.save()
        two.learn_plan(COMMAND, [{"goal": "Two"}])
        two.plans[key].last_used = one.plans[key].last_used
        assert two.save()
        assert _goal_tag(on_disk(path)["plans"][key]) == "Two"

    @pytest.mark.parametrize("use", sorted(STALE_USES))
    def test_a_stale_copy_that_fails_or_replays_leaves_the_relearn_alone(self, tmp_path,
                                                                          ticking, use):
        section, key, learn, stale_use, tag = STALE_USES[use]
        path = tmp_path / "m.json"
        console = Experience(path)
        learn(console, "Old")
        assert key in getattr(console, section)
        cli = Experience(path)
        assert cli.forget(section, key) is True
        learn(cli, "New")
        before = on_disk(path)
        assert tag(before[section][key]) == "New"
        # The console looked the row up before the forget; now its run fails
        # (or its replay succeeds), with no lookup in between.
        assert stale_use(console) is None, "a forgotten replay is not 'confirmed'"
        after = on_disk(path)
        assert after[section].get(key) == before[section][key]
        # The stale copy is dropped, not counted: nothing is written for it.
        assert after["saved"] == before["saved"]
        assert key not in getattr(console, section)


# --------------------------------------------------------------------------- #
# #22 a value left in its field is not proof of a commit that never happened
# --------------------------------------------------------------------------- #


class TestValuesEvidence:
    def test_a_typed_value_does_not_prove_a_goal_that_ended_with_a_click(self):
        find = Recipe(app="notepad.exe", goal="find",
                      steps=[RecipeStep("type", FIELD, text="foo"), RecipeStep("click", FIND_NEXT)],
                      start_title="Notatnik", end_title="Notatnik", values=[[FIELD, "foo"]])
        assert recipe_check(find, find_screen()) is None

    def test_it_still_proves_a_goal_whose_last_act_was_the_typing(self):
        typed = Recipe(app="notepad.exe", goal="type",
                       steps=[RecipeStep("click", FIND_NEXT), RecipeStep("type", FIELD, text="foo")],
                       start_title="Notatnik", end_title="Notatnik", values=[[FIELD, "foo"]])
        assert recipe_check(typed, find_screen()) is True
        assert recipe_check(typed, find_screen("bar")) is None

    def test_a_title_that_changed_as_learned_still_proves_it(self):
        find = Recipe(app="notepad.exe", goal="find",
                      steps=[RecipeStep("type", FIELD, text="foo"), RecipeStep("click", FIND_NEXT)],
                      start_title="Notatnik", end_title="Wyniki - Notatnik",
                      values=[[FIELD, "foo"]])
        assert recipe_check(find, find_screen(title="Wyniki - Notatnik")) is True
        assert recipe_check(find, find_screen()) is None


# --------------------------------------------------------------------------- #
# #24 a malformed values row
# --------------------------------------------------------------------------- #


class TestMalformedValues:
    GOOD = [FIELD, "foo"]

    def test_from_dict_keeps_only_identity_text_pairs(self):
        recipe = Recipe.from_dict({"app": "a", "goal": "g", "values": [
            [], [[]], [[], "x"], [FIELD, "foo", "extra"], [["edit", 5], "x"], [FIELD, 5],
            "junk", self.GOOD]})
        assert recipe.values == [self.GOOD]

    def test_a_bad_row_in_the_file_loads_and_verifies_without_raising(self, tmp_path):
        path = tmp_path / "m.json"
        key = Experience.recipe_key(APP, "Type foo")
        path.write_text(json.dumps({
            "schema": experience.SCHEMA, "saved": 0.0, "plans": {}, "lessons": {},
            "recipes": {key: {"app": APP.casefold(), "goal": "type foo",
                              "steps": [{"op": "type", "identity": FIELD, "text": "foo"}],
                              "values": [[[]], [FIELD, "foo", "x"], self.GOOD]}},
        }), encoding="utf-8")
        recipe = Experience(path).recipe_for(APP, "Type foo")
        assert recipe.values == [self.GOOD]
        assert recipe_check(recipe, find_screen()) is True

    @pytest.mark.parametrize("values", [[[]], [[FIELD, "foo", "extra"]], [None], [[FIELD]]])
    def test_recipe_check_never_raises(self, values):
        bad = Recipe(app="a", goal="g", steps=[RecipeStep("type", FIELD, text="foo")],
                     values=values)
        assert recipe_check(bad, find_screen()) is None


# --------------------------------------------------------------------------- #
# #25 the bounds hold in the file, not only until the next save
# --------------------------------------------------------------------------- #


def learn_one(mem, section, tag, index):
    if section == "plans":
        mem.learn_plan("command %s %d" % (tag, index), [{"goal": "Do %d" % index}])
    elif section == "recipes":
        mem.learn_recipe(APP, "Goal %s %d" % (tag, index), "cmd",
                         typed_trajectory("Field %s %d" % (tag, index)))
    else:
        mem.learn_subgoal_lesson(APP, SAVE_PHASE, "Child %s %d" % (tag, index), "x")


@pytest.mark.parametrize("section, constant", [("plans", "MAX_PLANS"),
                                               ("recipes", "MAX_RECIPES"),
                                               ("lessons", "MAX_LESSONS")])
def test_the_bound_holds_after_merging_with_another_process(tmp_path, monkeypatch,
                                                            section, constant):
    monkeypatch.setattr(experience, constant, 3)
    limit = getattr(experience, constant)
    path = tmp_path / "m.json"
    one, two = Experience(path, autosave=False), Experience(path, autosave=False)
    for index in range(limit):
        learn_one(one, section, "one", index)
        learn_one(two, section, "two", index)
    # Distinct, known ages: every row of ``two`` is newer than every row of ``one``.
    for base, mem in ((1000.0, one), (2000.0, two)):
        for offset, row in enumerate(getattr(mem, section).values()):
            row.last_used = base + offset
    newest = set(getattr(two, section))
    assert one.save() and two.save()
    on_disk = json.loads(path.read_text(encoding="utf-8"))[section]
    assert len(on_disk) == limit and len(getattr(two, section)) == limit
    assert set(on_disk) == set(getattr(two, section)) == newest
    # And the older process's next save keeps the file bounded too.
    assert one.save()
    assert len(json.loads(path.read_text(encoding="utf-8"))[section]) == limit

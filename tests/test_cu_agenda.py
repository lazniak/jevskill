"""The mission tree's pure half: coercion, walking, persistence, checks.

Everything here is offline and desktop-free; the fake step stands in for the
runner's PlanStep because agenda.py must not import the runner.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from jevskill.cu import agenda
from jevskill.cu.agenda import (
    CHECK_KINDS, CLOSED, DONE_WHEN_CAP, FILE_READ_CAP, GOAL_CAP, MAX_CHILDREN,
    MAX_DEPTH, MAX_PHASES, OPEN_LEAF, Check, PlanNode, child_prefix,
    classify_irreversible, coerce_tree, evaluate_checks, quote_on_screen,
    repair_prefix,
)
from jevskill.cu.types import Snapshot, UIElement


@dataclass
class FakeStep:
    goal: str
    done_when: str = ""
    launch: object = None
    status: str = "pending"

    def to_dict(self):
        return {"goal": self.goal, "done_when": self.done_when,
                "launch": self.launch, "status": self.status}


def make_step(node):
    return FakeStep(goal=node.goal, done_when=node.done_when, launch=node.launch)


def load_step(d, node):
    return FakeStep(goal=d.get("goal", node.goal), done_when=d.get("done_when", ""),
                    launch=d.get("launch"), status=d.get("status", "pending"))


def root_with(items, **kw):
    root = PlanNode(id="0", goal="the command", kind="phase", expanded=True)
    root.add_children(coerce_tree(items, depth=1, id_prefix=child_prefix(root),
                                  source=kw.pop("source", "model"), make_step=make_step, **kw))
    return root


def phase(goal, children, **extra):
    return dict({"goal": goal, "kind": "phase", "children": children}, **extra)


def snap(title="", elements=()):
    return Snapshot(window_title=title, app="notepad.exe", pid=1, hwnd=1, taken_at=0.0,
                    elapsed_ms=0.0, elements=list(elements))


# --------------------------------------------------------------------------- #
# 1. coerce_tree
# --------------------------------------------------------------------------- #

def test_flat_items_become_depth_one_leaves_with_steps():
    items = ["Open Notepad", {"goal": "Type hello", "done_when": "'hello'", "launch": " notepad "},
             {"goal": "Save it", "kind": "leaf"}]
    nodes = coerce_tree(items, depth=1, id_prefix="", source="model", make_step=make_step)
    assert [n.id for n in nodes] == ["1", "2", "3"]
    assert all(n.kind == "leaf" and n.depth == 1 and n.parent is None for n in nodes)
    assert all(isinstance(n.step, FakeStep) and n.step.goal == n.goal for n in nodes)
    assert nodes[1].launch == "notepad" and nodes[1].done_when == "'hello'"
    assert nodes[0].launch is None and nodes[0].source == "model"


def test_clamps_match_the_flat_step_clamps():
    nodes = coerce_tree([{"goal": "g" * (GOAL_CAP + 50), "done_when": "d" * (DONE_WHEN_CAP + 9)}],
                        depth=1, id_prefix="", source="model")
    assert len(nodes[0].goal) == GOAL_CAP and len(nodes[0].done_when) == DONE_WHEN_CAP


def test_nested_phase_is_expanded_with_child_ids_and_parents():
    items = [phase("Write", ["type a", "type b"], app="notepad.exe"), "Close"]
    nodes = coerce_tree(items, depth=1, id_prefix="", source="model", make_step=make_step)
    write = nodes[0]
    assert write.kind == "phase" and write.expanded and write.step is None
    assert [c.id for c in write.children] == ["1.1", "1.2"]
    assert all(c.parent is write and c.depth == 2 for c in write.children)
    assert all(isinstance(c.step, FakeStep) for c in write.children)
    assert nodes[1].id == "2"


def test_phase_without_children_waits_for_expansion():
    nodes = coerce_tree([phase("Later", []), "x"], depth=1, id_prefix="", source="model")
    assert nodes[0].kind == "phase" and not nodes[0].expanded and nodes[0].children == []


def test_phase_at_max_depth_becomes_a_leaf():
    nodes = coerce_tree([phase("Deep", ["a", "b"])], depth=MAX_DEPTH, id_prefix="1.1",
                        source="model", make_step=make_step)
    assert nodes[0].kind == "leaf" and nodes[0].children == [] and nodes[0].id == "1.1.1"
    assert isinstance(nodes[0].step, FakeStep)


def test_nesting_stops_at_max_depth():
    deep = phase("L1", [phase("L2", [phase("L3", ["x", "y"]), "b"]), "c"])
    root = root_with([deep])
    assert max(n.depth for n in root.walk()) == MAX_DEPTH
    assert all(n.kind == "leaf" for n in root.walk() if n.depth == MAX_DEPTH)


def test_one_child_phase_collapses_into_the_child():
    items = ["first", phase("Only", [{"goal": "the child"}], app="calc.exe", done_when="'42'")]
    nodes = coerce_tree(items, depth=1, id_prefix="", source="model", make_step=make_step)
    child = nodes[1]
    assert child.id == "2" and child.depth == 1 and child.kind == "leaf"
    assert child.goal == "the child" and child.app == "calc.exe" and child.done_when == "'42'"
    assert child.step.done_when == "'42'"


def test_collapsed_child_keeps_its_own_app_and_done_when():
    items = [phase("Only", [{"goal": "c", "app": "a.exe", "done_when": "'x'"}, 7],
                   app="b.exe", done_when="'y'")]
    child = coerce_tree(items, depth=1, id_prefix="", source="model")[0]
    assert (child.app, child.done_when) == ("a.exe", "'x'")


def test_children_capped_and_phases_capped():
    many = [phase("P%d" % i, ["c%d" % j for j in range(MAX_CHILDREN + 4)])
            for i in range(MAX_PHASES + 3)]
    nodes = coerce_tree(many, depth=1, id_prefix="", source="model")
    assert len(nodes) == MAX_PHASES
    assert all(len(n.children) == MAX_CHILDREN for n in nodes)
    flat = coerce_tree(["g%d" % i for i in range(MAX_PHASES + 3)], depth=1, id_prefix="",
                       source="model")
    assert len(flat) == MAX_CHILDREN
    assert len(coerce_tree(["a", "b", "c"], depth=1, id_prefix="", source="m", max_items=2)) == 2


def test_garbage_and_unknown_checks_dropped():
    items = [None, 3, "", "  ", {"goal": ""}, {"no": "goal"}, [],
             {"goal": "keep", "checks": [{"kind": "file_exists", "arg": "C:/a.txt"},
                                         {"kind": "registry_has", "arg": "HKLM"},
                                         {"kind": "file_exists", "arg": " "},
                                         {"kind": "file_contains", "arg": "x"},
                                         "garbage"],
              "weight": "heavy", "optional": "false"}]
    nodes = coerce_tree(items, depth=1, id_prefix="", source="model")
    assert len(nodes) == 1 and nodes[0].id == "1"
    assert nodes[0].checks == [Check("file_exists", "C:/a.txt")]
    assert nodes[0].weight == 1.0 and nodes[0].optional is False


def test_weight_is_clamped():
    nodes = coerce_tree([{"goal": "a", "weight": 1000}, {"goal": "b", "weight": -3},
                         {"goal": "c", "weight": float("nan")}, {"goal": "d", "weight": 2.5}],
                        depth=1, id_prefix="", source="model")
    assert [n.weight for n in nodes] == [agenda.WEIGHT_MAX, agenda.WEIGHT_MIN, 1.0, 2.5]


@pytest.mark.parametrize("bad", [[], [None, "", {"goal": " "}], "not a list", None])
def test_empty_input_raises(bad):
    with pytest.raises(ValueError):
        coerce_tree(bad, depth=1, id_prefix="", source="model")


def test_every_node_irreversible_is_classified():
    nodes = coerce_tree([phase("Save the report", ["type it", "Zapisz jako a.txt"])],
                        depth=1, id_prefix="", source="model")
    assert nodes[0].irreversible is True
    assert [c.irreversible for c in nodes[0].children] == [False, True]


def test_check_from_dict_rejects_unknown_kind_and_empty_arg():
    for kind in CHECK_KINDS:
        assert Check.from_dict({"kind": kind, "arg": "x", "text": "t"}).kind == kind
    with pytest.raises(ValueError):
        Check.from_dict({"kind": "nope", "arg": "x"})
    with pytest.raises(ValueError):
        Check.from_dict({"kind": "file_exists", "arg": ""})
    with pytest.raises(ValueError):
        Check.from_dict("file_exists")


# --------------------------------------------------------------------------- #
# 2. persistence and ids
# --------------------------------------------------------------------------- #

def test_to_dict_from_dict_round_trip_with_parents_and_steps():
    root = root_with([phase("Write", ["type a", {"goal": "type b", "checks": [
        {"kind": "title_contains", "arg": "Notepad"}]}], app="notepad.exe"),
        phase("Save", []), "Close"])
    root.children[0].children[0].status = "done"
    root.children[0].children[0].step.status = "done"
    root.children[0].children[1].tried = ["click e3"]
    root.children[0].spend["llm_calls"] = 2
    d = root.to_dict()
    back = PlanNode.from_dict(d, step_loader=load_step)
    assert back.to_dict() == d
    assert back == root
    for node in back.walk():
        for child in node.children:
            assert child.parent is node
        if node.kind == "leaf":
            assert isinstance(node.step, FakeStep)
    assert back.find("1.1").step.status == "done"
    assert back.find("2").step is None  # unexpanded phase: no step


def test_from_dict_without_loader_leaves_steps_empty():
    back = PlanNode.from_dict(root_with(["a", "b"]).to_dict())
    assert all(n.step is None for n in back.walk())


def test_shallow_to_dict_has_no_children():
    root = root_with(["a", "b"])
    assert "children" not in root.to_dict(deep=False)


def test_repair_ids_unique_across_generations():
    root = root_with([phase("P", ["a", "b"]), "c"])
    scope = root.find("1")
    for g in (1, 2):
        scope.generation = g
        scope.add_children(coerce_tree(["fix a", phase("fix b", ["x", "y"])],
                                       depth=scope.depth + 1, id_prefix=repair_prefix(scope, g),
                                       source="repair"))
    root.generation = 1
    root.add_children(coerce_tree(["again"], depth=1, id_prefix=repair_prefix(root, 1),
                                  source="repair"))
    ids = [n.id for n in root.walk()]
    assert len(ids) == len(set(ids))
    assert {"1.r1.1", "1.r1.2.1", "1.r2.1", "r1.1"} <= set(ids)
    assert root.find("1.r1.1").source == "repair" and root.find("1.r1.1").parent is scope


def test_id_prefixes():
    root = PlanNode(id="0", goal="r", kind="phase")
    node = PlanNode(id="2", goal="p", kind="phase")
    assert child_prefix(root) == "" and child_prefix(node) == "2"
    assert repair_prefix(root, 1) == "r1" and repair_prefix(node, 3) == "2.r3"
    assert agenda.child_id("", 4) == "4" and agenda.child_id("2.r1", 1) == "2.r1.1"


def test_add_children_sets_parent_pointers():
    root = PlanNode(id="0", goal="r", kind="phase")
    kids = [PlanNode(id="1", goal="a"), PlanNode(id="2", goal="b")]
    root.add_children(kids)
    assert root.children == kids and all(k.parent is root for k in kids)
    assert kids[1].path() == [root, kids[1]]


def test_item_round_trips_through_coerce_tree():
    items = [phase("Write", ["type a", {"goal": "Save as a.txt", "done_when": "'a.txt'",
                                        "checks": [{"kind": "file_exists", "arg": "%TEMP%/a"}]}],
                   app="notepad.exe", weight=2.0, optional=True),
             phase("Later", []), {"goal": "Open", "launch": "notepad"}]
    first = [n.item() for n in coerce_tree(items, depth=1, id_prefix="", source="model")]
    again = [n.item() for n in coerce_tree(first, depth=1, id_prefix="", source="model")]
    assert again == first
    assert "children" in first[0] and "children" not in first[1]


def test_walk_leaves_phase_path():
    root = root_with([phase("P", ["a", phase("Q", ["x", "y"])]), phase("Lazy", []), "c"])
    assert [n.id for n in root.walk()] == ["0", "1", "1.1", "1.2", "1.2.1", "1.2.2", "2", "3"]
    assert [n.id for n in root.leaves()] == ["1.1", "1.2.1", "1.2.2", "3"]
    x = root.find("1.2.1")
    assert x.phase().id == "1.2" and root.find("1.2").phase().id == "1"
    assert root.find("3").phase() is root and root.phase() is root
    assert [n.id for n in x.path()] == ["0", "1", "1.2", "1.2.1"]
    assert root.find("nope") is None


# --------------------------------------------------------------------------- #
# 3. next_open
# --------------------------------------------------------------------------- #

def test_next_open_dfs_order():
    root = root_with([phase("P", ["a", "b"]), phase("Lazy", []), "c"])
    assert root.next_open().id == "1.1"
    root.find("1.1").status = "running"
    assert root.next_open().id == "1.2"  # a running leaf is not handed out twice
    root.find("1.1").status = "done"
    root.find("1.2").status = "done"
    assert root.next_open().id == "1"  # all children closed: close the phase
    root.find("1").status = "done"
    assert root.next_open().id == "2"  # unexpanded: break it down
    root.find("2").status = "skipped"
    assert root.next_open().id == "3"
    root.find("3").status = "done"
    assert root.next_open() is root
    root.status = "done"
    assert root.next_open() is None


def test_next_open_skips_superseded_and_counts_interrupted_as_open():
    root = root_with([phase("P", ["a", "b"])])
    scope = root.find("1")
    a = root.find("1.1")
    a.status, a.superseded = "failed", True
    scope.add_children(coerce_tree(["a again"], depth=2, id_prefix=repair_prefix(scope, 1),
                                   source="repair"))
    root.find("1.2").status = "interrupted"
    assert root.next_open().id == "1.2"
    for status in OPEN_LEAF:
        root.find("1.2").status = status
        assert root.next_open().id == "1.2"
    root.find("1.2").status = "done"
    assert root.next_open().id == "1.r1.1"
    root.find("1.r1.1").status = "done"
    assert root.next_open() is scope
    assert all(n.is_closed for n in scope.children)
    assert set(CLOSED) == {"done", "skipped", "failed"}


# --------------------------------------------------------------------------- #
# 4. evaluate_checks
# --------------------------------------------------------------------------- #

def test_file_checks_with_windows_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("JEVTESTDIR", str(tmp_path))
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    ok, detail = evaluate_checks([Check("file_exists", "%JEVTESTDIR%/hello.txt"),
                                  {"kind": "file_contains", "arg": "$JEVTESTDIR/hello.txt",
                                   "text": "world"}], None)
    assert ok is True and "true" in detail
    assert "hello world" not in detail  # contents never leak into the detail
    ok, detail = evaluate_checks([Check("file_contains", "%JEVTESTDIR%/hello.txt", "zakupy")],
                                 None)
    assert ok is False and str(tmp_path) in detail


def test_file_contains_reads_only_the_cap(tmp_path):
    path = tmp_path / "big.txt"
    path.write_bytes(b"a" * FILE_READ_CAP + b"NEEDLE")
    assert evaluate_checks([Check("file_contains", str(path), "NEEDLE")], None)[0] is False
    path.write_bytes(b"a" * (FILE_READ_CAP - len(b"NEEDLE")) + b"NEEDLE")
    assert evaluate_checks([Check("file_contains", str(path), "NEEDLE")], None)[0] is True


def test_title_contains():
    checks = [Check("title_contains", "hello.TXT")]
    assert evaluate_checks(checks, snap("hello.txt - Notatnik"))[0] is True
    assert evaluate_checks(checks, snap("Bez tytułu - Notatnik"))[0] is False
    assert evaluate_checks(checks, SimpleNamespace(window_title="x hello.txt"))[0] is True


def test_title_contains_is_undecided_on_an_unsaved_title():
    """Review #18: ``*hello.txt - Notatnik`` names the file, but nothing was
    written yet — it must not close a save phase with evidence ``code``."""
    check = Check("title_contains", "hello.txt")
    dirty = (["*hello.txt - Notatnik", "  *hello.txt - Notatnik"]
             + ["%s hello.txt - Editor" % dot for dot in agenda.UNSAVED_DOTS]
             + ["hello.txt %s - Editor" % dot for dot in agenda.UNSAVED_DOTS])
    for title in dirty:
        assert agenda.is_unsaved_title(title), title
        verdict, detail = agenda._eval_one(check, snap(title))
        assert verdict is None and "undecided" in detail, title
        assert evaluate_checks([check], snap(title)) == (None, ""), title
    assert not agenda.is_unsaved_title("hello.txt - Notatnik")
    assert evaluate_checks([check], snap("hello.txt - Notatnik"))[0] is True
    # The marker never turns a definite no into a maybe.
    assert evaluate_checks([check], snap("*Bez tytułu - Notatnik"))[0] is False


@pytest.mark.parametrize("title", [
    "*hello.txt - Notatnik",
    "*hello.txt",
    "● hello.txt - Visual Studio Code",
    "● hello.txt - projekt - Visual Studio Code",
    "• hello.txt - Notepad",
    "hello.txt • - Sublime Text",
    "hello.txt •",
    "hello.txt* - Paint.NET",
])
def test_the_unsaved_marker_opens_or_closes_the_document_part(title):
    assert agenda.is_unsaved_title(title)


@pytest.mark.parametrize("template, needle", [
    ("Instagram %s Photos - Google Chrome", "Photos"),
    ("Song %s Artist - Spotify", "Artist"),
    ("Inbox %s Slack", "Slack"),
    ("hello.txt - Notatnik %s Pomoc", "hello.txt"),
])
def test_a_dot_used_as_a_separator_is_not_the_unsaved_marker(template, needle):
    """A saved title that separates its words with a dot was read as unsaved:
    the dot was looked for anywhere, and ``title_contains`` stayed undecided
    on it forever."""
    for dot in agenda.UNSAVED_DOTS:
        title = template % dot
        assert not agenda.is_unsaved_title(title), title
        assert evaluate_checks([Check("title_contains", needle)], snap(title)) == (
            True, "title_contains %r: true" % needle), title


def test_since_makes_a_file_older_than_the_run_prove_nothing(tmp_path):
    """Review #17: yesterday's hello.txt on the Desktop is not today's save."""
    path = tmp_path / "hello.txt"
    path.write_text("hello world", encoding="utf-8")
    # The run starts after the file was made. A file created a moment before
    # ``since`` is inside the slack — its creation (ctime) stamp is evidence
    # too — and Windows does not let a test set that stamp back.
    start = time.time() + agenda.MTIME_SLACK_S + 5.0
    old = start - agenda.MTIME_SLACK_S - 3600.0
    os.utime(path, (old, old))
    checks = [Check("file_exists", str(path)), Check("file_contains", str(path), "hello")]
    for check in checks:
        assert evaluate_checks([check], None)[0] is True, "no since: blind to age, as before"
        assert evaluate_checks([check], None, since=start) == (None, "")
        verdict, detail = agenda._eval_one(check, None, start)
        assert verdict is None and "stale" in detail
    # Inside the slack it is still this run's save.
    recent = start - agenda.MTIME_SLACK_S / 2
    os.utime(path, (recent, recent))
    assert evaluate_checks(checks, None, since=start)[0] is True
    # Missing is still false, and an old file lacking the text a definite no.
    os.utime(path, (old, old))
    missing = Check("file_exists", str(tmp_path / "missing.txt"))
    assert evaluate_checks([missing], None, since=start)[0] is False
    assert evaluate_checks([Check("file_contains", str(path), "zakupy")], None,
                           since=start)[0] is False


def test_since_counts_a_file_the_run_copied_into_place(tmp_path):
    """A copy (or a move across volumes, or a zip extraction) keeps the
    source's mtime: judged by mtime alone, the file this run put in place
    read as stale and the goal could not close in code."""
    source = tmp_path / "source.txt"
    source.write_text("hello world", encoding="utf-8")
    day_ago = time.time() - 24 * 3600.0
    os.utime(source, (day_ago, day_ago))
    since = time.time()
    copy = tmp_path / "hello.txt"
    shutil.copy2(source, copy)
    assert os.path.getmtime(copy) < since - agenda.MTIME_SLACK_S, "premise: mtime is old"
    for check in (Check("file_exists", str(copy)), Check("file_contains", str(copy), "hello")):
        verdict, detail = agenda._eval_one(check, None, since)
        assert verdict is True and "stale" not in detail, detail
    # Untouched since before the run started — old content, made before
    # ``since`` — is still stale.
    later = time.time() + agenda.MTIME_SLACK_S + 5.0
    verdict, detail = agenda._eval_one(Check("file_exists", str(source)), None, later)
    assert verdict is None and "stale" in detail


def test_max_children_lifts_the_per_phase_cap_for_a_known_tree():
    """Review #1: a remembered or user tree with a phase wider than a model
    reply may be must not lose that phase's tail."""
    wide = MAX_CHILDREN + 2
    names = ["c%d" % i for i in range(wide)]
    items = [phase("Big", names), phase("Outer", [phase("Inner", names), "x"]), "after"]
    capped = coerce_tree(items, depth=1, id_prefix="", source="model")
    assert len(capped[0].children) == MAX_CHILDREN
    assert len(capped[1].children[0].children) == MAX_CHILDREN
    kept = coerce_tree(items, depth=1, id_prefix="", source="memory", max_children=wide)
    assert [c.goal for c in kept[0].children] == names
    assert [c.goal for c in kept[1].children[0].children] == names, "nested phases too"
    assert kept[0].children[-1].id == "1.%d" % wide
    # max_items still caps only the top level.
    assert len(coerce_tree(items, depth=1, id_prefix="", source="memory", max_items=1,
                           max_children=wide)[0].children) == wide


def test_undecided_and_definite_false(tmp_path):
    assert evaluate_checks([], snap("x")) == (None, "")
    assert evaluate_checks(None, None) == (None, "")
    assert evaluate_checks([Check("title_contains", "x")], None) == (None, "")
    missing = str(tmp_path / "missing.txt")
    ok, detail = evaluate_checks([Check("file_exists", missing),
                                  Check("title_contains", "x")], None)
    assert ok is False and missing in detail
    assert evaluate_checks([{"kind": "bogus", "arg": "x"}], None) == (None, "")


# --------------------------------------------------------------------------- #
# 5. quote_on_screen, classify_irreversible
# --------------------------------------------------------------------------- #

def test_quote_on_screen():
    s = snap("hello.txt - Notatnik", [UIElement(id="e0", role="edit", name="Text editor",
                                                 value="Lista zakupów"),
                                      UIElement(id="e1", role="button", name="Zapisz")])
    assert quote_on_screen("HELLO.txt", s)
    assert quote_on_screen("  zakupów ", s)
    assert quote_on_screen("zapisz", s)
    assert not quote_on_screen("ok", s)  # under three characters
    assert not quote_on_screen("absent", s)
    assert not quote_on_screen("hello", None)
    ns = SimpleNamespace(window_title="", elements=[{"name": "", "value": "saved fine"}])
    assert quote_on_screen("saved", ns)


@pytest.mark.parametrize("goal", ["Save the file as hello.txt", "Zapisz dokument",
                                  "Send the e-mail", "Wyślij wiadomość", "Delete old.txt",
                                  "Usuń plik", "Submit the form", "Pay the invoice",
                                  "Overwrite the copy", "Nadpisz plik", "Zapłać"])
def test_irreversible_words_raise(goal):
    assert classify_irreversible(goal, False) is True


def test_irreversible_never_lowers_the_model_flag():
    assert classify_irreversible("Type hello", True) is True
    assert classify_irreversible("Type hello", "true") is True
    assert classify_irreversible("Type hello", False) is False
    assert classify_irreversible("Open the unsaved draft", False) is False
    for verb in agenda.IRREVERSIBLE_VERBS:
        assert classify_irreversible("please %s it" % verb, False) is True

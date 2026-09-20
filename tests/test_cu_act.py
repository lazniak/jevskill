"""Execution, gating and settling — all of it against a fake backend.

The live path in :class:`jevskill.cu.act.UiaBackend` is not exercised anywhere,
here or in production, and these tests do not pretend otherwise: they pin the
*decisions* around the call (which pattern, on which element, with what refusal
when something is missing) and substitute a recorder for the call itself. That
is the part a refactor breaks silently; a wrong COM cast fails loudly the first
time someone runs it against a window.

The import contract matters as much as the behaviour: ``jevskill.cu.act`` must
import on a machine with no Windows and no ``comtypes``, so a Linux CI can run
everything above the backend.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from jevskill.cu.act import (DESTRUCTIVE_NAMES, SETTLE_CAP_COMBOBOX_MS,
                             SETTLE_CAP_MS, VK_CODES, Action, ActResult,
                             execute, is_destructive_name, parse_chord,
                             risky_ids, settle, settle_cap_for, vk_code)
from jevskill.cu.hashing import tree_hash
from jevskill.cu.types import Snapshot, UIElement


class Recorder:
    """A backend that records calls instead of touching a desktop."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.fail_on:
                raise RuntimeError("provider said no")
        return call

    @property
    def names(self):
        return [name for name, _, _ in self.calls]


def window(elements=None, title="Editor"):
    elements = elements if elements is not None else [
        UIElement(id="e1", role="button", name="Save", bbox=(10, 10, 60, 30),
                  patterns=("invoke",)),
        UIElement(id="e2", role="checkbox", name="Read only", bbox=(10, 50, 60, 20),
                  patterns=("toggle",)),
        UIElement(id="e3", role="edit", name="File name", bbox=(10, 80, 200, 28),
                  patterns=("value",), value=""),
        UIElement(id="e4", role="listitem", name="report.txt", bbox=(10, 120, 200, 20),
                  patterns=("select",)),
        UIElement(id="e5", role="document", name="body", bbox=(10, 150, 400, 300),
                  patterns=("scroll",)),
        UIElement(id="e6", role="button", name="Plain", bbox=(300, 10, 40, 20)),
    ]
    return Snapshot(window_title=title, app="editor.exe", pid=1, hwnd=2,
                    taken_at=0.0, elapsed_ms=1.0, elements=list(elements),
                    _handles={el.id: "handle:" + el.id for el in elements})


class TestDestructiveNames:
    def test_the_list_is_act_mds(self):
        assert DESTRUCTIVE_NAMES == ("delete", "remove", "send", "pay", "buy",
                                     "format", "uninstall", "empty")

    @pytest.mark.parametrize("name", [
        "Delete", "delete all documents", "Delete All Documents",
        "Empty Recycle Bin", "Send now", "Uninstall app", "Pay $40",
        "Remove filter", "Format painter", "Buy now"])
    def test_it_fires_case_folded_and_as_a_substring(self, name):
        assert is_destructive_name(name)

    @pytest.mark.parametrize("name", ["Save", "Cancel", "Open", "", None, "Rename"])
    def test_it_leaves_reversible_controls_alone(self, name):
        assert not is_destructive_name(name)

    def test_over_firing_is_the_chosen_direction_of_error(self):
        # "Format painter" and "Remove filter" are both harmless and both fire.
        # A needless confirmation costs a second; a missed one costs the data.
        assert is_destructive_name("Format painter")

    def test_risky_ids_names_the_candidates_the_gate_will_fire_on(self):
        elements = window().elements + [
            UIElement(id="e9", role="button", name="Delete all documents")]
        assert risky_ids(elements) == ["e9"]


class TestMethodChoice:
    """Which mechanism, decided from the patterns — the prefer-UIA rule."""

    @pytest.mark.parametrize("op,target,method", [
        ("click", "e1", "invoke"),
        ("click", "e2", "toggle"),
        ("click", "e4", "selectionitem"),
        ("click", "e6", "click_point"),      # no pattern at all: the last resort
        ("type", "e3", "setvalue"),
        ("type", "e5", "sendinput_text"),    # a document has no ValuePattern
        ("select", "e4", "selectionitem"),
        ("scroll_down", "e5", "scroll"),
        ("scroll_down", "e1", "scrollitem"),
        ("key", None, "sendinput_key"),
    ])
    def test_the_dry_run_reports_the_mechanism_a_real_run_would_use(self, op, target, method):
        action = Action(op=op, target=target, text="x" if op == "type" else None,
                        key="enter" if op == "key" else None)
        result = execute(action, window(), dry_run=True)
        assert result.ok and result.method == "dry:" + method


class TestExecute:
    def test_a_click_prefers_invoke_over_synthetic_input(self):
        backend = Recorder()
        result = execute(Action(op="click", target="e1"), window(), backend=backend)
        assert result.ok and result.method == "invoke"
        assert backend.calls == [("invoke", ("handle:e1",), {})]

    def test_a_checkbox_is_toggled(self):
        backend = Recorder()
        execute(Action(op="click", target="e2"), window(), backend=backend)
        assert backend.names == ["toggle"]

    def test_typing_uses_setvalue_when_the_control_has_one(self):
        backend = Recorder()
        execute(Action(op="type", target="e3", text="draft.txt"), window(),
                backend=backend)
        assert backend.calls == [("set_value", ("handle:e3", "draft.txt"), {})]

    def test_typing_without_a_value_pattern_focuses_first(self):
        backend = Recorder()
        execute(Action(op="type", target="e5", text="hello"), window(), backend=backend)
        assert backend.names == ["focus", "send_text"]

    def test_scrolling_a_scrollable_uses_the_scroll_pattern(self):
        backend = Recorder()
        execute(Action(op="scroll_down", target="e5", amount=2), window(),
                backend=backend)
        assert backend.calls == [("scroll", ("handle:e5", "down", 2), {})]

    def test_a_key_needs_no_element(self):
        backend = Recorder()
        result = execute(Action(op="key", key="ctrl+s"), window(), backend=backend)
        assert result.ok and backend.calls == [("send_keys", ("ctrl+s",), {})]

    def test_wait_and_the_terminal_ops_do_nothing(self):
        backend = Recorder()
        for op in ("wait", "done", "blocked"):
            assert execute(Action(op=op), window(), backend=backend).ok
        assert backend.calls == []

    def test_a_dry_run_calls_nothing(self):
        backend = Recorder()
        execute(Action(op="click", target="e1"), window(), backend=backend,
                dry_run=True)
        assert backend.calls == []

    @pytest.mark.parametrize("action,fragment", [
        (Action(op="click", target="e99"), "no element"),
        (Action(op="type", target="e3"), "type without text"),
        (Action(op="key"), "key without a chord"),
        (Action(op="select", target="e1"), "no selection pattern"),
    ])
    def test_it_refuses_rather_than_improvises(self, action, fragment):
        result = execute(action, window(), backend=Recorder())
        assert not result.ok and fragment in result.error

    def test_a_dead_handle_is_an_error_not_an_exception(self):
        snapshot = window()
        snapshot._handles.pop("e1")
        result = execute(Action(op="click", target="e1"), snapshot, backend=Recorder())
        assert not result.ok and "no live handle" in result.error

    def test_a_com_failure_is_reported_with_its_type(self):
        result = execute(Action(op="click", target="e1"), window(),
                         backend=Recorder(fail_on={"invoke"}))
        assert not result.ok and result.error.startswith("RuntimeError:")

    def test_the_result_carries_the_elapsed_time(self):
        assert execute(Action(op="click", target="e1"), window(),
                       backend=Recorder()).elapsed_ms >= 0.0


class TestKeys:
    def test_a_chord_is_split_and_validated(self):
        assert parse_chord("Ctrl+Shift+S") == ["ctrl", "shift", "s"]

    def test_a_single_letter_gets_its_ascii_code_without_growing_the_table(self):
        before = dict(VK_CODES)
        assert vk_code("s") == ord("S")
        assert VK_CODES == before

    @pytest.mark.parametrize("chord", ["", "ctrl+nope", "f13"])
    def test_an_unknown_key_raises_naming_itself(self, chord):
        with pytest.raises(KeyError):
            parse_chord(chord)

    def test_enter_and_escape_are_the_two_a_dialog_needs(self):
        assert vk_code("enter") == 0x0D and vk_code("escape") == 0x1B


class TestSettle:
    """The code-side ``stuck`` detector: two hashes, not a question."""

    def frames(self, *snapshots):
        queue = list(snapshots)

        def observe():
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return observe

    def test_it_returns_as_soon_as_the_tree_differs(self):
        before = window()
        after = window(elements=before.elements[:3])
        observe = self.frames(after)
        snapshot, changed, waited = settle(observe, tree_hash(before.elements),
                                           timeout_ms=500, poll_ms=1)
        assert changed and snapshot is after and waited >= 0.0

    def test_it_gives_up_at_the_ceiling_and_says_so(self):
        before = window()
        slept = []
        snapshot, changed, waited = settle(
            self.frames(before), tree_hash(before.elements), timeout_ms=5,
            poll_ms=1, sleep=slept.append)
        assert not changed
        assert slept, "a poll loop that never sleeps is a spin"

    def test_a_first_observation_with_no_previous_hash_is_a_change(self):
        _, changed, _ = settle(self.frames(window()), None, timeout_ms=1)
        assert changed

    def test_the_hash_function_is_injectable(self):
        # The loop compares the *reduced* candidates, not the whole tree, and
        # the two hashes are not interchangeable.
        seen = []

        def key(snapshot):
            seen.append(snapshot)
            return "constant"
        settle(self.frames(window()), "constant", timeout_ms=1, poll_ms=0,
               key=key, sleep=lambda _s: None)
        assert seen

    def test_the_published_caps_are_act_mds(self):
        combo = UIElement(id="e1", role="combobox", name="Type")
        assert settle_cap_for(combo) == SETTLE_CAP_COMBOBOX_MS == 200.0
        assert settle_cap_for(None) == SETTLE_CAP_MS == 50.0


class TestImportContract:
    def test_importing_act_does_not_import_comtypes(self):
        """The whole module must load where ``UIAutomationCore.dll`` does not."""
        code = ("import sys; import jevskill.cu.act as a; "
                "print(int('comtypes' in sys.modules), int('ctypes.wintypes' in sys.modules), "
                "a.DESTRUCTIVE_NAMES[0])")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["0", "0", "delete"]


class TestActResult:
    def test_it_serialises_for_a_run_log(self):
        data = ActResult(ok=True, method="invoke", elapsed_ms=1.2345).to_dict()
        assert data == {"ok": True, "method": "invoke", "error": "",
                        "elapsed_ms": 1.234, "dry_run": False}

    def test_an_action_serialises_too(self):
        assert Action(op="click", target="e1").to_dict()["op"] == "click"

"""What perception promises the rest of the loop, provable without Windows.

The snapshot itself needs UIA, so the walk is exercised live only when
``JEVSKILL_CU_LIVE=1`` on Windows. Everything else — the state shape, the token
budget, the import contract, the guarantee that a COM pointer never reaches the
serialised state — runs from the fixtures on any platform, which is what keeps
this file useful on Linux CI where the ``cu`` extra is not installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from jevskill.cu import candidates
from jevskill.cu.observe import GRID, state_tokens, to_state
from jevskill.cu.types import Snapshot, UIElement

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"
REAL = ("notepad", "calculator")
ALL_FIXTURES = ("synthetic_50", "synthetic_500", "synthetic_2000") + REAL

#: The published budget for a reduced state (``docs/plan-2026-09-20.md`` §4.2:
#: 60 candidates is the cap, and the state has to stay small enough that the
#: decision call is dominated by the network rather than by tokens).
TOKEN_BUDGET = 3500

LIVE = os.environ.get("JEVSKILL_CU_LIVE") == "1" and os.name == "nt"
live_only = pytest.mark.skipif(
    not LIVE, reason="live UIA walk: set JEVSKILL_CU_LIVE=1 on Windows")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))


def load(name: str) -> Snapshot:
    return Snapshot.from_dict(fixture(name)["snapshot"])


class TestImportContract:
    def test_importing_the_package_does_not_import_comtypes(self):
        """``import jevskill.cu`` must work on a machine without the extra.

        A subprocess, because by the time this test runs the extra may already
        be imported by another test — the guarantee is about a fresh process,
        which is where a user meets it.
        """
        code = ("import sys, jevskill.cu as cu; "
                "assert 'comtypes' not in sys.modules, sorted(sys.modules)[:0]; "
                "assert cu.candidates and cu.tree_hash")
        subprocess.run([sys.executable, "-c", code], check=True,
                       cwd=str(Path(__file__).resolve().parents[1]))

    def test_snapshot_error_names_the_extra(self, monkeypatch):
        from jevskill.cu import observe

        monkeypatch.setattr(observe, "_local", threading.local())
        monkeypatch.setitem(sys.modules, "comtypes", None)
        monkeypatch.setitem(sys.modules, "comtypes.client", None)
        with pytest.raises(ImportError) as err:
            observe.snapshot(hwnd=1)
        message = str(err.value)
        assert 'pip install "jevskill[cu]"' in message
        assert "Windows" in message

    def test_lazy_attribute_is_the_real_function(self):
        import jevskill.cu as cu
        from jevskill.cu import observe

        assert cu.snapshot is observe.snapshot
        assert cu.to_state is observe.to_state
        with pytest.raises(AttributeError):
            cu.no_such_thing

    def test_strategy_is_validated_before_any_com_call(self):
        from jevskill.cu import observe

        with pytest.raises(ValueError) as err:
            observe.snapshot(hwnd=1, strategy="fastest")
        assert "subtree" in str(err.value)


class TestStateShape:
    def test_keys_match_the_benchmarked_shape(self):
        snap = load("calculator")
        state = to_state(snap)
        assert set(state) == {"window_title", "app", "elements"}
        for entry in state["elements"].values():
            assert set(entry) <= {"role", "name", "bbox", "enabled", "focused",
                                  "value", "sel", "tog", "dup"}
            assert "role" in entry and "name" in entry

    def test_elements_are_keyed_by_id(self):
        snap = load("calculator")
        state = to_state(snap)
        assert list(state["elements"]) == [el.id for el in snap.elements]
        assert all(key.startswith("e") for key in state["elements"])

    def test_literal_bench_shape_is_available(self):
        snap = load("calculator")
        state = to_state(snap, coarse_bbox=False, omit_defaults=False)
        entry = state["elements"]["e0"]
        assert entry["enabled"] is True and entry["focused"] is False
        assert isinstance(entry["bbox"], list) and len(entry["bbox"]) == 4

    def test_coarse_bbox_is_a_grid_cell(self):
        snap = load("calculator")
        cells = {entry["bbox"] for entry in to_state(snap)["elements"].values()
                 if "bbox" in entry}
        assert cells, "every sized element should carry a cell"
        for cell in cells:
            row, col = cell[1:].split("c")
            assert 0 <= int(row) < GRID and 0 <= int(col) < GRID

    def test_the_grid_does_not_move_when_the_list_shortens(self):
        snap = load("calculator")
        full = to_state(snap)["elements"]
        short = to_state(snap, elements=candidates(snap.elements))["elements"]
        for element_id, entry in short.items():
            assert entry.get("bbox") == full[element_id].get("bbox")

    def test_unsized_elements_omit_the_key(self):
        elements = [UIElement(id="e0", role="pane", name="host",
                              bbox=(0, 0, 0, 0)),
                    UIElement(id="e1", role="button", name="ok",
                              bbox=(10, 10, 40, 30))]
        state = to_state(elements)
        assert "bbox" not in state["elements"]["e0"]
        assert "bbox" in state["elements"]["e1"]

    def test_defaults_are_dropped_but_the_signal_is_not(self):
        elements = [UIElement(id="e0", role="button", name="ok",
                              bbox=(0, 0, 40, 30)),
                    UIElement(id="e1", role="button", name="focused",
                              bbox=(0, 40, 40, 30), focused=True),
                    UIElement(id="e2", role="button", name="greyed",
                              bbox=(0, 80, 40, 30), enabled=False)]
        state = to_state(elements)["elements"]
        assert set(state["e0"]) == {"role", "name", "bbox"}, (
            "enabled=True and focused=False carry no information")
        assert state["e1"]["focused"] is True
        assert state["e2"]["enabled"] is False
        assert "focused" not in state["e2"]


class TestNoHandlesEscape:
    def test_snapshot_to_dict_drops_the_handle_map(self):
        snap = Snapshot(window_title="t", app="a.exe", pid=1, hwnd=2,
                        taken_at=0.0, elapsed_ms=1.0,
                        elements=[UIElement(id="e0", role="button", name="ok",
                                            bbox=(0, 0, 40, 30))],
                        _handles={"e0": object()})
        payload = snap.to_dict()
        assert "_handles" not in payload
        json.dumps(payload)  # a live COM pointer would raise here
        assert snap.handle("e0") is not None, "the map itself must still work"

    def test_to_state_is_json_safe(self):
        snap = load("notepad")
        snap._handles["e0"] = object()
        json.dumps(to_state(snap))

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_committed_fixtures_carry_no_handles(self, name):
        assert "_handles" not in fixture(name)["snapshot"]


class TestTokenBudget:
    def test_sixty_candidates_fit_the_budget(self):
        snap = load("synthetic_500")
        shortlist = candidates(snap.elements, cap=60)
        assert len(shortlist) == 60, "fixture no longer produces a full cap"
        tokens = state_tokens(to_state(snap, elements=shortlist))
        assert tokens <= TOKEN_BUDGET, "%d tokens for 60 candidates" % tokens

    def test_reduction_is_what_buys_the_budget(self):
        snap = load("synthetic_2000")
        full = state_tokens(to_state(snap))
        reduced = state_tokens(to_state(snap, elements=candidates(snap.elements)))
        assert reduced * 4 < full

    @pytest.mark.parametrize("name", REAL)
    def test_real_windows_fit(self, name):
        snap = load(name)
        tokens = state_tokens(to_state(snap, elements=candidates(snap.elements)))
        assert tokens <= TOKEN_BUDGET

    def test_long_names_are_truncated_at_the_source(self):
        from jevskill.cu.observe import MAX_NAME_CHARS, _text

        assert len(_text("x" * 5000, MAX_NAME_CHARS)) == MAX_NAME_CHARS
        assert _text("x" * 5000, MAX_NAME_CHARS).endswith("…")


class TestRealFixtures:
    @pytest.mark.parametrize("name", REAL)
    def test_round_trips(self, name):
        snap = load(name)
        assert Snapshot.from_dict(snap.to_dict()).to_dict() == snap.to_dict()

    @pytest.mark.parametrize("name", REAL)
    def test_tree_is_well_formed(self, name):
        snap = load(name)
        ids = {el.id for el in snap.elements}
        assert snap.elements[0].depth == 0 and snap.elements[0].parent is None
        for el in snap.elements[1:]:
            assert el.parent in ids, "%s has a dangling parent" % el.id
            assert el.region is None or el.region in ids
            assert el.is_sized(), "an unsized node was recorded"

    def test_calculator_looks_like_a_calculator(self):
        snap = load("calculator")
        buttons = [el for el in snap.elements
                   if el.role == "button" and "invoke" in el.patterns]
        assert len(buttons) > 20, "a calculator is mostly invokable buttons"
        assert any(el.role == "group" for el in snap.elements)

    def test_fixtures_were_scrubbed(self):
        """A committed fixture must not carry the machine owner's content."""
        for name in REAL:
            data = fixture(name)
            assert data["snapshot"]["window_title"] == name
            for element in data["snapshot"]["elements"]:
                assert element.get("value") in (None, "<scrubbed>")


# --------------------------------------------------------------------------- #
# A fake UI Automation client
#
# The walk is the half of this module that cannot be exercised from a fixture,
# and on this machine it cannot be exercised live either. Faking the COM
# surface is not elegant, but the alternative is that COM initialisation,
# COMError translation and the popup merge ship untested — and every one of
# those is a bug that only appears on somebody else's thread or somebody else's
# menu. The fake implements exactly the surface observe.py touches.
# --------------------------------------------------------------------------- #

WINDOW, BUTTON, MENU, MENUITEM = 50032, 50000, 50009, 50011


class FakeRect:
    def __init__(self, bbox):
        left, top, width, height = bbox
        self.left, self.top = left, top
        self.right, self.bottom = left + width, top + height


class FakeArray:
    def __init__(self, items):
        self._items = list(items)
        self.Length = len(self._items)

    def GetElement(self, index):
        return self._items[index]


class FakeElement:
    """One cached UIA node. ``props`` holds anything read by property id."""

    def __init__(self, control_type, name="", bbox=(0, 0, 200, 40), children=(),
                 enabled=True, focused=False, offscreen=False,
                 automation_id="", class_name="", props=None):
        self.CachedControlType = control_type
        self.CachedName = name
        self.CachedBoundingRectangle = FakeRect(bbox)
        self.CachedIsEnabled = enabled
        self.CachedHasKeyboardFocus = focused
        self.CachedIsOffscreen = offscreen
        self.CachedAutomationId = automation_id
        self.CachedClassName = class_name
        self.children = list(children)
        self.props = dict(props or {})

    def GetCachedChildren(self):
        return FakeArray(self.children)

    def BuildUpdatedCache(self, _cache):
        return self

    def GetCachedPropertyValue(self, prop):
        return self.props.get(prop, 0)


class FakeCacheRequest:
    def __init__(self):
        self.properties = []
        self.TreeScope = None
        self.TreeFilter = None
        self.AutomationElementMode = None

    def AddProperty(self, prop):
        self.properties.append(prop)


class FakeIUIAutomation:
    def __init__(self, roots, focused=None):
        self.roots = roots            # hwnd -> FakeElement (or an exception)
        self.ControlViewCondition = object()
        self._focused = focused
        self.cache_requests = []

    def CreateCacheRequest(self):
        request = FakeCacheRequest()
        self.cache_requests.append(request)
        return request

    def ElementFromHandle(self, hwnd):
        root = self.roots.get(int(hwnd))
        if root is None:
            raise FakeComError(0x80070057, "no such window")
        if isinstance(root, Exception):
            raise root
        return root

    def GetFocusedElement(self):
        if self._focused is None:
            raise FakeComError(0x80040201, "no focus")
        return self._focused


class FakeComError(Exception):
    """Shaped like ``comtypes.COMError``: an ``hresult``, and not an OSError."""

    def __init__(self, hresult, text):
        super().__init__(hresult, text, None)
        self.hresult = hresult
        self.text = text


def fake_uia_module():
    """The generated ``comtypes.gen.UIAutomationClient``, as far as we use it."""
    mod = types.SimpleNamespace()
    names = ("Invoke", "Value", "Toggle", "SelectionItem", "ExpandCollapse",
             "Scroll", "RangeValue")
    for index, name in enumerate(names):
        setattr(mod, "UIA_Is%sPatternAvailablePropertyId" % name, 30000 + index)
    for index, name in enumerate((
            "Name", "ControlType", "BoundingRectangle", "IsEnabled",
            "HasKeyboardFocus", "IsOffscreen", "AutomationId", "ClassName",
            "ProcessId", "ValueValue")):
        setattr(mod, "UIA_%sPropertyId" % name, 31000 + index)
    mod.UIA_IsDialogPropertyId = 30174
    mod.UIA_SelectionItemIsSelectedPropertyId = 30079
    mod.UIA_ToggleToggleStatePropertyId = 30086
    mod.TreeScope_Element, mod.TreeScope_Children = 1, 2
    mod.TreeScope_Subtree = 7
    mod.AutomationElementMode_Full = 1
    mod.CUIAutomation = types.SimpleNamespace(_reg_clsid_="{fake}")
    mod.IUIAutomation = object
    return mod


def install_fake_comtypes(monkeypatch, iuia, record=None):
    """Put a fake ``comtypes`` in ``sys.modules`` and reset the thread-local.

    ``record`` collects ``("CoInitializeEx", flags)`` so a test can assert the
    call happened without a real COM apartment anywhere near it.
    """
    from jevskill.cu import observe

    calls = [] if record is None else record
    uia_mod = fake_uia_module()

    comtypes = types.ModuleType("comtypes")
    comtypes.CLSCTX_INPROC_SERVER = 1

    def co_initialize_ex(flags=None):
        calls.append(("CoInitializeEx", flags))

    comtypes.CoInitializeEx = co_initialize_ex
    comtypes.CoCreateInstance = lambda *a, **kw: iuia
    client = types.ModuleType("comtypes.client")
    client.GetModule = lambda _dll: None
    comtypes.client = client
    gen = types.ModuleType("comtypes.gen")
    gen.UIAutomationClient = uia_mod

    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.UIAutomationClient", uia_mod)
    monkeypatch.setattr(observe, "_local", threading.local())
    # The Win32 side is not what these tests are about, and it does not exist
    # on the Linux runner at all.
    monkeypatch.setattr(observe, "window_pid", lambda hwnd: 4242)
    monkeypatch.setattr(observe, "process_name", lambda pid: "fake.exe")
    monkeypatch.setattr(observe, "window_title", lambda hwnd: "Fake Window")
    monkeypatch.setattr(observe, "popup_hwnds", lambda pid: [])
    return calls


class TestComInitialisation:
    """A worker thread's own client needs its own apartment.

    ``comtypes`` calls ``CoInitializeEx`` once, on the thread that first
    imports it. ``_Uia`` is thread-local on purpose, so the second thread to
    ask for one got a client whose first call failed with
    ``CO_E_NOTINITIALIZED`` — in production, never in a test.
    """

    def test_building_a_client_initialises_com(self, monkeypatch):
        from jevskill.cu import observe

        calls = install_fake_comtypes(
            monkeypatch, FakeIUIAutomation({1: FakeElement(WINDOW, "root")}))
        observe._client()
        assert calls == [("CoInitializeEx", observe.COINIT_MULTITHREADED)]

    def test_multithreaded_is_the_documented_choice(self):
        from jevskill.cu import observe

        assert observe.COINIT_MULTITHREADED == 0x0

    def test_an_apartment_that_already_exists_is_not_an_error(self):
        """``RPC_E_CHANGED_MODE`` means somebody else got there first."""
        from jevskill.cu import observe

        class Changed(types.SimpleNamespace):
            @staticmethod
            def CoInitializeEx(flags=None):
                error = OSError("already an STA")
                error.winerror = observe._RPC_E_CHANGED_MODE
                raise error

        observe._co_initialize(Changed())  # must not raise

    def test_a_signed_hresult_is_recognised_too(self):
        from jevskill.cu import observe

        class Changed(types.SimpleNamespace):
            @staticmethod
            def CoInitializeEx(flags=None):
                error = OSError("already an STA")
                error.winerror = observe._RPC_E_CHANGED_MODE_SIGNED
                raise error

        observe._co_initialize(Changed())

    def test_any_other_failure_still_propagates(self):
        from jevskill.cu import observe

        class Broken(types.SimpleNamespace):
            @staticmethod
            def CoInitializeEx(flags=None):
                error = OSError("out of memory")
                error.winerror = 0x8007000E
                raise error

        with pytest.raises(OSError):
            observe._co_initialize(Broken())


class TestDyingWindowIsAnOSError:
    """The docstring promised ``OSError``; COM raised ``COMError``.

    A packaged app suspended by Process Lifetime Management, or a window closed
    between the enumeration and the read, comes back as ``COMError`` — which is
    not an ``OSError``, so every ``except OSError`` around a snapshot missed it.
    """

    def test_element_from_handle_failure(self, monkeypatch):
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation(
            {7: FakeComError(0x80040201, "EVENT_E_ALL_SUBSCRIBERS_FAILED")}))
        with pytest.raises(OSError) as err:
            observe.snapshot(hwnd=7)
        message = str(err.value)
        assert "0x80040201" in message
        assert "0x00000007" in message, "the hwnd has to be in the message"
        assert "suspended" in message

    def test_build_updated_cache_failure(self, monkeypatch):
        from jevskill.cu import observe

        class Dying(FakeElement):
            def BuildUpdatedCache(self, _cache):
                raise FakeComError(0x80010108, "RPC_E_DISCONNECTED")

        install_fake_comtypes(monkeypatch,
                              FakeIUIAutomation({9: Dying(WINDOW, "root")}))
        with pytest.raises(OSError) as err:
            observe.snapshot(hwnd=9)
        assert "0x80010108" in str(err.value)

    def test_an_unknown_hresult_still_produces_an_oserror(self, monkeypatch):
        from jevskill.cu import observe

        class Odd(Exception):
            pass

        install_fake_comtypes(monkeypatch,
                              FakeIUIAutomation({3: Odd("no hresult here")}))
        with pytest.raises(OSError) as err:
            observe.snapshot(hwnd=3)
        assert "Odd" in str(err.value)


def popup_tree():
    """A File menu, as Windows delivers it: its own top-level ``#32768``."""
    return FakeElement(MENU, "File", (100, 60, 200, 300), children=[
        FakeElement(MENUITEM, "New tab", (100, 60, 200, 30)),
        FakeElement(MENUITEM, "Open...", (100, 90, 200, 30)),
        FakeElement(MENUITEM, "Save", (100, 120, 200, 30)),
    ])


class TestPopups:
    """One HWND's subtree does not contain the menu it just opened.

    Menus, dropdowns and context menus are separate top-level windows, so after
    clicking "File" the next snapshot saw the same tree and ``tree_hash``
    reported "unchanged" — the loop concluded its click had done nothing.
    """

    def test_merge_renumbers_without_colliding(self):
        from jevskill.cu.observe import POPUP_REGION, merge_popup

        window = [UIElement(id="e0", role="window", name="App",
                            bbox=(0, 0, 800, 600)),
                  UIElement(id="e1", role="button", name="File",
                            bbox=(0, 0, 60, 30), parent="e0", region="e0")]
        popup = [UIElement(id="e0", role="menu", name="File",
                           bbox=(100, 60, 200, 300), region=POPUP_REGION),
                 UIElement(id="e1", role="menuitem", name="Save",
                           bbox=(100, 120, 200, 30), parent="e0", region="e0")]
        merged = merge_popup(window, popup)
        assert [el.id for el in merged] == ["e0", "e1", "e2", "e3"]
        assert merged[2].region == POPUP_REGION and merged[2].parent is None
        assert merged[3].parent == "e2", "the item must follow its own root"
        assert merged[3].name == "Save"
        assert [el.id for el in window] == ["e0", "e1"], "input was mutated"

    def test_merge_keeps_ids_unique_across_three_popups(self):
        from jevskill.cu.observe import merge_popup

        elements = [UIElement(id="e0", role="window", name="App",
                              bbox=(0, 0, 800, 600))]
        for _ in range(3):
            elements = merge_popup(elements, [
                UIElement(id="e0", role="menu", name="m", bbox=(0, 0, 40, 40)),
                UIElement(id="e1", role="menuitem", name="i",
                          bbox=(0, 0, 40, 20), parent="e0")])
        ids = [el.id for el in elements]
        assert len(ids) == len(set(ids)) == 7

    def test_an_open_menu_reaches_the_snapshot(self, monkeypatch):
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: FakeElement(WINDOW, "App", (0, 0, 800, 600), children=[
                FakeElement(BUTTON, "File", (0, 0, 60, 30))]),
            2: popup_tree(),
        }))
        monkeypatch.setattr(observe, "popup_hwnds", lambda pid: [2])
        snap = observe.snapshot(hwnd=1)
        names = [el.name for el in snap.elements]
        assert "Save" in names, "the menu item the loop wanted to click"
        assert sum(1 for el in snap.elements
                   if el.region == observe.POPUP_REGION) == 1
        assert len({el.id for el in snap.elements}) == len(snap.elements)

    def test_the_menu_changes_the_tree_hash(self, monkeypatch):
        """The whole point: "I clicked File" must not read as "nothing happened"."""
        from jevskill.cu import hashing, observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: FakeElement(WINDOW, "App", (0, 0, 800, 600), children=[
                FakeElement(BUTTON, "File", (0, 0, 60, 30))]),
            2: popup_tree(),
        }))
        closed = observe.snapshot(hwnd=1)
        monkeypatch.setattr(observe, "popup_hwnds", lambda pid: [2])
        opened = observe.snapshot(hwnd=1)
        assert hashing.tree_hash(closed.elements) != hashing.tree_hash(opened.elements)
        assert hashing.diff(closed.elements, opened.elements).added

    def test_popups_can_be_turned_off(self, monkeypatch):
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: FakeElement(WINDOW, "App", (0, 0, 800, 600)), 2: popup_tree()}))
        monkeypatch.setattr(observe, "popup_hwnds", lambda pid: [2])
        snap = observe.snapshot(hwnd=1, include_popups=False)
        assert all("Save" != el.name for el in snap.elements)

    def test_a_popup_that_closed_mid_walk_is_skipped(self, monkeypatch):
        """Enumeration and read are two moments; a menu closes between them."""
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: FakeElement(WINDOW, "App", (0, 0, 800, 600))}))
        monkeypatch.setattr(observe, "popup_hwnds", lambda pid: [999])
        snap = observe.snapshot(hwnd=1)
        assert len(snap.elements) == 1 and not snap.truncated


class TestBudgetSemantics:
    """``truncated`` and ``over_budget`` answer two different questions.

    ``budget_ms`` bounds the Python walk, and in ``"subtree"`` the one provider
    call that fetches the whole tree has already been paid before the walk
    starts. A slow window therefore blows the budget and still returns a
    complete list — reporting that as ``truncated`` would be a lie in the other
    direction, and aborting the walk would throw away a tree already bought.
    """

    def test_a_slow_provider_call_is_over_budget_but_not_truncated(self, monkeypatch):
        from jevskill.cu import observe

        class Slow(FakeElement):
            def BuildUpdatedCache(self, _cache):
                time.sleep(0.02)
                return self

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: Slow(WINDOW, "App", (0, 0, 800, 600), children=[
                FakeElement(BUTTON, "b%d" % i, (i * 50, 0, 40, 30))
                for i in range(5)])}))
        snap = observe.snapshot(hwnd=1, budget_ms=1.0)
        assert snap.over_budget is True
        assert snap.truncated is False, "nothing was left out"
        assert len(snap.elements) == 6, (
            "the tree was already paid for; walking it costs microseconds")

    def test_lazy_charges_the_provider_call_to_the_budget(self, monkeypatch):
        """The asymmetry is the point: lazy can still decline to pay more."""
        from jevskill.cu import observe

        class Slow(FakeElement):
            def BuildUpdatedCache(self, _cache):
                time.sleep(0.02)
                return self

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: Slow(WINDOW, "App", (0, 0, 800, 600), children=[
                FakeElement(BUTTON, "b%d" % i, (i * 50, 0, 40, 30))
                for i in range(5)])}))
        snap = observe.snapshot(hwnd=1, budget_ms=1.0, strategy="lazy")
        assert snap.truncated and snap.over_budget
        assert len(snap.elements) == 1

    def test_a_fast_walk_is_neither(self, monkeypatch):
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation(
            {1: FakeElement(WINDOW, "App")}))
        snap = observe.snapshot(hwnd=1, budget_ms=5000.0)
        assert not snap.over_budget and not snap.truncated

    def test_max_nodes_truncates_and_says_so(self, monkeypatch):
        from jevskill.cu import observe

        install_fake_comtypes(monkeypatch, FakeIUIAutomation({
            1: FakeElement(WINDOW, "App", (0, 0, 800, 600), children=[
                FakeElement(BUTTON, "b%d" % i, (i * 50, 0, 40, 30))
                for i in range(10)])}))
        snap = observe.snapshot(hwnd=1, max_nodes=4)
        assert snap.truncated and len(snap.elements) == 4

    def test_over_budget_round_trips_only_when_set(self):
        cheap = Snapshot(window_title="t", app="a", pid=1, hwnd=2,
                         taken_at=0.0, elapsed_ms=1.0)
        assert "over_budget" not in cheap.to_dict()
        slow = Snapshot(window_title="t", app="a", pid=1, hwnd=2, taken_at=0.0,
                        elapsed_ms=900.0, over_budget=True)
        assert Snapshot.from_dict(slow.to_dict()).over_budget is True


class TestDuplicatesReachTheModel:
    """"One of nine" was computed, stored on the element, and never sent."""

    def test_dup_is_emitted_when_it_means_something(self):
        elements = [UIElement(id="e0", role="button", name="Save",
                              bbox=(0, 0, 60, 30), duplicates=8),
                    UIElement(id="e1", role="button", name="Open",
                              bbox=(70, 0, 60, 30))]
        state = to_state(elements)["elements"]
        assert state["e0"]["dup"] == 8
        assert "dup" not in state["e1"], "a unique control says nothing"

    def test_the_reduction_pipeline_carries_it_through(self):
        toolbar = [UIElement(id="bar", role="toolbar", name="Main",
                             bbox=(0, 0, 800, 40))]
        toolbar += [UIElement(id="m%d" % i, role="button", name="More options",
                              bbox=(i * 60, 4, 50, 32), parent="bar",
                              region="bar", patterns=("invoke",))
                    for i in range(9)]
        state = to_state(toolbar, elements=candidates(toolbar))["elements"]
        assert [entry["dup"] for entry in state.values() if "dup" in entry] == [8]

    def test_selection_and_toggle_reach_the_model_too(self):
        elements = [UIElement(id="e0", role="listitem", name="row 1",
                              bbox=(0, 0, 400, 30), selected=True),
                    UIElement(id="e1", role="checkbox", name="Word wrap",
                              bbox=(0, 40, 200, 24), toggled=2),
                    UIElement(id="e2", role="button", name="Save",
                              bbox=(0, 70, 60, 30))]
        state = to_state(elements)["elements"]
        assert state["e0"]["sel"] is True
        assert state["e1"]["tog"] == 2
        assert set(state["e2"]) == {"role", "name", "bbox"}

    def test_the_extra_keys_stay_inside_the_token_budget(self):
        snap = load("synthetic_500")
        shortlist = candidates(snap.elements, cap=60)
        assert state_tokens(to_state(snap, elements=shortlist)) <= TOKEN_BUDGET


@live_only
class TestLive:
    def test_foreground_snapshot(self):
        from jevskill.cu import observe

        snap = observe.snapshot()
        assert snap.elements and snap.elapsed_ms > 0
        assert snap.hwnd and snap.pid
        # Counts only: this is somebody's desktop.
        assert len(candidates(snap.elements)) <= 60
        assert all(el.is_sized() for el in snap.elements)

    def test_handles_are_live_and_private(self):
        from jevskill.cu import observe

        snap = observe.snapshot()
        assert snap.handle(snap.elements[0].id) is not None
        assert "_handles" not in snap.to_dict()

    def test_both_strategies_see_the_same_window(self):
        from jevskill.cu import observe

        hwnd = observe.foreground_hwnd()
        subtree = observe.snapshot(hwnd, strategy="subtree", budget_ms=5000)
        lazy = observe.snapshot(hwnd, strategy="lazy", budget_ms=5000)
        assert abs(len(subtree.elements) - len(lazy.elements)) <= 2

    def test_budget_truncates_rather_than_hangs(self):
        from jevskill.cu import observe

        snap = observe.snapshot(budget_ms=0.0)
        assert snap.truncated and len(snap.elements) >= 1

    def test_max_nodes_truncates(self):
        from jevskill.cu import observe

        snap = observe.snapshot(max_nodes=5)
        assert snap.truncated and len(snap.elements) == 5

"""Read one Windows window's UI Automation tree, fast, and shape it for a model.

Which library, and why. Measured by ``bench/cu_observe_bench.py`` (Windows 11
Pro 26100, Python 3.13.2, 3 warm runs per app, numbers in
``bench/cu_observe_results.json``; the machine was running other work, so read
the spread, not the third digit):

=================================  ==================  =====================
walk                               Notepad (34 nodes)  Calculator (53 nodes)
=================================  ==================  =====================
``uiautomation`` 2.0.29            187.9-196.6 ms      144.0-160.6 ms
``pywinauto`` 0.6.9 (uia backend)  137.6-223.3 ms      100.6-123.0 ms
``comtypes`` + one CacheRequest    72.6-104.2 ms       42.4-51.6 ms
=================================  ==================  =====================

Every figure here is one decimal from ``cu_observe_results.json``, never a
rounded one: the repo's rule is that a number is never rounded *up*, and
"188 ms" for a measured 187.9 breaks it in the flattering direction.

``uiautomation`` and ``pywinauto`` read every property live, so a node costs one
cross-process call *per property*. This module caches 20 properties in a single
``CacheRequest`` — Name, ControlType, BoundingRectangle, IsEnabled,
HasKeyboardFocus, IsOffscreen, AutomationId, ClassName, ProcessId, Value,
IsDialog, SelectionItem.IsSelected, Toggle.ToggleState and seven pattern
availability flags — turning 20 round trips per node into one for the whole
window, after which the Python side reads them in-process. The two libraries
also return a different tree — they walk the raw view and keep offscreen and
zero-sized nodes — which is why their node counts are higher for the same
window.

Four things the measurement changed about the plan. Figures without a table
above come from the selection probes (not from the committed results file), and
are marked as such because they are not reproducible from a clean profile:

1. **The 10-60 ms state budget holds only for small windows.** Cost tracks the
   provider's node count, not the client: Calculator (53 nodes) 42.4-51.6 ms, an
   empty Notepad (34) 72.6-104.2 ms, the foreground window in the same run (506
   nodes) 339.8 ms. *Selection probe:* a Notepad that had restored 18 tabs — 213
   control-view nodes, 183 of them under one
   ``Microsoft.UI.Content.DesktopChildSiteBridge`` XAML island that cost 337 ms
   by itself — took 368-410 ms. Budget roughly a millisecond per node, and do
   not assume a window is empty.
2. **No client-side trick moves it.** *Selection probe, on that 213-node
   Notepad:* ``AutomationElementMode_None`` (no live handles), a 6-property
   cache instead of the full one, ``ContentView`` or ``RawView`` instead of
   ``ControlView``, and MTA instead of STA were all within noise of each other
   (360-470 ms), and ``FindAllBuildCache`` was 3x worse (2.1-2.7 s). The cost
   is the provider's. (MTA being free is why :func:`_co_initialize` picks it.)
3. **One round trip beats sixty-four.** A lazy per-container descent ties the
   single ``TreeScope_Subtree`` call on an idle machine (*selection probe*:
   368-392 vs 375-410 ms) and loses on a working one (committed run, Calculator:
   69.6-73.1 ms lazy vs 46.3-53.6 subtree; Notepad, 34 nodes and 11 containers,
   is a tie at 91.4-102.5 vs 83.4-107.6), because each round trip pays the
   contention again. ``strategy="subtree"`` is the default for that reason;
   ``"lazy"`` stays available for a window too large to fetch at once, and is
   the only one that can stop mid-walk.
4. **Packaged apps suspend.** Notepad and Calculator are both packaged, so
   Process Lifetime Management suspends them seconds after they lose the
   foreground and every UIA call then fails with
   ``EVENT_E_ALL_SUBSCRIBERS_FAILED``. Snapshot a packaged app immediately
   after launching it, or keep it in front — and expect :func:`snapshot` to
   raise ``OSError`` carrying that HRESULT when it happens anyway.

A fifth number worth keeping: the same run's foreground window reduced to 60
candidates and **3,093 tokens** — under the 3,500 budget, but only because
``to_state`` drops ``"enabled": true`` and ``"focused": false``. A real screen
is not the synthetic one (the synthetic 500-node tree reduces to 2,497 tokens,
the 2,000-node one to 2,488; ``bench/cu_observe_bench.py --from-fixtures``).
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..orchestrate import count_tokens
from .reduce import union_bbox
from .types import CONTROL_TYPES, REGION_ROLES, Snapshot, UIElement

#: A model does not need a document's whole text to pick a control, and a
#: ``document`` node's Name can be the entire file. Truncation is marked.
MAX_NAME_CHARS = 120
MAX_VALUE_CHARS = 200

#: The coarse grid ``to_state`` maps a rectangle onto. Eight columns and eight
#: rows is enough for "the button on the right of the bottom bar" and costs
#: five characters instead of the ~20 a pixel rectangle costs.
GRID = 8

#: See :func:`snapshot`. One round trip beats sixty-four whenever the machine
#: is not idle, and a computer-use loop never runs on an idle machine.
DEFAULT_STRATEGY = "subtree"

_INSTALL_HINT = (
    "jevskill.cu.snapshot() needs the Windows UI Automation client. "
    'Install the extra: pip install "jevskill[cu]" (comtypes), and run on '
    "Windows — UIAutomationCore.dll has no Linux or macOS equivalent."
)

_MENU_CONTROL_TYPES = (50009, 50011)  # Menu, MenuItem
_MENUBAR = "menubar"
_local = threading.local()

#: ``CoInitializeEx``: MTA. Measured indistinguishable from STA on the 213-node
#: Notepad selection probe, and MTA is the honest apartment for this object —
#: it is created lazily on whatever thread first calls :func:`snapshot`, which
#: in a daemon is a worker with no message pump, and an STA without a pump is a
#: deadlock waiting for a cross-apartment call.
COINIT_MULTITHREADED = 0x0
#: ``RPC_E_CHANGED_MODE``: this thread already joined the other apartment kind.
#: Not an error for us — somebody else (pywinauto, a GUI toolkit, a host
#: application) initialised COM first and their apartment works fine.
_RPC_E_CHANGED_MODE = 0x80010106
_RPC_E_CHANGED_MODE_SIGNED = _RPC_E_CHANGED_MODE - 0x100000000

#: Win32 class of a menu / dropdown / context-menu popup. Popups are separate
#: top-level windows, so one HWND's subtree never contains them; see
#: :func:`snapshot`.
POPUP_CLASS = "#32768"
#: The ``region`` a merged popup's own root carries, so a caller can tell
#: "this came from another window" without a second field on every element.
POPUP_REGION = "popup"


def _co_initialize(comtypes: Any) -> None:
    """Join a COM apartment on *this* thread, tolerating one already joined.

    ``comtypes`` calls ``CoInitializeEx`` once, on the thread that first
    imports it. A :class:`_Uia` is thread-local precisely so a worker thread
    can have its own client — and that worker's first UIA call then failed with
    ``CO_E_NOTINITIALIZED`` (0x800401F0), because nobody had initialised COM
    there. This is the missing call.

    No matching ``CoUninitialize``: the client is cached in :data:`_local` for
    the lifetime of the thread, and every element handle in every
    :class:`Snapshot` it produced is a live cross-process pointer into that
    apartment. Uninitialising while those are alive is how you turn a clean
    shutdown into ``RPC_E_DISCONNECTED`` in somebody else's code. A daemon's
    perception thread is expected to outlive its snapshots; a thread that
    genuinely wants to hand the apartment back should drop ``_local.uia``, drop
    its snapshots, and call ``CoUninitialize`` itself.
    """
    try:
        comtypes.CoInitializeEx(COINIT_MULTITHREADED)
    except OSError as exc:  # pragma: no cover - needs a real COM apartment
        code = getattr(exc, "winerror", None)
        if code is None:
            code = getattr(exc, "hresult", None)
        if code not in (_RPC_E_CHANGED_MODE, _RPC_E_CHANGED_MODE_SIGNED):
            raise
    except AttributeError:  # pragma: no cover - a comtypes without the helper
        pass


# --------------------------------------------------------------------------- #
# Win32 helpers (ctypes, so the import costs nothing on a non-Windows box)
# --------------------------------------------------------------------------- #

#: ``EnumWindows`` callback type. Built once: a fresh ``WINFUNCTYPE`` per call
#: leaks a thunk, and this runs on every snapshot.
_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if hasattr(
        ctypes, "WINFUNCTYPE") else None


def _user32():
    if not hasattr(ctypes, "windll"):  # pragma: no cover - non-Windows
        raise ImportError(_INSTALL_HINT)
    return ctypes.windll.user32


def foreground_hwnd() -> int:
    """The window the user is actually looking at."""
    return int(_user32().GetForegroundWindow())


def window_title(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def process_name(pid: int) -> str:
    """Executable name for a pid, without psutil.

    ``PROCESS_QUERY_LIMITED_INFORMATION`` is used on purpose: it succeeds for
    processes at a different integrity level, where the older
    ``PROCESS_QUERY_INFORMATION`` fails with access denied.
    """
    if not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# UIA client
# --------------------------------------------------------------------------- #

class _Uia:
    """One IUIAutomation plus one prepared CacheRequest, per thread.

    COM objects are apartment-bound, so this is thread-local rather than a
    module global: sharing one across a worker thread is the classic way to get
    ``RPC_E_WRONG_THREAD`` in production and never in a test.
    """

    def __init__(self) -> None:
        try:
            import comtypes
            import comtypes.client
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(_INSTALL_HINT) from exc
        _co_initialize(comtypes)
        try:
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia_mod
        except Exception as exc:  # pragma: no cover - depends on the OS
            raise ImportError(_INSTALL_HINT) from exc
        self.mod = uia_mod
        self.iuia = comtypes.CoCreateInstance(
            uia_mod.CUIAutomation._reg_clsid_, interface=uia_mod.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self.pattern_props: List[Tuple[str, int]] = [
            ("invoke", uia_mod.UIA_IsInvokePatternAvailablePropertyId),
            ("value", uia_mod.UIA_IsValuePatternAvailablePropertyId),
            ("toggle", uia_mod.UIA_IsTogglePatternAvailablePropertyId),
            ("select", uia_mod.UIA_IsSelectionItemPatternAvailablePropertyId),
            ("expand", uia_mod.UIA_IsExpandCollapsePatternAvailablePropertyId),
            ("scroll", uia_mod.UIA_IsScrollPatternAvailablePropertyId),
            ("rangevalue", uia_mod.UIA_IsRangeValuePatternAvailablePropertyId),
        ]
        self.dialog_prop = getattr(uia_mod, "UIA_IsDialogPropertyId", 30174)
        #: State, not availability. Two more cached properties (read
        #: in-process, with the rest) buy the difference between "this list
        #: item can be selected" and "this list item is selected".
        self.selected_prop = getattr(
            uia_mod, "UIA_SelectionItemIsSelectedPropertyId", 30079)
        self.toggle_prop = getattr(
            uia_mod, "UIA_ToggleToggleStatePropertyId", 30086)
        #: One call for the whole window (atomic, one round trip).
        self.cache_subtree = self._cache_request(uia_mod.TreeScope_Subtree)
        #: One call per container (prunable, budget can stop it mid-walk).
        self.cache = self._cache_request(
            uia_mod.TreeScope_Element | uia_mod.TreeScope_Children)

    def _cache_request(self, scope):
        uia_mod = self.mod
        cache = self.iuia.CreateCacheRequest()
        props = [uia_mod.UIA_NamePropertyId, uia_mod.UIA_ControlTypePropertyId,
                 uia_mod.UIA_BoundingRectanglePropertyId,
                 uia_mod.UIA_IsEnabledPropertyId,
                 uia_mod.UIA_HasKeyboardFocusPropertyId,
                 uia_mod.UIA_IsOffscreenPropertyId,
                 uia_mod.UIA_AutomationIdPropertyId,
                 uia_mod.UIA_ClassNamePropertyId, uia_mod.UIA_ProcessIdPropertyId,
                 uia_mod.UIA_ValueValuePropertyId, self.selected_prop,
                 self.toggle_prop]
        props.extend(pid for _, pid in self.pattern_props)
        for prop in props:
            cache.AddProperty(prop)
        try:
            cache.AddProperty(self.dialog_prop)  # Win10 1809+; harmless if absent
        except Exception:  # pragma: no cover - old Windows
            self.dialog_prop = 0
        cache.TreeScope = scope
        cache.TreeFilter = self.iuia.ControlViewCondition
        # Full, not None: the returned elements stay live, which is what a later
        # Invoke/SetValue needs. Measured indistinguishable from None mode.
        cache.AutomationElementMode = uia_mod.AutomationElementMode_Full
        return cache

    def menu_is_open(self) -> bool:
        """Cheap check for "a menu is currently dropped down".

        One cross-process call, and only made when the walk actually meets a
        MenuBar. Skipping closed MenuBar subtrees is what
        ``typesafe-computer-use`` does, for the same reason: a closed menu's
        items are not actionable, and realising them makes the provider build
        the whole menu tree.
        """
        try:
            focused = self.iuia.GetFocusedElement()
            return int(focused.CurrentControlType) in _MENU_CONTROL_TYPES
        except Exception:
            return False


def _client() -> _Uia:
    client = getattr(_local, "uia", None)
    if client is None:
        client = _Uia()
        _local.uia = client
    return client


# --------------------------------------------------------------------------- #
# Reading one node
# --------------------------------------------------------------------------- #

def _text(value: Any, limit: int) -> str:
    if not value:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def _read(el: Any, uia: _Uia, element_id: str, depth: int,
          parent: Optional[str], region: Optional[str]) -> Optional[UIElement]:
    """Cached properties -> :class:`UIElement`, or ``None`` when not worth it.

    Every read here is in-process against the cache filled by the last
    ``BuildUpdatedCache``. The equivalent live read is a cross-process call per
    property — the whole reason the comparison table at the top of this module
    shows ``uiautomation`` and ``pywinauto`` costing 2-4x as much for the same
    two windows. (An earlier draft quoted per-property microseconds here; no
    artifact in this repo produces them, so they are gone rather than guessed.)
    """
    try:
        rect = el.CachedBoundingRectangle
        bbox = (int(rect.left), int(rect.top),
                int(rect.right - rect.left), int(rect.bottom - rect.top))
        control_type = int(el.CachedControlType)
        role = CONTROL_TYPES.get(control_type, "custom")
        if role == "window" and uia.dialog_prop:
            try:
                if bool(el.GetCachedPropertyValue(uia.dialog_prop)):
                    role = "dialog"
            except Exception:
                pass
        patterns = tuple(
            key for key, prop in uia.pattern_props
            if _cached_bool(el, prop))
        value = None
        if "value" in patterns:
            value = _text(el.GetCachedPropertyValue(uia.mod.UIA_ValueValuePropertyId),
                          MAX_VALUE_CHARS) or None
        # Only asked when the pattern is advertised: a provider that does not
        # support SelectionItem/Toggle answers these with a default that would
        # otherwise read as "off" rather than "not applicable".
        selected = "select" in patterns and _cached_bool(el, uia.selected_prop)
        toggled = None
        if "toggle" in patterns:
            try:
                toggled = int(el.GetCachedPropertyValue(uia.toggle_prop))
            except Exception:
                toggled = None
        return UIElement(
            id=element_id, role=role, name=_text(el.CachedName, MAX_NAME_CHARS),
            value=value, enabled=bool(el.CachedIsEnabled),
            focused=bool(el.CachedHasKeyboardFocus),
            offscreen=bool(el.CachedIsOffscreen), bbox=bbox, patterns=patterns,
            automation_id=_text(el.CachedAutomationId, 64),
            class_name=_text(el.CachedClassName, 64), depth=depth,
            parent=parent, region=region, selected=selected, toggled=toggled)
    except Exception:
        # A node can die mid-walk (a tooltip closing, a UWP app suspending).
        # One dead node is not a failed snapshot.
        return None


def _cached_bool(el: Any, prop: int) -> bool:
    try:
        return bool(el.GetCachedPropertyValue(prop))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

def _root_of(uia: _Uia, hwnd: int, strategy: str) -> Any:
    """``ElementFromHandle`` + ``BuildUpdatedCache``, as an ``OSError``.

    Both raise ``COMError`` — not ``OSError`` — when the window is dying,
    suspended by Process Lifetime Management, or owned by a process at a higher
    integrity level. The docstring of :func:`snapshot` promises ``OSError`` for
    "there is no such window", and a caller that wrote ``except OSError`` around
    a snapshot got a ``COMError`` through the gap. Re-raised here with the
    HRESULT and the hwnd, because "0x80040201 on hwnd 0x000707A2" is the whole
    diagnosis and ``COMError(...)`` alone is none of it.
    """
    try:
        live = uia.iuia.ElementFromHandle(hwnd)
        return live.BuildUpdatedCache(
            uia.cache_subtree if strategy == "subtree" else uia.cache)
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        if hresult is None:
            hresult = getattr(exc, "winerror", None)
        code = "0x%08X" % (hresult & 0xFFFFFFFF) if hresult is not None else "?"
        raise OSError(
            "cannot read window 0x%08X: %s %s (%s) — the window may have "
            "closed, or a packaged app may have been suspended"
            % (hwnd, type(exc).__name__, code, exc)) from exc


def _walk(uia: _Uia, root: Any, strategy: str, *, max_nodes: int,
          clock: float, budget_ms: float, first_index: int = 0,
          parent: Optional[str] = None,
          region: Optional[str] = None) -> Tuple[List[UIElement], Dict[str, Any], bool]:
    """The pure descent, shared by the main window and by every popup.

    Returns ``(elements, handles, truncated)``. ``first_index`` continues the
    ``e0, e1, ...`` numbering, so a popup's elements can be appended to the
    window's list without either renumbering or colliding.
    """
    elements: List[UIElement] = []
    handles: Dict[str, Any] = {}
    truncated = False
    menu_open: Optional[bool] = None
    # (element, depth, parent id, region id, its children are already cached)
    stack: List[Tuple[Any, int, Optional[str], Optional[str], bool]] = [
        (root, 0, parent, region, True)]

    while stack:
        el, depth, node_parent, node_region, has_children = stack.pop()
        record = _read(el, uia, "e%d" % (first_index + len(elements)), depth,
                       node_parent, node_region)
        if record is None:
            continue

        # A 0x0 or 1x1 node is layout, not a control: it can never be clicked,
        # so it is not recorded. Its *children* still are — measured on a
        # Notepad with tabs restored (a selection probe, not the committed
        # fixture), dropping the subtree with the node cost 74 of 135 nodes,
        # including real buttons under an unsized WinUI content host. The
        # children take the dropped node's place (same parent, same depth), so
        # the recorded tree stays a tree.
        keep = not depth or record.is_sized()
        if keep:
            elements.append(record)
            handles[record.id] = el
            if first_index + len(elements) >= max_nodes:
                truncated = True
                break
        if (time.perf_counter() - clock) * 1000.0 >= budget_ms:
            truncated = True
            break
        if record.offscreen:
            continue
        if record.role == _MENUBAR:
            if menu_open is None:
                menu_open = uia.menu_is_open()
            if not menu_open:
                continue
        try:
            if has_children or strategy == "subtree":
                children = el.GetCachedChildren()
            else:
                children = el.BuildUpdatedCache(uia.cache).GetCachedChildren()
        except Exception:
            continue
        if not children:
            continue
        child_parent = record.id if keep else node_parent
        child_depth = depth + 1 if keep else depth
        child_region = (record.id if keep and record.role in REGION_ROLES
                        else node_region)
        for index in range(children.Length - 1, -1, -1):
            stack.append((children.GetElement(index), child_depth, child_parent,
                          child_region, False))
    return elements, handles, truncated


def popup_hwnds(pid: int) -> List[int]:
    """Visible ``#32768`` popups belonging to ``pid``, topmost last.

    A dropped-down menu, a combo box list and a context menu are *separate
    top-level windows* on Windows, so the subtree of the application's HWND
    does not contain them. That is why clicking "File" appeared to do nothing:
    the menu opened, the next snapshot walked the same window, and ``tree_hash``
    reported "unchanged".

    Filtered by pid because a popup is created by the thread that owns the menu;
    a shell-extension context menu hosted out of process would be missed, which
    is a limitation this function does not hide.
    """
    user32 = _user32()
    found: List[int] = []

    def callback(popup: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(popup):
            return True
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(popup, buf, 64)
        if buf.value != POPUP_CLASS:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(popup, ctypes.byref(owner))
        if not pid or int(owner.value) == pid:
            found.append(int(popup))
        return True

    user32.EnumWindows(_ENUM_WINDOWS_PROC(callback), 0)
    return found


def merge_popup(elements: Sequence[UIElement],
                popup: Sequence[UIElement]) -> List[UIElement]:
    """Append a popup's elements to a window's, renumbering as we go.

    Pure, so the merge is testable without a menu on screen. The popup's own
    root keeps ``parent=None`` — it genuinely has no parent inside this window
    — and carries ``region=POPUP_REGION`` so a caller (and
    :func:`jevskill.cu.reduce.regions`) can tell where it came from. Its
    descendants point at ids inside the popup, which is why the renumbering has
    to rewrite ``parent`` and ``region`` and not just ``id``.
    """
    out = list(elements)
    offset = len(out)
    remap = {el.id: "e%d" % (offset + index) for index, el in enumerate(popup)}
    for el in popup:
        parent = remap.get(el.parent) if el.parent else None
        region = remap.get(el.region, POPUP_REGION) if el.region else POPUP_REGION
        out.append(replace(el, id=remap[el.id], parent=parent, region=region))
    return out


def snapshot(hwnd: Optional[int] = None, *, max_nodes: int = 4000,
             budget_ms: float = 600.0, strategy: str = DEFAULT_STRATEGY,
             include_popups: bool = True) -> Snapshot:
    """Observe one window. Defaults to whatever is in the foreground.

    ``max_nodes`` and ``budget_ms`` both set ``truncated``; the element list is
    then a document-order prefix of the tree, never a sample, so "the first N
    controls" stays a meaningful set.

    **What ``budget_ms`` actually bounds, precisely.** It bounds the *Python
    walk*, checked once per node, and it bounds nothing else.

    In ``"subtree"`` the tree arrives in a single ``BuildUpdatedCache`` that has
    already completed before the first check could run, and that call is the
    expensive half — a 4,000-node window can spend seconds inside it. The
    budget clock therefore starts **after** that call returns. Charging the
    walk for the provider's time was worse than useless: it aborted after the
    root node, so the caller paid the whole provider cost and then threw the
    tree away, when walking a cached 2,000-node tree costs 2.8 ms. In
    ``"lazy"`` the clock runs from the start, because there every step really
    can decide not to pay for the next one.

    So ``truncated`` answers "is this list incomplete?" — ``max_nodes`` was hit,
    or the Python walk itself ran long — and never "was the budget honoured?".
    :attr:`Snapshot.over_budget` answers the second question, from total wall
    time, after the fact. A snapshot can be ``over_budget`` and complete, and
    that is the common case on a slow window.

    Deliberately **not** done: probing with ``FindAll`` to guess the node count
    and falling back to ``"lazy"``. That probe is itself a cross-process call
    over the same tree — it costs the thing it is trying to bound, and on the
    213-node selection probe ``FindAllBuildCache`` measured 3x *worse* than the
    walk it would protect. A caller who needs a hard wall should pass
    ``strategy="lazy"``, which pays more round trips and is the only mode that
    can stop mid-tree, or run perception on a thread it is willing to abandon.

    ``strategy`` picks how the tree is fetched, and the choice is measured
    rather than argued (``bench/cu_observe_bench.py`` runs both):

    ``"subtree"``
        One ``BuildUpdatedCache(TreeScope_Subtree)`` for the whole window, then
        a pure-Python walk over the cached children. One cross-process round
        trip, an atomic view of the tree, and the pruning still happens — but
        on the client side, so the provider has already paid for the MenuBar
        subtree by the time it is skipped.
    ``"lazy"``
        One call per container — 11 on the committed 34-node Notepad fixture,
        13 on Calculator, and a *selection probe* of a Notepad with 18 tabs
        restored put it at 60-64. Equal to ``"subtree"`` on an idle machine and
        measurably worse under load — see the module docstring for both sets of
        figures — because every round trip pays the contention again. It buys
        the ability to abandon a walk mid-tree.

    ``include_popups`` merges any visible ``#32768`` popup owned by the same
    process into the element list (see :func:`popup_hwnds`). It costs one
    ``EnumWindows`` — an in-process call, microseconds — and only descends when
    a popup is actually up. **Not verified against a live menu**: the merge
    logic is covered offline, the enumeration is not, so treat a popup that
    fails to appear as a bug in this function rather than in the caller.

    Raises ``ImportError`` naming the extra when comtypes or Windows is
    missing. Raises ``OSError`` when there is no such window, and when reading
    it fails — including the ``COMError`` a suspended or dying window raises.
    """
    if strategy not in ("subtree", "lazy"):
        raise ValueError("strategy must be 'subtree' or 'lazy', not %r" % strategy)
    started = time.time()
    clock = time.perf_counter()
    uia = _client()
    if hwnd is None:
        hwnd = foreground_hwnd()
    if not hwnd:
        raise OSError("no foreground window")

    root = _root_of(uia, hwnd, strategy)
    # See the docstring: in "subtree" the provider has already been paid, so
    # the walk gets its own clock and actually walks what was bought.
    walk_clock = time.perf_counter() if strategy == "subtree" else clock
    elements, handles, truncated = _walk(
        uia, root, strategy, max_nodes=max_nodes, clock=walk_clock,
        budget_ms=budget_ms)

    pid = window_pid(hwnd)
    app = process_name(pid)
    if app.lower() == "applicationframehost.exe":
        # A UWP window's frame belongs to ApplicationFrameHost; the app itself
        # is the child. Report the app the user would name.
        real = _hosted_pid(elements, handles, uia, pid)
        if real:
            pid, app = real, process_name(real)

    if include_popups and not truncated:
        for popup in popup_hwnds(pid):
            if popup == hwnd:
                continue
            try:
                popup_root = _root_of(uia, popup, strategy)
            except OSError:
                continue  # a menu can close between enumeration and read
            found, popup_handles, popup_truncated = _walk(
                uia, popup_root, strategy, max_nodes=max_nodes,
                clock=walk_clock, budget_ms=budget_ms,
                first_index=len(elements), region=POPUP_REGION)
            if not found:
                continue
            elements = merge_popup(elements, found)
            for element_id, handle in popup_handles.items():
                handles[element_id] = handle
            truncated = truncated or popup_truncated
            if truncated:
                break

    elapsed = (time.perf_counter() - clock) * 1000.0
    return Snapshot(
        window_title=window_title(hwnd), app=app, pid=pid, hwnd=int(hwnd),
        taken_at=started, elapsed_ms=elapsed, elements=elements,
        truncated=truncated, over_budget=elapsed > budget_ms,
        _handles=handles)


def _hosted_pid(elements: Sequence[UIElement], handles: Dict[str, Any],
                uia: _Uia, frame_pid: int) -> int:
    """The pid of the app *inside* a UWP frame.

    The frame's own title bar is a child too and reports the frame's pid, so
    "the first child" is the wrong answer — the first child with a *different*
    pid is the right one.
    """
    for el in elements[1:]:
        handle = handles.get(el.id)
        if handle is None:
            continue
        try:
            pid = int(handle.GetCachedPropertyValue(uia.mod.UIA_ProcessIdPropertyId))
        except Exception:
            continue
        if pid and pid != frame_pid:
            return pid
    return 0


# --------------------------------------------------------------------------- #
# The state a model sees
# --------------------------------------------------------------------------- #

def _cell(bbox: Tuple[int, int, int, int],
          frame: Tuple[int, int, int, int], grid: int = GRID) -> str:
    """"r3c5" — the element's centre in a grid over the window.

    Coarse on purpose. A model asked to click a control never needs the pixel
    rectangle (the code has it, keyed by id); it needs "top-right" to
    disambiguate two controls with the same name. Five characters instead of
    twenty, on every element, on every step.
    """
    left, top, width, height = frame
    if width <= 0 or height <= 0:
        return ""
    cx, cy = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
    col = min(grid - 1, max(0, int((cx - left) * grid // width)))
    row = min(grid - 1, max(0, int((cy - top) * grid // height)))
    return "r%dc%d" % (row, col)


def _frame(elements: Sequence[UIElement]) -> Tuple[int, int, int, int]:
    """The rectangle the grid is drawn over: the union of what is present.

    Not "element 0's rectangle": ``to_state`` is normally handed the *reduced*
    list, whose first element is a button, and a grid over a button puts every
    candidate in the same cell.
    """
    return union_bbox([el.bbox for el in elements])


def to_state(snapshot: Union[Snapshot, Sequence[Any]], *,
             elements: Optional[Sequence[Any]] = None,
             coarse_bbox: bool = True, grid: int = GRID,
             omit_defaults: bool = True) -> Dict[str, Any]:
    """The JSON a decision call carries: ``{"window_title", "app", "elements"}``.

    Same shape as ``bench/cu_bench.py`` measured the model on — ``elements``
    keyed ``e0..`` with ``role``/``name``/``bbox``/``enabled``/``focused`` —
    with two deliberate differences, both token decisions:

    * ``coarse_bbox`` replaces the pixel rectangle with a grid cell. The live
      handles and exact rectangles stay in the :class:`Snapshot`, which is
      where the clicking code reads them from.
    * ``omit_defaults`` drops ``"enabled": true`` and ``"focused": false``.
      After reduction every candidate is enabled, so the key carries no
      information on 60 of 60 elements while the one ``"focused": true`` is
      what the model should notice. Pass ``omit_defaults=False`` for the
      literal ``cu_bench`` shape.

    Three keys the ``cu_bench`` shape does not have, each earning its tokens:

    * ``dup`` — ``duplicates`` from :func:`jevskill.cu.reduce.dedupe`, emitted
      only when non-zero. The reduction collapses identical siblings and then
      *nothing told the model*: a survivor standing for nine looked exactly
      like a unique control, so "click the only Save button" and "click one of
      nine identical Save buttons" were the same question. Three characters.
    * ``sel`` — ``selected``, emitted only when true. Which row is highlighted
      is usually the whole state of a list.
    * ``tog`` — ``toggled``, emitted only when the control has a toggle state.
      ``0`` off, ``1`` on, ``2`` indeterminate.

    Never contains a live UIA handle: this is data, and it crosses a network.
    """
    if isinstance(snapshot, Snapshot):
        whole = list(snapshot.elements)
        head: Dict[str, Any] = {"window_title": snapshot.window_title,
                                "app": snapshot.app}
    else:
        whole = [el if isinstance(el, UIElement) else UIElement.from_dict(el)
                 for el in snapshot]
        head = {}
    items = list(elements) if elements is not None else whole
    # The grid is drawn over the *window*, not over the shortlist: a cell has
    # to mean the same thing before and after a reduction, or "the button at
    # the bottom right" moves when the candidate list shortens.
    frame = _frame(whole)

    out: Dict[str, Any] = {}
    for el in items:
        if not isinstance(el, UIElement):
            el = UIElement.from_dict(el)
        entry: Dict[str, Any] = {"role": el.role, "name": el.name}
        if el.is_sized():
            entry["bbox"] = _cell(el.bbox, frame, grid) if coarse_bbox else list(el.bbox)
        if not el.enabled or not omit_defaults:
            entry["enabled"] = el.enabled
        if el.focused or not omit_defaults:
            entry["focused"] = el.focused
        if el.value:
            entry["value"] = el.value
        if el.selected:
            entry["sel"] = True
        if el.toggled is not None:
            entry["tog"] = el.toggled
        if el.duplicates:
            entry["dup"] = el.duplicates
        out[el.id] = entry
    head["elements"] = out
    return head


def state_tokens(state: Dict[str, Any]) -> int:
    """Token estimate for a state, through the package's one calibrated rule."""
    return count_tokens(state)

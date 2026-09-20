"""Read one Windows window's UI Automation tree, fast, and shape it for a model.

Which library, and why. Measured by ``bench/cu_observe_bench.py`` (Windows 11
Pro 26100, Python 3.13.2, 3 warm runs per app, numbers in
``bench/cu_observe_results.json``; the machine was running other work, so read
the spread, not the third digit):

=================================  ==================  ====================
walk                               Notepad (34 nodes)  Calculator (53 nodes)
=================================  ==================  ====================
``uiautomation`` 2.0.29            188-197 ms          144-161 ms
``pywinauto`` 0.6.9 (uia backend)  138-223 ms          101-123 ms
``comtypes`` + one CacheRequest    73-104 ms           42-52 ms
=================================  ==================  ====================

``uiautomation`` and ``pywinauto`` read every property live, so a node costs one
cross-process call *per property*. Caching Name/ControlType/BoundingRectangle/
IsEnabled/HasKeyboardFocus/IsOffscreen/AutomationId/ClassName/pattern
availability in a single ``CacheRequest`` turns 16 round trips per node into one
for the whole window; the Python side then reads them in-process at ~0.014 ms
each. The two libraries also return a different tree — they walk the raw view
and keep offscreen and zero-sized nodes — which is why their node counts are
higher for the same window.

Four things the measurement changed about the plan. Figures without a table
above come from the selection probes (not from the committed results file), and
are marked as such because they are not reproducible from a clean profile:

1. **The 10-60 ms state budget holds only for small windows.** Cost tracks the
   provider's node count, not the client: Calculator (53 nodes) 42-52 ms, an
   empty Notepad (34) 73-104 ms, the foreground window in the same run (506
   nodes) 340 ms. *Selection probe:* a Notepad that had restored 18 tabs — 213
   control-view nodes, 183 of them under one
   ``Microsoft.UI.Content.DesktopChildSiteBridge`` XAML island that cost 337 ms
   by itself — took 368-410 ms. Budget roughly a millisecond per node, and do
   not assume a window is empty.
2. **No client-side trick moves it.** *Selection probe, on that 213-node
   Notepad:* ``AutomationElementMode_None`` (no live handles), a 6-property
   cache instead of 16, ``ContentView`` or ``RawView`` instead of
   ``ControlView``, and MTA instead of STA were all within noise of each other
   (360-470 ms), and ``FindAllBuildCache`` was 3x worse (2.1-2.7 s). The cost
   is the provider's.
3. **One round trip beats sixty-four.** A lazy per-container descent ties the
   single ``TreeScope_Subtree`` call on an idle machine (*selection probe*:
   368-392 vs 375-410 ms) and loses on a working one (committed run, Calculator:
   70-73 ms lazy vs 46-54 subtree; Notepad, 34 nodes and 11 containers, is a tie
   at 91-103 vs 83-108), because each round trip pays the contention again.
   ``strategy="subtree"`` is the default for that reason; ``"lazy"`` stays
   available for a window too large to fetch at once, and is the only one that
   can stop mid-walk.

A fourth number worth keeping: the same run's foreground window reduced to 60
candidates and **3,093 tokens** — under the 3,500 budget, but only because
``to_state`` drops ``"enabled": true`` and ``"focused": false``. A real screen
is not the synthetic one (the synthetic 500-node tree reduces to 2,479).
4. **Packaged apps suspend.** Notepad and Calculator are both packaged, so
   Process Lifetime Management suspends them seconds after they lose the
   foreground and every UIA call then fails with
   ``EVENT_E_ALL_SUBSCRIBERS_FAILED``. Snapshot a packaged app immediately
   after launching it, or keep it in front.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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


# --------------------------------------------------------------------------- #
# Win32 helpers (ctypes, so the import costs nothing on a non-Windows box)
# --------------------------------------------------------------------------- #

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
                 uia_mod.UIA_ValueValuePropertyId]
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
    ``BuildUpdatedCache``; a live read would cost ~5 ms instead of ~0.014 ms.
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
        return UIElement(
            id=element_id, role=role, name=_text(el.CachedName, MAX_NAME_CHARS),
            value=value, enabled=bool(el.CachedIsEnabled),
            focused=bool(el.CachedHasKeyboardFocus),
            offscreen=bool(el.CachedIsOffscreen), bbox=bbox, patterns=patterns,
            automation_id=_text(el.CachedAutomationId, 64),
            class_name=_text(el.CachedClassName, 64), depth=depth,
            parent=parent, region=region)
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

def snapshot(hwnd: Optional[int] = None, *, max_nodes: int = 4000,
             budget_ms: float = 600.0, strategy: str = DEFAULT_STRATEGY) -> Snapshot:
    """Observe one window. Defaults to whatever is in the foreground.

    ``max_nodes`` and ``budget_ms`` are both hard stops that set
    ``truncated``; the element list is then a document-order prefix of the
    tree, never a sample, so "the first N controls" stays a meaningful set.

    ``strategy`` picks how the tree is fetched, and the choice is measured
    rather than argued (``bench/cu_observe_bench.py`` runs both):

    ``"subtree"``
        One ``BuildUpdatedCache(TreeScope_Subtree)`` for the whole window, then
        a pure-Python walk over the cached children. One cross-process round
        trip, an atomic view of the tree, and the pruning still happens — but
        on the client side, so the provider has already paid for the MenuBar
        subtree by the time it is skipped, and ``budget_ms`` can only stop the
        Python half.
    ``"lazy"``
        One call per container (60-64 on a Notepad with tabs open). Equal to
        ``"subtree"`` on an idle machine and measurably worse under load — see
        the module docstring for both sets of figures — because every round
        trip pays the contention again. It buys the ability to abandon a walk
        mid-tree, which matters for an application far larger than
        ``max_nodes``.

    Raises ``ImportError`` naming the extra when comtypes or Windows is
    missing. Raises ``OSError`` when there is no such window.
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

    root_live = uia.iuia.ElementFromHandle(hwnd)
    root = root_live.BuildUpdatedCache(
        uia.cache_subtree if strategy == "subtree" else uia.cache)

    elements: List[UIElement] = []
    handles: Dict[str, Any] = {}
    truncated = False
    menu_open: Optional[bool] = None
    # (element, depth, parent id, region id, its children are already cached)
    stack: List[Tuple[Any, int, Optional[str], Optional[str], bool]] = [
        (root, 0, None, None, True)]

    while stack:
        el, depth, parent, region, has_children = stack.pop()
        record = _read(el, uia, "e%d" % len(elements), depth, parent, region)
        if record is None:
            continue

        # A 0x0 or 1x1 node is layout, not a control: it can never be clicked,
        # so it is not recorded. Its *children* still are — measured on
        # Notepad, dropping the subtree with the node cost 74 of 135 nodes,
        # including real buttons under an unsized WinUI content host. The
        # children take the dropped node's place (same parent, same depth), so
        # the recorded tree stays a tree.
        keep = not depth or record.is_sized()
        if keep:
            elements.append(record)
            handles[record.id] = el
            if len(elements) >= max_nodes:
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
        child_parent = record.id if keep else parent
        child_depth = depth + 1 if keep else depth
        child_region = (record.id if keep and record.role in REGION_ROLES
                        else region)
        for index in range(children.Length - 1, -1, -1):
            stack.append((children.GetElement(index), child_depth, child_parent,
                          child_region, False))

    pid = window_pid(hwnd)
    app = process_name(pid)
    if app.lower() == "applicationframehost.exe":
        # A UWP window's frame belongs to ApplicationFrameHost; the app itself
        # is the child. Report the app the user would name.
        real = _hosted_pid(elements, handles, uia, pid)
        if real:
            pid, app = real, process_name(real)
    return Snapshot(
        window_title=window_title(hwnd), app=app, pid=pid, hwnd=int(hwnd),
        taken_at=started, elapsed_ms=(time.perf_counter() - clock) * 1000.0,
        elements=elements, truncated=truncated, _handles=handles)


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
        out[el.id] = entry
    head["elements"] = out
    return head


def state_tokens(state: Dict[str, Any]) -> int:
    """Token estimate for a state, through the package's one calibrated rule.

    Imported here rather than at module import time so ``jevskill.cu`` stays
    importable on its own.
    """
    from ..orchestrate import count_tokens

    return count_tokens(state)

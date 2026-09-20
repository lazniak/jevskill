"""The element shape a computer-use loop decides over.

One dataclass, not a dict, because the reduction step (:mod:`jevskill.cu.reduce`)
is the place where a wrong field name silently drops a candidate and the model
gets blamed for it. A dataclass turns that into an ``AttributeError`` in a test.

Two rules that are easy to get wrong and expensive to debug:

* **Ids are positional, not stable.** ``e0`` is "the first node in this walk",
  so the same button is ``e12`` before a dialog opens and ``e31`` after. Never
  key a cache or a diff on an id across snapshots — :mod:`jevskill.cu.hashing`
  matches on ``(role, name, automation_id, class_name)`` for exactly this
  reason.
* **Live UIA handles never reach the serialised state.** They are COM pointers:
  they cannot be JSON-encoded, they keep a cross-process reference alive, and a
  state sent to a model must be pure data. :class:`Snapshot` keeps them in
  ``_handles`` and neither :meth:`Snapshot.to_dict` nor
  :func:`jevskill.cu.observe.to_state` ever looks at that field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# UIA ControlType ids -> the lowercased name this package uses as ``role``.
# Hardcoded rather than read from the generated comtypes module so that
# reduce/hashing/tests run on Linux CI where UIAutomationCore.dll does not
# exist. The ids are stable Win32 constants (UIA_*ControlTypeId).
CONTROL_TYPES: Dict[int, str] = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar",
    50038: "separator", 50039: "semanticzoom", 50040: "appbar",
}

#: Roles a click/type/select can sensibly target. A role outside this set still
#: becomes a candidate when it advertises an actionable pattern — WinUI ships
#: plenty of ``pane`` and ``custom`` nodes that are really buttons.
INTERACTIVE_ROLES = frozenset({
    "button", "checkbox", "combobox", "edit", "hyperlink", "listitem",
    "menuitem", "radiobutton", "slider", "spinner", "splitbutton", "tabitem",
    "treeitem", "dataitem", "headeritem", "calendar", "scrollbar", "thumb",
})

#: Patterns that make a node actionable regardless of its role.
ACTIONABLE_PATTERNS = frozenset({"invoke", "value", "toggle", "select"})

#: The full pattern vocabulary kept on an element (a subset of UIA's).
PATTERN_KEYS: Tuple[str, ...] = (
    "invoke", "value", "toggle", "select", "expand", "scroll", "rangevalue",
)

#: Roles that can act as the grouping ancestor of a region. ``dialog`` is not a
#: UIA control type: :mod:`jevskill.cu.observe` promotes a window whose
#: ``IsDialog`` property is true to this role, because "the dialog on top" is
#: the single most useful prior a computer-use loop has. ``menu`` is here as
#: well as ``menubar``: a dropped-down menu arrives as the root of a separate
#: ``#32768`` popup window, and without it every item in an open File menu
#: would be grouped under the window behind it.
REGION_ROLES = frozenset({
    "dialog", "window", "pane", "group", "toolbar", "menu", "menubar",
    "statusbar", "titlebar", "document", "list", "tree", "table", "datagrid",
    "tab", "header", "appbar",
})

#: Below this, a rectangle is a layout artefact rather than a control. Windows
#: is full of 1x1 and 0x0 nodes; they cost tokens and can never be clicked.
MIN_SIDE_PX = 4


@dataclass
class UIElement:
    """One node of an accessibility tree, already normalised.

    ``bbox`` is ``(left, top, width, height)`` in physical screen pixels, which
    is what ``SendInput`` and UIA's ``BoundingRectangle`` both speak. Storing
    ``width``/``height`` rather than ``right``/``bottom`` is deliberate: every
    consumer here asks "is this big enough to click", not "where does it end".
    """

    id: str
    role: str
    name: str = ""
    value: Optional[str] = None
    enabled: bool = True
    focused: bool = False
    offscreen: bool = False
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    patterns: Tuple[str, ...] = ()
    automation_id: str = ""
    class_name: str = ""
    depth: int = 0
    parent: Optional[str] = None
    region: Optional[str] = None
    #: ``SelectionItemPattern.IsSelected``. ``patterns`` says a control *can*
    #: be selected; this says it *is*. Without the distinction an arrow-key
    #: move down a list produced an identical tree hash — measured — and the
    #: loop was told its keystroke did nothing. One cached property to read.
    selected: bool = False
    #: ``TogglePattern.ToggleState``: 0 off, 1 on, 2 indeterminate, ``None``
    #: when the control has no TogglePattern. An int rather than a bool because
    #: indeterminate is a real third state (a tri-state checkbox in a
    #: "select all" header) and collapsing it to False would make ticking the
    #: last child invisible.
    toggled: Optional[int] = None
    #: How many identical siblings this element stands for after
    #: :func:`jevskill.cu.reduce.dedupe`. Kept so a caller can tell "one Save
    #: button" from "one of nine identical Save buttons" without re-walking.
    duplicates: int = 0

    @property
    def area(self) -> int:
        return max(0, self.bbox[2]) * max(0, self.bbox[3])

    @property
    def center(self) -> Tuple[int, int]:
        left, top, width, height = self.bbox
        return left + width // 2, top + height // 2

    def is_sized(self) -> bool:
        """True when the rectangle is big enough to be a real control."""
        return self.bbox[2] >= MIN_SIDE_PX and self.bbox[3] >= MIN_SIDE_PX

    def to_dict(self) -> Dict[str, Any]:
        """JSON for a fixture or a log — defaults omitted.

        Absent means default, which :meth:`from_dict` restores. Most nodes in a
        real window have no automation id, no class name, no value and no
        duplicates, and writing those out quadrupled the committed fixtures
        (the 2,000-node tree: 776 KB with every key, 220 KB without) while
        burying the fields that differ.
        """
        d: Dict[str, Any] = {"id": self.id, "role": self.role,
                             "bbox": list(self.bbox)}
        if self.name:
            d["name"] = self.name
        if self.value is not None:
            d["value"] = self.value
        if not self.enabled:
            d["enabled"] = False
        if self.focused:
            d["focused"] = True
        if self.offscreen:
            d["offscreen"] = True
        if self.patterns:
            d["patterns"] = list(self.patterns)
        if self.automation_id:
            d["automation_id"] = self.automation_id
        if self.class_name:
            d["class_name"] = self.class_name
        if self.depth:
            d["depth"] = self.depth
        if self.parent is not None:
            d["parent"] = self.parent
        if self.region is not None:
            d["region"] = self.region
        if self.selected:
            d["selected"] = True
        if self.toggled is not None:
            d["toggled"] = self.toggled
        if self.duplicates:
            d["duplicates"] = self.duplicates
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UIElement":
        return cls(
            id=d["id"], role=d.get("role", ""), name=d.get("name", "") or "",
            value=d.get("value"), enabled=bool(d.get("enabled", True)),
            focused=bool(d.get("focused", False)),
            offscreen=bool(d.get("offscreen", False)),
            bbox=tuple(d.get("bbox") or (0, 0, 0, 0)),  # type: ignore[arg-type]
            patterns=tuple(d.get("patterns") or ()),
            automation_id=d.get("automation_id", "") or "",
            class_name=d.get("class_name", "") or "",
            depth=int(d.get("depth", 0)), parent=d.get("parent"),
            region=d.get("region"),
            selected=bool(d.get("selected", False)),
            toggled=(None if d.get("toggled") is None else int(d["toggled"])),
            duplicates=int(d.get("duplicates", 0)),
        )


@dataclass
class Region:
    """A named group of elements — the coarse level of the reduce cascade.

    When a screen has more candidates than the cap, asking "which *region*"
    first and "which element in it" second is the documented
    ``hierarchical_classification`` shape, and it keeps every single call inside
    the option-count range where the model was measured to be accurate.
    """

    id: str
    name: str
    role: str
    bbox: Tuple[int, int, int, int]
    members: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role,
                "bbox": list(self.bbox), "members": list(self.members)}


@dataclass
class Snapshot:
    """One observation of one window.

    ``elapsed_ms`` is wall time for the whole walk including the UIA calls, so
    a caller can enforce a step budget without instrumenting this module.
    ``truncated`` says the walk stopped early (node cap or time budget) — the
    element list is then a prefix of the tree in document order, not a sample.

    ``truncated`` and ``over_budget`` are two different questions and were
    conflated once already. ``truncated`` means "this list is incomplete".
    ``over_budget`` means "this took longer than you asked", which happens
    *without* truncation whenever the single provider call in
    ``strategy="subtree"`` runs long: the budget is only checked between Python
    steps, so a slow ``BuildUpdatedCache`` blows it before the first check and
    still returns the whole tree.
    """

    window_title: str
    app: str
    pid: int
    hwnd: int
    taken_at: float
    elapsed_ms: float
    elements: List[UIElement] = field(default_factory=list)
    truncated: bool = False
    over_budget: bool = False
    #: ``id -> live UIA element``. Never serialised: see the module docstring.
    _handles: Dict[str, Any] = field(default_factory=dict, repr=False,
                                     compare=False)

    def __len__(self) -> int:
        return len(self.elements)

    def by_id(self, element_id: str) -> Optional[UIElement]:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def handle(self, element_id: str) -> Any:
        """The live UIA element behind an id, for Invoke/SetValue later."""
        return self._handles.get(element_id)

    @property
    def focus_id(self) -> Optional[str]:
        for el in self.elements:
            if el.focused:
                return el.id
        return None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe. Deliberately does not touch ``_handles``.

        ``over_budget`` is written only when true, so adding it did not
        invalidate a single committed fixture — the same "absent means default"
        rule :meth:`UIElement.to_dict` follows, for the same reason.
        """
        out: Dict[str, Any] = {
            "window_title": self.window_title, "app": self.app, "pid": self.pid,
            "hwnd": self.hwnd, "taken_at": self.taken_at,
            "elapsed_ms": self.elapsed_ms, "truncated": self.truncated,
            "elements": [el.to_dict() for el in self.elements],
        }
        if self.over_budget:
            out["over_budget"] = True
        return out

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        return cls(
            window_title=d.get("window_title", ""), app=d.get("app", ""),
            pid=int(d.get("pid", 0)), hwnd=int(d.get("hwnd", 0)),
            taken_at=float(d.get("taken_at", 0.0)),
            elapsed_ms=float(d.get("elapsed_ms", 0.0)),
            elements=[UIElement.from_dict(e) for e in d.get("elements", [])],
            truncated=bool(d.get("truncated", False)),
            over_budget=bool(d.get("over_budget", False)),
        )


def as_elements(items: Sequence[Any]) -> List[UIElement]:
    """Accept either dataclasses or plain dicts, return dataclasses.

    Fixtures are JSON; live snapshots are objects. Every public function in this
    package goes through here so a test never has to build COM objects.
    """
    out: List[UIElement] = []
    for item in items:
        out.append(item if isinstance(item, UIElement) else UIElement.from_dict(item))
    return out

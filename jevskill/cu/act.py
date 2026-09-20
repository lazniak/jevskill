"""Perform one action on a live window — and decide, in code, when it may happen.

**Nothing in this module has been exercised against a real desktop.** Every
test drives a fake backend; :class:`UiaBackend` is written against the
documented UIA pattern interfaces and the Win32 ``SendInput`` signature, and it
has never invoked a real control. Treat the first live run as a bring-up, with a
throwaway window, not as a regression: a wrong ``VARIANT`` here does not raise,
it clicks something.

Two rules this module exists to enforce, both of them code's job and neither of
them the model's:

* **The deterministic name list is the gate.** :data:`DESTRUCTIVE_NAMES` is
  matched case-folded, as a substring, against the control's name. It fires
  before any probability is read. The per-element Noul in
  :mod:`jevskill.cu.decide` is a second opinion that *adds* to the list, and it
  is measurably not a boundary: 0.78 and 0.76 (act.md §9) and 0.82 (the
  2026-09-20 review run) for a button named literally "Delete all documents" —
  all three below the 0.85 bar an automatic policy would need.
* **Prefer the platform's patterns to synthetic input.** ``Invoke``,
  ``SetValue``, ``Toggle``, ``SelectionItem.Select`` and ``ScrollItem`` act on
  the control directly: they work when the window is occluded, they cannot land
  on whatever moved under the cursor between the snapshot and the click, and
  they do not depend on DPI. ``SendInput`` is the fallback, and a pixel click is
  the fallback's fallback.

The module imports cleanly on Linux: ``ctypes`` and ``comtypes`` are imported
inside :class:`UiaBackend` methods, never at module scope, so
``import jevskill.cu.act`` costs nothing and needs no Windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .hashing import tree_hash
from .types import Snapshot, UIElement

#: act.md §4's list, unchanged. Case-folded substring match, so "Delete all
#: documents", "Empty Recycle Bin" and "Send now" all fire. Substring matching
#: over-fires ("Format painter", "Remove filter") and that is the intended
#: direction of the error: a needless confirmation costs a second, a missed one
#: costs the data.
DESTRUCTIVE_NAMES: Tuple[str, ...] = (
    "delete", "remove", "send", "pay", "buy", "format", "uninstall", "empty",
)

#: Settle caps from ``jev-ultrafast``, via act.md §4. A combobox whose
#: suggestions populate asynchronously needs the longer one; everything else
#: settles inside two animation frames or has not moved at all.
SETTLE_CAP_MS = 50.0
SETTLE_CAP_COMBOBOX_MS = 200.0

#: The ceiling :func:`settle` uses when the caller does not name one. Longer
#: than act.md's per-control caps on purpose: this function *polls a hash*
#: rather than waiting on a UIA event, so it returns the moment the tree moves
#: and the ceiling is only paid when nothing happens at all.
SETTLE_TIMEOUT_MS = 800.0
SETTLE_POLL_MS = 40.0

#: Virtual-key codes for the names a decision may carry. Deliberately small: a
#: loop that needs a key the model did not name is escalating, not guessing.
VK_CODES: Dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21,
    "pagedown": 0x22, "f4": 0x73, "f5": 0x74, "f10": 0x79,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
}


def is_destructive_name(name: Optional[str]) -> bool:
    """Does this control's name put it behind the human gate?

    The gate, not a hint: :func:`jevskill.cu.decide.validate` sets
    ``requires_confirm`` from this and the loop refuses to act without an
    explicit yes, whatever the model's confidence was.
    """
    if not name:
        return False
    folded = str(name).casefold()
    return any(word in folded for word in DESTRUCTIVE_NAMES)


def risky_ids(elements: Sequence[Any]) -> list:
    """Ids of every candidate whose name matches :data:`DESTRUCTIVE_NAMES`."""
    from .types import as_elements

    return [el.id for el in as_elements(elements) if is_destructive_name(el.name)]


@dataclass
class Action:
    """One step's output: what to do, to what, with what.

    ``key`` and ``text`` are filled by *code* — a key from the focused role and
    the dialog, text from a small LLM gated by ``needs_text``. The decision
    model names neither (prompting.md §0, mode 11: "not trained to generate
    text").
    """

    op: str
    target: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None
    amount: int = 3
    #: Where this action came from: ``"jev"``, ``"macro"``, ``"code"`` or
    #: ``"escalation"``. Carried into the :class:`StepRecord` as ``decided_by``.
    source: str = "jev"
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "target": self.target, "text": self.text,
                "key": self.key, "amount": self.amount, "source": self.source,
                "note": self.note}


@dataclass
class ActResult:
    """What happened when the action was performed.

    ``ok`` is "the call returned without raising", which is *not* "the screen
    changed" — that is :func:`settle`'s answer and it comes from a hash, not
    from a return code.
    """

    ok: bool
    method: str = ""
    error: str = ""
    elapsed_ms: float = 0.0
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "method": self.method, "error": self.error,
                "elapsed_ms": round(self.elapsed_ms, 3), "dry_run": self.dry_run}


class UiaBackend:
    """The live half: UIA patterns first, ``SendInput`` second.

    **Untested against a real desktop.** Every method below is written from the
    UI Automation client documentation and the ``SendInput`` signature; the test
    suite substitutes a fake. The pattern ids are the stable ``UIA_*PatternId``
    constants, hardcoded for the same reason
    :data:`jevskill.cu.types.CONTROL_TYPES` is: so this module imports and is
    readable where ``UIAutomationCore.dll`` does not exist.
    """

    #: UIA_*PatternId — stable Win32 constants.
    INVOKE = 10000
    SELECTION_ITEM = 10010
    VALUE = 10002
    SCROLL = 10004
    SCROLL_ITEM = 10017
    TOGGLE = 10015
    EXPAND_COLLAPSE = 10005

    def _pattern(self, handle: Any, pattern_id: int, interface_name: str) -> Any:
        """Fetch one pattern from a live element, typed.

        ``GetCurrentPattern`` returns an ``IUnknown``; comtypes will not call a
        pattern method on it until it is cast to the pattern's own interface,
        and the failure mode of forgetting that is an ``AttributeError`` at the
        worst possible moment.
        """
        from comtypes.gen import UIAutomationClient as uia_mod

        raw = handle.GetCurrentPattern(pattern_id)
        if not raw:
            raise RuntimeError("element does not support %s" % interface_name)
        return raw.QueryInterface(getattr(uia_mod, interface_name))

    # ---- UIA patterns ----------------------------------------------------
    def invoke(self, handle: Any) -> None:
        self._pattern(handle, self.INVOKE, "IUIAutomationInvokePattern").Invoke()

    def toggle(self, handle: Any) -> None:
        self._pattern(handle, self.TOGGLE, "IUIAutomationTogglePattern").Toggle()

    def expand(self, handle: Any) -> None:
        self._pattern(handle, self.EXPAND_COLLAPSE,
                      "IUIAutomationExpandCollapsePattern").Expand()

    def select(self, handle: Any) -> None:
        self._pattern(handle, self.SELECTION_ITEM,
                      "IUIAutomationSelectionItemPattern").Select()

    def set_value(self, handle: Any, text: str) -> None:
        self._pattern(handle, self.VALUE, "IUIAutomationValuePattern").SetValue(text)

    def scroll_into_view(self, handle: Any) -> None:
        self._pattern(handle, self.SCROLL_ITEM,
                      "IUIAutomationScrollItemPattern").ScrollIntoView()

    def scroll(self, handle: Any, direction: str, amount: int = 1) -> None:
        """``ScrollPattern.Scroll`` with the documented ``NoAmount`` sentinel.

        The enum is ``ScrollAmount``: 0 LargeDecrement, 1 SmallDecrement,
        2 NoAmount, 3 LargeIncrement, 4 SmallIncrement. Passing 2 for the axis
        that must not move is required — leaving it out scrolls both.
        """
        pattern = self._pattern(handle, self.SCROLL, "IUIAutomationScrollPattern")
        vertical = 0 if direction == "up" else 3      # Large decrement / increment
        for _ in range(max(1, int(amount))):
            pattern.Scroll(2, vertical)

    # ---- synthetic input (the fallback) ----------------------------------
    def _send_input(self, inputs: Any, count: int) -> None:
        import ctypes

        sent = ctypes.windll.user32.SendInput(count, ctypes.byref(inputs),
                                              ctypes.sizeof(inputs[0]))
        if sent != count:
            raise RuntimeError("SendInput sent %d of %d events" % (sent, count))

    def _input_struct(self):
        """Build the ``INPUT`` union once per call, in-process.

        Defined inside a method rather than at module scope because
        ``ctypes.wintypes`` is a Windows type library and this file must import
        on Linux CI.
        """
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

        return INPUT, KEYBDINPUT

    def send_keys(self, keys: str) -> None:
        """One chord, e.g. ``"ctrl+s"`` or ``"enter"``. Down in order, up reversed."""

        INPUT, KEYBDINPUT = self._input_struct()
        codes = [vk_code(part) for part in parse_chord(keys)]
        events = []
        for code in codes:
            events.append((code, 0))
        for code in reversed(codes):
            events.append((code, 2))  # KEYEVENTF_KEYUP
        buffer = (INPUT * len(events))()
        for index, (code, flags) in enumerate(events):
            buffer[index].type = 1  # INPUT_KEYBOARD
            buffer[index].ki = KEYBDINPUT(code, 0, flags, 0, None)
        self._send_input(buffer, len(events))

    def send_text(self, text: str) -> None:
        """Unicode keystrokes: ``KEYEVENTF_UNICODE`` with the scan code as the char.

        This is what types into a control that has no ``ValuePattern`` — a
        RichEdit document, a custom canvas. It goes to the *focused* window, so
        the caller must have focused the target first.
        """

        INPUT, KEYBDINPUT = self._input_struct()
        events = []
        for char in text:
            events.append((ord(char), 0x0004))            # KEYEVENTF_UNICODE
            events.append((ord(char), 0x0004 | 0x0002))   # + KEYUP
        if not events:
            return
        buffer = (INPUT * len(events))()
        for index, (scan, flags) in enumerate(events):
            buffer[index].type = 1
            buffer[index].ki = KEYBDINPUT(0, scan, flags, 0, None)
        self._send_input(buffer, len(events))

    def click_point(self, x: int, y: int) -> None:
        """The fallback's fallback: a synthetic click at a screen point.

        Used only for a control that advertises no actionable pattern at all.
        It is the one path that can land on whatever moved under the cursor
        between the snapshot and the click, which is exactly why every other
        branch is tried first. Coordinates are normalised to the *virtual*
        screen (``SM_XVIRTUALSCREEN``..), not the primary monitor, or a click
        on a second display lands on the first one.
        """
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

        user32 = ctypes.windll.user32
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78) or 1
        height = user32.GetSystemMetrics(79) or 1
        nx = int((x - left) * 65535 / width)
        ny = int((y - top) * 65535 / height)
        # MOVE | ABSOLUTE | VIRTUALDESK, then down, then up.
        flags = (0x0001 | 0x8000 | 0x4000, 0x0002, 0x0004)
        buffer = (INPUT * 3)()
        for index, flag in enumerate(flags):
            buffer[index].type = 0  # INPUT_MOUSE
            buffer[index].mi = MOUSEINPUT(nx, ny, 0, flag, 0, None)
        self._send_input(buffer, 3)

    def focus(self, handle: Any) -> None:
        handle.SetFocus()


def vk_code(name: str) -> int:
    """Virtual-key code for one key name.

    A single letter or digit is not in :data:`VK_CODES` because its code *is*
    its uppercase ASCII value (``VK_A``..``VK_Z``, ``VK_0``..``VK_9``) — mapping
    it here rather than filling the table keeps the table the list of keys a
    decision may legitimately name.
    """
    key = str(name).casefold()
    if key in VK_CODES:
        return VK_CODES[key]
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    raise KeyError("unknown key %r" % name)


def parse_chord(keys: str) -> list:
    """``"ctrl+shift+s"`` -> the key names, validated.

    Raises ``KeyError`` naming the unknown part rather than silently dropping
    it, because a chord missing its modifier is a different command.
    """
    parts = [part.strip().casefold() for part in str(keys).split("+") if part.strip()]
    if not parts:
        raise KeyError("empty key chord")
    for part in parts:
        vk_code(part)  # raises KeyError naming the offending part
    return parts


def _method_for(op: str, el: Optional[UIElement]) -> str:
    """Which mechanism a live execution would use, decided from the patterns.

    Pure, and exported through :func:`execute`'s result so a dry run reports
    the same string a real one would — that is what makes a dry run worth
    running.
    """
    patterns = set(el.patterns) if el is not None else set()
    if op == "click":
        for pattern, method in (("invoke", "invoke"), ("toggle", "toggle"),
                                ("select", "selectionitem"), ("expand", "expand")):
            if pattern in patterns:
                return method
        return "click_point"
    if op == "type":
        return "setvalue" if "value" in patterns else "sendinput_text"
    if op == "select":
        if "select" in patterns:
            return "selectionitem"
        return "expand" if "expand" in patterns else ""
    if op in ("scroll_up", "scroll_down"):
        if el is not None and "scroll" in patterns:
            return "scroll"
        return "scrollitem" if el is not None else "sendinput_key"
    if op == "key":
        return "sendinput_key"
    if op == "wait":
        return "wait"
    return "noop"


def execute(action: Action, snapshot: Snapshot, *, backend: Any = None,
            dry_run: bool = False) -> ActResult:
    """Perform one action against the live handles in ``snapshot``.

    The handles are the COM pointers :func:`jevskill.cu.observe.snapshot` kept
    out of the serialised state; they are what makes an ``Invoke`` land on *this*
    control rather than on whatever is now at those coordinates.

    ``dry_run=True`` resolves the element and the mechanism and performs
    nothing. It is not a courtesy: it is how this module is exercised at all
    today, and how ``bench/cu_run.py`` can measure the loop without touching a
    desktop.
    """
    started = time.perf_counter()

    def done(ok: bool, method: str, error: str = "") -> ActResult:
        return ActResult(ok=ok, method=method, error=error,
                         elapsed_ms=(time.perf_counter() - started) * 1000.0,
                         dry_run=dry_run)

    op = action.op
    if op in ("done", "blocked"):
        return done(True, "noop", "")
    element = snapshot.by_id(action.target) if action.target else None
    method = _method_for(op, element)
    if op not in ("key", "wait") and action.target and element is None:
        return done(False, method, "no element %r in snapshot" % action.target)
    if op == "select" and not method:
        return done(False, "", "element %r exposes no selection pattern" % action.target)
    if op == "type" and action.text is None:
        return done(False, method, "type without text: code writes the text, not the model")
    if op == "key" and not action.key:
        return done(False, method, "key without a chord: code chooses the key")

    if dry_run:
        return done(True, "dry:" + method)

    backend = backend if backend is not None else UiaBackend()
    handle = snapshot.handle(action.target) if action.target else None
    if method not in ("sendinput_key", "wait", "noop") and handle is None:
        return done(False, method, "no live handle for %r" % action.target)

    try:
        if op == "wait":
            return done(True, "wait")
        if op == "click":
            if method == "invoke":
                backend.invoke(handle)
            elif method == "toggle":
                backend.toggle(handle)
            elif method == "selectionitem":
                backend.select(handle)
            elif method == "expand":
                backend.expand(handle)
            else:
                # No pattern at all. A pixel click is the last resort and it is
                # the only path that can land on the wrong control.
                backend.click_point(*element.center)
            return done(True, method)
        if op == "type":
            if method == "setvalue":
                backend.set_value(handle, action.text)
            else:
                backend.focus(handle)
                backend.send_text(action.text)
            return done(True, method)
        if op == "select":
            if method == "selectionitem":
                backend.select(handle)
            else:
                backend.expand(handle)
            return done(True, method)
        if op in ("scroll_up", "scroll_down"):
            direction = "up" if op == "scroll_up" else "down"
            if method == "scroll":
                backend.scroll(handle, direction, action.amount)
            elif method == "scrollitem":
                backend.scroll_into_view(handle)
            else:
                backend.send_keys("pageup" if direction == "up" else "pagedown")
            return done(True, method)
        if op == "key":
            backend.send_keys(action.key)
            return done(True, method)
    except Exception as exc:  # a dead node, a refused pattern, a COM error
        return done(False, method, "%s: %s" % (type(exc).__name__, exc))
    return done(False, method, "no execution path for op %r" % op)


def settle(observe: Callable[[], Snapshot], prev_hash: Optional[str], *,
           timeout_ms: float = SETTLE_TIMEOUT_MS, poll_ms: float = SETTLE_POLL_MS,
           key: Optional[Callable[[Snapshot], str]] = None,
           sleep: Callable[[float], None] = time.sleep) -> Tuple[Snapshot, bool, float]:
    """Poll the tree hash until it differs from ``prev_hash`` or time runs out.

    This is the code-side ``stuck`` detector, and it is the whole reason the
    bundle has no ``stuck`` question: asked, that question returned 0.31-0.60 on
    screens that had plainly changed (act.md §9, prompting.md §11). Hashing two
    trees answers it exactly, in microseconds.

    Returns ``(snapshot, changed, waited_ms)``. ``changed`` is ``False`` on
    timeout, and a ``False`` here is what feeds ``last_action.outcome =
    "unchanged"`` on the next step — a fact the model is *told*, never asked.

    A fixed ``sleep`` after each action is the anti-pattern this replaces: it is
    how a 350 ms step becomes a 1 s step. The first poll happens immediately, so
    an action whose effect is already visible costs one hash.
    """
    hash_of = key or (lambda snap: tree_hash(snap.elements))
    deadline = time.perf_counter() + max(0.0, timeout_ms) / 1000.0
    started = time.perf_counter()
    snapshot = observe()
    while True:
        current = hash_of(snapshot)
        if prev_hash is None or current != prev_hash:
            return snapshot, True, (time.perf_counter() - started) * 1000.0
        if time.perf_counter() >= deadline:
            return snapshot, False, (time.perf_counter() - started) * 1000.0
        sleep(max(0.0, poll_ms) / 1000.0)
        snapshot = observe()


def settle_cap_for(element: Optional[UIElement]) -> float:
    """act.md §4's cap for this control: 200 ms for a combobox, 50 ms otherwise.

    Used by callers that want the published cap rather than :func:`settle`'s
    more forgiving default; the difference is only paid when nothing changes.
    """
    if element is not None and element.role == "combobox":
        return SETTLE_CAP_COMBOBOX_MS
    return SETTLE_CAP_MS


__all__ = [
    "Action", "ActResult", "DESTRUCTIVE_NAMES", "SETTLE_CAP_COMBOBOX_MS",
    "SETTLE_CAP_MS", "SETTLE_POLL_MS", "SETTLE_TIMEOUT_MS", "UiaBackend",
    "VK_CODES", "execute", "is_destructive_name", "parse_chord", "risky_ids",
    "settle", "settle_cap_for", "vk_code",
]

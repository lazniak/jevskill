"""Perception and candidate reduction for a Windows computer-use loop.

The shape of one step, and which half of it lives here::

    observe.snapshot()            UIA tree of the active window       ~40-400 ms
    reduce.candidates()           deterministic filter to <= 60       <= 2 ms
    hashing.tree_hash()/diff()    did the screen change               < 1 ms
    --- everything above is code; everything below is a decision ---
    jev fan-out                   target / op / goal_reached          ~300 ms

The split is the point. Visible, enabled, on-screen, big enough, duplicated,
unchanged — these are facts, and code settles facts faster, cheaper and more
reliably than a model does. What is left for the model is the one thing it is
better at: which of these controls the *goal* wants.

``import jevskill.cu`` is free and works everywhere: the reduce and hashing
halves are pure Python and are tested on Linux CI. Only :func:`snapshot` needs
Windows and the ``cu`` extra, and it says so in the ``ImportError`` rather than
failing at import time with a stack trace about ``comtypes.gen``.
"""

from __future__ import annotations

from typing import Any

from .hashing import TreeDiff, diff, normalise, tree_hash
from .reduce import (candidates, dedupe, interactive, prioritise, region_state,
                     regions, visible_enabled)
from .types import (CONTROL_TYPES, INTERACTIVE_ROLES, PATTERN_KEYS,
                    REGION_ROLES, Region, Snapshot, UIElement)

__all__ = [
    "CONTROL_TYPES", "INTERACTIVE_ROLES", "PATTERN_KEYS", "REGION_ROLES",
    "Region", "Snapshot", "TreeDiff", "UIElement", "candidates", "dedupe",
    "diff", "foreground_hwnd", "interactive", "normalise", "prioritise",
    "region_state", "regions", "snapshot", "state_tokens", "to_state",
    "tree_hash", "visible_enabled",
]

_LAZY = {"snapshot", "to_state", "state_tokens", "foreground_hwnd"}


def __getattr__(name: str) -> Any:
    """Defer the Windows-only half until it is actually called (PEP 562).

    Importing :mod:`jevskill.cu.observe` is itself harmless — it only touches
    ctypes and comtypes inside functions — but going through ``__getattr__``
    keeps that a guarantee of this module rather than a property of another
    one that a later edit could quietly break.
    """
    if name in _LAZY:
        from . import observe

        return getattr(observe, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(__all__)

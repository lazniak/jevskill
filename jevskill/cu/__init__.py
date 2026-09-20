"""Perception and candidate reduction for a Windows computer-use loop.

The shape of one step, and which half of it lives here::

    observe.snapshot()            UIA tree of the active window       ~40-400 ms
    reduce.candidates()           deterministic filter to <= 60       0.05-3 ms (34-2,000 nodes)
    hashing.tree_hash()/diff()    did the screen change               0.1-9 ms (34-2,000 nodes)
    macros.MacroCache.lookup()    has this screen been solved before  < 1 ms
    --- everything above is code; everything below is a decision ---
    decide.decide()               target / op / goal_reached          ~300 ms
    --- and everything below is code again ---
    decide.validate()             does the answer survive the checks  < 1 ms
    act.execute()                 UIA Invoke / SetValue / SendInput   ~1-20 ms
    act.settle()                  poll the hash until the screen moves  <= cap
    loop.run()                    all of the above, with the budgets

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

from .act import (DESTRUCTIVE_NAMES, Action, ActResult, execute,
                  is_destructive_name, settle)
from .contract import RunOptions, RunResult, StepRecord
# ``decide_step``, not ``decide``: ``jevskill.cu.decide`` is a *module*, and a
# from-import of a function with the same name would overwrite the module
# attribute on this package — so ``jevskill.cu.decide.build_bundle`` would
# resolve to an attribute of a function. ``run_loop`` is the same rule for
# ``jevskill.cu.loop``.
from .decide import (OPS, THRESHOLDS, Decision, Verdict, build_bundle,
                     build_state, decide as decide_step, decide_cascade,
                     validate)
from .hashing import TreeDiff, diff, normalise, tree_hash
from .loop import run as run_loop
from .macros import MacroCache
from .reduce import (candidates, dedupe, interactive, prioritise, region_state,
                     regions, visible_enabled)
from .types import (CONTROL_TYPES, INTERACTIVE_ROLES, PATTERN_KEYS,
                    REGION_ROLES, Region, Snapshot, UIElement)

__all__ = [
    "BEAM_MARGIN", "CONTROL_TYPES", "DESTRUCTIVE_NAMES", "INTERACTIVE_ROLES",
    "OPS", "PATTERN_KEYS", "REGION_ROLES", "THRESHOLDS", "Action", "ActResult",
    "Consistency", "ConsistencyGate", "Decision", "MacroCache", "Region",
    "RunOptions", "RunResult", "Snapshot", "Speculation", "Speculator",
    "StepRecord", "TreeDiff", "UIElement", "Verdict", "build_bundle",
    "build_state", "candidates", "decide_beam", "decide_cascade", "decide_step",
    "dedupe", "diff", "execute", "foreground_hwnd", "interactive",
    "is_destructive_name", "normalise", "prioritise", "region_state", "regions",
    "run_loop", "settle", "snapshot", "state_tokens", "to_state", "top_regions",
    "tree_hash", "validate", "visible_enabled",
]

_LAZY = {"snapshot": "observe", "to_state": "observe",
         "state_tokens": "observe", "foreground_hwnd": "observe",
         # Phase 5 (plan items 5.1-5.3). All three ship off by default and
         # nothing in the eager graph imports them — `decide_cascade` reaches
         # for `beam` inside the `beam_k > 1` branch, and the other two are
         # injected into `run()` or not at all. Importing them here anyway cost
         # 7 ms of the package's ~106 ms import for a feature most callers never
         # switch on, which is the same trade `observe` is deferred for.
         "Speculation": "speculate", "Speculator": "speculate",
         "Consistency": "consistency", "ConsistencyGate": "consistency",
         "BEAM_MARGIN": "beam", "decide_beam": "beam", "top_regions": "beam"}


def __getattr__(name: str) -> Any:
    """Defer the Windows-only half and the Phase 5 hooks (PEP 562).

    Importing :mod:`jevskill.cu.observe` is itself harmless — it only touches
    ctypes and comtypes inside functions — but going through ``__getattr__``
    keeps that a guarantee of this module rather than a property of another
    one that a later edit could quietly break.

    ``decide``, ``act``, ``loop`` and ``macros`` are imported eagerly above
    because they are pure Python: ``act`` keeps every ``ctypes`` and
    ``comtypes`` reference inside a method body, and ``loop`` imports
    ``observe`` only when its ``observe`` argument is left unset. Importing
    this package still costs nothing on a machine without the extra, and
    ``tests/test_cu_observe.py::TestImportContract`` proves it in a subprocess.
    """
    if name in _LAZY:
        from importlib import import_module

        return getattr(import_module("." + _LAZY[name], __name__), name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(__all__)

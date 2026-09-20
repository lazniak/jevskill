"""JEV decision-model client for AI coding harnesses.

Sends *state* + typed *questions* to the OpenRouter Decisions API and returns
typed decisions (noul / choice / score) with probability distributions.

Design goals, in priority order:

1. **Zero-config on a dev machine.** Reads the API key from
   ``JEVSKILL_API_KEY``, ``OPENROUTER_API_KEY``, ``OPEN_ROUTER_API_KEY``,
   ``JEVUSE_API_KEY``, then the Windows user registry (``HKCU\\Environment``),
   then ``~/.jevskill/config.json``. Never asks the caller to pass a secret.
2. **Cheap enough to be a habit.** One connection, precompiled question bytes,
   optional orjson. A decision costs ~$0.00002, so the skill should be used
   liberally rather than hoarded.
3. **Measurable.** Every call reports per-stage timing (serialize / http /
   parse) and token cost, which :mod:`jevskill.stats` accumulates into a real
   effectiveness ledger instead of a guess.

Officially documented at <https://docs.typesafe.ai>. This module deliberately
depends only on the Python standard library; ``httpx`` and ``orjson`` are used
automatically when importable.

**What this package exports, and why it has to.** ``SKILL.md`` §4 hands an agent
runnable Python — ``jev.decide(state, {...: noul(...)})``, ``next_round(...)``,
``combine_weighted(...)`` — but until 0.11.1 the package exported ``__version__``
and nothing else, so every one of those snippets died on ``ImportError`` at the
first line. The promise was published; the import path is the promise being kept.

The split between eager and lazy names is measured, not stylistic. Everything
listed eagerly is standard-library only (~70 ms, most of it ``dataclasses`` and
``pathlib``). :mod:`jevskill.client` imports ``httpx`` at module level, which
costs ~525 ms on this machine because ``httpx._main`` drags in ``click`` and
``rich`` — so ``JevClient``, ``Decisions`` and ``Answer`` load on first attribute
access instead (:pep:`562`). ``from jevskill import JevClient`` still works and
still pays that cost; ``from jevskill import noul`` no longer does, and neither
does reading ``__version__``.
"""

from importlib import import_module as _import_module
from typing import Any as _Any

from .config import Config
from .errors import JevApiError, JevConfigError, JevError, JevQuestionError
from .orchestrate import Round, combine_weighted, next_round, plan_for, should_use_jev
from .primitives import Q, choice, noul, score

__version__ = "0.11.0"

#: Names served from :mod:`jevskill.client`, which is the expensive import.
_LAZY = {"Answer": "client", "Decisions": "client", "JevClient": "client"}

__all__ = [
    "__version__",
    # client (lazy)
    "JevClient",
    "Decisions",
    "Answer",
    "Config",
    # questions
    "noul",
    "choice",
    "score",
    "Q",
    # orchestration
    "next_round",
    "Round",
    "combine_weighted",
    "plan_for",
    "should_use_jev",
    # errors
    "JevError",
    "JevApiError",
    "JevConfigError",
    "JevQuestionError",
]


def __getattr__(name: str) -> _Any:
    """Resolve the client names on first use, then cache them in ``globals()``.

    ``__getattr__`` is only consulted on a miss, so writing the resolved object
    back means the ``httpx`` import is paid once per process and never again.
    """
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(__all__)
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
"""

__version__ = "0.5.0"
__all__ = ["__version__"]
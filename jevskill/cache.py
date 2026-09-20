"""A disk cache for decisions that were already paid for.

The cheapest call is the one you do not make. Re-asking the same question about
the same state — re-running yesterday's triage, retrying a batch after a failure,
two agents looking at the same diff — costs full input tokens and returns what the
model already said.

**It is off by default, and that is deliberate.** A stale decision is worse than a
paid one when the state is moving, so the caller opts in with `--cache` and chooses
the window with `--cache-ttl`. The key is a hash of the *exact* request body, so a
hit means the model would have been asked byte-identical input — not merely
something similar.

Two rules keep a hit honest:

* a cached response reports ``cached=True`` and its age, never a fresh timestamp;
* it carries **zero tokens and zero cost** in the ledger, because the model did no
  work. Reporting a cache hit as a token saving the model earned is exactly the
  kind of flattering arithmetic this project exists to avoid.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .config import DATACLASS_SLOTS
from dataclasses import dataclass
from pathlib import Path

#: How long a cached decision stays usable. Long enough to cover a retry or a
#: re-run in the same working session, short enough that yesterday's answer does
#: not silently stand in for today's state.
DEFAULT_TTL_S = 900.0

#: Bumped when the on-disk shape changes, so old entries are ignored rather than
#: misread after an upgrade.
SCHEMA = 1


@dataclass(**DATACLASS_SLOTS)
class CacheEntry:
    """One stored response, with what it takes to report the hit honestly."""

    payload: dict
    age_s: float


def cache_dir(root: Path | str | None = None) -> Path:
    """Where entries live — the same precedence as the ledger, so the cache and
    the ledger for one project cannot end up in two different places.

    1. an explicit ``root`` — the caller said exactly where, so we obey;
    2. ``JEVSKILL_LEDGER_DIR`` — a *default* for callers that did not say;
    3. ``<cwd>/.jevskill/cache`` — so the cache travels with the repo.
    """
    if root is not None:
        return Path(root) / ".jevskill" / "cache"
    override = os.environ.get("JEVSKILL_LEDGER_DIR")
    if override:
        return Path(override) / "cache"
    return Path.cwd() / ".jevskill" / "cache"


def request_key(body: bytes) -> str:
    """The cache key: the request body *is* the question, so hash it.

    Whole-body hashing rather than (state, questions) means the key cannot drift
    from what is actually sent — a new field in the body changes the key by
    construction instead of silently reusing an entry.
    """
    return hashlib.sha256(body).hexdigest()


class DecisionCache:
    """A TTL cache on disk, keyed by request body."""

    def __init__(self, root: Path | str | None = None, ttl_s: float = DEFAULT_TTL_S):
        self.dir = cache_dir(root)
        self.ttl_s = float(ttl_s)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        # Two-level fan-out keeps any single directory small on a big project.
        return self.dir / key[:2] / f"{key}.json"

    def lookup(self, body: bytes) -> CacheEntry | None:
        """Return the stored response for this exact body, or None."""
        key = request_key(body)
        path = self._path(key)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        if stored.get("schema") != SCHEMA or stored.get("key") != key:
            self.misses += 1
            return None
        age = time.time() - float(stored.get("created", 0.0))
        if age < 0 or age > self.ttl_s:
            # Expired entries are removed so the store reflects what is usable.
            try:
                path.unlink()
            except OSError:
                pass
            self.misses += 1
            return None
        payload = stored.get("payload")
        if not isinstance(payload, dict) or not payload.get("answers"):
            self.misses += 1
            return None
        self.hits += 1
        return CacheEntry(payload=payload, age_s=round(age, 3))

    def store(self, body: bytes, payload: dict) -> None:
        """Persist a response. Failures are swallowed: a cache that cannot write
        must not fail the call that already succeeded."""
        key = request_key(body)
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "key": key,
                        "created": time.time(),
                        "payload": payload,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def clear(self) -> int:
        """Delete every entry. Returns how many files were removed."""
        removed = 0
        for path in self.dir.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def size(self) -> int:
        return sum(1 for _ in self.dir.rglob("*.json"))
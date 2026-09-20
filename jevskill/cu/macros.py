"""Replay a step that already worked: a decision cache keyed on the *screen*.

A computer-use run is repetitive in a way a coding session is not. The same
dialog appears every time the same task is performed, the same "Save" button in
the same place, and paying ~300 ms and ~6,000 input tokens to be told "e11,
click" for the fortieth time is the cheapest waste in the loop to remove.

**Why this is not :mod:`jevskill.cache`.** That cache is keyed on the exact
request body, and deliberately: "a hit means the model would have been asked
byte-identical input". For a screen that is the wrong key. The body carries the
goal's prose, the coarse grid cell of every candidate, the focus flag, and 60
generated criteria strings — so a window moved by one pixel into the next grid
cell, or focus landing on a different control, changes the bytes while the
*decision* is identical. A macro is keyed on the reduced tree's normalised hash
(:func:`jevskill.cu.hashing.tree_hash`, which already drops ``bbox``, ``focused``
and volatile ``value``s), so it hits on the screen rather than on the sentence.
The two also store different things: ``DecisionCache`` stores the model's whole
answer payload and expires on a TTL, a macro stores the *conclusion*
``(target, op)`` and is invalidated by **behaviour** — an entry whose replay did
not change the tree is wrong regardless of its age. Both hash with
:func:`jevskill.cache.request_key`, which is the part that genuinely is the same
job.

**Why the identity is stored next to the id.** Element ids are positional
(``e11`` is "the twelfth node in this walk"), and the tree hash deliberately does
not cover them — a node appearing above the candidate would otherwise make an
unchanged screen look different. So a hit re-resolves the stored
``(role, name, automation_id, class_name)`` against the current candidates and
returns *that* id. If the identity is no longer present the entry does not hit:
better a paid decision than a click on whatever inherited the id.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from ..cache import request_key
from .hashing import tree_hash
from .types import UIElement, as_elements

#: Bumped when the on-disk shape changes, so old entries are ignored rather
#: than misread after an upgrade — the same rule :mod:`jevskill.cache` uses.
SCHEMA = 1

#: Where macros live when the caller does not say. Not the per-project ledger
#: directory: a macro is a fact about an *application's* UI, not about a repo,
#: and the same "Save changes?" dialog is worth replaying from any project.
DEFAULT_PATH = Path.home() / ".jevskill" / "cu_macros.json"


def goal_fingerprint(goal: str) -> str:
    """Case- and whitespace-insensitive identity of a goal.

    Two callers that phrase the same task with different spacing or casing are
    on the same screen doing the same thing. Anything stronger (stemming,
    embeddings) would make a hit a judgement call, and a wrong macro clicks.
    """
    return " ".join(str(goal or "").split()).casefold()


@dataclass
class MacroEntry:
    """One replayable conclusion."""

    target: str
    op: str
    #: ``(role, name, automation_id, class_name)`` — what makes the stored
    #: target "the same control" in a later snapshot.
    identity: Tuple[str, str, str, str] = ("", "", "", "")
    goal: str = ""
    outcome: str = ""
    created: float = 0.0
    last_used: float = 0.0
    hits: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["identity"] = list(self.identity)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MacroEntry":
        identity = tuple(data.get("identity") or ("", "", "", ""))
        while len(identity) < 4:
            identity = identity + ("",)
        return cls(target=str(data.get("target", "")), op=str(data.get("op", "")),
                   identity=identity[:4],  # type: ignore[arg-type]
                   goal=str(data.get("goal", "")), outcome=str(data.get("outcome", "")),
                   created=float(data.get("created", 0.0)),
                   last_used=float(data.get("last_used", 0.0)),
                   hits=int(data.get("hits", 0)))


def _identity(el: UIElement) -> Tuple[str, str, str, str]:
    return (el.role, el.name, el.automation_id, el.class_name)


class MacroCache:
    """``(goal, screen, last outcome)`` -> ``(target, op)``, on disk.

    ``outcome`` is part of the key because the same screen means different
    things depending on how it was reached: a dialog that is up because the
    previous click opened it (``new_window``) wants a different next step from
    the same dialog after an action that did nothing (``unchanged``).
    """

    def __init__(self, path: Optional[Any] = None, *, autosave: bool = True,
                 max_entries: int = 2000) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.autosave = bool(autosave)
        self.max_entries = int(max_entries)
        self.entries: Dict[str, MacroEntry] = {}
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.stores = 0
        self._lock = threading.Lock()
        self.load()

    # ---- keying ----------------------------------------------------------
    def key(self, goal: str, candidates: Sequence[Any],
            outcome: Optional[str] = None) -> str:
        """The macro key. Same hash function as the request cache, different input."""
        parts = "\x1f".join((goal_fingerprint(goal),
                             tree_hash(as_elements(candidates)),
                             str(outcome or "")))
        return request_key(parts.encode("utf-8"))

    # ---- the hot path ----------------------------------------------------
    def lookup(self, goal: str, candidates: Sequence[Any],
               outcome: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """``(target_id, op)`` for this screen, or ``None``.

        The returned id is re-resolved against *these* candidates, so it is
        valid for the snapshot the caller is holding, not for the one the macro
        was recorded from.
        """
        els = as_elements(candidates)
        entry_key = self.key(goal, els, outcome)
        with self._lock:
            entry = self.entries.get(entry_key)
        if entry is None:
            self.misses += 1
            return None
        target = self._resolve(entry, els)
        if target is None:
            # The hash matched but the control did not: the walk numbered the
            # tree differently. Not a hit, and not an error either.
            self.misses += 1
            return None
        with self._lock:
            entry.hits += 1
            entry.last_used = time.time()
        self.hits += 1
        return target, entry.op

    def _resolve(self, entry: MacroEntry, els: Sequence[UIElement]) -> Optional[str]:
        if entry.target == "none":
            # A viewport move or a keyboard command: worth replaying, and there
            # is no element to re-resolve. The caller turns "none" back into a
            # targetless action.
            return "none"
        for el in els:
            if el.id == entry.target and _identity(el) == entry.identity:
                return el.id
        for el in els:
            if _identity(el) == entry.identity:
                return el.id
        return None

    def store(self, goal: str, candidates: Sequence[Any], outcome: Optional[str],
              target: str, op: str) -> str:
        """Remember a conclusion that worked. Returns the key it was stored under.

        Only call this after the action *changed the tree*: an entry recorded
        from a step that did nothing is a macro that will do nothing, forever,
        with a cache hit's confidence.
        """
        els = as_elements(candidates)
        entry_key = self.key(goal, els, outcome)
        element = next((el for el in els if el.id == target), None)
        now = time.time()
        entry = MacroEntry(
            target=target, op=op,
            identity=_identity(element) if element is not None else ("", "", "", ""),
            goal=goal_fingerprint(goal), outcome=str(outcome or ""),
            created=now, last_used=now, hits=0)
        with self._lock:
            self.entries[entry_key] = entry
            if len(self.entries) > self.max_entries:
                self._evict_locked()
        self.stores += 1
        if self.autosave:
            self.save()
        return entry_key

    def invalidate(self, goal: str, candidates: Sequence[Any],
                   outcome: Optional[str] = None) -> bool:
        """Drop the entry for this screen. Returns whether one was there.

        act.md §6: "Invalidate an entry whose replay produced ``outcome:
        unchanged``." A macro that no longer moves the screen is not stale in a
        TTL sense — it is wrong, and one more step is one more wasted action.
        """
        entry_key = self.key(goal, as_elements(candidates), outcome)
        return self.invalidate_key(entry_key)

    def invalidate_key(self, entry_key: str) -> bool:
        with self._lock:
            existed = self.entries.pop(entry_key, None) is not None
        if existed:
            self.invalidations += 1
            if self.autosave:
                self.save()
        return existed

    def _evict_locked(self) -> None:
        """Least recently used first. Called with the lock held."""
        ordered = sorted(self.entries.items(), key=lambda kv: kv[1].last_used)
        for entry_key, _ in ordered[: len(self.entries) - self.max_entries]:
            self.entries.pop(entry_key, None)

    # ---- persistence -----------------------------------------------------
    def load(self) -> int:
        """Read the store. A damaged or newer file is ignored, never raised.

        A cache that cannot be read must not stop the loop it was meant to speed
        up — the same rule :mod:`jevskill.cache` follows for writes.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if data.get("schema") != SCHEMA:
            return 0
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return 0
        loaded = {k: MacroEntry.from_dict(v) for k, v in entries.items()
                  if isinstance(v, dict)}
        with self._lock:
            self.entries = loaded
        return len(loaded)

    def save(self) -> bool:
        """Write the store. Failures are swallowed and reported as ``False``."""
        with self._lock:
            payload = {"schema": SCHEMA, "saved": time.time(),
                       "entries": {k: v.to_dict() for k, v in self.entries.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # ---- reporting -------------------------------------------------------
    @property
    def total_lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Hits over lookups, 0.0 when nothing was looked up.

        Reported rather than inferred from the token saving: a macro hit costs
        zero tokens, so counting it as a *saving* the model earned is the
        flattering arithmetic this project exists to avoid.
        """
        return (self.hits / self.total_lookups) if self.total_lookups else 0.0

    def stats(self) -> Dict[str, Any]:
        return {"entries": len(self.entries), "hits": self.hits,
                "misses": self.misses, "stores": self.stores,
                "invalidations": self.invalidations,
                "hit_rate": round(self.hit_rate, 4)}

    def clear(self) -> int:
        with self._lock:
            count = len(self.entries)
            self.entries = {}
        if self.autosave:
            self.save()
        return count

    def __len__(self) -> int:
        return len(self.entries)


__all__ = ["DEFAULT_PATH", "MacroCache", "MacroEntry", "SCHEMA",
           "goal_fingerprint"]

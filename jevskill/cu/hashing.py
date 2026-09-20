"""Did the screen change? Code answers this, not the model.

The measurement that motivated this module: a ``stuck`` noul ("does the current
state show that the last action had no effect?") came back at 0.42-0.60 across
runs on both providers — a coin flip dressed as a probability. That is not a
provider problem and not a prompting problem: the model sees one state and is
asked about two. A hash of the previous tree and a hash of the current one
answers it exactly, in microseconds, for free.

So: never ask the model whether the screen changed. Ask it what to do *given*
that it did or did not.

What is deliberately ignored by default, and exactly how far that goes:

* ``bbox`` — a window moved by one pixel, or a scroll of two lines, is not a
  state change for the purpose of "did my click do anything".
* ``focused`` — focus flickers during a click and would make every step differ.
* ``value`` — a progress percentage or a character counter would otherwise
  report a change on every single step.

**The hole this leaves, named honestly.** Ignoring ``value`` also means a
Rename dialog showing ``notes.txt`` and the same dialog showing
``taxes-2026.txt`` hash *identically*. That is measured, not hypothetical. A
caller waiting for content — a file name appearing in an edit box, a search box
filling in — must pass ``ignore=()``, which hashes every field in
:data:`HASHED_FIELDS` including ``bbox`` and ``focused``, or
``ignore=("bbox", "focused")`` to keep ``value`` while still tolerating a moved
window. The default answers one question only: "did the UI move on".

**"A ticking clock is not a change" was false as written.** It holds only when
the clock lives in ``value``. UIA puts clocks, line/column readouts and status
text in ``Name``, which *is* hashed — the committed Notepad fixture has 7 such
``text``/``statusbar`` nodes and Calculator has 4. Genuinely volatile chrome is
handled by :data:`VOLATILE_NAME_ROLES` and the opt-in ``volatile_text=True``,
which blanks ``Name`` for those roles only. Measured on the fixtures: with it
on, a status line going "Ln 1, Col 1" -> "Ln 9, Col 42" stops registering, and
a dialog opening still registers, because the dialog is a *new node* rather
than a renamed one. It is off by default because "the status bar now says
Saved" is often the very thing a loop is waiting for.

**Selection and toggle state are hashed**, and must be: ``patterns`` carries
*availability* (this control can be selected), never state (this control *is*
selected). Without them an arrow-key move down a list hashed identically to the
list before it — also measured — and a loop that pressed Down and then asked
"did anything happen" was told no.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from .types import UIElement, as_elements

#: Fields a tree hash covers, in a fixed order. Order is part of the hash, so
#: adding a field here changes every stored hash — which is why the default
#: ignore set exists instead of a second field list.
HASHED_FIELDS: Tuple[str, ...] = (
    "role", "name", "automation_id", "class_name", "enabled", "offscreen",
    "depth", "patterns", "value", "bbox", "focused", "selected", "toggled",
)

#: Volatile fields: present on an element but excluded from the structural hash
#: by default. ``diff`` reports them as *changed* rather than added/removed.
#: ``selected`` and ``toggled`` are deliberately **not** here: they are state,
#: and hiding state is what made an arrow-key move invisible.
DEFAULT_IGNORE: Tuple[str, ...] = ("bbox", "focused", "value")

#: Roles whose ``Name`` is a readout rather than a label — the clocks, the
#: "Ln 9, Col 42", the "3 of 47 items". Only consulted when a caller asks for
#: ``volatile_text=True``; see the module docstring for why that is opt-in.
VOLATILE_NAME_ROLES = frozenset({"text", "statusbar"})

_SEP = "\x1f"
_ROW = "\x1e"


def _field_text(el: UIElement, name: str, volatile_text: bool = False) -> str:
    if name == "patterns":
        return ",".join(sorted(el.patterns))
    if name == "bbox":
        return ",".join(str(int(v)) for v in el.bbox)
    if name == "name" and volatile_text and el.role in VOLATILE_NAME_ROLES:
        return ""
    value = getattr(el, name)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def normalise(elements: Sequence[Any],
              ignore: Sequence[str] = DEFAULT_IGNORE,
              *, volatile_text: bool = False) -> List[str]:
    """One stable line per element — the thing that is actually hashed.

    Exposed because a failing hash comparison is otherwise undebuggable: diff
    two ``normalise()`` outputs and the offending element is visible.
    """
    skip = set(ignore)
    fields = [f for f in HASHED_FIELDS if f not in skip]
    return [_SEP.join(_field_text(el, f, volatile_text) for f in fields)
            for el in as_elements(elements)]


def tree_hash(elements: Sequence[Any], *,
              ignore: Sequence[str] = DEFAULT_IGNORE,
              volatile_text: bool = False) -> str:
    """A stable digest of a reduced or full element list.

    blake2b truncated to 16 bytes: this is a cache key and a change detector,
    not a security boundary, and a short hex string keeps step logs readable.
    Element *ids* are not hashed — they are positional, so hashing them would
    make an unchanged screen look different after one node appears above it.

    ``volatile_text=True`` blanks ``Name`` for :data:`VOLATILE_NAME_ROLES`. Use
    it for an app whose status line ticks; do not use it while waiting for a
    status line to say something.

    Cost, measured by ``bench/cu_observe_bench.py --from-fixtures``: 0.085 ms on
    the 34-node Notepad fixture, 1.127 ms at 500 nodes, 4.638 ms at 2,000. The
    "< 1 ms" this used to be described with holds to roughly 500 nodes.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(_ROW.join(
        normalise(elements, ignore, volatile_text=volatile_text)).encode("utf-8"))
    return digest.hexdigest()


def _identity(el: UIElement,
              volatile_text: bool = False) -> Tuple[str, str, str, str]:
    """What makes an element "the same control" across two snapshots.

    Not the id (positional), not the bbox (moves), not the value (changes).
    Name plus automation id plus class is what survives a repaint.
    """
    name = "" if volatile_text and el.role in VOLATILE_NAME_ROLES else el.name
    return (el.role, name, el.automation_id, el.class_name)


@dataclass
class TreeDiff:
    """What changed between two snapshots.

    ``added``/``removed``/``changed`` are element ids — ``added`` and
    ``changed`` from the *current* list, ``removed`` from the *previous* one,
    because an id only means anything inside the snapshot it came from.

    The brief asked for "a boolean ``changed``"; ``changed`` is already the id
    list, so the boolean is :attr:`changed_any` (and ``bool(diff)``). A dict
    cannot hold both under one key and silently picking one would have made
    ``if diff["changed"]`` mean two different things depending on the caller.
    """

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)

    @property
    def changed_any(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def __bool__(self) -> bool:
        return self.changed_any

    def to_dict(self) -> Dict[str, Any]:
        return {"added": list(self.added), "removed": list(self.removed),
                "changed": list(self.changed), "changed_any": self.changed_any}


def diff(prev: Sequence[Any], cur: Sequence[Any], *,
         ignore: Sequence[str] = DEFAULT_IGNORE,
         volatile_text: bool = False) -> TreeDiff:
    """Structural difference, matched on identity rather than position.

    ``ignore`` here means the opposite of what it means in :func:`tree_hash`,
    and that is intentional: those fields are exactly the ones whose change
    makes an element *changed* instead of leaving it untouched. One constant,
    two uses, no way for them to drift apart.

    Each identity bucket is a :class:`collections.deque` consumed from the
    left, so matching is linear. It used to re-scan every bucket from the start
    for the first unmatched member, which is O(k^2) in the size of the bucket —
    and a bucket is every control sharing ``(role, name, automation_id,
    class_name)``, which on a virtualised list is all of them. Measured on
    identical controls, before: 12.8 ms at 500, 45.9 at 1,000, 176 at 2,000,
    747 at 4,000 — quadratic, inside a loop whose whole step budget is ~400 ms.
    After: 1.98, 3.91, 8.26, 16.7 — linear, and 4,000 identical controls now
    cost less than 500 used to.
    """
    prev_els, cur_els = as_elements(prev), as_elements(cur)
    watched = [f for f in ignore if f in HASHED_FIELDS]

    prev_by_key: Dict[Tuple[str, str, str, str], Deque[UIElement]] = {}
    for el in prev_els:
        prev_by_key.setdefault(_identity(el, volatile_text),
                               deque()).append(el)
    matched: Dict[int, bool] = {}

    result = TreeDiff()
    for el in cur_els:
        bucket = prev_by_key.get(_identity(el, volatile_text))
        pick: Optional[UIElement] = None
        if bucket:
            pick = bucket.popleft()
            matched[id(pick)] = True
        if pick is None:
            result.added.append(el.id)
            continue
        if any(_field_text(pick, f, volatile_text)
               != _field_text(el, f, volatile_text) for f in watched):
            result.changed.append(el.id)
    for el in prev_els:
        if id(el) not in matched:
            result.removed.append(el.id)
    return result

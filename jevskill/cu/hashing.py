"""Did the screen change? Code answers this, not the model.

The measurement that motivated this module: a ``stuck`` noul ("does the current
state show that the last action had no effect?") came back at 0.42-0.60 across
runs on both providers — a coin flip dressed as a probability. That is not a
provider problem and not a prompting problem: the model sees one state and is
asked about two. A hash of the previous tree and a hash of the current one
answers it exactly, in microseconds, for free.

So: never ask the model whether the screen changed. Ask it what to do *given*
that it did or did not.

What is deliberately ignored by default:

* ``bbox`` — a window moved by one pixel, or a scroll of two lines, is not a
  state change for the purpose of "did my click do anything".
* ``focused`` — focus flickers during a click and would make every step differ.
* ``value`` — a clock, a progress percentage or a character counter would
  otherwise report a change on every single step.

Pass ``ignore=()`` when the *content* is the thing you are waiting for (a file
name appearing in an edit box), and keep the default when the question is
"did the UI move on".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from .types import UIElement, as_elements

#: Fields a tree hash covers, in a fixed order. Order is part of the hash, so
#: adding a field here changes every stored hash — which is why the default
#: ignore set exists instead of a second field list.
HASHED_FIELDS: Tuple[str, ...] = (
    "role", "name", "automation_id", "class_name", "enabled", "offscreen",
    "depth", "patterns", "value", "bbox", "focused",
)

#: Volatile fields: present on an element but excluded from the structural hash
#: by default. ``diff`` reports them as *changed* rather than added/removed.
DEFAULT_IGNORE: Tuple[str, ...] = ("bbox", "focused", "value")

_SEP = "\x1f"
_ROW = "\x1e"


def _field_text(el: UIElement, name: str) -> str:
    if name == "patterns":
        return ",".join(sorted(el.patterns))
    if name == "bbox":
        return ",".join(str(int(v)) for v in el.bbox)
    value = getattr(el, name)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def normalise(elements: Sequence[Any],
              ignore: Sequence[str] = DEFAULT_IGNORE) -> List[str]:
    """One stable line per element — the thing that is actually hashed.

    Exposed because a failing hash comparison is otherwise undebuggable: diff
    two ``normalise()`` outputs and the offending element is visible.
    """
    skip = set(ignore)
    fields = [f for f in HASHED_FIELDS if f not in skip]
    return [_SEP.join(_field_text(el, f) for f in fields)
            for el in as_elements(elements)]


def tree_hash(elements: Sequence[Any],
              *, ignore: Sequence[str] = DEFAULT_IGNORE) -> str:
    """A stable digest of a reduced or full element list.

    blake2b truncated to 16 bytes: this is a cache key and a change detector,
    not a security boundary, and a short hex string keeps step logs readable.
    Element *ids* are not hashed — they are positional, so hashing them would
    make an unchanged screen look different after one node appears above it.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(_ROW.join(normalise(elements, ignore)).encode("utf-8"))
    return digest.hexdigest()


def _identity(el: UIElement) -> Tuple[str, str, str, str]:
    """What makes an element "the same control" across two snapshots.

    Not the id (positional), not the bbox (moves), not the value (changes).
    Name plus automation id plus class is what survives a repaint.
    """
    return (el.role, el.name, el.automation_id, el.class_name)


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


def diff(prev: Sequence[Any], cur: Sequence[Any],
         *, ignore: Sequence[str] = DEFAULT_IGNORE) -> TreeDiff:
    """Structural difference, matched on identity rather than position.

    ``ignore`` here means the opposite of what it means in :func:`tree_hash`,
    and that is intentional: those fields are exactly the ones whose change
    makes an element *changed* instead of leaving it untouched. One constant,
    two uses, no way for them to drift apart.
    """
    prev_els, cur_els = as_elements(prev), as_elements(cur)
    watched = [f for f in ignore if f in HASHED_FIELDS]

    prev_by_key: Dict[Tuple[str, str, str, str], List[UIElement]] = {}
    for el in prev_els:
        prev_by_key.setdefault(_identity(el), []).append(el)
    matched: Dict[int, bool] = {}

    result = TreeDiff()
    for el in cur_els:
        bucket = prev_by_key.get(_identity(el))
        pick = None
        if bucket:
            for candidate in bucket:
                if id(candidate) not in matched:
                    pick = candidate
                    matched[id(candidate)] = True
                    break
        if pick is None:
            result.added.append(el.id)
            continue
        if any(_field_text(pick, f) != _field_text(el, f) for f in watched):
            result.changed.append(el.id)
    for el in prev_els:
        if id(el) not in matched:
            result.removed.append(el.id)
    return result

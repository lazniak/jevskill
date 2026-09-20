"""Turn a window's accessibility tree into the few controls worth deciding over.

This is the cheap half of a computer-use step and it must stay cheap: the step
budget is ~350-500 ms and ~300 ms of it is the network. Everything here is pure
Python over dataclasses — no COM, no I/O, no globals — so it is unit-testable
offline and measurable. ``bench/cu_observe_bench.py --from-fixtures`` writes the
numbers to ``bench/cu_observe_results.json``: ``candidates()`` costs 0.047 ms on
the 34-node Notepad fixture, 0.095 on Calculator, 0.708 ms at 500 nodes and
3.041 ms at 2,000. The "<= 2 ms on a 500-node tree" the first draft of this
docstring claimed is true at 500 and false by 2,000, so the honest statement is:
the filter is cheap enough to ignore below 500 nodes, and above that the *cap* —
not the filter — is what keeps a step inside its budget.

Why reduce at all, rather than send the tree:

* A 2,000-node WinUI tree is ~80k tokens of mostly layout panes (measured on
  ``synthetic_2000``: 79,672 tokens whole, 2,488 reduced). Reduce for **latency
  and tokens**, not for accuracy: ``bench/cu_bench.py`` measured ``target`` at
  0.97-0.99 with 241 options, so the model does not fall over at 60 — but
  latency climbs +125 ms (vendor) / +205 ms (OpenRouter) by 240 elements, and
  60 candidates is where a state still fits the 3,500-token budget.
* Every filter here is a *deterministic* fact — offscreen, disabled, 1x1,
  duplicate. Asking a model to re-derive them wastes the one thing it is good
  at. Code decides what is possible; the model decides what is wanted.

Order matters and is not arbitrary:

``visible_enabled`` -> ``interactive`` -> ``dedupe`` -> ``prioritise`` -> cap

Filtering before deduping means the duplicate that survives is a *reachable*
one; deduping before prioritising means the sort runs over the short list. The
one thing that must *not* follow that order is the dialog prior: see
:func:`prioritise`.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .types import (ACTIONABLE_PATTERNS, INTERACTIVE_ROLES, MIN_SIDE_PX,
                    REGION_ROLES, Region, UIElement, as_elements)

#: Two controls whose tops differ by less than this belong to the same visual
#: row and are read left-to-right. 8 px is below every standard Windows control
#: height (>= 16 px) and above the sub-pixel jitter WinUI layout produces.
ROW_TOLERANCE_PX = 8


def visible_enabled(elements: Sequence[Any]) -> List[UIElement]:
    """Drop what cannot be acted on: offscreen, disabled, or too small.

    ``offscreen`` is UIA's own word for "scrolled out of view or on a hidden
    tab" and it is reliable; size is the guard against the 0x0 and 1x1 layout
    nodes that WinUI emits in quantity.
    """
    return [el for el in as_elements(elements)
            if el.enabled and not el.offscreen and el.is_sized()]


def interactive(elements: Sequence[Any]) -> List[UIElement]:
    """Keep the elements a click, a keystroke or a selection can target.

    Role *or* pattern: WinUI apps ship real buttons typed as ``pane`` or
    ``custom`` that only advertise themselves through ``InvokePattern``. Testing
    the role alone loses them; testing the pattern alone loses controls whose
    provider reports no pattern at all (plain Win32 ``listitem``).
    """
    out: List[UIElement] = []
    for el in as_elements(elements):
        if el.role in INTERACTIVE_ROLES:
            out.append(el)
        elif ACTIONABLE_PATTERNS.intersection(el.patterns):
            out.append(el)
    return out


def _focus_point(elements: Sequence[UIElement],
                 focus: Optional[Any]) -> Optional[Tuple[int, int]]:
    if isinstance(focus, UIElement):
        return focus.center
    if isinstance(focus, (tuple, list)) and len(focus) == 2:
        return int(focus[0]), int(focus[1])
    if isinstance(focus, str):
        for el in elements:
            if el.id == focus:
                return el.center
        return None
    for el in elements:
        if el.focused:
            return el.center
    return None


def dedupe(elements: Sequence[Any], *, focus: Optional[Any] = None) -> List[UIElement]:
    """Collapse ``(role, name, region, parent)`` tuples to one representative.

    Ribbons and toolbars repeat the same name many times; nine identical "More
    options" buttons cost nine options in a Choice and give the model nine ways
    to be right, which reads as nine ways to be wrong.

    ``parent`` is in the key and that is the whole correctness of this function.
    Without it the key was ``(role, name, region)``, and a list is one region:
    ten rows each carrying a "Delete" button collapsed to **one** survivor with
    ``duplicates=9``, so "delete row 7" was unreachable — measured, and it is
    exactly the shape of screen a computer-use loop meets most. Per-row controls
    have different parents and now all survive; true duplicates (same parent,
    same role, same name) still collapse. A list of 200 identical rows is a
    *volume* problem, and volume is what the cap and :func:`prioritise` are for
    — a filter that answers it by deleting rows answers the wrong question.

    The survivor is the one nearest the focused element, because the control the
    user (or the previous step) was last working in is the one a follow-up
    action almost always belongs to. With no focus, document order wins, which
    keeps the function deterministic — the property the tests rely on.

    The survivor carries ``duplicates`` = how many it stands for.
    """
    els = as_elements(elements)
    point = _focus_point(els, focus)
    groups: Dict[Tuple[str, str, Optional[str], Optional[str]],
                 List[Tuple[int, UIElement]]] = {}
    for index, el in enumerate(els):
        groups.setdefault((el.role, el.name, el.region, el.parent),
                          []).append((index, el))

    keep: Dict[str, UIElement] = {}
    for members in groups.values():
        if len(members) == 1:
            keep[members[0][1].id] = members[0][1]
            continue
        if point is None:
            winner = members[0][1]
        else:
            def rank(pair: Tuple[int, UIElement]) -> Tuple[float, int]:
                cx, cy = pair[1].center
                return math.hypot(cx - point[0], cy - point[1]), pair[0]
            winner = min(members, key=rank)[1]
        # A copy, not a mutation: these functions are pure, and the caller's
        # snapshot must read the same before and after a reduction.
        keep[winner.id] = replace(winner, duplicates=len(members) - 1)
    # Document order, so the result is a stable prefix of the input.
    return [keep[el.id] for el in els if el.id in keep]


def _reading_order(elements: Sequence[UIElement]) -> List[UIElement]:
    """Top-to-bottom, left-to-right, with rows detected rather than bucketed.

    Fixed-size buckets split a row whenever it straddles a boundary (two
    controls 1 px apart landing in different buckets). Greedy row building from
    a top-sorted list has no boundary to straddle.
    """
    ordered = sorted(elements, key=lambda el: (el.bbox[1], el.bbox[0], el.id))
    out: List[UIElement] = []
    row: List[UIElement] = []
    row_top = None
    for el in ordered:
        if row_top is None or abs(el.bbox[1] - row_top) <= ROW_TOLERANCE_PX:
            if row_top is None:
                row_top = el.bbox[1]
            row.append(el)
        else:
            out.extend(sorted(row, key=lambda e: (e.bbox[0], e.id)))
            row, row_top = [el], el.bbox[1]
    out.extend(sorted(row, key=lambda e: (e.bbox[0], e.id)))
    return out


def prioritise(elements: Sequence[Any], focus_id: Optional[str] = None, *,
               full: Optional[Sequence[Any]] = None) -> List[UIElement]:
    """Focused first, then the active dialog, then the focused pane, then reading order.

    The cap truncates the tail, so this ordering decides what the model never
    sees. The priors are the two that hold across applications: a modal dialog
    owns the next action while it is up, and the pane holding focus is where the
    task was left off.

    **Pass ``full``** — the unreduced element list — or the dialog prior does
    not exist. :func:`interactive` strips the ``dialog`` node itself and every
    unnamed ``group`` between it and its buttons, so both ``dialog_ids`` and the
    parent chain come up empty when this function only sees the reduced list.
    Measured on a synthetic 72-control screen with a modal open: the two modal
    buttons ranked 71st and 72nd of 72 — outside a cap of 60, with and without
    focus — so the loop could not see the only two controls that mattered.

    A dialog member also gets a bucket strictly *above* the focus-region bucket.
    Merging the two (the original code returned 1 for both) let geometry decide:
    when focus sat in the pane behind a modal, reading order interleaved the
    modal's buttons with 70 controls the modal had disabled in spirit if not in
    UIA. A modal is modal; it outranks wherever focus happens to be.
    """
    els = as_elements(elements)
    # The reduced list is what gets ordered; the full list is what gets *asked*
    # about ancestry. Two different lists on purpose.
    scope = as_elements(full) if full is not None else els
    by_id = {el.id: el for el in scope}
    dialog_ids = {el.id for el in scope if el.role == "dialog"}
    focus_region = None
    for el in scope:
        if (focus_id is not None and el.id == focus_id) or (focus_id is None and el.focused):
            focus_region = el.region
            if focus_id is None:
                focus_id = el.id
            break

    in_dialog: Dict[str, bool] = {}

    def under_dialog(el: UIElement) -> bool:
        # Region ids alone are not enough: a control nested two panes deep
        # inside a modal still belongs to the modal. Walk the parent chain
        # once per element, memoised, and only when a dialog exists at all.
        if el.id in in_dialog:
            return in_dialog[el.id]
        chain: List[str] = []
        current: Optional[UIElement] = el
        seen: Set[str] = set()
        result = False
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.id in in_dialog:
                result = in_dialog[current.id]
                break
            if current.id in dialog_ids:
                result = True
                break
            chain.append(current.id)
            current = by_id.get(current.parent) if current.parent else None
        for element_id in chain:
            in_dialog[element_id] = result
        return result

    def rank(el: UIElement) -> int:
        if el.id == focus_id:
            return 0
        if dialog_ids and under_dialog(el):
            return 1
        if focus_region is not None and el.region == focus_region:
            return 2
        return 3

    buckets: Dict[int, List[UIElement]] = {0: [], 1: [], 2: [], 3: []}
    for el in els:
        buckets[rank(el)].append(el)
    return (buckets[0] + _reading_order(buckets[1]) + _reading_order(buckets[2])
            + _reading_order(buckets[3]))


def candidates(elements: Sequence[Any], cap: int = 60) -> List[UIElement]:
    """The whole reduction, in the order the docstring above justifies.

    ``cap`` defaults to 60 for **latency and tokens**, not accuracy: a 60-element
    state is 2,488 tokens on the 2,000-node fixture (budget 3,500), and latency
    climbs +125 to +205 ms between 30 and 240 elements. Accuracy is not the
    binding constraint — ``target`` held at 0.97-0.99 with 241 options. Above
    the cap use :func:`region_state` and decide in two steps, because two cheap
    calls beat one slow one, not because one call would be wrong.

    The full list is handed to :func:`prioritise` as ``full``: the dialog and
    the parent chain that proves membership are both stripped by the filters.
    """
    els = as_elements(elements)
    focus_id = next((el.id for el in els if el.focused), None)
    focus_point = _focus_point(els, None)
    kept = interactive(visible_enabled(els))
    kept = dedupe(kept, focus=focus_point)
    return prioritise(kept, focus_id, full=els)[:cap]


def _region_ancestor(el: UIElement, by_id: Dict[str, UIElement],
                     memo: Dict[str, Optional[str]]) -> Optional[str]:
    """Nearest *named* ancestor with a region role; else nearest region role."""
    if el.id in memo:
        return memo[el.id]
    fallback: Optional[str] = None
    seen = set()
    current = by_id.get(el.parent) if el.parent else None
    # The element's own ``region`` hint from observe() is a shortcut, but the
    # walk still has to continue upwards from it to find a *named* one.
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.role in REGION_ROLES:
            if current.name.strip():
                memo[el.id] = current.id
                return current.id
            if fallback is None:
                fallback = current.id
        current = by_id.get(current.parent) if current.parent else None
    memo[el.id] = fallback
    return fallback


def regions(elements: Sequence[Any]) -> List[Region]:
    """Group elements under the nearest named pane/group/toolbar/dialog.

    Call this with the *full* element list (the ancestors have to be present to
    be found) and read ``members`` for the candidate ids you care about.
    Regions with no members are not returned: an empty option is a wasted one.
    """
    els = as_elements(elements)
    by_id = {el.id: el for el in els}
    memo: Dict[str, Optional[str]] = {}
    order: List[str] = []
    members: Dict[str, List[str]] = {}
    for el in els:
        if el.role in REGION_ROLES:
            continue  # a container is not a member of itself
        parent_id = _region_ancestor(el, by_id, memo)
        if parent_id is None:
            continue
        if parent_id not in members:
            members[parent_id] = []
            order.append(parent_id)
        members[parent_id].append(el.id)

    out: List[Region] = []
    for region_id in order:
        host = by_id.get(region_id)
        ids = members[region_id]
        if host is not None and host.name.strip():
            name, role, bbox = host.name, host.role, host.bbox
        else:
            role = host.role if host is not None else "group"
            name = _representative_name(ids, by_id) or (role + " " + region_id)
            bbox = union_bbox([by_id[i].bbox for i in ids if i in by_id])
            if host is not None and host.is_sized():
                bbox = host.bbox
        out.append(Region(id=region_id, name=name, role=role, bbox=bbox, members=ids))
    return out


def _representative_name(ids: Sequence[str], by_id: Dict[str, UIElement]) -> str:
    for element_id in ids:
        el = by_id.get(element_id)
        if el is not None and el.name.strip():
            return el.name.strip()
    return ""


def union_bbox(boxes: Sequence[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    """Smallest ``(left, top, width, height)`` covering every non-empty box.

    Public because :mod:`jevskill.cu.observe` needs it to draw the grid
    ``to_state`` reports cells in, and one module reaching into another's
    underscore name is a dependency nobody declared. Empty input is ``(0,0,0,0)``
    rather than an exception: an empty region is a real case (everything in it
    was filtered) and a caller that has to try/except a geometry helper will
    eventually not.
    """
    boxes = [b for b in boxes if b[2] > 0 and b[3] > 0]
    if not boxes:
        return (0, 0, 0, 0)
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    return (left, top, right - left, bottom - top)


def _chunk_suffix(index: int) -> str:
    """0 -> "a", 25 -> "z", 26 -> "aa". Letters, so ``r3b`` reads as a part."""
    out = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        out = chr(ord("a") + remainder) + out
    return out


def region_state(elements: Sequence[Any], cap: int = 60) -> Dict[str, Any]:
    """The coarse half of the cascade: ``{"regions": {...}}``.

    Used when :func:`candidates` still returns ``cap`` elements after reduction.
    One Choice picks the region, a second Choice picks inside it — two calls of
    ~10 options each instead of one call of 300, which is the documented
    ``hierarchical_classification`` shape.

    The region list is capped too, and that is not a detail: a 2,000-node tree
    produced 71 regions here, and a 71-option Choice is exactly the situation
    the cascade exists to avoid. Regions are ordered by their best-ranked
    member — so the focused control's region and the active dialog's region are
    never the ones dropped — and entries are emitted until ``cap`` is reached.

    **An oversized region is split, not truncated.** ``members[:cap]`` made
    member 61 of a 254-member region unreachable: the flat cap had already hidden
    it and the cascade's second round hid it again, with no third level to
    recover it. A region with more than ``cap`` members is now published as
    ``r3a``, ``r3b``, ... — each chunk a Choice of at most ``cap`` options,
    ranked, carrying ``"part": "2/5"`` so the two rounds still reach every
    member. The residue is honest and bounded: if a screen needs more chunks
    than ``cap`` entries, the tail is still dropped and a third round would be
    required. At ``cap=60`` that needs 3,600 ranked candidates in one window.
    """
    els = as_elements(elements)
    ranked = prioritise(dedupe(interactive(visible_enabled(els))),
                        next((el.id for el in els if el.focused), None),
                        full=els)
    rank = {el.id: index for index, el in enumerate(ranked)}

    scored: List[Tuple[int, Region, List[str]]] = []
    for region in regions(els):
        members = [i for i in region.members if i in rank]
        if members:
            scored.append((min(rank[i] for i in members), region, members))
    scored.sort(key=lambda row: (row[0], row[1].id))

    out: Dict[str, Any] = {}
    for index, (_, region, members) in enumerate(scored):
        if len(out) >= cap:
            break
        members.sort(key=lambda i: rank[i])
        chunks = [members[start:start + cap]
                  for start in range(0, len(members), cap)]
        for part, chunk in enumerate(chunks):
            if len(out) >= cap:
                break
            row: Dict[str, Any] = {
                "name": region.name, "role": region.role,
                "members": chunk, "count": len(chunk),
            }
            if len(chunks) > 1:
                row["part"] = "%d/%d" % (part + 1, len(chunks))
            out["r%d%s" % (index, _chunk_suffix(part) if len(chunks) > 1 else "")] = row
    return {"regions": out}

"""The mission tree: phases broken down lazily into leaves, each leaf one goal.

A flat plan of up to eight goals was enough for "type hello in Notepad" and
not for "write the report, save it, mail it": one failed goal replanned the
whole command, re-ran what had already worked, and a long task hit the plan
cap before it reached its last step. The tree keeps a failure local — a
repair rewrites the children of the phase that failed, not the command — and
lets a phase be broken down only once its screen exists.

This module is **pure**: no desktop, no network, no clock. The runner imports
it; it never imports the runner (that would be a cycle, and it would drag the
UIA backend into every test of a data structure). The legacy ``PlanStep`` the
status payload shows is therefore built through callbacks (``make_step`` in
:func:`coerce_tree`, ``step_loader`` in :meth:`PlanNode.from_dict`) instead of
being imported here.

Every bound below is a design bound, not a measurement: nothing in this repo
has timed a live mission yet, and the constants say so rather than pretend.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .act import is_destructive_name

# --------------------------------------------------------------------------- #
# Bounds (design bounds, not measurements)
# --------------------------------------------------------------------------- #

#: Root is depth 0, leaves live at depth <= 3. A phase the model puts at depth 3
#: is forced to a leaf: a fourth level was never asked for by a real task and
#: every level adds a round trip before the first action.
MAX_DEPTH = 3
#: Today's flat plan cap, kept as the per-phase fan-out so a flat command and a
#: phase's children are bounded by the same number.
MAX_CHILDREN = 8
MAX_PHASES = 12
MAX_LEAVES = 60
#: Equal to the runner's MAX_REPLANS, so a flat run repairs exactly as often as
#: the flat planner replanned before the tree existed.
MAX_REPAIRS_PER_NODE = 2
MAX_REPAIRS_TOTAL = 8
#: A repair that re-proposes the goal that already failed this many times is
#: filtered out; without it a model can loop "retry the same thing" until the
#: LLM cap, which is the most expensive way to fail.
MAX_SAME_GOAL_FAILS = 2
MIN_PHASE_S = 60.0
MIN_LEAF_S = 20.0
PHASE_SLACK = 1.5

CHECK_KINDS: Tuple[str, ...] = ("file_exists", "file_contains", "title_contains")
#: A check reads at most this much of a file. It is an acceptance probe, not a
#: parser: a multi-GB log must not stall the operator thread at a phase close.
FILE_READ_CAP = 64 * 1024

OPEN_LEAF: Tuple[str, ...] = ("pending", "interrupted", "stopped")
CLOSED: Tuple[str, ...] = ("done", "skipped", "failed")
EVIDENCE: Tuple[str, ...] = ("code", "replay", "jev", "model", "children", "user")

#: The flat planner's clamps from before the tree (0.14.0): a flat plan coerced here
#: must produce byte-identical PlanSteps to the ones it produced before.
GOAL_CAP = 400
DONE_WHEN_CAP = 300
APP_CAP = 120
WEIGHT_MIN, WEIGHT_MAX = 0.1, 10.0
#: Checks per node. Each one may read 64 KiB at a phase close; a model that
#: emits forty of them is producing noise, not acceptance criteria.
_MAX_CHECKS = 8

#: Verbs that make a goal irreversible even when the model did not say so.
#: ``is_destructive_name`` already covers delete/send/pay; this adds the verbs a
#: *goal* uses (save, submit, overwrite) and their Polish forms, because the
#: redo question after a crash must fire for "Zapisz jako hello.txt" too.
IRREVERSIBLE_VERBS: Tuple[str, ...] = (
    "save", "zapisz", "send", "wyślij", "submit", "pay", "zapłać",
    "delete", "usuń", "overwrite", "nadpisz",
)
# Word start only: "saved"/"saves" count, "unsaved" does not. A full word
# boundary on both sides would miss every inflected Polish form.
_VERB_RE = re.compile(r"(?<!\w)(?:%s)" % "|".join(re.escape(v) for v in IRREVERSIBLE_VERBS))
_WIN_VAR_RE = re.compile(r"%([^%\s]+)%")
#: Unsaved-document dots. A leading ``*`` is the classic marker ("*hello.txt -
#: Notatnik"); the dots are counted anywhere because editors put them in
#: different places — in front of the name, after it, between it and the app.
UNSAVED_DOTS: Tuple[str, ...] = ("•", "●")
#: How much older than ``since`` a file may be and still count as written by
#: this run: file-system mtimes are coarse (FAT keeps two seconds, NTFS flushes
#: lazily), and a save that landed a moment before the mark is still this save.
MTIME_SLACK_S = 1.0


def is_unsaved_title(title: Any) -> bool:
    """Does this window title say the document has unsaved changes?

    A title is the operator's cheapest proof that a save happened — and
    ``*hello.txt - Notatnik`` contains ``hello.txt`` exactly like the saved
    ``hello.txt - Notatnik`` does. Reading the dirty title as "saved" closed a
    save goal whose document was never written (review finding #18).
    """
    text = str(title or "").lstrip()
    return text.startswith("*") or any(dot in text for dot in UNSAVED_DOTS)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

@dataclass
class Check:
    """One code-decidable acceptance probe for a phase.

    ``from_dict`` raises on anything it does not understand and callers drop
    the check: a guessed check is worse than none, because a check that is
    wrongly true closes a phase with evidence ``code`` — the strongest label
    the tree has.
    """

    kind: str
    arg: str
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "arg": self.arg, "text": self.text}

    @classmethod
    def from_dict(cls, d: Any) -> "Check":
        if isinstance(d, Check):
            return cls(d.kind, d.arg, d.text)
        if not isinstance(d, dict):
            raise ValueError("a check is an object")
        kind = str(d.get("kind") or "").strip().casefold()
        if kind not in CHECK_KINDS:
            raise ValueError("unknown check kind %r" % kind)
        arg = d.get("arg")
        arg = arg.strip() if isinstance(arg, str) else ""
        if not arg:
            raise ValueError("a check needs an arg")
        text = d.get("text")
        text = text if isinstance(text, str) else ""
        if kind == "file_contains" and not text:
            # An empty needle is in every file, so this would silently become
            # file_exists with a stronger-sounding name. Drop it instead.
            raise ValueError("file_contains needs text")
        return cls(kind=kind, arg=arg, text=text)


def _expand_path(path: str) -> str:
    """``%VAR%`` first, by hand, because ``posixpath.expandvars`` ignores it.

    The model writes Windows paths (``%USERPROFILE%\\Desktop\\a.txt``), and the
    offline suite runs on Linux CI where only ``$VAR`` is understood. An unset
    variable is left as written, like ``ntpath.expandvars`` does, so the check
    reports the path it could not find instead of a mangled one.
    """
    path = path.strip().strip('"').strip("'")

    def sub(m: "re.Match[str]") -> str:
        value = os.environ.get(m.group(1))
        return value if value is not None else m.group(0)

    path = _WIN_VAR_RE.sub(sub, path)
    return os.path.expanduser(os.path.expandvars(path))


def _eval_one(check: Check, snapshot: Any,
              since: Optional[float] = None) -> Tuple[Optional[bool], str]:
    if check.kind == "title_contains":
        title = getattr(snapshot, "window_title", None) if snapshot is not None else None
        if not isinstance(title, str):
            return None, "title_contains %r: undecided" % check.arg
        ok = check.arg.casefold() in title.casefold()
        if ok and is_unsaved_title(title):
            # The name is there, but so is the unsaved marker: the document
            # this title names has not been written yet. Not a no either —
            # the save may be one keystroke away — so it is left undecided.
            return None, "title_contains %r: unsaved title, undecided" % check.arg
        return ok, "title_contains %r: %s" % (check.arg, str(ok).lower())
    path = _expand_path(check.arg)
    if not os.path.isfile(path):
        return False, "%s %s: false" % (check.kind, path)
    stale = False
    if since is not None:
        try:
            stale = os.path.getmtime(path) < float(since) - MTIME_SLACK_S
        except (OSError, TypeError, ValueError):
            return None, "%s %s: mtime unreadable" % (check.kind, path)
    if check.kind == "file_exists":
        if stale:
            # Yesterday's hello.txt on the Desktop is not proof that today's
            # save happened (review finding #17): only a file written since the
            # run started can close the goal in code.
            return None, "file_exists %s: stale" % path
        return True, "file_exists %s: true" % path
    try:
        with open(path, "rb") as fh:
            head = fh.read(FILE_READ_CAP)
    except OSError:
        # Exists but unreadable (locked by the app that is still saving it):
        # not a definite no, so not a reason to repair.
        return None, "file_contains %s: unreadable" % path
    ok = check.text in head.decode("utf-8", errors="replace")
    if ok and stale:
        # ``since`` only ever weakens a yes: an old file that lacks the text
        # is still a definite no about the file as it is now.
        return None, "file_contains %s: stale" % path
    # The detail never quotes the file: it lands in events, the journal and
    # the ledger, and the file may be the user's document.
    return ok, "file_contains %s: %s" % (path, str(ok).lower())


def evaluate_checks(checks: Optional[Sequence[Any]], snapshot: Any, *,
                    since: Optional[float] = None) -> Tuple[Optional[bool], str]:
    """``(True, detail)`` all hold, ``(False, detail)`` one is definitely false,
    ``(None, "")`` nothing to decide on or one cannot be decided.

    A definite false wins over an undecided one: a missing file is a missing
    file whatever the title says. Never raises — it runs at a phase close on
    the operator thread, and an exception there would end the run over a
    probe.

    ``since`` (a ``time.time()`` stamp, normally the run's start) makes a file
    check prove only what this run wrote: a file whose mtime is older than
    ``since`` minus :data:`MTIME_SLACK_S` is undecided rather than true. A
    missing file is still false. ``None`` keeps the check blind to age, as it
    always was.
    """
    parsed: List[Check] = []
    for c in checks or ():
        try:
            parsed.append(Check.from_dict(c))
        except Exception:  # noqa: BLE001 - garbage is dropped, as in coerce_tree
            continue
    if not parsed:
        return None, ""
    results: List[Tuple[Optional[bool], str]] = []
    for c in parsed:
        try:
            results.append(_eval_one(c, snapshot, since))
        except Exception:  # noqa: BLE001 - see docstring
            results.append((None, "%s: error" % c.kind))
    detail = "; ".join(d for _, d in results)
    if any(r is False for r, _ in results):
        return False, detail
    if any(r is None for r, _ in results):
        return None, ""
    return True, detail


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def quote_on_screen(quote: Optional[str], snapshot: Any) -> bool:
    """Is the model's quoted evidence actually on the screen it saw?

    A repair verdict of ``completed`` is only believed with a quote that code
    can find; three characters is the floor below which almost anything
    matches ("a", "ok").
    """
    if snapshot is None or not isinstance(quote, str):
        return False
    q = quote.strip().casefold()
    if len(q) < 3:
        return False
    title = _field(snapshot, "window_title")
    if isinstance(title, str) and q in title.casefold():
        return True
    for el in _field(snapshot, "elements") or ():
        for name in ("name", "value"):
            text = _field(el, name)
            if isinstance(text, str) and q in text.casefold():
                return True
    return False


def classify_irreversible(goal: Optional[str], model_flag: Any) -> bool:
    """Model flag OR a destructive control word OR a committing verb.

    It can only raise the flag. The model under-reports: it called "Save as
    hello.txt" reversible, and the cost of the extra redo question after a
    crash is one click, while the cost of silently repeating a send is a
    second e-mail.
    """
    if _as_bool(model_flag):
        return True
    text = str(goal or "")
    if is_destructive_name(text):
        return True
    return bool(_VERB_RE.search(text.casefold()))


# --------------------------------------------------------------------------- #
# The tree
# --------------------------------------------------------------------------- #

def _default_spend() -> Dict[str, float]:
    return {"active_s": 0.0, "llm_calls": 0, "llm_usd": 0.0,
            "jev_calls": 0, "jev_usd": 0.0, "steps": 0}


@dataclass
class PlanNode:
    """One node: the root and every phase are ``kind="phase"``, goals are leaves.

    ``parent`` is excluded from ``repr`` and equality: it is a back pointer,
    and comparing it would recurse up and down the tree forever.
    """

    id: str
    goal: str
    kind: str = "leaf"
    depth: int = 0
    done_when: str = ""
    launch: Optional[str] = None
    app: str = ""
    checks: List[Check] = field(default_factory=list)
    weight: float = 1.0
    optional: bool = False
    irreversible: bool = False
    status: str = "pending"
    evidence: str = ""
    source: str = ""
    expanded: bool = False
    superseded: bool = False
    repairs: int = 0
    generation: int = 0
    acted: int = 0
    tried: List[str] = field(default_factory=list)
    note: str = ""
    stop_reason: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    paused_mark: float = 0.0
    budget_s: float = 0.0
    spend: Dict[str, float] = field(default_factory=_default_spend)
    children: List["PlanNode"] = field(default_factory=list)
    step: Optional[Any] = None
    parent: Optional["PlanNode"] = field(default=None, repr=False, compare=False)

    # ---------------------------------------------------------------- walkers
    def walk(self) -> Iterator["PlanNode"]:
        """Pre-order, self first. Iterative: a pathological tree from a
        corrupt checkpoint must not hit the recursion limit."""
        stack: List[PlanNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def leaves(self) -> List["PlanNode"]:
        """Every existing leaf in DFS order, failed and superseded included.

        An unexpanded phase is not a leaf: it has no goal the loop can run yet,
        and counting it would make ``max_leaves`` refuse expansions early.
        """
        return [n for n in self.walk() if n.kind == "leaf"]

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED

    def next_open(self) -> Optional["PlanNode"]:
        """The one cursor of the driver (spec §3.1).

        A running leaf is not returned: it is the goal in flight, and handing it
        out again is how one goal ends up driven twice. A phase is returned when
        it still needs breaking down, or when all its children are closed and
        it needs closing.
        """
        if self.is_closed:
            return None
        if self.kind != "phase":
            return self if self.status in OPEN_LEAF else None
        if not self.expanded:
            return self
        if all(c.is_closed for c in self.children):
            return self
        for child in self.children:
            found = child.next_open()
            if found is not None:
                return found
        return None

    def find(self, node_id: str) -> Optional["PlanNode"]:
        for node in self.walk():
            if node.id == node_id:
                return node
        return None

    def phase(self) -> "PlanNode":
        """The nearest ancestor phase: a leaf's phase, a phase's parent, and
        the root for the root (there is nothing above it to pin or budget by)."""
        node = self.parent
        while node is not None:
            if node.kind == "phase":
                return node
            node = node.parent
        return self.path()[0]

    def path(self) -> List["PlanNode"]:
        out: List[PlanNode] = []
        node: Optional[PlanNode] = self
        while node is not None:
            out.append(node)
            node = node.parent
        out.reverse()
        return out

    def add_children(self, nodes: Sequence["PlanNode"]) -> None:
        for node in nodes:
            node.parent = self
            self.children.append(node)

    # ---------------------------------------------------------- serialisation
    def item(self) -> Dict[str, Any]:
        """The nested plan-item form: what the planner emits and memory stores.

        It is the input shape of :func:`coerce_tree`, so ``coerce(item())``
        rebuilds the same plan — which is what lets a remembered tree replay
        without a model call.
        """
        out: Dict[str, Any] = {
            "goal": self.goal, "done_when": self.done_when, "launch": self.launch,
            "kind": self.kind, "app": self.app,
            "checks": [c.to_dict() for c in self.checks],
            "weight": self.weight, "optional": self.optional,
            "irreversible": self.irreversible,
        }
        if self.kind == "phase" and self.children:
            out["children"] = [c.item() for c in self.children]
        return out

    def to_dict(self, deep: bool = True) -> Dict[str, Any]:
        step = self.step
        step_d = step.to_dict() if step is not None and hasattr(step, "to_dict") else None
        out: Dict[str, Any] = {
            "id": self.id, "goal": self.goal, "kind": self.kind, "depth": self.depth,
            "done_when": self.done_when, "launch": self.launch, "app": self.app,
            "checks": [c.to_dict() for c in self.checks], "weight": self.weight,
            "optional": self.optional, "irreversible": self.irreversible,
            "status": self.status, "evidence": self.evidence, "source": self.source,
            "expanded": self.expanded, "superseded": self.superseded,
            "repairs": self.repairs, "generation": self.generation,
            "acted": self.acted, "tried": list(self.tried), "note": self.note,
            "stop_reason": self.stop_reason, "started_at": self.started_at,
            "ended_at": self.ended_at, "paused_mark": self.paused_mark,
            "budget_s": self.budget_s, "spend": dict(self.spend), "step": step_d,
        }
        if deep:
            out["children"] = [c.to_dict(True) for c in self.children]
        return out

    @classmethod
    def from_dict(cls, d: Any, parent: Optional["PlanNode"] = None, *,
                  step_loader: Optional[Callable[[Dict[str, Any], "PlanNode"], Any]] = None
                  ) -> "PlanNode":
        """Rebuild a tree from a checkpoint, lenient about types.

        The checkpoint is written by this build but read by a later one, maybe
        after a crash mid-write of a neighbouring field; a missing key takes its
        default instead of failing the resume. The legacy step is rebuilt by
        ``step_loader`` because this module cannot import the runner.
        """
        if not isinstance(d, dict):
            raise ValueError("a plan node is an object")
        checks: List[Check] = []
        for c in d.get("checks") or ():
            try:
                checks.append(Check.from_dict(c))
            except ValueError:
                continue
        spend = _default_spend()
        if isinstance(d.get("spend"), dict):
            spend.update(d["spend"])
        launch = d.get("launch")
        node = cls(
            id=str(d.get("id", "")), goal=str(d.get("goal") or ""),
            kind="phase" if d.get("kind") == "phase" else "leaf",
            depth=_as_int(d.get("depth")), done_when=str(d.get("done_when") or ""),
            launch=launch if isinstance(launch, str) and launch else None,
            app=str(d.get("app") or ""), checks=checks,
            weight=_as_weight(d.get("weight", 1.0)),
            optional=_as_bool(d.get("optional")),
            irreversible=_as_bool(d.get("irreversible")),
            status=str(d.get("status") or "pending"),
            evidence=str(d.get("evidence") or ""), source=str(d.get("source") or ""),
            expanded=_as_bool(d.get("expanded")), superseded=_as_bool(d.get("superseded")),
            repairs=_as_int(d.get("repairs")), generation=_as_int(d.get("generation")),
            acted=_as_int(d.get("acted")),
            tried=[str(t) for t in (d.get("tried") or ()) if isinstance(t, str)],
            note=str(d.get("note") or ""), stop_reason=str(d.get("stop_reason") or ""),
            started_at=_as_float(d.get("started_at")), ended_at=_as_float(d.get("ended_at")),
            paused_mark=_as_float(d.get("paused_mark")), budget_s=_as_float(d.get("budget_s")),
            spend=spend, parent=parent,
        )
        for child in d.get("children") or ():
            node.children.append(cls.from_dict(child, node, step_loader=step_loader))
        if node.kind == "leaf" and step_loader is not None:
            step_d = d.get("step")
            node.step = step_loader(step_d if isinstance(step_d, dict) else {}, node)
        return node


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #

def child_prefix(node: PlanNode) -> str:
    """The prefix for a node's first-generation children: "" under the root,
    so root children are "1".."n" exactly like today's flat step numbers."""
    return "" if node.id == "0" else node.id


def repair_prefix(node: PlanNode, generation: int) -> str:
    """Repair generation ``g`` gets its own namespace ("2.r1", root "r1"), so a
    revised child can never reuse the id of the failed one it replaces — the
    journal and the events refer to nodes by id."""
    return "r%d" % generation if node.id == "0" else "%s.r%d" % (node.id, generation)


def child_id(prefix: str, k: int) -> str:
    """The id of the k-th (1-based) node built under ``prefix``."""
    return "%s.%d" % (prefix, k) if prefix else str(k)


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #

def _as_bool(value: Any) -> bool:
    # "false" from a sloppy JSON writer is truthy in Python; the model does
    # emit strings for booleans.
    if isinstance(value, str):
        return value.strip().casefold() in ("true", "yes", "1", "tak")
    return bool(value)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _as_weight(value: Any) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(w) or isinstance(value, bool):
        return 1.0
    return min(WEIGHT_MAX, max(WEIGHT_MIN, w))


def _normalise(item: Any, depth: int,
               max_children: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """One raw item -> a clean dict, or None for garbage.

    Collapse and depth forcing happen here, before any id exists, so a
    collapsed one-child phase takes its parent's id instead of leaving a gap
    ("1", "3") that a reader would take for a dropped goal.

    ``max_children`` is the per-phase fan-out, at every level below this one;
    ``None`` is :data:`MAX_CHILDREN`, the bound on a fresh model reply.
    """
    child_cap = MAX_CHILDREN if max_children is None else max(0, int(max_children))
    if isinstance(item, str):
        goal = item.strip()
        if not goal:
            return None
        return {"goal": goal[:GOAL_CAP], "done_when": "", "launch": None, "kind": "leaf",
                "app": "", "checks": [], "weight": 1.0, "optional": False,
                "irreversible": False}
    if not isinstance(item, dict):
        return None
    goal = str(item.get("goal") or "").strip()
    if not goal:
        return None
    launch = item.get("launch")
    launch = launch.strip() if isinstance(launch, str) and launch.strip() else None
    app = item.get("app")
    app = app.strip()[:APP_CAP] if isinstance(app, str) else ""
    checks: List[Check] = []
    raw_checks = item.get("checks")
    if isinstance(raw_checks, list):
        for c in raw_checks:
            try:
                checks.append(Check.from_dict(c))
            except (ValueError, TypeError, AttributeError):
                continue
            if len(checks) >= _MAX_CHECKS:
                break
    out: Dict[str, Any] = {
        "goal": goal[:GOAL_CAP],
        # Not stripped: the flat planner never stripped done_when, and flat parity
        # means the same PlanStep for the same reply.
        "done_when": str(item.get("done_when") or "")[:DONE_WHEN_CAP],
        "launch": launch, "kind": "leaf", "app": app, "checks": checks,
        "weight": _as_weight(item.get("weight", 1.0)),
        "optional": _as_bool(item.get("optional")),
        "irreversible": _as_bool(item.get("irreversible")),
    }
    kind = item.get("kind")
    if not (isinstance(kind, str) and kind.strip().casefold() == "phase"):
        return out
    if depth >= MAX_DEPTH:
        return out  # forced to a leaf; its children are dropped
    raw_children = item.get("children")
    children: List[Dict[str, Any]] = []
    if isinstance(raw_children, list):
        for raw in raw_children:
            if len(children) >= child_cap:
                break
            norm = _normalise(raw, depth + 1, max_children)
            if norm is not None:
                children.append(norm)
    if len(children) == 1:
        # A phase of one goal is that goal; keeping the level would cost a
        # phase close (and maybe a check read) for nothing.
        only = children[0]
        if not only["app"]:
            only["app"] = out["app"]
        if not only["done_when"]:
            only["done_when"] = out["done_when"]
        return only
    out["kind"] = "phase"
    out["children"] = children
    return out


def _build(norm: Dict[str, Any], depth: int, node_id: str, source: str,
           make_step: Optional[Callable[[PlanNode], Any]],
           parent: Optional[PlanNode]) -> PlanNode:
    node = PlanNode(
        id=node_id, goal=norm["goal"], kind=norm["kind"], depth=depth,
        done_when=norm["done_when"], launch=norm["launch"], app=norm["app"],
        checks=list(norm["checks"]), weight=norm["weight"], optional=norm["optional"],
        irreversible=classify_irreversible(norm["goal"], norm["irreversible"]),
        source=source, parent=parent,
    )
    if node.kind == "phase":
        kids = norm.get("children") or []
        node.expanded = bool(kids)
        for k, child in enumerate(kids, 1):
            node.children.append(_build(child, depth + 1, child_id(node_id, k), source,
                                        make_step, node))
    elif make_step is not None:
        node.step = make_step(node)
    return node


def coerce_tree(items: Any, *, depth: int, id_prefix: str, source: str,
                make_step: Optional[Callable[[PlanNode], Any]] = None,
                max_items: Optional[int] = None,
                max_children: Optional[int] = None) -> List[PlanNode]:
    """Model, memory or user items -> nodes, dropping what cannot be used.

    Today's flat reply (strings, ``{goal, done_when, launch}``) comes out as the
    same leaves the flat planner made, so a short command costs exactly what
    it cost before the tree. The returned top-level nodes have no parent: the
    caller attaches them where they belong with :meth:`PlanNode.add_children`.

    ``max_items`` caps the top level; ``max_children`` caps each nested
    phase's children (``None``: :data:`MAX_CHILDREN`). The defaults bound a
    model's reply. A tree the user wrote, or one memory replays, already ran or
    was asked for as it is: capping its phases at eight silently dropped the
    tail of a ten-child phase (review finding #1), so the caller lifts both.
    """
    if not isinstance(items, (list, tuple)):
        raise ValueError("a plan is a list")
    normed = [n for n in (_normalise(i, depth, max_children) for i in items) if n is not None]
    if max_items is not None:
        cap = max(0, int(max_items))
    else:
        cap = MAX_PHASES if any(n["kind"] == "phase" for n in normed) else MAX_CHILDREN
    normed = normed[:cap]
    if not normed:
        raise ValueError("the plan has no usable step")
    return [_build(n, depth, child_id(id_prefix, k), source, make_step, None)
            for k, n in enumerate(normed, 1)]

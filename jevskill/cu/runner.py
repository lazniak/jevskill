"""The operator: one complex command in, a sequence of Jev-driven sub-goals out.

The console's computer-use panel talks to this class and nothing else. It owns
the only thing the per-step loop (:func:`jevskill.cu.loop.run`) deliberately
does not have — an opinion about *what the user meant* — and it gets that
opinion from a model the user picks (:mod:`jevskill.cu.llm`), used **between**
Jev steps and never instead of them:

* **plan** — the command becomes 1-8 sub-goals, each one window or dialog.
* **compose** — when a step needs text and the goal does not quote it.
* **verify** — whether a sub-goal's ``done_when`` holds on this screen.
* **escalate** — when Jev is unsure, invalid twice, or blocked.
* **replan** — when a sub-goal ends without ``done``: finish, revise or give up.

Without a model (``model=None``) the whole command is one goal and the run is
Jev-only: quoted text still types, nothing else is generated, and a step that
needs language ends the run with a reason instead of a guess.

**Stopping is the first feature, not the last.** A :class:`KillSwitch` is armed
for the duration of a run and checked *twice per step* — in the ``observe`` hook
before deciding and in the ``execute`` hook before touching the desktop — so the
STOP button, ``Ctrl+Alt+Esc``, the mouse in the top-left corner and
``jevskill cu stop`` all take effect before the next action, whatever the loop is
doing. The loop itself is unchanged: a tripped switch raises :class:`Stopped`
inside a hook, the loop records ``stop_reason="error"`` with that name, and this
module reports the run as **stopped**.

**A destructive step waits for a human.** The loop's ``confirm`` gate is wired
to a pending question the page polls; nothing happens until someone clicks
*allow*, and a switch tripped while waiting answers *deny*.

**Dry run.** ``dry_run=True`` plans for real (the model is called) and then
*simulates* the steps — synthetic records, no desktop, no Jev spend — so the
whole panel can be exercised on a machine that must not be touched. Every
simulated event says so.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..errors import JevConfigError
from ..stats import Ledger, ledger_path
from . import act as act_module
from .agenda import (MAX_CHILDREN, MAX_LEAVES, MAX_PHASES, MAX_REPAIRS_PER_NODE, MAX_REPAIRS_TOTAL,
                     MAX_SAME_GOAL_FAILS, MIN_LEAF_S, MIN_PHASE_S, PHASE_SLACK, PlanNode,
                     child_prefix, coerce_tree, evaluate_checks, quote_on_screen,
                     repair_prefix)
from .contract import RunOptions, RunResult, StepRecord
from .experience import (Experience, Recipe, RecipeStep, Trajectory, command_key,
                         extract_slots, fill, goal_key, has_markers, identity_of,
                         literal_check, recipe_check, resolve)
from .hashing import tree_hash
from .journal import STALE_S, RunNotResumable, RunStore, UnknownRun, valid_run_id
from .killswitch import STOP_FILE_NAME, KillSwitch, Stopped
from .llm import LLMError, OpenRouterLLM, cached_models, pricing_table
from .reduce import candidates as reduce_candidates
from .types import Snapshot

CONSOLE_TITLE = "jev · console"
MAX_PLAN_STEPS = 8
MAX_REPLANS = 2
MAX_LLM_CALLS = 40
CONFIRM_TIMEOUT_S = 120.0
WINDOW_WAIT_S = 10.0
#: A key chord waits for this many seconds without user input before it
#: takes the foreground from whatever the user is doing.
USER_PAUSE_S = 0.8
LAUNCH_SETTLE_S = 1.5
LAUNCH_WAIT_S = 5.0
#: The model sees what the loop saw. 60 hid a Save As dialog's file-name
#: field from every escalation and re-plan (measured live, 2026-09-21).
ELEMENTS_FOR_LLM = 150
EVENT_LIMIT = 2000
#: Candidates the loop keeps before falling back to the two-stage region
#: cascade. The loop's own default is 60, tuned on Notepad and Calculator; a
#: Windows Save As dialog reduces to ~90 interactive controls, and at 60 the
#: reading-order cut dropped exactly the file-name field and the Save button
#: while the region stage found "no region holds the control" (measured
#: 2026-09-21, every step of the dialog goal ended `unknown_op`). At 150 the
#: dialog is one decision of ~1,400 state tokens — cents — and the cascade is
#: kept for windows that really are large.
OPERATOR_CAP = 150
#: The observe budget: the same dialog took 634 ms to walk once and 448 ms the
#: next time; the loop's default of 600 ms marks that `over_budget`.
OBSERVE_BUDGET_MS = 1500.0
#: How often a launch looks for the new window. The old flow slept a fixed
#: 1.5 s first and then waited for the user's pause before switching to it;
#: a window read by handle needs neither, only to be found.
LAUNCH_POLL_S = 0.1
#: A replayed step whose recorded outcome was a new window (a dialog) gets this
#: long to show it. Settle's usual 800 ms called a dialog that paints in about
#: a second "unchanged" and would have failed a correct recipe.
REPLAY_NEW_WINDOW_MS = 2000.0
#: A mission's planning-model allowance. A flat run keeps MAX_LLM_CALLS; each
#: phase of a tree adds its expansion and a little recovery, up to a ceiling a
#: runaway plan cannot talk its way past. Design bounds, not measurements.
LLM_CAP_CEIL = 200
LLM_CAP_PER_PHASE = 6
#: A live run rewrites its checkpoint at least this often, so another console
#: can tell a run that is working (fresh heartbeat) from one whose process died.
HEARTBEAT_S = 5.0
#: A pause longer than this ends the run as stopped; it stays resumable.
PAUSE_MAX_S = 1800.0
#: Checkpoint directories kept; the oldest go when a run starts.
RUNS_KEPT = 50

#: Programs a plan may start without asking. Anything else goes through the
#: same confirm gate as a destructive click — a model that decides to launch
#: ``powershell`` is exactly the case the gate exists for.
LAUNCH_ALLOW = frozenset({
    "notepad", "notepad.exe", "calc", "calc.exe", "mspaint", "mspaint.exe",
    "explorer", "explorer.exe", "wordpad", "wordpad.exe", "write", "write.exe",
    "snippingtool", "snippingtool.exe", "control", "control.exe",
})

ESCALATION_OPS = frozenset({"click", "type", "select", "scroll_up", "scroll_down",
                            "key", "wait"})

#: The break-down call: one phase into leaves, from the screen the previous
#: goal left. It must not contain "plan desktop tasks" (the planning prompt's
#: marker) — fakes and logs tell the two families apart by it.
EXPAND_SYSTEM = (
    "You break one phase of a long desktop task into sub-goals for a UI agent on Windows "
    "that sees UI Automation trees (roles, names, values), not pixels, one action per step. "
    "Return 1-8 children that finish THIS phase starting from `screen`. Same rules as "
    "planning: each leaf one short English imperative inside a single window or dialog, "
    "observable `done_when`, text to type in the user's language inside double quotes "
    "verbatim, `launch` only when a program must start, full paths in file dialogs, "
    "visible controls and menus over key chords. A child may be `\"kind\": \"phase\"` "
    "(with `done_when`, no children) only when `depth` < 3 and it clearly needs more than 8 "
    "actions. `lessons` are earlier failures here; reuse `known_goals` wording verbatim when "
    "one fits. Never add delete/send/pay/overwrite steps the command does not ask for. "
    "Reply with JSON only: {\"steps\": [...], \"note\": \"\"}")
#: The tree repair: only the failed scope's remaining work may change. It says
#: "ended without" like the flat re-plan, so an older fake answers give_up.
REPAIR_SYSTEM = (
    "Part of a long desktop task ended without finishing. Only the remaining work inside "
    "`scope` may change; completed siblings stay done. If the failed item's end state is "
    "already visible on `screen`, answer completed=true and copy into `quote` a string that "
    "appears verbatim in the screen JSON and proves it. Otherwise return in `steps` the "
    "remaining children of `scope`, revised to continue from THIS screen (same rules as "
    "planning). Never repeat a goal listed in `failed_twice`. skip=true only if the failed "
    "item is optional. escalate_up=true when `scope` itself is wrong and the level above "
    "must re-plan. ask_user: one short question when a person must act first (log in, pick "
    "a file). give_up: the reason when continuing is unsafe. interrupted=true means the run "
    "stopped or crashed while the item ran and it had acted `acted` times: never redo "
    "typing or saving the screen shows already happened. Reply with JSON only: "
    "{\"completed\": false, \"quote\": \"\", \"steps\": [], \"skip\": false, "
    "\"escalate_up\": false, \"ask_user\": null, \"give_up\": null}")

_QUOTED = re.compile(r'"([^"\n]{1,400})"|„([^”\n]{1,400})”|«([^»\n]{1,400})»|`([^`\n]{1,400})`')


class OperatorBusy(RuntimeError):
    """A run is already active; this class runs one at a time."""


class OperatorUnavailable(RuntimeError):
    """A live run cannot happen here (not Windows, no ``comtypes``)."""


class NotRunning(LookupError):
    """``pause`` was asked with no active run."""


@dataclass
class PlanStep:
    """One goal the loop drives — a leaf of the plan tree, in the flat form the
    panel and the ledger have always read (``run.plan``)."""

    goal: str
    done_when: str = ""
    launch: Optional[str] = None
    status: str = "pending"
    stop_reason: str = ""
    note: str = ""
    steps: int = 0
    tokens_in: int = 0
    cost_usd: float = 0.0
    wall_ms: float = 0.0
    #: Where the goal sits in the tree: its node, depth, and its phase's goal
    #: ("" for a goal directly under the command).
    node_id: str = ""
    depth: int = 1
    phase: str = ""
    #: What proved it done: code, replay, jev, model, children or user.
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "done_when": self.done_when, "launch": self.launch,
                "status": self.status, "stop_reason": self.stop_reason, "note": self.note,
                "steps": self.steps, "tokens_in": self.tokens_in,
                "cost_usd": round(self.cost_usd, 8), "wall_ms": round(self.wall_ms, 1),
                "node_id": self.node_id, "depth": self.depth, "phase": self.phase,
                "evidence": self.evidence}

    @classmethod
    def from_dict(cls, data: Dict[str, Any], node: Any = None) -> "PlanStep":
        """Rebuild from a checkpoint; the node's own fields win when given."""
        data = data if isinstance(data, dict) else {}
        step = cls(goal=str(data.get("goal") or getattr(node, "goal", "") or ""),
                   done_when=str(data.get("done_when") or getattr(node, "done_when", "") or ""),
                   launch=data.get("launch", getattr(node, "launch", None)),
                   status=str(data.get("status") or "pending"),
                   stop_reason=str(data.get("stop_reason") or ""),
                   note=str(data.get("note") or ""), steps=int(data.get("steps") or 0),
                   tokens_in=int(data.get("tokens_in") or 0),
                   cost_usd=float(data.get("cost_usd") or 0.0),
                   wall_ms=float(data.get("wall_ms") or 0.0),
                   node_id=str(data.get("node_id") or ""), depth=int(data.get("depth") or 1),
                   phase=str(data.get("phase") or ""), evidence=str(data.get("evidence") or ""))
        if node is not None:
            step.goal, step.done_when, step.launch = node.goal, node.done_when, node.launch
        return step


def _step_for(node: Any) -> PlanStep:
    """``make_step`` for :func:`coerce_tree`: a leaf's legacy view."""
    return PlanStep(goal=node.goal, done_when=node.done_when, launch=node.launch,
                    node_id=node.id, depth=node.depth)


def _stem(name: str) -> str:
    stem = os.path.basename(str(name or "")).strip().casefold()
    return stem[:-4] if stem.endswith(".exe") else stem


@dataclass
class Run:
    id: str
    command: str
    model: Optional[str]
    dry_run: bool = False
    max_steps: int = 25
    budget_s: float = 90.0
    total_budget_s: float = 600.0
    usd_cap: float = 0.5
    state: str = "planning"
    plan: List[PlanStep] = field(default_factory=list)
    current: int = -1
    events: List[Dict[str, Any]] = field(default_factory=list)
    spend: Dict[str, Any] = field(default_factory=lambda: {
        "jev_calls": 0, "jev_tokens": 0, "jev_usd": 0.0,
        "llm_calls": 0, "llm_tokens": 0, "llm_usd": 0.0, "usd": 0.0})
    pending_confirm: Optional[Dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    stop_reason: str = ""
    error: str = ""
    replans: int = 0
    #: Use and feed the operator's memory (:mod:`jevskill.cu.experience`).
    memory: bool = True
    #: Where the plan came from: ``memory``, ``model``, ``user`` or ``command``.
    plan_source: str = ""
    #: What memory did for this run, counted rather than claimed.
    learned: Dict[str, int] = field(default_factory=lambda: {
        "replayed_goals": 0, "replayed_steps": 0, "replay_failures": 0,
        "code_verified": 0, "memory_answers": 0, "recipes_learned": 0,
        "lessons_learned": 0})
    #: The plan as a tree (:mod:`jevskill.cu.agenda`); ``plan`` is its leaves.
    tree: Optional[PlanNode] = None
    #: May the planner answer with phases that are broken down later.
    hierarchical: bool = True
    llm_cap: int = MAX_LLM_CALLS
    llm_cap_explicit: bool = False
    max_leaves: int = MAX_LEAVES
    repairs_total: int = 0
    #: Active time: pauses are excluded, earlier segments carried over.
    paused_s: float = 0.0
    paused_since: float = 0.0
    active_s_before: float = 0.0
    #: 1 for a fresh run, +1 per resume.
    segment: int = 1
    #: The index of ``events[0]``: the in-memory list is a ring, the full log
    #: is ``events.jsonl`` in the run's checkpoint directory.
    events_base: int = 0
    #: Which store a remembered plan came from: ``flat`` or ``tree``.
    plan_memory: str = ""
    heartbeat_at: float = 0.0
    #: Process and title of the window, for re-pinning after a resume.
    target_hint: Dict[str, Any] = field(default_factory=dict)
    #: Checkpointed and journalled (a real run, not a planning preview).
    persist: bool = False

    @property
    def active(self) -> bool:
        return self.state in ("planning", "waiting_window", "running", "waiting_confirm",
                              "paused")

    def active_seconds(self, now: Optional[float] = None) -> float:
        """Seconds of work so far, across resumes, pauses excluded."""
        now = self.ended_at or (time.time() if now is None else now)
        paused = self.paused_s + ((now - self.paused_since) if self.paused_since else 0.0)
        return self.active_s_before + max(0.0, now - self.started_at - paused)

    def progress(self) -> Dict[str, Any]:
        root = self.tree
        evidence = {k: 0 for k in ("code", "replay", "jev", "model", "children", "user")}
        out: Dict[str, Any] = {"phases_done": 0, "phases_total": 0, "leaves_done": 0,
                               "leaves_known": 0, "unexpanded_phases": 0, "current_path": [],
                               "evidence": evidence,
                               "active_s": round(self.active_seconds(), 1),
                               "paused_s": round(self.paused_s, 1)}
        if root is None:
            return out
        for node in root.walk():
            if node is root or node.superseded:
                continue
            if node.kind == "phase":
                out["phases_total"] += 1
                out["phases_done"] += node.status == "done"
                out["unexpanded_phases"] += not node.expanded
            else:
                out["leaves_known"] += 1
                out["leaves_done"] += node.status == "done"
            if node.status == "done" and node.evidence in evidence:
                evidence[node.evidence] += 1
        current = self._current_node()
        if current is not None:
            out["current_path"] = [n.goal for n in current.path()[1:]]
        return out

    def _current_node(self) -> Optional[PlanNode]:
        if self.tree is None:
            return None
        running = [n for n in self.tree.walk() if n.status == "running" and n is not self.tree]
        return running[-1] if running else None

    def to_dict(self, since: int = 0) -> Dict[str, Any]:
        since = max(0, int(since))
        events = self.events[max(0, since - self.events_base):]
        current = self._current_node()
        now = time.time()
        return {
            "run_id": self.id, "command": self.command, "model": self.model,
            "dry_run": self.dry_run, "state": self.state,
            "plan": [step.to_dict() for step in self.plan], "current": self.current,
            "events": events, "next": self.events_base + len(self.events),
            "events_base": self.events_base, "events_truncated": since < self.events_base,
            "pending_confirm": self.pending_confirm,
            "spend": {k: (round(v, 8) if isinstance(v, float) else v)
                      for k, v in self.spend.items()},
            "started_at": self.started_at, "ended_at": self.ended_at,
            "elapsed_s": round(self.active_s_before + (self.ended_at or now) - self.started_at, 1),
            "stop_reason": self.stop_reason, "error": self.error,
            "limits": {"max_steps": self.max_steps, "budget_s": self.budget_s,
                       "total_budget_s": self.total_budget_s, "usd_cap": self.usd_cap,
                       "max_llm_calls": self.llm_cap, "max_leaves": self.max_leaves},
            "memory": dict(self.learned, enabled=self.memory, plan_source=self.plan_source),
            "tree": self.tree.to_dict() if self.tree is not None else None,
            "current_node": current.id if current is not None else None,
            "progress": self.progress(), "paused": self.state == "paused",
            "heartbeat_age_s": round(now - self.heartbeat_at, 1) if self.heartbeat_at else None,
            "segment": self.segment, "hierarchical": self.hierarchical,
        }


def platform_status() -> Dict[str, Any]:
    """Can a *live* run happen on this machine, and if not, why."""
    if sys.platform != "win32":
        return {"ok": False, "reason": "computer use drives Windows UI Automation; "
                                       "this is %s" % sys.platform}
    if importlib.util.find_spec("comtypes") is None:
        return {"ok": False, "reason": "comtypes is not installed: pip install \"jevskill[cu]\""}
    return {"ok": True, "reason": ""}


def quoted_text(goal: str) -> Optional[str]:
    """The one quoted string in a goal, or ``None`` when there is not exactly one."""
    found = [next(g for g in m.groups() if g is not None) for m in _QUOTED.finditer(goal or "")]
    return found[0] if len(found) == 1 else None


def _default_observe() -> Snapshot:
    from .observe import snapshot  # Windows-only; resolved per call

    return snapshot(budget_ms=OBSERVE_BUDGET_MS)


def _default_observe_window(hwnd: int) -> Snapshot:
    from .observe import snapshot  # Windows-only; resolved per call

    return snapshot(hwnd=hwnd, budget_ms=OBSERVE_BUDGET_MS)


def _default_active_popup(hwnd: int) -> int:
    """The window of ``hwnd``'s that is active: a dialog it opened, else itself."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        popup = int(user32.GetLastActivePopup(hwnd) or 0)
        return popup if popup and user32.IsWindowVisible(popup) else int(hwnd)
    except Exception:
        return int(hwnd)


def _default_idle_seconds() -> float:
    """Seconds since the user's last key or mouse event (``GetLastInputInfo``)."""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return float("inf")
        elapsed = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0
    except Exception:
        return float("inf")


def _default_foreground_title() -> str:
    from .observe import foreground_hwnd, window_title

    return window_title(foreground_hwnd())


def _default_foreground_hwnd() -> int:
    from .observe import foreground_hwnd

    return int(foreground_hwnd())


def _default_window_title(hwnd: int) -> str:
    from .observe import window_title

    return str(window_title(int(hwnd)) or "")


def _default_top_windows() -> set:
    """Handles of the visible, titled top-level windows — before/after a launch."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return set(found)


def _default_window_pid(hwnd: int) -> int:
    from .observe import window_pid

    return int(window_pid(int(hwnd)) or 0)


def _default_window_process(hwnd: int) -> str:
    """Executable name owning ``hwnd`` (``Notepad.exe``), or ``""``."""
    from .observe import process_name, window_pid

    try:
        return str(process_name(window_pid(hwnd)) or "")
    except Exception:
        return ""


def _default_bring_to_front(hwnd: int) -> bool:
    """Ask Windows to put ``hwnd`` in front; True when it is there afterwards.

    A process that is not the foreground process is normally refused
    ``SetForegroundWindow`` — the launched window flashes in the taskbar instead.
    ``SwitchToThisWindow`` is the documented-as-legacy call that still switches
    in that case; ``SetForegroundWindow`` follows for the cases where it is
    allowed, and a minimised window is restored first. The caller verifies.
    """
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    hwnd = int(hwnd)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    try:
        user32.SwitchToThisWindow(hwnd, True)
    except Exception:
        pass
    user32.SetForegroundWindow(hwnd)
    if int(user32.GetForegroundWindow()) == hwnd:
        return True
    # Refused. Attach to the foreground window's input thread, which makes this
    # thread count as "the process that owns the foreground" for the duration,
    # then ask again. Measured 2026-09-21: from the console's own process the
    # plain call above was refused every time; this one was not.
    front = user32.GetForegroundWindow()
    front_thread = user32.GetWindowThreadProcessId(front, None) if front else 0
    ours = kernel32.GetCurrentThreadId()
    attached = bool(front_thread and front_thread != ours
                    and user32.AttachThreadInput(ours, front_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, front_thread, False)
    return int(user32.GetForegroundWindow()) == hwnd


def _default_launcher(target: str) -> None:
    if ":" in target and not os.path.exists(target):  # a URI such as ms-settings:
        os.startfile(target)  # type: ignore[attr-defined]  # Windows only
        return
    subprocess.Popen([target], close_fds=True)


class _GuardedBackend:
    """The desktop backend, callable only from inside ``Operator._execute_hook``.

    The hook is where the kill switch, the foreground rule and the action
    counter live. A tree of plans adds many code paths that *think* about the
    desktop — expansion, repair, phase acceptance, resume — and none of them
    may act on it; this proxy makes that a runtime error instead of a code
    review note. Attribute lookups pass through, so ``hasattr`` stays truthful.
    """

    __slots__ = ("_inner", "_operator")

    def __init__(self, inner: Any, operator: "Operator") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_operator", operator)

    def __getattr__(self, name: str) -> Any:
        value = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(value):
            return value
        operator = object.__getattribute__(self, "_operator")

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if operator._acting <= 0:
                raise RuntimeError("desktop action outside Operator._execute_hook: %s" % name)
            return value(*args, **kwargs)

        return guarded


class Operator:
    """One instance per console process. See the module docstring."""

    def __init__(self, *, ledger_root: Any = None,
                 client_getter: Optional[Callable[[], Any]] = None,
                 llm_factory: Optional[Callable[[str], Any]] = None,
                 run_loop: Optional[Callable[..., RunResult]] = None,
                 observe: Optional[Callable[[], Snapshot]] = None,
                 backend_factory: Optional[Callable[[], Any]] = None,
                 foreground_title: Optional[Callable[[], str]] = None,
                 foreground_hwnd: Optional[Callable[[], int]] = None,
                 top_windows: Optional[Callable[[], set]] = None,
                 window_pid: Optional[Callable[[int], int]] = None,
                 window_process: Optional[Callable[[int], str]] = None,
                 bring_to_front: Optional[Callable[[int], bool]] = None,
                 launcher: Optional[Callable[[str], Any]] = None,
                 platform_check: Optional[Callable[[], Dict[str, Any]]] = None,
                 kill_switch: Optional[KillSwitch] = None,
                 ledger: Any = None,
                 observe_window: Optional[Callable[[int], Snapshot]] = None,
                 active_popup: Optional[Callable[[int], int]] = None,
                 idle_seconds: Optional[Callable[[], float]] = None,
                 window_title: Optional[Callable[[int], str]] = None,
                 experience: Optional[Experience] = None) -> None:
        self.ledger_root = ledger_root
        self._client_getter = client_getter
        self._llm_factory = llm_factory or self._default_llm
        self._run_loop = run_loop
        self._observe = observe or _default_observe
        # The pinned window is read by handle, so a run keeps working while the
        # user reads the console. A fake `observe` without a window reader
        # keeps the foreground-only path.
        self._observe_window = observe_window or (
            _default_observe_window if observe is None else None)
        self._active_popup = active_popup or _default_active_popup
        self._idle_seconds = idle_seconds or _default_idle_seconds
        self._backend_factory = backend_factory or (lambda: act_module.UiaBackend())
        self._foreground_title = foreground_title or _default_foreground_title
        self._foreground_hwnd = foreground_hwnd or _default_foreground_hwnd
        self._top_windows = top_windows or _default_top_windows
        self._window_pid = window_pid or _default_window_pid
        self._window_process = window_process or _default_window_process
        #: The window a run works in: pinned after a launch or after the user
        #: brought it to the front. It is observed by handle (a dialog it
        #: opened counts), in front or not. Only synthetic input — a key chord,
        #: typed text without a ValuePattern, a click by point — needs it in
        #: front: then it waits for the user's hands to pause, brings the
        #: window back, and refuses to act if Windows will not give it.
        self._target: Dict[str, int] = {"hwnd": 0, "pid": 0}
        self._abort_goal: str = ""
        self._exec_failures: int = 0
        self._bring_to_front = bring_to_front or _default_bring_to_front
        self._launcher = launcher or _default_launcher
        self._platform_check = platform_check or platform_status
        stop_file = ledger_path(ledger_root).parent / STOP_FILE_NAME
        self.kill = kill_switch or KillSwitch(stop_file)
        self._ledger = ledger
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._confirm_event = threading.Event()
        self._confirm_answer: Optional[bool] = None
        self.run: Optional[Run] = None
        self._verify_cache: Dict[str, bool] = {}
        self._window_title = window_title or _default_window_title
        #: What the operator has learned (see :mod:`jevskill.cu.experience`).
        #: Next to the ledger when a root is named — tests get a private one —
        #: else machine-wide, like the macro cache.
        self.experience = experience if experience is not None else \
            Experience.for_ledger_root(ledger_root)
        #: The current goal's actions and their observed effect.
        self._trajectory: Optional[Trajectory] = None
        #: Where the last composed text came from, so a recipe knows whether
        #: it may be replayed literally (a quote) or must be written again.
        self._text_source: str = ""
        self._text_value: Optional[str] = None
        #: The window the user had in front before a key chord took it; given
        #: back when the run ends if the foreground is still the agent's.
        self._user_front: int = 0
        self._goal_replayed: bool = False
        #: Checkpoints and full event logs, one directory per run, next to the
        #: ledger (and the stop file) so `jevskill cu` and the console share them.
        self._journal = RunStore(ledger_path(ledger_root).parent / "cu_runs")
        #: The tree node whose work is under way (spend rolls up its path).
        self._current_node: Optional[PlanNode] = None
        #: What proved the current goal done, set by the hook that proved it.
        self._goal_evidence: str = ""
        #: The last observation, reused by an expansion instead of a new walk.
        self._last_snapshot: Any = None
        #: >0 while ``_execute_hook`` is acting; ``_GuardedBackend`` checks it.
        self._acting: int = 0
        self._pause_requested: bool = False
        self._resume_event = threading.Event()
        self._checkpoint_warned: bool = False
        self._last_checkpoint: float = 0.0
        self._runs_cache: Any = (0.0, [])
        #: The interrupted goal a resume decided to run again: its launch is
        #: skipped when the program's window is already there.
        self._resumed_leaf: str = ""

    # ------------------------------------------------------------------ public
    @property
    def busy(self) -> bool:
        """A run is active — or its thread is still finishing it.

        The state turns terminal inside ``_finish``, before the final
        checkpoint is written and before ``_main`` disarms and resets the kill
        switch. Reporting idle in that gap let a resume read the previous
        checkpoint (active, fresh heartbeat) and refuse it, and let a new run's
        armed switch be reset by the old thread (14 of 15 resumes refused in a
        repro that resumed the moment ``busy`` turned False).
        """
        run = self.run
        if run is not None and run.active:
            return True
        thread = self._thread
        return bool(thread is not None and thread.is_alive()
                    and thread is not threading.current_thread())

    def _settle_previous(self, timeout: float = 5.0) -> None:
        """Let a run that has ended finish its bookkeeping before a new one
        starts; never called with the lock held (``_finish`` takes it)."""
        thread = self._thread
        run = self.run
        if thread is not None and thread is not threading.current_thread() and                 thread.is_alive() and (run is None or not run.active):
            thread.join(timeout)

    def status(self, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            payload: Dict[str, Any] = {"state": "idle", "run": None, "events": [], "next": 0}
            if self.run is not None:
                payload = self.run.to_dict(since)
                payload["run"] = self.run.id
            payload["busy"] = self.busy
            payload["platform"] = self._platform_check()
            payload["kill_switch"] = self.kill.describe()
            if not payload["busy"]:
                payload["resumable"] = self._cached_runs()
            return payload

    def plan(self, command: str, model: Optional[str], *, memory: bool = True,
             hierarchical: bool = True) -> Dict[str, Any]:
        """Plan without running. Touches nothing; calls the model unless the
        command's plan is remembered (``memory=False`` asks the model anyway).

        ``steps`` are the command's direct children as plan items: a leaf is
        today's step dict plus ``kind``; a phase carries its ``children`` once
        broken down, so sending the preview back as ``plan`` keeps the tree.
        """
        command = (command or "").strip()
        if not command:
            raise ValueError("the command is empty")
        if not self.busy:
            # A STOP that ended the previous run must not veto this planning
            # call: the switch is armed per run, and between runs it is silent.
            self.kill.reset()
        remembered = self._remembered_plan(command, memory, hierarchical)
        llm = self._make_llm(model) if remembered is None else None
        scratch = Run(id="plan", command=command, model=self._model_id(model),
                      memory=bool(memory), hierarchical=bool(hierarchical))
        root, note = self._build_tree(scratch, llm)
        return {"steps": self._preview(root), "note": note,
                "model": scratch.model, "spend": scratch.spend,
                "events": scratch.events, "source": scratch.plan_source,
                "tree": root.to_dict()}

    def start(self, command: str, model: Optional[str] = None, *, dry_run: bool = False,
              plan: Optional[Sequence[Any]] = None, max_steps: int = 25,
              budget_s: float = 90.0, total_budget_s: float = 600.0,
              usd_cap: float = 0.5, memory: bool = True, hierarchical: bool = True,
              max_llm_calls: Optional[int] = None, max_leaves: int = MAX_LEAVES) -> str:
        command = (command or "").strip()
        if not command:
            raise ValueError("the command is empty")
        self._settle_previous()
        with self._lock:
            if self.busy:
                raise OperatorBusy("a run is already active: %s" % self.run.id)  # type: ignore[union-attr]
            if not dry_run:
                platform = self._platform_check()
                if not platform["ok"]:
                    raise OperatorUnavailable(platform["reason"])
                self._client()  # raises JevConfigError when there is no key
            llm = self._make_llm(model)  # raises JevConfigError when no OpenRouter key
            run = Run(id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
                      command=command, model=self._model_id(model), dry_run=bool(dry_run),
                      max_steps=max(1, int(max_steps)), budget_s=max(5.0, float(budget_s)),
                      total_budget_s=max(10.0, float(total_budget_s)),
                      usd_cap=max(0.0, float(usd_cap)), memory=bool(memory),
                      hierarchical=bool(hierarchical),
                      max_leaves=min(200, max(1, int(max_leaves or MAX_LEAVES))))
            if max_llm_calls is not None:
                run.llm_cap = min(LLM_CAP_CEIL, max(1, int(max_llm_calls)))
                run.llm_cap_explicit = True
            if plan:
                run.tree = self._user_tree(run, plan)
                run.plan_source = "user"
                self._sync_plan(run)
            run.persist = True
            self.run = run
            self._reset_run_state()
            try:
                self._journal.prune(RUNS_KEPT)
            except Exception:
                pass
            self._thread = threading.Thread(target=self._main, args=(run, llm),
                                            name="jev-operator", daemon=True)
            self._thread.start()
            return run.id

    def stop(self, reason: str = "STOP button") -> Dict[str, Any]:
        if not self.busy:
            # Nothing to stop. Tripping the switch anyway would leave it set
            # until the next run arms it, and `plan()` would then refuse with
            # "Stopped: STOP button" — which is how the panel's plan button
            # first answered 500.
            return self.status()
        self.kill.trigger(reason)
        self._confirm_answer = False
        self._confirm_event.set()
        self._resume_event.set()
        return self.status()

    def pause(self, flag: bool = True) -> Dict[str, Any]:
        """Hold the run at the next goal boundary (``flag=False`` continues).

        Never inside a goal: a half-typed field or an open dialog is exactly
        the state a pause must not leave behind. The kill switch stays armed.
        """
        with self._lock:
            if not self.busy:
                raise NotRunning("no run is active")
            self._pause_requested = bool(flag)
            if flag:
                self._resume_event.clear()
            else:
                self._resume_event.set()
        return self.status()

    def runs(self) -> List[Dict[str, Any]]:
        """Runs that can be resumed: stopped, interrupted (their process went
        away mid-run), or failed with work left. Never a finished one."""
        live = self.run.id if (self.run is not None and self.busy) else None
        try:
            return self._journal.resumable(live_run_id=live)
        except Exception:
            return []

    def resume(self, run_id: str, model: Optional[str] = None, *,
               total_budget_s: Optional[float] = None, usd_cap: Optional[float] = None,
               max_llm_calls: Optional[int] = None) -> str:
        """Continue a checkpointed run where it left off, in this process.

        Refused (:class:`RunNotResumable`) when the run is done, or when its
        checkpoint is fresh and its state active — it may be alive in another
        console. Nothing is ever focused on resume: the window is re-pinned by
        handle, else by process and title, else the user is asked to click it.
        A goal that had already acted is not simply rerun (see ``_resume_leaf``).
        Limits come from the checkpoint; the arguments override them.
        """
        run_id = valid_run_id(run_id)
        self._settle_previous()
        with self._lock:
            if self.busy:
                raise OperatorBusy("a run is already active: %s" % self.run.id)  # type: ignore[union-attr]
            saved = self._journal.load(run_id)
            state = str(saved.get("state") or "")
            if state == "done":
                raise RunNotResumable("run %s finished; there is nothing to resume" % run_id)
            beat = float(saved.get("heartbeat_at") or 0.0)
            if state in ("planning", "waiting_window", "running", "waiting_confirm", "paused") \
                    and time.time() - beat <= STALE_S:
                raise RunNotResumable("run %s wrote a checkpoint %.0f s ago and may still be "
                                      "running in another console" % (run_id, time.time() - beat))
            if not isinstance(saved.get("tree"), dict):
                raise RunNotResumable("run %s has no plan to resume" % run_id)
            dry_run = bool(saved.get("dry_run"))
            if not dry_run:
                platform = self._platform_check()
                if not platform["ok"]:
                    raise OperatorUnavailable(platform["reason"])
                self._client()
            model = model or saved.get("model")
            llm = self._make_llm(model)
            limits = saved.get("limits") or {}
            run = Run(id=run_id, command=str(saved.get("command") or ""),
                      model=self._model_id(model), dry_run=dry_run,
                      max_steps=max(1, int(limits.get("max_steps") or 25)),
                      budget_s=max(5.0, float(limits.get("budget_s") or 90.0)),
                      total_budget_s=max(10.0, float(total_budget_s if total_budget_s
                                                     else limits.get("total_budget_s") or 600.0)),
                      usd_cap=max(0.0, float(usd_cap if usd_cap is not None
                                             else limits.get("usd_cap", 0.5))),
                      memory=bool(saved.get("memory", True)),
                      hierarchical=bool(saved.get("hierarchical", True)),
                      max_leaves=int(limits.get("max_leaves") or MAX_LEAVES))
            run.llm_cap = int(limits.get("llm_cap") or MAX_LLM_CALLS)
            run.llm_cap_explicit = bool(limits.get("llm_cap_explicit"))
            if max_llm_calls is not None:
                run.llm_cap = min(LLM_CAP_CEIL, max(1, int(max_llm_calls)))
                run.llm_cap_explicit = True
            run.segment = int(saved.get("segment") or 1) + 1
            run.active_s_before = float(saved.get("active_s") or 0.0)
            for key, value in (saved.get("spend") or {}).items():
                if key in run.spend and isinstance(value, (int, float)):
                    run.spend[key] = value
            for key, value in (saved.get("learned") or {}).items():
                if key in run.learned and isinstance(value, int):
                    run.learned[key] = value
            run.repairs_total = run.replans = int(saved.get("repairs_total") or 0)
            run.plan_source = str(saved.get("plan_source") or "")
            run.plan_memory = str(saved.get("plan_memory") or "")
            run.events_base = int(saved.get("events_next") or 0)
            run.tree = PlanNode.from_dict(saved["tree"], step_loader=PlanStep.from_dict)
            open_nodes = self._reopen(run)
            target = saved.get("target") or {}
            self._target = {"hwnd": 0, "pid": 0}
            hwnd, pid = int(target.get("hwnd") or 0), int(target.get("pid") or 0)
            if not dry_run and hwnd and pid:
                try:
                    same = int(self._window_pid(hwnd) or 0) == pid and \
                        _stem(self._window_process(hwnd)) == _stem(target.get("process") or "")
                except Exception:
                    same = False
                if same:
                    self._target = {"hwnd": hwnd, "pid": pid}
            if not self._target["pid"]:
                run.target_hint = {"process": str(target.get("process") or ""),
                                   "title": str(target.get("title") or "")}
            self._sync_plan(run)
            run.persist = True
            self.run = run
            target_kept = dict(self._target)
            self._reset_run_state()
            self._target = target_kept
            self._event(run, "resume", "resuming run %s — segment %d, %d open item%s%s" % (
                run.id, run.segment, open_nodes, "" if open_nodes == 1 else "s",
                "" if run.dry_run else ("; the window is still there" if self._target["pid"]
                                        else "; the window will be found again")),
                {"segment": run.segment, "open": open_nodes})
            self._thread = threading.Thread(target=self._main, args=(run, llm),
                                            name="jev-operator", daemon=True)
            self._thread.start()
            return run.id

    def _reopen(self, run: Run) -> int:
        """Make a checkpointed tree runnable again; the number of open items.

        A leaf left ``running`` was interrupted mid-goal: it becomes
        ``interrupted`` and goes through ``_resume_leaf``. A failed item that
        ended the run is opened again — resuming is the user asking to retry —
        as ``interrupted`` when it had acted, ``pending`` when it had not.
        Phases still open restart their clock with what they had left.
        """
        root = run.tree
        assert root is not None
        root.status = "running"
        count = 0
        now = time.time()
        for node in root.walk():
            if node is root or node.superseded:
                continue
            if node.kind == "leaf":
                if node.status == "running":
                    node.status = "interrupted"
                elif node.status == "failed":
                    node.status = "interrupted" if node.acted else "pending"
                if node.status in ("pending", "interrupted", "stopped"):
                    count += 1
                if node.step is not None:
                    node.step.status = node.status if node.status in (
                        "done", "failed", "skipped") else "pending"
            else:
                if node.status == "failed":
                    node.status = "pending"
                if node.status == "running":
                    node.budget_s = max(MIN_PHASE_S, node.budget_s - float(
                        node.spend.get("active_s") or 0.0)) if node.budget_s else 0.0
                    node.started_at, node.paused_mark = now, 0.0
                if node.status in ("pending", "running"):
                    count += 1
        return count

    def _reset_run_state(self) -> None:
        """Per-run operator fields, reset by ``start`` and ``resume`` alike."""
        self._verify_cache = {}
        self._target = {"hwnd": 0, "pid": 0}
        self._abort_goal = ""
        self._exec_failures = 0
        self._agreed_done = ""
        self._trajectory = None
        self._user_front = 0
        self._goal_replayed = False
        self._current_node = None
        self._goal_evidence = ""
        self._last_snapshot = None
        self._acting = 0
        self._pause_requested = False
        self._resume_event.clear()
        self._checkpoint_warned = False
        self._last_checkpoint = 0.0
        self._runs_cache = (0.0, [])
        self._resumed_leaf = ""

    def _cached_runs(self) -> List[Dict[str, Any]]:
        stamp, rows = self._runs_cache
        if time.time() - stamp > 5.0:
            rows = self.runs()
            self._runs_cache = (time.time(), rows)
        return rows

    def confirm(self, allow: bool) -> Dict[str, Any]:
        with self._lock:
            run = self.run
            if run is None or run.pending_confirm is None:
                raise LookupError("nothing is waiting for confirmation")
            self._confirm_answer = bool(allow)
            self._confirm_event.set()
        return self.status()

    def wait(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------ the run body
    def _main(self, run: Run, llm: Any) -> None:
        self.kill.arm()
        try:
            self._execute_run(run, llm)
        except Stopped as exc:
            self._finish(run, "stopped", stop_reason=str(exc))
        except Exception as exc:  # pragma: no cover - defensive: the run must end
            self._finish(run, "failed", error="%s: %s" % (type(exc).__name__, exc))
        finally:
            self.kill.disarm()
            # The reason is already on the run; a set switch outliving its run
            # would veto the next `plan()`.
            self.kill.reset()
            ledger = self._ledger
            if ledger is not None:
                try:
                    ledger.flush()
                except Exception:
                    pass

    def _execute_run(self, run: Run, llm: Any) -> None:
        """One cursor over the plan tree, strictly sequential, one desktop.

        Only leaves act: every desktop effect below comes from ``_run_leaf``
        (the loop, or a replay, both through ``_execute_hook``) or a launch.
        Phases are broken down when the cursor reaches them, from the screen
        the previous goal left, and a failure is repaired in the scope that
        failed — its phase — before the level above is asked.
        """
        self._event(run, "start", "run %s — %s%s" % (
            run.id, "DRY RUN (simulated), " if run.dry_run else "",
            "model %s" % run.model if run.model else "Jev only, no planning model"))
        self._event(run, "kill_switch", self._kill_text())
        if run.tree is None:
            self._set_state(run, "planning")
            run.tree, note = self._build_tree(run, llm)
            if note:
                self._event(run, "plan_note", note)
        self._sync_plan(run)
        phases = [c for c in run.tree.children if c.kind == "phase"]
        self._event(run, "plan", "%d step%s%s" % (
            len(run.plan), "" if len(run.plan) == 1 else "s",
            (" in %d phase%s" % (len(phases), "" if len(phases) == 1 else "s")) if phases else ""),
            {"steps": [s.to_dict() for s in run.plan], "tree": run.tree.to_dict()})
        if phases and not run.llm_cap_explicit:
            run.llm_cap = min(LLM_CAP_CEIL, MAX_LLM_CALLS + LLM_CAP_PER_PHASE * len(phases))
        self._checkpoint(run, force=True)
        while True:
            self._check_budget(run)
            self._pause_point(run)
            node = run.tree.next_open()
            if node is None:
                break
            self._enter_chain(run, node)
            if node.kind == "phase":
                if not node.expanded:
                    self._expand(run, llm, node)
                    self._sync_plan(run)
                    self._checkpoint(run, force=True)
                    continue
                if not self._close_phase(run, llm, node):
                    if not self._repair_up(run, llm, node, None, reason=node.stop_reason):
                        return self._fail_run(run, node)
                    self._sync_plan(run)
                continue
            spent = self._phase_over_budget(run, node)
            if spent is not None:
                spent.status, spent.stop_reason = "failed", "phase_budget"
                self._event(run, "phase", "phase %s spent its %.0f s: %s" % (
                    spent.id, spent.budget_s, spent.goal[:80]), {"node": spent.id})
                if not self._repair_up(run, llm, spent, None, reason="phase budget spent"):
                    return self._fail_run(run, spent)
                self._sync_plan(run)
                continue
            if node.status in ("interrupted", "stopped"):
                verdict = self._resume_leaf(run, llm, node)
                if verdict in ("done", "handled"):
                    self._sync_plan(run)
                    continue
                if verdict == "failed":
                    return self._fail_run(run, node)
            result, started = self._run_leaf(run, llm, node)
            if node.status == "done":
                continue
            if not self._repair_up(run, llm, node, result, started=started):
                return self._fail_run(run, node)
            self._sync_plan(run)
        self._learn_plan(run)
        self._finish(run, "done")

    def _run_leaf(self, run: Run, llm: Any, node: PlanNode):
        """Drive one goal; ``(result, started)``. The node ends ``done`` or
        ``failed`` — a failed goal never asks for its own recovery; its
        parent's scope does (``_repair_up``)."""
        step = node.step
        if step is None:
            step = node.step = _step_for(node)
            self._sync_plan(run)
        index = run.plan.index(step) if step in run.plan else len(run.plan)
        run.current = index
        self._set_node(node, "running")
        node.acted = 0
        node.started_at, node.paused_mark = time.time(), run.paused_s
        self._current_node = node
        self._goal_evidence = ""
        self._set_state(run, "running")
        self._event(run, "goal", "goal %d/%d: %s" % (index + 1, len(run.plan), step.goal),
                    {"index": index, "node_id": node.id, "phase": step.phase})
        self._before_goal(run, node)
        self._trajectory = Trajectory(cap=OPERATOR_CAP)
        self._goal_replayed = False
        goal_started = time.perf_counter()
        result = self._run_goal(run, llm, step, index, budget_s=self._leaf_budget(run, node))
        step.stop_reason = result.stop_reason
        step.steps = len(result.steps)
        step.wall_ms = result.wall_ms
        step.tokens_in += result.tokens_in
        step.cost_usd += result.cost_usd
        node.stop_reason = result.stop_reason
        if result.stop_reason == "error" and result.error.startswith("Stopped"):
            raise Stopped(result.error.split(":", 1)[-1].strip() or self.kill.reason)
        if self.kill.event.is_set():
            # STOP that landed while the goal was waiting on a confirmation:
            # the gate answered "deny", the loop ended the goal as `blocked`,
            # and without this check a Jev-only run would go on to report
            # itself *failed* — the user pressed STOP, and that is the reason.
            raise Stopped(self.kill.reason or "stopped")
        agreed = self._agreed_done if result.stop_reason == "escalated" else ""
        node.ended_at = time.time()
        if result.stop_reason == "done" or agreed:
            evidence = "replay" if self._goal_replayed else \
                ("model" if agreed else (self._goal_evidence or "jev"))
            self._set_node(node, "done", evidence=evidence)
            if agreed:
                step.stop_reason = node.stop_reason = "done"
                step.note = node.note = agreed[:200]
            self._event(run, "goal_done", "goal %d done after %d step%s%s%s" % (
                index + 1, step.steps, "" if step.steps == 1 else "s",
                " from memory" if self._goal_replayed else "",
                (" — %s judged it complete: %s" % (run.model, agreed)) if agreed else ""),
                {"node_id": node.id, "evidence": evidence})
            self._learn_goal(run, step, True, goal_started)
            self._checkpoint(run, force=True)
            return result, goal_started
        step.note = node.note = (result.error or (result.steps[-1].note if result.steps else ""))[:200]
        trajectory = self._trajectory
        node.tried = list(trajectory.tried()) if trajectory is not None else []
        self._set_node(node, "failed")
        self._event(run, "goal_stalled", "goal %d ended: %s%s" % (
            index + 1, result.stop_reason, (" — " + step.note) if step.note else ""),
            {"node_id": node.id})
        return result, goal_started

    # ------------------------------------------------------------ the tree
    def _set_node(self, node: PlanNode, status: str, *, evidence: str = "") -> None:
        node.status = status
        if evidence:
            node.evidence = evidence
        if status in ("done", "failed", "skipped"):
            node.ended_at = node.ended_at or time.time()
        step = node.step
        if step is not None:
            step.status = status if status in ("pending", "running", "done", "failed",
                                               "skipped", "stopped") else "pending"
            if evidence:
                step.evidence = evidence

    def _new_root(self, run: Run) -> PlanNode:
        return PlanNode(id="0", goal=run.command, kind="phase", depth=0, expanded=True,
                        status="running", source=run.plan_source, started_at=time.time())

    def _sync_plan(self, run: Run) -> None:
        """``run.plan`` is the tree's leaves in order — the flat view the panel,
        the ledger and every earlier test read. A leaf remembers its phase."""
        if run.tree is None:
            return
        plan: List[PlanStep] = []
        for leaf in run.tree.leaves():
            if leaf.step is None:
                leaf.step = _step_for(leaf)
            step = leaf.step
            step.node_id, step.depth = leaf.id, leaf.depth
            parent = leaf.parent
            step.phase = parent.goal if parent is not None and parent is not run.tree else ""
            plan.append(step)
        run.plan = plan

    def _preview(self, root: PlanNode) -> List[Dict[str, Any]]:
        """The root's children as plan items (see ``plan``)."""
        out = []
        for child in root.children:
            if child.kind == "leaf" and child.step is not None:
                item = child.step.to_dict()
                item.update(kind="leaf", app=child.app, checks=[c.to_dict() for c in child.checks],
                            optional=child.optional, irreversible=child.irreversible)
            else:
                item = child.item()
            out.append(item)
        return out

    def _user_tree(self, run: Run, plan: Sequence[Any]) -> PlanNode:
        items = [item for item in plan if item]
        if not run.hierarchical:
            items = [self._flat_item(item) for item in items]
        try:
            children = coerce_tree(items, depth=1, id_prefix="", source="user",
                                   make_step=_step_for)
        except ValueError:
            raise ValueError("the supplied plan has no usable steps")
        root = self._new_root(run)
        root.source = "user"
        root.add_children(children)
        if len(root.leaves()) > run.max_leaves:
            raise ValueError("the supplied plan has %d goals; the limit is %d"
                             % (len(root.leaves()), run.max_leaves))
        return root

    @staticmethod
    def _flat_item(item: Any) -> Any:
        if isinstance(item, dict):
            return {"goal": item.get("goal"), "done_when": item.get("done_when"),
                    "launch": item.get("launch")}
        return item

    def _enter_chain(self, run: Run, node: PlanNode) -> None:
        """Start every pending ancestor (and the node, when a phase): clock,
        pause mark and — for a phase — its share of the time left."""
        entered = False
        for item in node.path():
            if item is run.tree:
                continue
            if item.kind == "phase" and item.status == "pending":
                item.status = "running"
                item.started_at, item.paused_mark = time.time(), run.paused_s
                if not item.budget_s:
                    item.budget_s = self._carve(run, item)
                entered = True
                self._event(run, "phase", "phase %s: %s%s" % (
                    item.id, item.goal[:120],
                    (" · %.0f s" % item.budget_s) if item.budget_s else ""),
                    {"node": item.id, "budget_s": round(item.budget_s, 1)})
        if entered:
            self._checkpoint(run, force=True)

    def _carve(self, run: Run, phase: PlanNode) -> float:
        """A phase's seconds: its weight's share of what its parent has left,
        with slack, never under MIN_PHASE_S and never over what is left."""
        parent = phase.parent
        remaining = self._remaining_s(run, parent)
        siblings = [c for c in (parent.children if parent is not None else [phase])
                    if not c.is_closed and not c.superseded]
        weights = sum(max(0.1, float(c.weight or 1.0)) for c in siblings) or 1.0
        share = remaining * max(0.1, float(phase.weight or 1.0)) / weights * PHASE_SLACK
        return max(0.0, min(remaining, max(MIN_PHASE_S, share)))

    def _node_active_s(self, run: Run, node: PlanNode) -> float:
        if not node.started_at:
            return 0.0
        return max(0.0, (time.time() - node.started_at) - (run.paused_s - node.paused_mark))

    def _remaining_s(self, run: Run, phase: Optional[PlanNode]) -> float:
        if phase is None or phase is run.tree or not phase.budget_s:
            if phase is not None and phase is not run.tree and phase.parent is not None:
                return self._remaining_s(run, phase.parent)
            return max(0.0, run.total_budget_s - run.active_seconds())
        return max(0.0, phase.budget_s - self._node_active_s(run, phase))

    def _phase_over_budget(self, run: Run, leaf: PlanNode) -> Optional[PlanNode]:
        node = leaf.parent
        while node is not None and node is not run.tree:
            if node.kind == "phase" and node.budget_s and \
                    self._node_active_s(run, node) > node.budget_s:
                return node
            node = node.parent
        return None

    def _leaf_budget(self, run: Run, leaf: PlanNode) -> float:
        parent = leaf.parent
        if parent is None or parent is run.tree:
            return run.budget_s
        inner = parent if parent.budget_s else None
        if inner is None:
            return run.budget_s
        return min(run.budget_s, max(MIN_LEAF_S, self._remaining_s(run, inner)))

    def _fail_run(self, run: Run, node: PlanNode) -> None:
        if node.kind == "leaf" and node.status not in ("failed", "interrupted"):
            self._set_node(node, "failed")
        self._forget_failed_plan(run)
        self._finish(run, "failed", stop_reason=node.stop_reason or "failed",
                     error=node.note or node.stop_reason or "failed")

    # ------------------------------------------------------------------ memory
    def _target_app(self) -> str:
        """The pinned window's executable, the key recipes are filed under."""
        hwnd = self._target.get("hwnd") or 0
        if not hwnd or not self._target.get("pid"):
            return ""
        try:
            return str(self._window_process(hwnd) or "").strip().casefold()
        except Exception:
            return ""

    def _learn_goal(self, run: Run, step: PlanStep, done: bool, started: float) -> None:
        """Keep what this goal taught: its recipe when it was done, and a
        lesson for every action that changed nothing or was refused.

        Only a real desktop teaches — a dry run's steps are simulated — and a
        run with memory off neither reads nor writes it.
        """
        trajectory, self._trajectory = self._trajectory, None
        if run.dry_run or not run.memory or trajectory is None:
            return
        trajectory.close()
        app = self._target_app()
        if not app:
            return
        try:
            lessons = self.experience.learn_lessons(app, step.goal, trajectory)
            run.learned["lessons_learned"] += lessons
            if not done:
                return
            wall_ms = (time.perf_counter() - started) * 1000.0
            recipe = self.experience.learn_recipe(
                app, step.goal, run.command, trajectory, wall_ms=wall_ms,
                launched=bool(step.launch), replayed=self._goal_replayed)
        except Exception as exc:  # a memory that cannot write must not end a run
            self._event(run, "memory", "could not record what goal %s taught: %s"
                        % (step.goal[:60], type(exc).__name__))
            return
        if recipe is None:
            return
        if self._goal_replayed:
            self._event(run, "memory", "recipe confirmed: %s (%d successes)" % (
                step.goal[:80], recipe.successes))
        else:
            run.learned["recipes_learned"] += 1
            self._event(run, "memory", "learned: %s — %s; next time it replays without "
                        "a decision" % (step.goal[:80], "; ".join(
                            s.describe() for s in recipe.steps[:4]) or "the launch alone"),
                        {"steps": [s.to_dict() for s in recipe.steps]})

    def _learn_plan(self, run: Run) -> None:
        """Keep the plan that finished: flat as before, or — when the command
        was split into phases — the tree of what finished, under its own key,
        so the same command next time skips the planning call and every
        expansion call."""
        if run.dry_run or not run.memory or run.plan_source == "command" or run.tree is None:
            return
        if any(c.kind == "phase" for c in run.tree.children if c.status == "done"):
            items = self._done_items(run.tree)
            if not items:
                return
            try:
                self.experience.learn_tree(run.command, items, model=run.model or "")
            except Exception:
                return
            if run.plan_source != "memory":
                self._event(run, "memory", "plan remembered as %d phase%s; the same command skips "
                                           "the planning and break-down calls next time" % (
                                               len(items), "" if len(items) == 1 else "s"))
            return
        done = [s.to_dict() for s in run.plan if s.status == "done"]
        if not done:
            return
        try:
            self.experience.learn_plan(run.command, done, model=run.model or "")
        except Exception:
            return
        if run.plan_source != "memory":
            self._event(run, "memory", "plan remembered: %d goal%s; the same command skips "
                        "the planning call next time" % (len(done), "" if len(done) == 1 else "s"))

    def _done_items(self, node: PlanNode) -> List[Dict[str, Any]]:
        """Plan items of the children that finished, in order, recursively."""
        out = []
        for child in node.children:
            if child.status != "done" or child.superseded:
                continue
            item = child.item()
            if child.kind == "phase":
                item["children"] = self._done_items(child)
            out.append(item)
        return out

    def _forget_failed_plan(self, run: Run) -> None:
        if run.dry_run or not run.memory or run.plan_source != "memory":
            return
        try:
            if run.plan_memory == "tree":
                self.experience.tree_failed(run.command)
            else:
                self.experience.plan_failed(run.command)
        except Exception:
            pass

    def _learn_decomposition(self, run: Run, phase: PlanNode) -> None:
        """A phase that closed teaches how it was broken down: any mission
        with the same phase (and another file name) expands it without a call."""
        if run.dry_run or not run.memory or phase is run.tree:
            return
        app = phase.app or self._target_app()
        children = self._done_items(phase)
        if not app or not children:
            return
        try:
            self.experience.learn_decomposition(app, phase.goal, children, model=run.model or "")
        except Exception:
            pass

    def _before_goal(self, run: Run, node: PlanNode, *, launch: bool = True) -> None:
        """Pin the goal's window, by handle and without taking the foreground.

        A launch pins the new window. A goal in another program's phase pins
        that program's window by process (and title, when the phase names
        one). Only when nothing is pinned yet does the run wait for the user
        to bring a window up — today's first-goal path.
        """
        step = node.step
        program = (step.launch if step is not None else node.launch) or ""
        if run.dry_run:
            if program:
                self._event(run, "launch", "would launch %s (dry run)" % program)
            return
        if program:
            if (not launch or node.id == self._resumed_leaf) and self._pin_app(run, program, ""):
                # A resumed goal that had launched already: its program's
                # window is there, so a second launch would open a second one.
                return
            if launch:
                self._launch(run, step if step is not None else _step_for(node))
            return
        phase = node.phase() if node.parent is not None else None
        app = node.app or (phase.app if phase is not None else "")
        if app and _stem(self._target_app()) != _stem(app):
            self._pin_app(run, app, self._title_hint(run, phase))
        elif not self._target["pid"] and run.target_hint.get("process"):
            self._pin_app(run, run.target_hint["process"], run.target_hint.get("title", ""))
        if not self._target["hwnd"] and not self._target["pid"]:
            self._wait_for_target_window(run)

    @staticmethod
    def _title_hint(run: Run, phase: Optional[PlanNode]) -> str:
        if phase is not None:
            quoted = extract_slots(phase.done_when or "")
            if quoted:
                return quoted[0]
        return str(run.target_hint.get("title") or "")

    def _pin_app(self, run: Run, app: str, hint: str) -> bool:
        """Pin a window of ``app`` already on the desktop — preferring one whose
        title holds ``hint`` — by handle. Never brings it to the front."""
        windows = self._windows_of(app, self._windows_or_empty())
        if not windows:
            return False
        chosen = windows[0]
        hint = (hint or "").strip().casefold()
        if hint:
            for hwnd in windows:
                if hint in self._title_of(hwnd).casefold():
                    chosen = hwnd
                    break
        self._pin(run, chosen)
        return True

    def _launch(self, run: Run, step: PlanStep) -> None:
        target = (step.launch or "").strip()
        allowed = target.lower() in LAUNCH_ALLOW or target.lower().startswith("ms-settings:")
        if not allowed:
            if not self._ask(run, "launch %s?" % target,
                             {"op": "launch", "target": target, "name": target}):
                raise Stopped("launch of %s refused" % target) if self.kill.event.is_set() \
                    else RuntimeError("launch refused: %s" % target)
        known = self._windows_or_empty()
        existing = {hwnd: self._title_of(hwnd) for hwnd in self._windows_of(target, known)}
        self._event(run, "launch", "launching %s" % target)
        # Through the execute hook like every other desktop effect: the kill
        # switch is checked right before, and an allow-listed launch — which
        # asks nobody — no longer skips that check.
        self._execute_hook(act_module.Action(op="launch", text=target, source="operator"), None)
        # The launched window is *found*, not switched to. It used to be
        # brought to the front, after a fixed 1.5 s sleep and a wait for the
        # user's hands to pause — measured live (2026-09-21), the user's next
        # two keystrokes landed in the new Notepad anyway. Since the pinned
        # window is read by handle, nothing needs it in front until an action
        # needs the keyboard, and that action waits for the pause itself.
        # A new top-level window first; failing that, a window the program
        # already had — Windows 11 Notepad is single-instance and answers a
        # second launch with a new tab — once its title shows the new tab.
        started = time.time()
        deadline = started + LAUNCH_WAIT_S
        found = 0
        while not found:
            self.kill.raise_if_tripped()
            current = self._windows_or_empty()
            fresh = sorted(current - known)
            if fresh:
                owned = self._windows_of(target, set(fresh))
                found = owned[0] if owned else fresh[0]
                break
            mine = self._windows_of(target, current)
            changed = [h for h in mine if h in existing and self._title_of(h) != existing[h]]
            if changed:
                found = changed[0]
                break
            if mine and time.time() - started >= LAUNCH_SETTLE_S:
                found = mine[0]
                break
            if time.time() >= deadline:
                break
            time.sleep(LAUNCH_POLL_S)
        if not found:
            raise RuntimeError("%s started but no window of it appeared within %.0f s; open it "
                               "yourself and start again without the launch"
                               % (target, LAUNCH_WAIT_S))
        self._event(run, "launch", "found %s's window in %.1f s — working in it by handle, "
                                   "without taking the foreground"
                    % (target, time.time() - started))
        self._pin(run, found)

    def _wait_for_target_window(self, run: Run) -> None:
        deadline = time.time() + WINDOW_WAIT_S
        warned = False
        while CONSOLE_TITLE in self._title_or_empty().lower():
            if not warned:
                self._set_state(run, "waiting_window")
                self._event(run, "waiting_window",
                            "the console is the foreground window — click the window the "
                            "agent should work in (%.0f s)" % WINDOW_WAIT_S)
                warned = True
            if time.time() >= deadline:
                raise RuntimeError("the console stayed in the foreground for %.0f s; "
                                   "bring the target window to the front and start again"
                                   % WINDOW_WAIT_S)
            self.kill.raise_if_tripped()
            time.sleep(0.25)
        self._pin(run, self._hwnd_or_zero())
        self._set_state(run, "running")

    def _pin(self, run: Run, hwnd: int) -> None:
        pid = 0
        try:
            pid = int(self._window_pid(hwnd) or 0) if hwnd else 0
        except Exception:
            pid = 0
        self._target = {"hwnd": int(hwnd or 0), "pid": pid}
        self._event(run, "window", "working in: %s" % (self._title_of(hwnd) or "?"),
                    {"hwnd": self._target["hwnd"], "pid": pid})
        self._checkpoint(run, force=True)

    def _title_of(self, hwnd: int) -> str:
        """A window's own title; the foreground's when it is that window and
        its own cannot be read (off Windows, in tests without a reader)."""
        title = ""
        try:
            title = str(self._window_title(int(hwnd)) or "") if hwnd else ""
        except Exception:
            title = ""
        if not title and hwnd and self._hwnd_or_zero() == int(hwnd):
            title = self._title_or_empty()
        return title

    def _ensure_target_in_front(self, what: str) -> None:
        """Before synthetic input: is the pinned app in front?

        The foreground may legitimately be another window of the same process
        (a Save As dialog), so the comparison is by pid. If the user switched
        away, the step waits for a pause in their typing, brings the window
        back, and if Windows refuses raises rather than type into their window.
        """
        target = self._target
        if not target["pid"]:
            return
        front = self._hwnd_or_zero()
        try:
            front_pid = int(self._window_pid(front) or 0) if front else 0
        except Exception:
            front_pid = 0
        if front_pid == target["pid"]:
            return
        if front and not self._user_front:
            # Whatever the user had in front goes back to them when the run
            # ends (`_give_back`), unless they have moved on by then.
            self._user_front = int(front)
        waited = self._wait_for_user_pause()
        switched = False
        try:
            switched = bool(self._bring_to_front(target["hwnd"]))
        except Exception:
            switched = False
        if self.run is not None:
            self._event(self.run, "refocus", "%s: the target window was not in front — %s%s" % (
                what, "brought back" if switched else "could not bring it back",
                (" after waiting %.1f s for the user to pause" % waited) if waited >= 0.3 else ""))
        if not switched:
            raise RuntimeError("the target window lost the foreground before the %s and could "
                               "not be brought back; not acting on %r" % (what, self._title_or_empty()))

    def _wait_for_user_pause(self) -> float:
        """Seconds waited until the user's hands paused (``USER_PAUSE_S`` with
        no input), at most ``WINDOW_WAIT_S``. Measured live (2026-09-21): the
        window was taken back three times in 40 s while the user typed in
        Discord, until Windows itself refused the fourth."""
        started = time.monotonic()
        while True:
            try:
                idle = float(self._idle_seconds())
            except Exception:
                idle = float("inf")
            if idle >= USER_PAUSE_S or time.monotonic() - started >= WINDOW_WAIT_S:
                return time.monotonic() - started
            self.kill.raise_if_tripped()
            time.sleep(0.1)

    def _title_or_empty(self) -> str:
        try:
            return str(self._foreground_title() or "")
        except Exception:
            return ""

    def _hwnd_or_zero(self) -> int:
        try:
            return int(self._foreground_hwnd() or 0)
        except Exception:
            return 0

    def _windows_or_empty(self) -> set:
        try:
            return set(self._top_windows() or ())
        except Exception:
            return set()

    def _windows_of(self, target: str, windows: set) -> List[int]:
        """Windows owned by the launched program, matched on the executable stem."""
        stem = os.path.basename(target).lower()
        stem = stem[:-4] if stem.endswith(".exe") else stem
        if not stem or ":" in stem:
            return []
        out = []
        for hwnd in sorted(windows):
            try:
                owner = self._window_process(hwnd).lower()
            except Exception:
                continue
            if owner == stem or owner == stem + ".exe":
                out.append(hwnd)
        return out

    def _run_goal(self, run: Run, llm: Any, step: PlanStep, index: int,
                  budget_s: Optional[float] = None) -> RunResult:
        options = RunOptions(max_steps=run.max_steps, budget_s=budget_s or run.budget_s,
                             dry_run=run.dry_run)
        self._abort_goal = ""
        self._exec_failures = 0
        self._agreed_done = ""
        # With memory, a Jev-only run still gets a verifier and an escalation
        # handler: code-only ones. The verifier answers True when the screen
        # proves the goal and abstains (None) otherwise; the handler answers
        # from a recipe or not at all. Neither ever calls a model.
        use_hooks = llm is not None or run.memory
        hooks = dict(
            task_id="%s:%d" % (run.id, index), run=0,
            observe=self._observe_hook, ledger=self._ledger_or_none(),
            on_step=functools.partial(self._on_step, run, step),
            confirm=functools.partial(self._confirm_hook, run),
            compose_text=functools.partial(self._compose_hook, run, llm, step),
            escalate=(functools.partial(self._escalate_hook, run, llm, step)
                      if use_hooks else None),
            verify=(functools.partial(self._verify_hook, run, llm, step)
                    if use_hooks else None),
        )
        if run.dry_run:
            return self._simulate(step.goal, options, **hooks)
        backend = _GuardedBackend(self._backend_factory(), self)
        replayed: List[StepRecord] = []
        recipe = self._recipe_for(run, step)
        if recipe is not None:
            outcome = self._replay(run, llm, step, recipe, backend)
            if outcome.stop_reason in ("done", "blocked"):
                return outcome
            # A step of the recipe did not hold: carry on from this screen the
            # ordinary way, with what the replay did already on the record.
            replayed = list(outcome.steps)
        if llm is not None:
            # The planning model composes the text a field needs, for that
            # field. Jev's post-type sanity check is for text nobody composed;
            # measured live (2026-09-21) it scored a correct
            # %USERPROFILE%\Desktop\hello.txt at 0.06 in a Save As file-name
            # field and cleared it. With a model, the check still runs and is
            # recorded, but never clears.
            from .decide import THRESHOLDS

            hooks["thresholds"] = dict(THRESHOLDS, text_sanity=0.0)
        loop = self._run_loop
        if loop is None:
            from .loop import run as loop  # type: ignore[no-redef]
        result = loop(step.goal, options, client=self._client(), cap=OPERATOR_CAP,
                      execute=functools.partial(self._execute_hook, backend=backend), **hooks)
        if replayed:
            result.steps = replayed + list(result.steps)
        return result

    # ------------------------------------------------------------ the replay
    def _recipe_for(self, run: Run, step: PlanStep) -> Optional[Recipe]:
        if not run.memory:
            return None
        app = self._target_app()
        if not app:
            return None
        try:
            return self.experience.recipe_for(app, step.goal)
        except Exception:
            return None

    def _slots(self, run: Run, step: PlanStep, recipe: Optional[Recipe]):
        """``(goal slots, command slots or None)`` for filling ``recipe``.

        Command slots are only offered when the command has the template the
        recipe was learned under: the same goal can come from a different
        command, whose values sit in different positions, and a filled
        ``⟦c1⟧`` would then be the wrong value typed into the right field.
        """
        goal_slots = extract_slots(step.goal)
        key, values = command_key(run.command)
        same = recipe is None or not recipe.command or recipe.command == key
        return goal_slots, (values if same else None)

    def _recipe_text(self, run: Run, llm: Any, step: PlanStep, rstep: RecipeStep,
                     element: Any, goal_slots, command_slots) -> str:
        """The text a replayed ``type`` enters: the template filled with this
        command's values, or — a composed text that cannot be filled safely —
        written again by the compose hook."""
        text = fill(rstep.text, goal_slots, command_slots)
        literal_ok = rstep.text_source != "compose" or has_markers(rstep.text) or \
            command_slots is not None
        if text is not None and literal_ok:
            self._text_source = rstep.text_source or "literal"
            self._text_value = text
            return text
        return self._compose_hook(run, llm, step, step.goal, element)

    def _replay(self, run: Run, llm: Any, step: PlanStep, recipe: Recipe,
                backend: Any) -> RunResult:
        """Perform a remembered goal: no Jev call, no model call, every step checked.

        Each step's control must resolve on the current screen, the action must
        execute, and the screen must change — the same evidence the loop asks
        of a macro. A destructive control still goes through the confirm gate:
        a recipe learned yesterday is not today's permission. The goal counts
        as done only when the screen matches how it looked when it was learned
        (:func:`jevskill.cu.experience.recipe_check`), or the verifier says so.
        Anything short of that returns what was done, and the loop carries on.
        """
        from .loop import TextNeeded

        started = time.perf_counter()
        goal_slots, command_slots = self._slots(run, step, recipe)
        result = RunResult(task_id="%s:memory" % run.id, stop_reason="", steps=[])
        app = self._target_app()
        self._event(run, "memory", "replaying from memory: %d step%s, %d earlier success%s" % (
            len(recipe.steps), "" if len(recipe.steps) == 1 else "s", recipe.successes,
            "" if recipe.successes == 1 else "es"),
            {"steps": [s.describe() for s in recipe.steps]})
        ledger = self._ledger_or_none()

        def fail(why: str, stop: str = "") -> RunResult:
            run.learned["replay_failures"] += 1
            if not stop:
                try:
                    self.experience.recipe_failed(app, step.goal, why)
                except Exception:
                    pass
            self._event(run, "memory", "replay stopped: %s — %s" % (
                why, "the goal ends there" if stop else "Jev carries on from this screen"))
            result.stop_reason = stop
            result.error = why
            result.wall_ms = (time.perf_counter() - started) * 1000.0
            return result

        for number, rstep in enumerate(recipe.steps, start=1):
            self._check_budget(run)
            mark = time.perf_counter()
            snapshot = self._observe_hook()
            observe_ms = (time.perf_counter() - mark) * 1000.0
            cands = reduce_candidates(getattr(snapshot, "elements", []) or [], OPERATOR_CAP)
            element = None
            if rstep.identity:
                element = resolve(rstep.identity, cands, goal_slots=goal_slots,
                                  command_slots=command_slots)
                if element is None:
                    return fail("step %d's control (%s) is not on the screen"
                                % (number, rstep.describe()))
            action = act_module.Action(op=rstep.op, target=element.id if element else None,
                                       key=rstep.key, source="macro", note="memory")
            if rstep.op == "type":
                try:
                    action.text = self._recipe_text(run, llm, step, rstep, element,
                                                    goal_slots, command_slots)
                except TextNeeded as exc:
                    return fail("step %d needs text: %s" % (number, exc))
            if self._replay_needs_confirm(step.goal, action, element, cands, snapshot):
                if not self._confirm_hook(run, "%s %r?" % (action.op, getattr(element, "name", None)
                                                           or action.key or ""), action, element):
                    return fail("the destructive gate refused step %d" % number, stop="blocked")
            ignore = act_module.settle_ignore_for(action.op)
            pre = tree_hash(cands, ignore=ignore)
            mark = time.perf_counter()
            done = self._execute_hook(action, snapshot, backend=backend)
            act_ms = (time.perf_counter() - mark) * 1000.0
            record = StepRecord(index=len(result.steps), t_ms=0.0,
                                stages_ms={"observe": observe_ms, "act": act_ms},
                                candidates=len(cands), op=action.op, target=action.target,
                                decided_by="macro", note="memory replay")
            if not getattr(done, "ok", False):
                record.note = "memory replay; did not execute: %s" % getattr(done, "error", "")
                self._record_replay_step(run, step, result, record, ledger)
                return fail("step %d did not execute: %s" % (number, getattr(done, "error", "")))
            record.executed = True
            timeout = REPLAY_NEW_WINDOW_MS if rstep.outcome == "new_window" else \
                max(act_module.SETTLE_TIMEOUT_MS, act_module.settle_cap_for(element))
            mark = time.perf_counter()
            _after, changed, _waited = act_module.settle(
                self._observe_hook, pre, timeout_ms=timeout,
                key=lambda snap, ignore=ignore: tree_hash(
                    reduce_candidates(getattr(snap, "elements", []) or [], OPERATOR_CAP),
                    ignore=ignore))
            record.stages_ms["settle"] = (time.perf_counter() - mark) * 1000.0
            record.tree_changed = bool(changed)
            self._record_replay_step(run, step, result, record, ledger)
            if not changed:
                return fail("step %d (%s) changed nothing" % (number, rstep.describe()))
            run.learned["replayed_steps"] += 1

        snapshot = self._observe_hook()
        verified, why = self._code_verify(run, step, snapshot)
        if not verified and llm is not None:
            verified = self._verify_hook(run, llm, step, step.goal, snapshot) is True
            why = "%s judged it done" % run.model
        if not verified:
            # Not a failure of the recipe: every step held. The loop will look
            # at the screen and, if the goal is done, say so for one decision.
            self._event(run, "memory", "replayed; the end could not be confirmed in code — "
                                       "Jev looks at the screen")
            result.stop_reason = ""
            result.wall_ms = (time.perf_counter() - started) * 1000.0
            return result
        self._goal_replayed = True
        run.learned["replayed_goals"] += 1
        result.stop_reason = "done"
        result.wall_ms = (time.perf_counter() - started) * 1000.0
        self._event(run, "memory", "goal replayed in %.1f s (learned in %.1f s) — %s" % (
            result.wall_ms / 1000.0, recipe.learned_ms / 1000.0, why))
        return result

    def _record_replay_step(self, run: Run, step: PlanStep, result: RunResult,
                            record: StepRecord, ledger: Any) -> None:
        from .loop import _record_ledger, _StepOutcome

        record.t_ms = sum(record.stages_ms.values())
        result.steps.append(record)
        # One row of the loop's shape, so `jevskill stats` counts a replayed
        # step next to a decided one instead of in a report of its own.
        _record_ledger(ledger, step.goal, result, _StepOutcome(record))
        self._on_step(run, step, record)

    @staticmethod
    def _replay_needs_confirm(goal: str, action: Any, element: Any, cands: Sequence[Any],
                              snapshot: Any) -> bool:
        """The loop's destructive rules, applied to a replayed step."""
        if element is not None and act_module.is_destructive_name(getattr(element, "name", "")):
            return True
        if action.op == "key" and action.key:
            chord = act_module.chord_for(goal, cands, target=action.target, snapshot=snapshot)
            try:
                last = act_module.parse_chord(action.key)[-1]
            except Exception:
                return True
            return bool(chord.requires_confirm and last in ("enter", "return", "delete"))
        return False

    def _code_verify(self, run: Run, step: PlanStep, snapshot: Any):
        """``(True, why)`` when the screen proves the goal, else ``(None, "")``.

        Two sources, both code: how the screen looked when this goal was last
        done (the recipe), and the quoted values of ``done_when``. The model is
        asked only when neither can tell — it took 2-3 s per verify in the
        measured run, and a title reading ``hello.txt - Notatnik`` needs no
        opinion.
        """
        if snapshot is None:
            return None, ""
        if run.memory:
            app = self._target_app()
            recipe = None
            try:
                recipe = self.experience.recipe_for(app, step.goal, usable_only=False) if app else None
            except Exception:
                recipe = None
            if recipe is not None:
                goal_slots, command_slots = self._slots(run, step, recipe)
                if recipe_check(recipe, snapshot, goal_slots=goal_slots,
                                command_slots=command_slots):
                    return True, "the screen matches how this goal ended before"
        if literal_check(step.done_when, snapshot):
            return True, "done_when's quoted value is on the screen"
        return None, ""

    def _memory_answer(self, run: Run, llm: Any, step: PlanStep,
                       context: Dict[str, Any]) -> Any:
        """Answer an escalation from the recipe, without a model.

        The replay stopped, or this goal was never replayed, but a recipe for
        it exists: its next step whose control is on this screen, and which
        this goal has not already tried, is a better answer than a model call
        — it is what worked the last time this exact goal was done. The loop
        validates it and gates it like any escalation answer.
        """
        if not run.memory:
            return None
        recipe = self._recipe_for(run, step)
        trajectory = self._trajectory
        if recipe is None or trajectory is None or not recipe.steps:
            return None
        snapshot = context.get("snapshot")
        cands = context.get("candidates") or reduce_candidates(
            getattr(snapshot, "elements", []) or [], OPERATOR_CAP)
        goal_slots, command_slots = self._slots(run, step, recipe)
        tried = {(a.op, tuple(a.identity) if a.identity else None, a.key)
                 for a in trajectory.attempts + ([trajectory.pending] if trajectory.pending else [])}
        for rstep in recipe.steps:
            element = None
            if rstep.identity:
                element = resolve(rstep.identity, cands, goal_slots=goal_slots,
                                  command_slots=command_slots)
                if element is None:
                    continue
            identity = identity_of(element)
            if (rstep.op, tuple(identity) if identity else None, rstep.key) in tried:
                continue
            action = act_module.Action(op=rstep.op, target=element.id if element else None,
                                       key=rstep.key, source="escalation", note="memory")
            if rstep.op == "type":
                try:
                    action.text = self._recipe_text(run, llm, step, rstep, element,
                                                    goal_slots, command_slots)
                except Exception:
                    continue
            run.learned["memory_answers"] += 1
            self._event(run, "memory", "answered from memory, no model call: %s"
                        % rstep.describe())
            return action
        return None

    def _memory_context(self, run: Run, step: PlanStep,
                        tried: Optional[List[str]] = None) -> Dict[str, Any]:
        """What a model is told about this goal's history, when there is any.

        Measured live (2026-09-21): the escalation model was asked, in two
        separate attempts, to scroll a navigation pane that refused ScrollItem
        both times, and navigated folders it could not see instead of typing a
        path. It was never told what had already been tried. ``tried`` stands
        in for the trajectory once the goal's own has been consumed.
        """
        out: Dict[str, Any] = {}
        trajectory = self._trajectory
        if tried is None and trajectory is not None:
            tried = trajectory.tried()
        if tried:
            out["tried"] = list(tried)
        if not run.memory:
            return out
        app = self._target_app()
        if not app:
            return out
        try:
            lessons = self.experience.lessons_for(app, step.goal)
            recipe = self.experience.recipe_for(app, step.goal, usable_only=False)
        except Exception:
            return out
        if lessons:
            out["lessons"] = lessons
        if recipe is not None and recipe.steps:
            out["remembered"] = [s.describe() for s in recipe.steps]
        return out

    # ------------------------------------------------------------------- hooks
    def _observe_hook(self) -> Snapshot:
        self.kill.raise_if_tripped()
        run = self.run
        if run is not None and run.active:
            # The loop swallows exceptions from the verify and escalate hooks,
            # so a cap crossed inside one of their model calls used to be
            # noticed only after the goal. An observation is the next place
            # the loop cannot swallow: the run stops before the next decision.
            self._check_budget(run)
        if self._abort_goal:
            reason, self._abort_goal = self._abort_goal, ""
            raise RuntimeError(reason)
        hwnd = self._window_to_observe()
        if hwnd and self._observe_window is not None:
            # By handle: the pinned window or the dialog it opened, in front or
            # not. Reading needs no screen; only keys do. Measured live
            # (2026-09-21): observing "the foreground" pulled the window in
            # front of the user's Discord three times in 40 s, and later ended
            # a run because the user was reading the console.
            snapshot = self._observe_window(hwnd)
        else:
            self._ensure_target_in_front("observation")
            snapshot = self._observe()
        self._last_snapshot = snapshot
        trajectory = self._trajectory
        if trajectory is not None:
            try:
                trajectory.observed(snapshot)
            except Exception:
                pass
        return snapshot

    def _window_to_observe(self) -> int:
        """The pinned process's active window: the foreground when it is theirs
        (a dialog counts), else the last active popup, else the window."""
        target = self._target
        if not target["pid"]:
            return 0
        front = self._hwnd_or_zero()
        try:
            if front and int(self._window_pid(front) or 0) == target["pid"]:
                return front
        except Exception:
            pass
        try:
            return int(self._active_popup(target["hwnd"]) or target["hwnd"])
        except Exception:
            return target["hwnd"]

    @staticmethod
    def _needs_foreground(action: Any, snapshot: Any) -> bool:
        """Only synthetic input needs the window in front: a key chord, typed
        text without a ValuePattern, a click by point, the wheel. UIA patterns
        (Invoke, SetValue, Select, Scroll) act on a background window."""
        op = getattr(action, "op", "")
        if op in ("done", "blocked", "wait"):
            return False
        if op == "key":
            return True
        try:
            target = getattr(action, "target", None)
            element = snapshot.by_id(target) if target else None
            method = act_module._method_for(op, element)
        except Exception:
            return True
        return method in ("sendinput_text", "sendinput_key", "click_point", "wheel")

    def _execute_hook(self, action: Any, snapshot: Any, *, backend: Any = None,
                      dry_run: bool = False) -> Any:
        self.kill.raise_if_tripped()
        if getattr(action, "op", "") == "launch":
            self._acting += 1
            try:
                self._launcher(str(getattr(action, "text", "") or ""))
            finally:
                self._acting -= 1
            return act_module.ActResult(ok=True, method="launch")
        if self._needs_foreground(action, snapshot):
            self._ensure_target_in_front("action")
        self._acting += 1
        try:
            result = act_module.execute(action, snapshot, backend=backend, dry_run=dry_run)
        finally:
            self._acting -= 1
        node = self._current_node
        if node is not None and node.kind == "leaf" and getattr(result, "ok", False) and \
                getattr(action, "op", "") not in ("done", "blocked", "wait"):
            node.acted += 1
            if node.acted == 1 and self.run is not None:
                # The first thing a goal changes on the desktop is the moment a
                # blind rerun stops being safe; the checkpoint must know it.
                self._checkpoint(self.run, force=True)
        if self.run is not None and not getattr(result, "ok", False):
            self._event(self.run, "act_error", "%s %s did not execute: %s" % (
                getattr(action, "op", "?"), getattr(action, "key", None) or getattr(action, "target", "") or "",
                getattr(result, "error", "") or "no error text"))
        trajectory = self._trajectory
        if trajectory is not None:
            source = ""
            if getattr(action, "op", "") == "type":
                text = getattr(action, "text", None)
                # Text that did not come through the compose hook came from an
                # escalation answer: model-written, so replayed like a composed one.
                source = self._text_source if (text is not None and text == self._text_value) \
                    else "compose"
            try:
                trajectory.acted(action, snapshot, bool(getattr(result, "ok", False)),
                                 str(getattr(result, "error", "") or ""), source)
            except Exception:
                pass
        return result

    def _on_step(self, run: Run, step: PlanStep, record: StepRecord) -> None:
        # Three escalation answers in a row that could not be performed is a
        # broken action path, not a hard screen: stop the goal instead of
        # buying the same failed answer from the model until the step cap.
        if record.decided_by == "escalation" and not record.executed:
            self._exec_failures += 1
            if self._exec_failures >= 3:
                self._abort_goal = ("%d consecutive escalation actions did not execute; "
                                    "giving the goal up" % self._exec_failures)
        else:
            self._exec_failures = 0
        jev_call = 1 if record.decided_by in ("jev", "cascade") else 0
        run.spend["jev_calls"] += jev_call
        run.spend["jev_tokens"] += int(record.tokens_in)
        run.spend["jev_usd"] += float(record.cost_usd)
        run.spend["usd"] = run.spend["jev_usd"] + run.spend["llm_usd"]
        self._charge(jev_calls=jev_call, jev_usd=float(record.cost_usd), steps=1)
        self._checkpoint(run)
        step.steps = record.index + 1
        stages = record.stages_ms or {}
        text = "#%d %s %s · %s · conf %.2f · %s%s%s" % (
            record.index, record.op or "-", record.target or "",
            record.decided_by or "-", float(record.confidence or 0.0),
            "executed" if record.executed else "not executed",
            (" · %.0f ms" % sum(stages.values())) if stages else "",
            (" · " + record.note) if record.note else "")
        self._event(run, "step", text, {"index": record.index, "op": record.op,
                                        "target": record.target,
                                        "decided_by": record.decided_by,
                                        "confidence": round(float(record.confidence or 0), 3),
                                        "executed": bool(record.executed),
                                        "stages_ms": {k: round(v, 1) for k, v in stages.items()},
                                        "note": record.note})

    def _confirm_hook(self, run: Run, prompt: str, action: Any, element: Any) -> bool:
        name = getattr(element, "name", None) or getattr(action, "target", None) or ""
        return self._ask(run, prompt, {"op": getattr(action, "op", ""),
                                       "target": getattr(action, "target", None),
                                       "key": getattr(action, "key", None),
                                       "name": name})

    def _ask(self, run: Run, prompt: str, data: Dict[str, Any]) -> bool:
        """Block the run on a question the page answers. Timeout and STOP deny."""
        with self._lock:
            self._confirm_event.clear()
            self._confirm_answer = None
            run.pending_confirm = dict(data, prompt=prompt, asked_at=time.time(),
                                       timeout_s=CONFIRM_TIMEOUT_S)
            self._set_state(run, "waiting_confirm")
        self._event(run, "confirm", "waiting for confirmation: %s" % prompt, data)
        self._checkpoint(run, force=True)
        # In slices, with a heartbeat between them: a run waiting two minutes
        # for an answer is alive, and another console must not see it as dead.
        deadline = time.time() + CONFIRM_TIMEOUT_S
        answered = False
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            if self._confirm_event.wait(min(5.0, left)):
                answered = True
                break
            if self.kill.event.is_set():
                break
            self._checkpoint(run)
        with self._lock:
            allow = bool(self._confirm_answer) if answered else False
            run.pending_confirm = None
            if run.active:
                self._set_state(run, "running")
        if self.kill.event.is_set():
            allow = False
        self._event(run, "confirm_result", "%s: %s" % (
            "allowed" if allow else ("denied" if answered else "timed out, denied"), prompt))
        self._checkpoint(run, force=True)
        return allow

    def _compose_hook(self, run: Run, llm: Any, step: PlanStep, goal: str, element: Any) -> str:
        # The planner is told to keep typed text in the goal, in quotes; when it
        # puts it in `done_when` instead ("The text area contains \"hello world\""),
        # that is the next place to look before a model is asked.
        for source, text in (("goal", step.goal), ("done_when", step.done_when)):
            literal = quoted_text(text)
            if literal is not None:
                self._event(run, "text", "typing the quoted text from the %s" % source)
                self._text_source, self._text_value = "literal", literal
                return literal
        if llm is None:
            # Jev only: the command's own quote is all there is. With a model
            # present it is *not* used — measured live (2026-09-21) it typed the
            # command's "hello world" into a Save As file-name field, because
            # that quote belonged to an earlier goal. With a model, ask.
            literal = quoted_text(run.command)
            if literal is not None:
                self._event(run, "text", "typing the quoted text from the command")
                self._text_source, self._text_value = "literal", literal
                return literal
            from .loop import TextNeeded

            raise TextNeeded("the step needs text and no planning model was chosen")
        reply = self._llm(run, llm, "compose", (
            "You write the exact text a desktop agent types into one field, for the "
            "current sub-goal of the command. For a file-name field give a full path, "
            "with %USERPROFILE% for the user's profile when the user name is unknown. "
            "Reply with JSON only: {\"text\": \"...\"}. No explanation, no quotes around "
            "the value beyond JSON's own."),
            json.dumps({"command": run.command, "goal": step.goal, "done_when": step.done_when,
                        "field": {"role": getattr(element, "role", ""),
                                  "name": getattr(element, "name", ""),
                                  "value": getattr(element, "value", None)}},
                       ensure_ascii=False), max_tokens=300)
        data = reply.json() if reply is not None else None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text:
            from .loop import TextNeeded

            raise TextNeeded("the planning model did not return text")
        self._event(run, "text", "text from %s (%d chars)" % (run.model, len(text)))
        self._text_source, self._text_value = "compose", text
        return text

    def _verify_hook(self, run: Run, llm: Any, step: PlanStep, goal: str,
                     snapshot: Any) -> Optional[bool]:
        verified, why = self._code_verify(run, step, snapshot)
        if verified:
            run.learned["code_verified"] += 1
            self._goal_evidence = "code"
            self._event(run, "verify", "verified in code, no model call — %s" % why)
            return True
        if llm is None:
            # Jev only: code could not tell, and there is nobody else to ask.
            # Abstaining keeps the loop's own rule (two `done` in a row).
            return None
        key = "%s|%s" % (step.goal, tree_hash(list(getattr(snapshot, "elements", []) or [])))
        if key in self._verify_cache:
            if self._verify_cache[key]:
                self._goal_evidence = "model"
            return self._verify_cache[key]
        reply = self._llm(run, llm, "verify", (
            "You judge whether a desktop sub-goal is complete from the UI Automation "
            "tree of the foreground window. Be strict: unsaved, half-typed or still-open "
            "dialogs are not done. Reply with JSON only: {\"done\": true|false, \"why\": \"...\"}; "
            "keep `why` under 25 words."),
            json.dumps({"goal": step.goal, "done_when": step.done_when,
                        "screen": self._screen(snapshot)}, ensure_ascii=False), max_tokens=300)
        data = reply.json() if reply is not None else None
        done = bool(isinstance(data, dict) and data.get("done") is True)
        why = data.get("why", "") if isinstance(data, dict) else ""
        self._verify_cache[key] = done
        if done:
            self._goal_evidence = "model"
        self._event(run, "verify", "%s judged %s%s" % (
            run.model, "done" if done else "not done", (" — " + str(why)[:120]) if why else ""))
        return done

    def _escalate_hook(self, run: Run, llm: Any, step: PlanStep,
                       context: Dict[str, Any]) -> Any:
        snapshot = context.get("snapshot")
        candidates = context.get("candidates") or []
        decision = context.get("decision")
        remembered = self._memory_answer(run, llm, step, context)
        if remembered is not None:
            return remembered
        if llm is None:
            return None
        system = (
            "A fast decision model drives a Windows UI agent one step at a time and "
            "just failed on this step. Choose the single next action from the elements "
            "listed. Ops: click, type, select, scroll_up, scroll_down, key (give the chord "
            "in `key`, e.g. ctrl+s, enter, escape), wait, done when `done_when` is already "
            "satisfied on this screen, or none when nothing safe applies. "
            "`target` must be an element id from the list (null for key/scroll/wait/done). "
            "Never choose an action that deletes, sends, pays or overwrites unless the goal "
            "says so. `tried` is what this goal already did and what happened: do not repeat "
            "an action that changed nothing or did not execute. `lessons` are failures from "
            "earlier runs on this machine; `remembered` is what completed this goal before. "
            "Prefer clicking a visible control, a menu or typing into a field over a key "
            "chord: those go through UI Automation while the user keeps working, a chord "
            "takes the keyboard from them. Reply with JSON only: {\"op\": \"...\", "
            "\"target\": \"e3\"|null, \"text\": null, \"key\": null, \"why\": \"...\"}; keep "
            "`why` under 25 words.")
        payload: Dict[str, Any] = {
            "command": run.command, "goal": step.goal, "done_when": step.done_when,
            "reason": context.get("reason"), "step": context.get("step"),
            "last_action": context.get("last_action"),
            "model_said": {"op": getattr(decision, "op", None),
                           "target": getattr(decision, "target", None),
                           "confidence": round(float(getattr(decision, "confidence", 0) or 0), 3)},
            "screen": self._screen(snapshot, candidates)}
        payload.update(self._memory_context(run, step))
        user = json.dumps(payload, ensure_ascii=False)
        reply = self._llm(run, llm, "escalate", system, user)
        data = reply.json() if reply is not None else None
        if reply is not None and not isinstance(data, dict):
            # Measured live (2026-09-21, Sonnet 5 in a Save As dialog): a reply
            # that ran to the 900-token cap and never closed its JSON, $0.015
            # for nothing. One terse retry, capped short, before giving up.
            head = " ".join(str(getattr(reply, "text", "") or "").split())[:160]
            self._event(run, "escalate", "%s gave no usable answer (%d tokens out): %s" % (
                run.model, int(getattr(reply, "tokens_out", 0) or 0), head or "empty reply"))
            reply = self._llm(run, llm, "escalate_retry", system + (
                " Your previous reply was not one JSON object. Reply with exactly one JSON "
                "object and nothing else."), user, max_tokens=300)
            data = reply.json() if reply is not None else None
        if not isinstance(data, dict):
            self._event(run, "escalate", "%s gave no usable answer" % run.model)
            return None
        op = str(data.get("op") or "none").strip().lower()
        why = str(data.get("why", ""))[:120]
        if op == "done":
            # The model read the screen and found `done_when` already true. The
            # loop has no channel for that from an escalation, so the operator
            # closes the goal once the loop returns. Measured live: Jev said
            # done with a target (incoherent_terminal), the model answered
            # "nothing left to do", and the goal still ended `escalated` and
            # bought a re-plan ($0.004, 2.5 s) to learn what it had been told.
            self._agreed_done = why or "the escalation model judged done_when satisfied"
            self._event(run, "escalate", "%s: done_when already satisfied — %s" % (run.model, why))
            return None
        if op not in ESCALATION_OPS:
            self._event(run, "escalate", "%s: %s — %s" % (run.model, op, str(data.get("why", ""))[:120]))
            return None
        target = data.get("target")
        target = str(target) if target not in (None, "", "null") else None
        text = data.get("text") if isinstance(data.get("text"), str) else None
        key = data.get("key") if isinstance(data.get("key"), str) else None
        self._event(run, "escalate", "%s proposes %s %s%s — %s" % (
            run.model, op, target or "", (" " + key) if key else "", str(data.get("why", ""))[:120]))
        return act_module.Action(op=op, target=target, text=text, key=key,
                                 source="escalation", note="llm:%s" % run.model)

    # --------------------------------------------------------------- planning
    def _remembered_plan(self, command: str, memory: bool, hierarchical: bool) -> Any:
        if not memory:
            return None
        try:
            if hierarchical:
                tree = self.experience.tree_for(command)
                if tree is not None:
                    return tree[0], tree[1], "tree"
            flat = self.experience.plan_for(command)
        except Exception:
            return None
        return (flat[0], flat[1], "flat") if flat is not None else None

    def _gap_system(self, hierarchical: bool) -> str:
        """The planning prompt. Flat, it is byte for byte the prompt every run
        used before trees; hierarchical, it adds phases after the same rules,
        so a short command still gets — and gives — the same answer."""
        rules = (
            "You plan desktop tasks for a UI agent on Windows. The agent sees the UI "
            "Automation tree of the foreground window (control roles, names, values) and "
            "performs one action per step: click, type, select, scroll, a key chord, wait. "
            "It cannot see pixels, cannot browse, and cannot run programs except a launch "
            "you name. Split the command into 1-%d sub-goals. Each goal: one short English "
            "imperative completable inside a single window or dialog. Write `goal` and "
            "`done_when` in English whatever language the command is in — the agent's "
            "decision model reads English best. Only the text the agent must type stays in "
            "the user's language, inside double quotes, verbatim. `done_when` "
            "is an observable end state (a window title, a control's value, a control that "
            "exists). `launch` is the program to start before the goal — notepad.exe, "
            "calc.exe, mspaint.exe, explorer.exe, a ms-settings: URI — or null. In a file "
            "dialog, prefer typing a full path (\"C:\\\\Users\\\\<user>\\\\Desktop\\\\name.txt\", "
            "using %%USERPROFILE%% when the user name is unknown) into the file-name field and "
            "pressing Save over navigating folders — the agent cannot see a folder tree "
            "well. Prefer clicking visible controls and menus over keyboard shortcuts: a "
            "click or a typed value goes through UI Automation while the user keeps "
            "working, a shortcut takes the keyboard from them. `known_goals`, when "
            "present, are goals this agent has completed on this machine before; when one "
            "fits, reuse its wording exactly, with «n» replaced by the value — those "
            "replay from memory in a fraction of the time. Do not add "
            "steps that delete, send, pay or overwrite unless the command says so. " % MAX_PLAN_STEPS)
        if not hierarchical:
            return rules + (
                "Reply with JSON only: {\"steps\": [{\"goal\": \"...\", \"done_when\": \"...\", "
                "\"launch\": null}], \"note\": \"...\"}")
        return rules + (
            "A sub-goal is a `leaf` when it happens inside one window or dialog in a few "
            "actions. For a long command (several documents, programs or milestones) you may "
            "instead return up to %d items of `\"kind\": \"phase\"`: one milestone in ONE "
            "program, with `app` (its executable), an observable `done_when`, optional "
            "`checks` the agent tests in code (`file_exists` {arg: path}, `file_contains` "
            "{arg: path, text}, `title_contains` {arg}), `weight` (relative effort, default 1), "
            "`optional`, and `irreversible` (sends, submits, saves over a file). Give "
            "`children` (leaf sub-goals, same rules) for the FIRST phase only; later phases "
            "are broken down when the agent reaches them, from the screen it sees then. "
            "Prefer leaves: a command that fits in %d one-window goals is all leaves. "
            "Reply with JSON only: {\"steps\": [{\"goal\": \"...\", \"done_when\": \"...\", "
            "\"launch\": null, \"kind\": \"leaf|phase\", \"app\": null, \"checks\": [{\"kind\": "
            "\"file_exists\", \"arg\": \"%%USERPROFILE%%/Desktop/a.txt\", \"text\": \"\"}], "
            "\"weight\": 1, \"optional\": false, \"irreversible\": false, \"children\": "
            "[{\"goal\": \"...\", \"done_when\": \"...\", \"launch\": null}]}], \"note\": \"\"}"
            % (MAX_PHASES, MAX_PLAN_STEPS))

    def _build_tree(self, run: Run, llm: Any):
        """``(root, note)``: the plan tree, from — in order — memory (a tree,
        then a flat plan: no call), the command itself (no model), or the
        General Agent Plan call. An unusable reply is the command as one goal."""
        root = self._new_root(run)
        items: Any = None
        note = ""
        remembered = self._remembered_plan(run.command, run.memory, run.hierarchical)
        if remembered is not None:
            # The planning call was 6.3 s of the measured 60.1 s run, and it
            # re-derived goals a successful run had already found. Values are
            # the new command's own (see experience.fill).
            items, memory, run.plan_memory = remembered
            run.plan_source = "memory"
            self._event(run, "memory", "plan from memory: %d %s, %d earlier success%s — "
                                       "no planning call" % (
                                           len(items), ("phase%s" if run.plan_memory == "tree"
                                                        else "goal%s") % (
                                               "" if len(items) == 1 else "s"),
                                           memory.successes, "" if memory.successes == 1 else "es"))
            note = "remembered from %d successful run%s" % (
                memory.successes, "" if memory.successes == 1 else "s")
        elif llm is None:
            run.plan_source = "command"
            root.add_children([self._leaf(run.command, "command")])
            root.source = "command"
            return root, "no planning model: the command is the goal"
        else:
            context: Dict[str, Any] = {"command": run.command, "os": "Windows",
                                       "foreground": (self._title_or_empty() if not run.dry_run else "")}
            if run.memory:
                try:
                    known = self.experience.skills_for()
                except Exception:
                    known = []
                if known:
                    context["known_goals"] = known
            reply = self._llm(run, llm, "plan", self._gap_system(run.hierarchical),
                              json.dumps(context, ensure_ascii=False))
            data = reply.json() if reply is not None else None
            items = data.get("steps") if isinstance(data, dict) else data
            run.plan_source = "model"
            note = str(data.get("note", ""))[:300] if isinstance(data, dict) else ""
        if isinstance(items, list) and not run.hierarchical:
            items = [self._flat_item(item) for item in items]
        children: List[PlanNode] = []
        if isinstance(items, list):
            try:
                children = coerce_tree(items, depth=1, id_prefix="", source=run.plan_source,
                                       make_step=_step_for)
            except ValueError:
                children = []
        if not children:
            if run.plan_source == "memory":
                run.plan_source, run.plan_memory = "command", ""
            else:
                self._event(run, "plan_fallback", "the model returned no usable plan; "
                                                  "running the command as one goal")
                run.plan_source = "command"
            children = [self._leaf(run.command, "command")]
            note = ""
        root.source = run.plan_source
        root.add_children(children)
        return root, note

    @staticmethod
    def _leaf(goal: str, source: str) -> PlanNode:
        node = PlanNode(id="1", goal=goal, kind="leaf", depth=1, source=source)
        node.step = _step_for(node)
        return node

    # ------------------------------------------------------------ break-down
    def _expand(self, run: Run, llm: Any, phase: PlanNode) -> None:
        """Break a phase down when the cursor reaches it: from memory (how this
        phase was broken down before), else one model call that sees the
        screen the previous goal left — no extra walk of the UI tree. When
        neither gives usable children, the phase becomes one goal for Jev."""
        app = phase.app or self._target_app()
        items: Any = None
        source = ""
        if run.memory and app:
            try:
                remembered = self.experience.decomposition_for(app, phase.goal)
            except Exception:
                remembered = None
            if remembered is not None:
                items, _memory = remembered
                source = "memory"
        if items is None and llm is not None:
            snapshot = self._last_snapshot
            if snapshot is None and not run.dry_run and self._target.get("pid"):
                try:
                    snapshot = self._observe_hook()
                except Stopped:
                    raise
                except Exception:
                    snapshot = None
            payload: Dict[str, Any] = {
                "command": run.command,
                "phase": {"id": phase.id, "goal": phase.goal, "done_when": phase.done_when,
                          "app": phase.app, "checks": [c.to_dict() for c in phase.checks]},
                "depth": phase.depth + 1,
                "outline": [{"id": n.id, "goal": n.goal, "status": n.status}
                            for n in run.tree.walk() if n is not run.tree and not n.superseded
                            and (n.kind == "phase" or n.parent is run.tree)],
                "screen": self._screen(snapshot) if snapshot is not None else
                ({"dry_run": True} if run.dry_run else {})}
            if run.memory:
                try:
                    known = self.experience.skills_for(app or None)
                    lessons = self.experience.lessons_for(app, phase.goal) if app else []
                except Exception:
                    known, lessons = [], []
                if known:
                    payload["known_goals"] = known
                if lessons:
                    payload["lessons"] = lessons
            previous, self._current_node = self._current_node, phase
            try:
                reply = self._llm(run, llm, "expand", EXPAND_SYSTEM,
                                  json.dumps(payload, ensure_ascii=False))
            finally:
                self._current_node = previous
            data = reply.json() if reply is not None else None
            items = data.get("steps") if isinstance(data, dict) else None
            source = "model"
        children: List[PlanNode] = []
        if isinstance(items, list) and items:
            try:
                children = coerce_tree(items, depth=phase.depth + 1, id_prefix=child_prefix(phase),
                                       source=source, make_step=_step_for)
            except ValueError:
                children = []
        for child in children:
            if child.kind == "phase" and app and self._has_recipe(app, child.goal):
                # A goal that replays from memory is never broken down further.
                self._to_leaf(child)
        known = len([n for n in run.tree.leaves() if not n.superseded])
        new = sum(len(c.leaves()) if c.kind == "phase" else 1 for c in children)
        if children and known + new > run.max_leaves:
            self._event(run, "expand", "breaking phase %s down would exceed %d goals; Jev tries "
                                       "it as one goal" % (phase.id, run.max_leaves),
                        {"node": phase.id, "source": "limit", "children": 0})
            children = []
        if not children:
            self._to_leaf(phase)
            phase.status = "pending"
            self._event(run, "expand", "phase %s could not be broken down%s; Jev tries it as one "
                                       "goal" % (phase.id, "" if llm is not None else
                                                 " without a planning model"),
                        {"node": phase.id, "source": "none", "children": 0})
            return
        phase.add_children(children)
        phase.expanded = True
        self._event(run, "expand", "phase %s broken down into %d goal%s%s" % (
            phase.id, len(children), "" if len(children) == 1 else "s",
            " from memory — no model call" if source == "memory" else " by %s" % run.model),
            {"node": phase.id, "source": source, "children": len(children),
             "steps": [c.item() for c in children]})

    def _has_recipe(self, app: str, goal: str) -> bool:
        try:
            return self.experience.recipe_for(app, goal) is not None
        except Exception:
            return False

    @staticmethod
    def _to_leaf(node: PlanNode) -> None:
        node.kind = "leaf"
        node.children = []
        node.expanded = False
        if node.step is None:
            node.step = _step_for(node)

    # ------------------------------------------------------------ closing
    def _close_phase(self, run: Run, llm: Any, phase: PlanNode) -> bool:
        """A phase whose children are all closed: accept it, by code when it
        can be (checks, done_when's quote), else on its children's word —
        and say which. False when acceptance failed and could not be repaired."""
        if phase is run.tree:
            phase.status, phase.evidence = "done", "children"
            phase.ended_at = time.time()
            return True
        evidence = ""
        snapshot = None
        if not run.dry_run and (phase.checks or phase.done_when):
            snapshot = self._fresh_observation()
        if not run.dry_run and phase.checks:
            verdict, detail = evaluate_checks(phase.checks, snapshot)
            if verdict is True:
                evidence = "code"
            elif verdict is False:
                repaired = self._repair(run, llm, phase, None, None,
                                        reason="acceptance failed: %s" % detail)
                if repaired == "revised" or repaired == "retry":
                    return True
                if repaired == "completed":
                    evidence = phase.evidence or "model"
                else:
                    phase.stop_reason = "acceptance"
                    phase.note = detail[:200]
                    return False
        if not evidence and snapshot is not None and literal_check(phase.done_when, snapshot):
            evidence = "code"
        evidence = evidence or "children"
        phase.status, phase.evidence = "done", evidence
        phase.ended_at = time.time()
        phase.spend["active_s"] = round(self._node_active_s(run, phase), 2)
        self._event(run, "phase_done", "phase %s done — %s: %s" % (
            phase.id, {"code": "verified in code", "children": "every goal in it done",
                       "model": "the model quoted the screen"}.get(evidence, evidence),
            phase.goal[:100]), {"node": phase.id, "evidence": evidence})
        self._learn_decomposition(run, phase)
        try:
            self._give_back(run)
        except Exception:
            pass
        self._checkpoint(run, force=True)
        return True

    def _fresh_observation(self) -> Any:
        """A read-only look at the pinned window, or None; a STOP still stops."""
        if not self._target.get("pid") and self._observe_window is not None:
            return None
        try:
            return self._observe_hook()
        except Stopped:
            raise
        except Exception:
            return None

    # ------------------------------------------------------------ repair
    def _repair_up(self, run: Run, llm: Any, failed: PlanNode, result: Any, *,
                   started: Optional[float] = None, reason: str = "") -> bool:
        """Recover in the scope that failed — the failed item's parent — and
        climb one level only when that scope cannot. False ends the run."""
        node, first = failed, True
        while True:
            scope = node.parent
            if scope is None:
                return False
            verdict = self._repair(run, llm, scope, node, result if first else None,
                                   reason if first else (reason or "failed below"))
            if first and node.kind == "leaf" and started is not None and node.step is not None:
                # The recipe-or-lesson decision waits for this verdict, as the
                # flat run's did: a goal the screen proves done still teaches.
                self._learn_goal(run, node.step, verdict == "completed", started)
            first = False
            if verdict == "completed":
                self._set_node(node, "done", evidence=node.evidence or "code")
                return True
            if verdict == "revised":
                self._set_node(node, "failed")
                node.superseded = True
                if node.kind == "leaf" and not self._flat(run, scope, node):
                    self._subgoal_lesson(run, scope, node)
                return True
            if verdict == "skip":
                self._set_node(node, "skipped")
                return True
            if verdict == "retry":
                self._set_node(node, "pending")
                node.acted = 0
                return True
            if verdict == "give_up":
                return False
            self._set_node(node, "failed")
            if scope is run.tree:
                return False
            scope.status = "failed"
            scope.stop_reason = node.stop_reason or "failed below"
            scope.note = scope.note or node.note
            if scope.children and all(c.source == "memory" for c in scope.children
                                      if not c.superseded):
                try:
                    self.experience.decomposition_failed(scope.app or self._target_app(),
                                                         scope.goal)
                except Exception:
                    pass
            self._event(run, "repair", "phase %s could not be repaired in place; asking the level "
                                       "above" % scope.id, {"scope": scope.id, "failed": node.id,
                                                            "verdict": "escalate_up"})
            node = scope

    def _subgoal_lesson(self, run: Run, scope: PlanNode, node: PlanNode) -> None:
        if run.dry_run or not run.memory:
            return
        app = node.app or scope.app or self._target_app()
        if not app:
            return
        try:
            self.experience.learn_subgoal_lesson(app, scope.goal, node.goal,
                                                 node.stop_reason or "failed")
            run.learned["lessons_learned"] += 1
        except Exception:
            pass

    def _repair(self, run: Run, llm: Any, scope: PlanNode, failed: Optional[PlanNode],
                result: Any, reason: str) -> str:
        """One verdict for ``failed`` inside ``scope``: completed, revised, skip,
        retry, give_up or escalate_up. Code first — a screen that proves the
        work done costs no call — then the bounds, then one model call: the
        flat run's own prompt when the plan is flat, else the tree prompt."""
        snapshot = None
        if not run.dry_run:
            snapshot = self._fresh_observation()
            if failed is not None and snapshot is not None:
                if failed.kind == "leaf" and failed.step is not None:
                    ok, why = self._code_verify(run, failed.step, snapshot)
                else:
                    ok = evaluate_checks(failed.checks, snapshot)[0] is True or \
                        bool(literal_check(failed.done_when, snapshot))
                    why = "its checks hold on the screen"
                if ok:
                    run.learned["code_verified"] += 1
                    failed.evidence = "code"
                    index = self._plan_index(run, failed)
                    self._event(run, "replan" if self._flat(run, scope, failed) else "repair",
                                "goal %d is complete — verified in code, no model call: %s" % (
                                    index + 1, why), {"scope": scope.id, "failed": failed.id,
                                                      "verdict": "completed"})
                    return "completed"
        if llm is None or scope.repairs >= MAX_REPAIRS_PER_NODE or \
                run.repairs_total >= MAX_REPAIRS_TOTAL or run.spend["llm_calls"] >= run.llm_cap:
            if llm is not None:
                self._event(run, "repair", "no repair left for %s (%d here, %d this run)" % (
                    "the plan" if scope is run.tree else "phase %s" % scope.id, scope.repairs,
                    run.repairs_total), {"scope": scope.id,
                                         "failed": failed.id if failed else None,
                                         "verdict": "escalate_up"})
            return "escalate_up"
        scope.repairs += 1
        run.repairs_total += 1
        run.replans = run.repairs_total
        previous, self._current_node = self._current_node, scope
        try:
            if failed is not None and self._flat(run, scope, failed):
                verdict = self._flat_replan(run, llm, scope, failed, result, snapshot)
            else:
                verdict = self._tree_repair(run, llm, scope, failed, result, reason, snapshot)
        finally:
            self._current_node = previous
        self._checkpoint(run, force=True)
        return verdict

    @staticmethod
    def _flat(run: Run, scope: PlanNode, failed: Optional[PlanNode]) -> bool:
        return scope is run.tree and failed is not None and failed.kind == "leaf" and \
            all(c.kind == "leaf" for c in scope.children)

    @staticmethod
    def _plan_index(run: Run, node: PlanNode) -> int:
        try:
            return run.plan.index(node.step)
        except ValueError:
            return len(run.plan)

    def _flat_replan(self, run: Run, llm: Any, scope: PlanNode, failed: PlanNode,
                     result: Any, snapshot: Any) -> str:
        """The flat run's re-plan, unchanged: same prompt, same payload."""
        index = self._plan_index(run, failed)
        step = failed.step
        remaining = [c.step.to_dict() for c in scope.children
                     if c.status == "pending" and c.step is not None]
        screen: Dict[str, Any] = {"dry_run": True} if run.dry_run else {}
        if snapshot is not None:
            try:
                screen = self._screen(snapshot)
            except Exception:
                pass
        reply = self._llm(run, llm, "replan", (
            "A sub-goal of a desktop task ended without the agent reporting done. Decide: "
            "if the screen shows the sub-goal is in fact complete, answer completed=true. "
            "Otherwise return the remaining sub-goals, revised so the agent can continue "
            "from this screen (same rules: one window each, quoted text verbatim, optional "
            "`launch`). If the task cannot continue safely, set give_up to the reason. Reply "
            "with JSON only: {\"completed\": false, \"steps\": [...], \"give_up\": null}"),
            json.dumps(dict({"command": run.command, "failed_goal": step.to_dict(),
                             "stop_reason": getattr(result, "stop_reason", failed.stop_reason),
                             "error": getattr(result, "error", failed.note),
                             "remaining": remaining, "screen": screen},
                            **self._memory_context(run, step, tried=failed.tried or None)),
                       ensure_ascii=False))
        data = reply.json() if reply is not None else None
        if not isinstance(data, dict):
            return "give_up"
        if data.get("completed") is True:
            failed.evidence = "model"
            self._event(run, "replan", "%s judged goal %d complete" % (run.model, index + 1))
            return "completed"
        if data.get("give_up"):
            self._event(run, "replan", "%s gives up: %s" % (run.model, str(data["give_up"])[:200]))
            run.error = str(data["give_up"])[:200]
            return "give_up"
        items = data.get("steps")
        if not isinstance(items, list):
            return "give_up"
        try:
            children = coerce_tree([self._flat_item(i) for i in items], depth=1,
                                   id_prefix=repair_prefix(scope, scope.generation + 1),
                                   source="repair", make_step=_step_for,
                                   max_items=MAX_PLAN_STEPS)
        except ValueError:
            return "give_up"
        self._replace_pending(scope, children)
        self._event(run, "replan", "%s revised the plan: %d step%s remain" % (
            run.model, len(children), "" if len(children) == 1 else "s"),
            {"steps": [c.step.to_dict() for c in children if c.step is not None]})
        return "revised"

    @staticmethod
    def _replace_pending(scope: PlanNode, children: List[PlanNode]) -> None:
        """Drop the scope's children that never ran, add the revised ones."""
        scope.children = [c for c in scope.children if c.status != "pending"]
        scope.generation += 1
        scope.add_children(children)

    def _failed_twice(self, scope: PlanNode, failed: Optional[PlanNode]) -> List[str]:
        counts: Dict[str, int] = {}
        names: Dict[str, str] = {}
        for child in scope.children:
            if child.status == "failed" or child is failed:
                key = goal_key(child.goal)[0]
                counts[key] = counts.get(key, 0) + 1
                names[key] = child.goal
        return [names[k] for k, n in counts.items() if n >= MAX_SAME_GOAL_FAILS]

    def _tree_repair(self, run: Run, llm: Any, scope: PlanNode, failed: Optional[PlanNode],
                     result: Any, reason: str, snapshot: Any, *, interrupted: bool = False) -> str:
        twice = self._failed_twice(scope, failed)
        payload: Dict[str, Any] = {
            "command": run.command,
            "scope": {"id": scope.id, "goal": scope.goal, "done_when": scope.done_when,
                      "kind": scope.kind, "checks": [c.to_dict() for c in scope.checks]},
            "children": [{"id": c.id, "goal": c.goal, "status": c.status, "evidence": c.evidence}
                         for c in scope.children if not c.superseded],
            "failed": ({"id": failed.id, "goal": failed.goal, "done_when": failed.done_when,
                        "stop_reason": failed.stop_reason,
                        "error": str(getattr(result, "error", "") or failed.note)[:200],
                        "tried": list(failed.tried)} if failed is not None else None),
            "reason": reason or "goal ended without done",
            "interrupted": bool(interrupted), "acted": int(failed.acted if failed else 0),
            "failed_twice": twice,
            "screen": self._screen(snapshot) if snapshot is not None else
            ({"dry_run": True} if run.dry_run else {})}
        app = (failed.app if failed is not None else "") or scope.app or self._target_app()
        if run.memory and app:
            try:
                lessons = self.experience.lessons_for(app, scope.goal)
                recipe = self.experience.recipe_for(app, failed.goal, usable_only=False) \
                    if failed is not None else None
            except Exception:
                lessons, recipe = [], None
            if lessons:
                payload["lessons"] = lessons
            if recipe is not None and recipe.steps:
                payload["remembered"] = [s.describe() for s in recipe.steps]
        reply = self._llm(run, llm, "repair", REPAIR_SYSTEM, json.dumps(payload, ensure_ascii=False))
        data = reply.json() if reply is not None else None
        where = "the plan" if scope is run.tree else "phase %s" % scope.id

        def said(verdict: str, text: str) -> str:
            self._event(run, "repair", "%s: %s" % (where, text),
                        {"scope": scope.id, "failed": failed.id if failed else None,
                         "verdict": verdict})
            return verdict

        if not isinstance(data, dict):
            return said("escalate_up", "%s gave no usable repair" % run.model)
        if data.get("give_up"):
            run.error = str(data["give_up"])[:200]
            return said("give_up", "%s gives up: %s" % (run.model, run.error))
        question = data.get("ask_user")
        if isinstance(question, str) and question.strip():
            question = question.strip()[:200]
            if self._ask(run, question, {"op": "continue", "name": question}):
                return said("retry", "the user answered; trying again")
            run.error = question
            if failed is not None:
                failed.note = question
            return said("give_up", "the user did not continue: %s" % question)
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        if data.get("completed") is True:
            if quote_on_screen(str(data.get("quote") or ""), snapshot):
                (failed if failed is not None else scope).evidence = "model"
                return said("completed", "%s showed it done on the screen: %r" % (
                    run.model, str(data.get("quote"))[:80]))
            if not steps:
                return said("escalate_up", "%s said done without a quote from the screen" % run.model)
        if data.get("skip") is True and failed is not None and failed.optional:
            return said("skip", "%s skips an optional item: %s" % (run.model, failed.goal[:80]))
        if data.get("escalate_up") is True:
            return said("escalate_up", "%s says the level above must re-plan" % run.model)
        blocked = {goal_key(g)[0] for g in twice}
        steps = [s for s in steps if not (
            isinstance(s, (str, dict)) and goal_key(s if isinstance(s, str) else
                                                    str(s.get("goal") or ""))[0] in blocked)]
        room = max(0, run.max_leaves - len([n for n in run.tree.leaves() if not n.superseded]))
        children: List[PlanNode] = []
        if steps and room:
            try:
                children = coerce_tree(steps, depth=scope.depth + 1,
                                       id_prefix=repair_prefix(scope, scope.generation + 1),
                                       source="repair", make_step=_step_for,
                                       max_items=min(MAX_CHILDREN, room))
            except ValueError:
                children = []
        if not children:
            return said("escalate_up", "%s proposed nothing new to try" % run.model)
        self._replace_pending(scope, children)
        return said("revised", "%s revised it: %d item%s" % (
            run.model, len(children), "" if len(children) == 1 else "s"))

    # ------------------------------------------------------------ resume
    def _resume_leaf(self, run: Run, llm: Any, node: PlanNode) -> str:
        """An interrupted goal: ``done`` (the screen proves it), ``run`` (safe
        to run again), ``handled`` (a repair decided) or ``failed``.

        A goal that had acted is never simply rerun: the text may be typed
        already, the file saved. Code looks first; then the model is asked
        with ``interrupted`` and ``acted`` in view, or — Jev only — the user.
        """
        self._current_node = node
        if not run.dry_run:
            self._before_goal(run, node, launch=False)
            snapshot = self._fresh_observation()
            phase = node.parent
            ok = False
            if snapshot is not None and node.step is not None:
                ok = bool(self._code_verify(run, node.step, snapshot)[0])
                if not ok and phase is not None and phase is not run.tree and phase.checks:
                    ok = evaluate_checks(phase.checks, snapshot)[0] is True
            if ok:
                run.learned["code_verified"] += 1
                self._set_node(node, "done", evidence="code")
                self._event(run, "resume", "interrupted goal already done — verified in code: %s"
                            % node.goal[:100], {"node_id": node.id})
                self._checkpoint(run, force=True)
                return "done"
        if not node.acted and not node.irreversible:
            self._resumed_leaf = node.id
            self._set_node(node, "pending")
            return "run"
        if llm is not None:
            scope = node.parent or run.tree
            snapshot = None if run.dry_run else self._last_snapshot
            if scope.repairs < MAX_REPAIRS_PER_NODE and run.repairs_total < MAX_REPAIRS_TOTAL:
                scope.repairs += 1
                run.repairs_total += 1
                run.replans = run.repairs_total
                verdict = self._tree_repair(run, llm, scope, node, None, "interrupted", snapshot,
                                            interrupted=True)
            else:
                verdict = "escalate_up"
            if verdict == "completed":
                self._set_node(node, "done", evidence=node.evidence or "model")
                return "handled"
            if verdict == "revised":
                self._set_node(node, "failed")
                node.superseded = True
                return "handled"
            if verdict == "retry":
                self._resumed_leaf = node.id
                self._set_node(node, "pending")
                return "run"
            if verdict == "skip":
                self._set_node(node, "skipped")
                return "handled"
            return "failed"
        question = "resume: '%s' had already acted %d time%s before the interruption; run it " \
                   "again?" % (node.goal[:120], node.acted, "" if node.acted == 1 else "s")
        if self._ask(run, question, {"op": "redo", "name": node.goal[:120]}):
            self._resumed_leaf = node.id
            self._set_node(node, "pending")
            return "run"
        node.stop_reason = node.stop_reason or "redo_refused"
        node.note = node.note or "not run again after the interruption"
        return "failed"

    # ------------------------------------------------------------ pause
    def _pause_point(self, run: Run) -> None:
        """Hold here, between goals, while a pause is requested."""
        if not self._pause_requested:
            return
        paused_at = time.time()
        run.paused_since = paused_at
        self._set_state(run, "paused")
        self._event(run, "pause", "paused between goals — nothing acts until you continue; "
                                  "STOP still works")
        self._checkpoint(run, force=True)
        try:
            while self._pause_requested:
                self._resume_event.wait(0.2)
                self.kill.raise_if_tripped()
                if time.time() - paused_at > PAUSE_MAX_S:
                    raise Stopped("paused for more than %.0f min" % (PAUSE_MAX_S / 60.0))
                self._checkpoint(run)
        finally:
            run.paused_s += time.time() - paused_at
            run.paused_since = 0.0
        self._set_state(run, "running")
        self._event(run, "resume", "continuing after %.1f s paused" % (time.time() - paused_at))
        self._checkpoint(run, force=True)

    # ------------------------------------------------------------ journal
    def _checkpoint(self, run: Run, force: bool = False) -> None:
        """Write the run's state so it can be resumed; a heartbeat when not
        forced (at most every HEARTBEAT_S). A failed write warns once and the
        run goes on — it only loses the ability to resume."""
        if not run.persist:
            return
        now = time.time()
        if not force and now - self._last_checkpoint < HEARTBEAT_S:
            return
        self._last_checkpoint = now
        run.heartbeat_at = now
        try:
            ok = self._journal.checkpoint(run.id, self._checkpoint_payload(run))
        except Exception:
            ok = False
        if not ok and not self._checkpoint_warned:
            self._checkpoint_warned = True
            self._event(run, "checkpoint", "could not write the checkpoint under %s; the run goes "
                                           "on but cannot be resumed" % self._journal.root)

    def _checkpoint_payload(self, run: Run) -> Dict[str, Any]:
        hwnd = int(self._target.get("hwnd") or 0)
        return {
            "schema": 1, "run_id": run.id, "command": run.command, "model": run.model,
            "dry_run": run.dry_run, "hierarchical": run.hierarchical, "memory": run.memory,
            "limits": {"max_steps": run.max_steps, "budget_s": run.budget_s,
                       "total_budget_s": run.total_budget_s, "usd_cap": run.usd_cap,
                       "llm_cap": run.llm_cap, "llm_cap_explicit": run.llm_cap_explicit,
                       "max_leaves": run.max_leaves},
            "state": run.state, "stop_reason": run.stop_reason, "error": run.error,
            "plan_source": run.plan_source, "plan_memory": run.plan_memory,
            "tree": run.tree.to_dict() if run.tree is not None else None,
            "spend": dict(run.spend), "learned": dict(run.learned),
            "repairs_total": run.repairs_total,
            "target": {"hwnd": hwnd, "pid": int(self._target.get("pid") or 0),
                       "process": self._target_app(), "title": self._title_of(hwnd) if hwnd else ""},
            "active_s": round(run.active_seconds(), 2), "paused_s": round(run.paused_s, 2),
            "segment": run.segment, "events_next": run.events_base + len(run.events),
            "heartbeat_at": run.heartbeat_at, "pid": os.getpid(), "progress": run.progress(),
        }

    def _charge(self, **amounts: float) -> None:
        """Roll spend up the current node's path, root included."""
        node = self._current_node
        while node is not None:
            for key, value in amounts.items():
                node.spend[key] = node.spend.get(key, 0) + value
            node = node.parent


    # ------------------------------------------------------------ LLM plumbing
    def _model_id(self, model: Optional[str]) -> Optional[str]:
        if model is None:
            return None
        model = str(model).strip()
        return None if model.lower() in ("", "none", "jev", "jev-only", "off") else model

    def _make_llm(self, model: Optional[str]) -> Any:
        model_id = self._model_id(model)
        return self._llm_factory(model_id) if model_id else None

    @staticmethod
    def _default_llm(model: str) -> Any:
        models, _source = cached_models()
        return OpenRouterLLM(model, pricing=pricing_table(models))

    def _llm(self, run: Run, llm: Any, purpose: str, system: str, user: str,
             **chat_kw: Any) -> Any:
        if run.spend["llm_calls"] >= run.llm_cap:
            self._event(run, "llm_cap", "the planning model was asked %d times; no more this run"
                        % run.llm_cap)
            return None
        self.kill.raise_if_tripped()
        try:
            reply = llm.chat(system, user, **chat_kw)
        except LLMError as exc:
            self._event(run, "llm_error", "%s: %s" % (purpose, str(exc)[:200]))
            return None
        run.spend["llm_calls"] += 1
        run.spend["llm_tokens"] += reply.tokens_in + reply.tokens_out
        run.spend["llm_usd"] += float(reply.cost_usd)
        run.spend["usd"] = run.spend["jev_usd"] + run.spend["llm_usd"]
        self._charge(llm_calls=1, llm_usd=float(reply.cost_usd))
        self._event(run, "llm", "%s · %s · %d+%d tok · $%.6f · %.0f ms" % (
            purpose, reply.model, reply.tokens_in, reply.tokens_out, reply.cost_usd,
            reply.latency_ms))
        ledger = self._ledger_or_none()
        if ledger is not None:
            try:
                ledger.record(which="cu_llm", intent=("%s: %s" % (purpose, run.command))[:80],
                              latency_ms=reply.latency_ms, tokens_in=reply.tokens_in,
                              tokens_out=reply.tokens_out, cost_usd=reply.cost_usd,
                              session_id=run.id,
                              extra={"model": reply.model, "purpose": purpose,
                                     "cost_source": reply.cost_source, "dry_run": run.dry_run})
            except Exception:
                pass
        self._check_budget(run)
        return reply

    def _screen(self, snapshot: Any, candidates: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
        """What the model sees: title, app and up to 150 reduced elements."""
        if snapshot is None:
            return {}
        try:
            from .observe import to_state

            state = to_state(snapshot, elements=list(candidates) if candidates else None)
        except Exception:
            state = {"window_title": getattr(snapshot, "window_title", ""),
                     "app": getattr(snapshot, "app", ""), "elements": {}}
        elements = state.get("elements") or {}
        if len(elements) > ELEMENTS_FOR_LLM:
            elements = dict(list(elements.items())[:ELEMENTS_FOR_LLM])
        return {"window_title": state.get("window_title", ""), "app": state.get("app", ""),
                "elements": elements}

    # ------------------------------------------------------------- simulation
    def _simulate(self, goal: str, options: RunOptions, **hooks: Any) -> RunResult:
        """A dry run's stand-in for the loop: three synthetic steps per goal.

        Exercises every hook the real loop would call — ``on_step``, ``confirm``
        on a goal that names something destructive, ``compose_text`` on a goal
        that types — with a short delay so the page's live view can be watched.
        No desktop, no Jev call, no spend. Each record says ``simulated``.
        """
        started = time.perf_counter()
        result = RunResult(task_id=hooks.get("task_id", ""), stop_reason="", steps=[])
        on_step = hooks.get("on_step")
        confirm = hooks.get("confirm")
        compose = hooks.get("compose_text")
        lowered = goal.lower()
        typing = any(word in lowered for word in ("type", "enter", "write", "wpisz", "napisz"))
        destructive = act_module.is_destructive_name(goal) or any(
            word in lowered for word in ("usuń", "skasuj", "wyślij", "zapłać"))
        script = [("click", "e1", 0.93)]
        if typing:
            script.append(("type", "e4", 0.88))
        script.append(("done", None, 0.96))
        for index, (op, target, confidence) in enumerate(script):
            self.kill.raise_if_tripped()
            time.sleep(0.35)
            record = StepRecord(index=index, t_ms=(time.perf_counter() - started) * 1000.0,
                                stages_ms={"observe": 4.0, "reduce": 0.6, "decide": 280.0,
                                           "validate": 0.1, "act": 1.0},
                                candidates=12, op=op, target=target,
                                decided_by="simulated", confidence=confidence,
                                margin=0.6, executed=False, note="simulated")
            if op == "type" and compose is not None:
                try:
                    text = compose(goal, act_module.Action(op="type", target=target))
                    record.note = "simulated · would type %d chars" % len(text)
                except Exception as exc:
                    record.note = "simulated · needs text: %s" % type(exc).__name__
                    result.steps.append(record)
                    if on_step:
                        on_step(record)
                    result.stop_reason = "escalated"
                    result.wall_ms = (time.perf_counter() - started) * 1000.0
                    return result
            if op == "click" and destructive and confirm is not None:
                allowed = confirm("%s %r?" % (op, goal[:40]),
                                  act_module.Action(op=op, target=target), None)
                if not allowed:
                    record.note = "simulated · destructive gate refused"
                    result.steps.append(record)
                    if on_step:
                        on_step(record)
                    result.stop_reason = "blocked"
                    result.wall_ms = (time.perf_counter() - started) * 1000.0
                    return result
                record.is_destructive = 0.9
            if op != "done":
                record.executed = True
            result.steps.append(record)
            if on_step:
                on_step(record)
        result.stop_reason = "done"
        result.wall_ms = (time.perf_counter() - started) * 1000.0
        return result

    # ------------------------------------------------------------- plumbing
    def _client(self) -> Any:
        if self._client_getter is None:
            from ..client import JevClient
            from ..config import Config

            self._client_getter = functools.partial(JevClient, Config())
            client = self._client_getter()
            self._client_getter = lambda c=client: c  # type: ignore[misc]
            return client
        client = self._client_getter()
        if client is None:
            raise JevConfigError("no Jev key: set JEV_API_KEY (vendor) or OPENROUTER_API_KEY")
        return client

    def _ledger_or_none(self) -> Any:
        if self._ledger is None:
            try:
                self._ledger = Ledger(root=self.ledger_root)
            except Exception:
                return None
        return self._ledger

    def _check_budget(self, run: Run) -> None:
        elapsed = time.time() - run.started_at
        if elapsed > run.total_budget_s:
            raise Stopped("total budget of %.0f s spent" % run.total_budget_s)
        if run.usd_cap and run.spend["usd"] > run.usd_cap:
            raise Stopped("spend cap of $%.2f reached ($%.4f)" % (run.usd_cap, run.spend["usd"]))

    def _kill_text(self) -> str:
        info = self.kill.describe()
        ways = ["the STOP button"]
        if info["hotkey"]:
            ways.append(info["hotkey"])
        if info["corner"]:
            ways.append("mouse to the top-left corner")
        ways.append("`jevskill cu stop`")
        return "to stop: " + " · ".join(ways)

    def _set_state(self, run: Run, state: str) -> None:
        run.state = state

    def _give_back(self, run: Run) -> None:
        """Return the foreground to the window the user had before a key chord
        took it — only if the agent's window still holds it. A user who has
        already clicked elsewhere is not yanked back to where they were."""
        user, self._user_front = self._user_front, 0
        if run.dry_run or not user or not self._target.get("pid"):
            return
        front = self._hwnd_or_zero()
        try:
            ours = bool(front) and int(self._window_pid(front) or 0) == self._target["pid"]
        except Exception:
            ours = False
        if not ours or front == user:
            return
        try:
            back = bool(self._bring_to_front(user))
        except Exception:
            back = False
        self._event(run, "focus", "%s the foreground to %s" % (
            "gave back" if back else "could not give back", self._title_of(user) or "the user's window"))

    def _finish(self, run: Run, state: str, *, stop_reason: str = "", error: str = "") -> None:
        if run.ended_at is None:
            try:
                self._give_back(run)
            except Exception:
                pass
        with self._lock:
            if run.ended_at is not None:
                return
            run.state = state
            run.ended_at = time.time()
            run.stop_reason = stop_reason or run.stop_reason
            run.error = error or run.error
            run.pending_confirm = None
            run.paused_since = 0.0
            if run.tree is not None:
                # The tree keeps what is left to do — a STOP leaves the goal it
                # interrupted `stopped` and everything after it `pending`, which
                # is what a resume starts from. The flat view below reports the
                # run's end the way it always has.
                for node in run.tree.walk():
                    if node.kind == "leaf" and node.status == "running":
                        node.status = "stopped" if state == "stopped" else "failed"
                if state == "done":
                    run.tree.status = "done"
            for step in run.plan:
                if step.status == "running":
                    step.status = "stopped" if state == "stopped" else "failed"
                elif step.status == "pending" and state != "done":
                    step.status = "skipped"
        summary = "%s after %.1f s · %d goal%s · jev $%.6f · llm $%.6f" % (
            state, run.ended_at - run.started_at, len(run.plan),
            "" if len(run.plan) == 1 else "s", run.spend["jev_usd"], run.spend["llm_usd"])
        if stop_reason:
            summary += " · " + stop_reason
        if error:
            summary += " · " + error
        self._event(run, "end", summary)
        self._checkpoint(run, force=True)

    def _event(self, run: Run, kind: str, text: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append an event: to the in-memory ring the page polls and, for a
        real run, to ``events.jsonl``. The list used to stop growing at
        EVENT_LIMIT and drop everything after — a long mission's end, which is
        the part a person reads. Now the oldest go, ``events_base`` counts them,
        and the file keeps all of them."""
        with self._lock:
            item: Dict[str, Any] = {"i": run.events_base + len(run.events),
                                    "t": round(time.time() - run.started_at, 2),
                                    "kind": kind, "text": text}
            if data:
                item["data"] = data
            run.events.append(item)
            overflow = len(run.events) - EVENT_LIMIT
            if overflow > 0:
                del run.events[:overflow]
                run.events_base += overflow
        if run.persist:
            try:
                self._journal.append_event(run.id, item)
            except Exception:
                pass


__all__ = ["CONFIRM_TIMEOUT_S", "CONSOLE_TITLE", "HEARTBEAT_S", "LAUNCH_ALLOW",
           "LLM_CAP_CEIL", "MAX_LLM_CALLS", "MAX_PLAN_STEPS", "MAX_REPLANS", "NotRunning",
           "Operator", "OperatorBusy", "OperatorUnavailable", "PAUSE_MAX_S", "PlanStep", "Run",
           "RunNotResumable", "UnknownRun", "platform_status", "quoted_text"]

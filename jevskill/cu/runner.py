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
from .contract import RunOptions, RunResult, StepRecord
from .experience import (Experience, Recipe, RecipeStep, Trajectory, command_key,
                         describe, extract_slots, fill, has_markers, identity_of,
                         literal_check, recipe_check, resolve)
from .hashing import tree_hash
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

_QUOTED = re.compile(r'"([^"\n]{1,400})"|„([^”\n]{1,400})”|«([^»\n]{1,400})»|`([^`\n]{1,400})`')


class OperatorBusy(RuntimeError):
    """A run is already active; this class runs one at a time."""


class OperatorUnavailable(RuntimeError):
    """A live run cannot happen here (not Windows, no ``comtypes``)."""


@dataclass
class PlanStep:
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

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "done_when": self.done_when, "launch": self.launch,
                "status": self.status, "stop_reason": self.stop_reason, "note": self.note,
                "steps": self.steps, "tokens_in": self.tokens_in,
                "cost_usd": round(self.cost_usd, 8), "wall_ms": round(self.wall_ms, 1)}


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

    @property
    def active(self) -> bool:
        return self.state in ("planning", "waiting_window", "running", "waiting_confirm")

    def to_dict(self, since: int = 0) -> Dict[str, Any]:
        events = self.events[max(0, int(since)):]
        return {
            "run_id": self.id, "command": self.command, "model": self.model,
            "dry_run": self.dry_run, "state": self.state,
            "plan": [step.to_dict() for step in self.plan], "current": self.current,
            "events": events, "next": len(self.events),
            "pending_confirm": self.pending_confirm,
            "spend": {k: (round(v, 8) if isinstance(v, float) else v)
                      for k, v in self.spend.items()},
            "started_at": self.started_at, "ended_at": self.ended_at,
            "elapsed_s": round((self.ended_at or time.time()) - self.started_at, 1),
            "stop_reason": self.stop_reason, "error": self.error,
            "limits": {"max_steps": self.max_steps, "budget_s": self.budget_s,
                       "total_budget_s": self.total_budget_s, "usd_cap": self.usd_cap},
            "memory": dict(self.learned, enabled=self.memory, plan_source=self.plan_source),
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

    # ------------------------------------------------------------------ public
    @property
    def busy(self) -> bool:
        run = self.run
        return bool(run is not None and run.active)

    def status(self, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            payload: Dict[str, Any] = {"state": "idle", "run": None, "events": [], "next": 0}
            if self.run is not None:
                payload = self.run.to_dict(since)
                payload["run"] = self.run.id
            payload["busy"] = self.busy
            payload["platform"] = self._platform_check()
            payload["kill_switch"] = self.kill.describe()
            return payload

    def plan(self, command: str, model: Optional[str], *, memory: bool = True) -> Dict[str, Any]:
        """Plan without running. Touches nothing; calls the model unless the
        command's plan is remembered (``memory=False`` asks the model anyway)."""
        command = (command or "").strip()
        if not command:
            raise ValueError("the command is empty")
        if not self.busy:
            # A STOP that ended the previous run must not veto this planning
            # call: the switch is armed per run, and between runs it is silent.
            self.kill.reset()
        remembered = self.experience.plan_for(command) if memory else None
        llm = self._make_llm(model) if remembered is None else None
        scratch = Run(id="plan", command=command, model=self._model_id(model),
                      memory=bool(memory))
        steps, note = self._plan(scratch, llm, command)
        return {"steps": [s.to_dict() for s in steps], "note": note,
                "model": scratch.model, "spend": scratch.spend,
                "events": scratch.events, "source": scratch.plan_source}

    def start(self, command: str, model: Optional[str] = None, *, dry_run: bool = False,
              plan: Optional[Sequence[Any]] = None, max_steps: int = 25,
              budget_s: float = 90.0, total_budget_s: float = 600.0,
              usd_cap: float = 0.5, memory: bool = True) -> str:
        command = (command or "").strip()
        if not command:
            raise ValueError("the command is empty")
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
                      usd_cap=max(0.0, float(usd_cap)), memory=bool(memory))
            if plan:
                run.plan = [self._coerce_step(item) for item in plan if item]
                if not run.plan:
                    raise ValueError("the supplied plan has no usable steps")
                run.plan_source = "user"
            self.run = run
            self._verify_cache = {}
            self._target = {"hwnd": 0, "pid": 0}
            self._abort_goal = ""
            self._exec_failures = 0
            self._agreed_done = ""
            self._trajectory = None
            self._user_front = 0
            self._goal_replayed = False
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
        return self.status()

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
        self._event(run, "start", "run %s — %s%s" % (
            run.id, "DRY RUN (simulated), " if run.dry_run else "",
            "model %s" % run.model if run.model else "Jev only, no planning model"))
        self._event(run, "kill_switch", self._kill_text())
        if not run.plan:
            self._set_state(run, "planning")
            steps, note = self._plan(run, llm, run.command)
            run.plan = steps
            if note:
                self._event(run, "plan_note", note)
        self._event(run, "plan", "%d step%s" % (len(run.plan), "" if len(run.plan) == 1 else "s"),
                    {"steps": [s.to_dict() for s in run.plan]})
        index = 0
        while index < len(run.plan):
            self._check_budget(run)
            step = run.plan[index]
            run.current = index
            step.status = "running"
            self._set_state(run, "running")
            self._event(run, "goal", "goal %d/%d: %s" % (index + 1, len(run.plan), step.goal),
                        {"index": index})
            self._before_goal(run, step, index)
            self._trajectory = Trajectory(cap=OPERATOR_CAP)
            self._goal_replayed = False
            goal_started = time.perf_counter()
            result = self._run_goal(run, llm, step, index)
            step.stop_reason = result.stop_reason
            step.steps = len(result.steps)
            step.wall_ms = result.wall_ms
            step.tokens_in += result.tokens_in
            step.cost_usd += result.cost_usd
            if result.stop_reason == "error" and result.error.startswith("Stopped"):
                raise Stopped(result.error.split(":", 1)[-1].strip() or self.kill.reason)
            if self.kill.event.is_set():
                # STOP that landed while the goal was waiting on a confirmation:
                # the gate answered "deny", the loop ended the goal as `blocked`,
                # and without this check a Jev-only run would go on to report
                # itself *failed* — the user pressed STOP, and that is the reason.
                raise Stopped(self.kill.reason or "stopped")
            agreed = self._agreed_done if result.stop_reason == "escalated" else ""
            if result.stop_reason == "done" or agreed:
                step.status = "done"
                if agreed:
                    step.stop_reason = "done"
                    step.note = agreed[:200]
                self._event(run, "goal_done", "goal %d done after %d step%s%s%s" % (
                    index + 1, step.steps, "" if step.steps == 1 else "s",
                    " from memory" if self._goal_replayed else "",
                    (" — %s judged it complete: %s" % (run.model, agreed)) if agreed else ""))
                self._learn_goal(run, step, True, goal_started)
                index += 1
                continue
            step.note = (result.error or (result.steps[-1].note if result.steps else ""))[:200]
            self._event(run, "goal_stalled", "goal %d ended: %s%s" % (
                index + 1, result.stop_reason, (" — " + step.note) if step.note else ""))
            verdict = self._replan(run, llm, index, result)
            if verdict == "completed":
                step.status = "done"
                self._learn_goal(run, step, True, goal_started)
                index += 1
                continue
            self._learn_goal(run, step, False, goal_started)
            if verdict == "revised":
                step.status = "failed"
                index += 1
                continue
            step.status = "failed"
            self._forget_failed_plan(run)
            self._finish(run, "failed", stop_reason=result.stop_reason,
                         error=step.note or result.stop_reason)
            return
        self._learn_plan(run)
        self._finish(run, "done")

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
        if run.dry_run or not run.memory:
            return
        done = [s.to_dict() for s in run.plan if s.status == "done"]
        if not done or run.plan_source == "command":
            return
        try:
            self.experience.learn_plan(run.command, done, model=run.model or "")
        except Exception:
            return
        if run.plan_source != "memory":
            self._event(run, "memory", "plan remembered: %d goal%s; the same command skips "
                        "the planning call next time" % (len(done), "" if len(done) == 1 else "s"))

    def _forget_failed_plan(self, run: Run) -> None:
        if run.dry_run or not run.memory or run.plan_source != "memory":
            return
        try:
            self.experience.plan_failed(run.command)
        except Exception:
            pass

    def _before_goal(self, run: Run, step: PlanStep, index: int) -> None:
        if run.dry_run:
            if step.launch:
                self._event(run, "launch", "would launch %s (dry run)" % step.launch)
            return
        if step.launch:
            self._launch(run, step)
            return
        if index == 0:
            self._wait_for_target_window(run)

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
        self._launcher(target)
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

    def _run_goal(self, run: Run, llm: Any, step: PlanStep, index: int) -> RunResult:
        options = RunOptions(max_steps=run.max_steps, budget_s=run.budget_s,
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
        backend = self._backend_factory()
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

    def _memory_context(self, run: Run, step: PlanStep) -> Dict[str, Any]:
        """What a model is told about this goal's history, when there is any.

        Measured live (2026-09-21): the escalation model was asked, in two
        separate attempts, to scroll a navigation pane that refused ScrollItem
        both times, and navigated folders it could not see instead of typing a
        path. It was never told what had already been tried.
        """
        out: Dict[str, Any] = {}
        trajectory = self._trajectory
        if trajectory is not None:
            tried = trajectory.tried()
            if tried:
                out["tried"] = tried
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
        if self._needs_foreground(action, snapshot):
            self._ensure_target_in_front("action")
        result = act_module.execute(action, snapshot, backend=backend, dry_run=dry_run)
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
        run.spend["jev_calls"] += 1 if record.decided_by in ("jev", "cascade") else 0
        run.spend["jev_tokens"] += int(record.tokens_in)
        run.spend["jev_usd"] += float(record.cost_usd)
        run.spend["usd"] = run.spend["jev_usd"] + run.spend["llm_usd"]
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
        answered = self._confirm_event.wait(CONFIRM_TIMEOUT_S)
        with self._lock:
            allow = bool(self._confirm_answer) if answered else False
            run.pending_confirm = None
            if run.active:
                self._set_state(run, "running")
        if self.kill.event.is_set():
            allow = False
        self._event(run, "confirm_result", "%s: %s" % (
            "allowed" if allow else ("denied" if answered else "timed out, denied"), prompt))
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
            self._event(run, "verify", "verified in code, no model call — %s" % why)
            return True
        if llm is None:
            # Jev only: code could not tell, and there is nobody else to ask.
            # Abstaining keeps the loop's own rule (two `done` in a row).
            return None
        key = "%s|%s" % (step.goal, tree_hash(list(getattr(snapshot, "elements", []) or [])))
        if key in self._verify_cache:
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
    def _plan(self, run: Run, llm: Any, command: str):
        remembered = None
        if run.memory:
            try:
                remembered = self.experience.plan_for(command)
            except Exception:
                remembered = None
        if remembered is not None:
            # The planning call was 6.3 s of the measured 60.1 s run, and it
            # re-derived goals a successful run had already found. Values are
            # the new command's own (see experience.fill).
            items, memory = remembered
            run.plan_source = "memory"
            self._event(run, "memory", "plan from memory: %d goal%s, %d earlier success%s — "
                                       "no planning call" % (
                                           len(items), "" if len(items) == 1 else "s",
                                           memory.successes, "" if memory.successes == 1 else "es"))
            return [self._coerce_step(item) for item in items], \
                "remembered from %d successful run%s" % (
                    memory.successes, "" if memory.successes == 1 else "s")
        if llm is None:
            run.plan_source = "command"
            return [PlanStep(goal=command, done_when="")], "no planning model: the command is the goal"
        context: Dict[str, Any] = {"command": command, "os": "Windows",
                                   "foreground": (self._title_or_empty() if not run.dry_run else "")}
        if run.memory:
            try:
                known = self.experience.skills_for()
            except Exception:
                known = []
            if known:
                context["known_goals"] = known
        reply = self._llm(run, llm, "plan", (
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
            "steps that delete, send, pay or overwrite unless the command says so. Reply "
            "with JSON only: {\"steps\": [{\"goal\": \"...\", \"done_when\": \"...\", "
            "\"launch\": null}], \"note\": \"...\"}" % MAX_PLAN_STEPS),
            json.dumps(context, ensure_ascii=False))
        data = reply.json() if reply is not None else None
        steps = self._steps_from(data)
        if not steps:
            self._event(run, "plan_fallback", "the model returned no usable plan; "
                                              "running the command as one goal")
            run.plan_source = "command"
            return [PlanStep(goal=command)], ""
        run.plan_source = "model"
        note = str(data.get("note", ""))[:300] if isinstance(data, dict) else ""
        return steps, note

    def _replan(self, run: Run, llm: Any, index: int, result: RunResult) -> str:
        """``"completed"``, ``"revised"`` (plan changed, move on) or ``"give_up"``.

        The screen is checked in code first. Measured live (2026-09-21): the
        save goal ended `escalated` *after* the file was saved (Jev proposed
        `type none` on the saved window) and a re-plan call spent 1.8 s to read
        ``hello.txt - Notatnik`` off the title.
        """
        step = run.plan[index]
        snapshot = None
        if not run.dry_run:
            try:
                snapshot = self._observe_hook()
            except Stopped:
                raise
            except Exception:
                snapshot = None
            verified, why = self._code_verify(run, step, snapshot)
            if verified:
                run.learned["code_verified"] += 1
                self._event(run, "replan", "goal %d is complete — verified in code, no model "
                                           "call: %s" % (index + 1, why))
                return "completed"
        if llm is None or run.replans >= MAX_REPLANS:
            return "give_up"
        run.replans += 1
        remaining = [s.to_dict() for s in run.plan[index + 1:]]
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
                             "stop_reason": result.stop_reason, "error": result.error,
                             "remaining": remaining, "screen": screen},
                            **self._memory_context(run, step)), ensure_ascii=False))
        data = reply.json() if reply is not None else None
        if not isinstance(data, dict):
            return "give_up"
        if data.get("completed") is True:
            self._event(run, "replan", "%s judged goal %d complete" % (run.model, index + 1))
            return "completed"
        if data.get("give_up"):
            self._event(run, "replan", "%s gives up: %s" % (run.model, str(data["give_up"])[:200]))
            run.error = str(data["give_up"])[:200]
            return "give_up"
        steps = self._steps_from(data)
        if not steps:
            return "give_up"
        del run.plan[index + 1:]
        run.plan.extend(steps)
        self._event(run, "replan", "%s revised the plan: %d step%s remain" % (
            run.model, len(steps), "" if len(steps) == 1 else "s"),
            {"steps": [s.to_dict() for s in steps]})
        return "revised"

    def _steps_from(self, data: Any) -> List[PlanStep]:
        items = data.get("steps") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        out: List[PlanStep] = []
        for item in items[:MAX_PLAN_STEPS]:
            try:
                step = self._coerce_step(item)
            except ValueError:
                continue
            out.append(step)
        return out

    @staticmethod
    def _coerce_step(item: Any) -> PlanStep:
        if isinstance(item, str):
            goal = item.strip()
            if not goal:
                raise ValueError("empty goal")
            return PlanStep(goal=goal)
        if isinstance(item, dict):
            goal = str(item.get("goal") or "").strip()
            if not goal:
                raise ValueError("empty goal")
            launch = item.get("launch")
            launch = str(launch).strip() if isinstance(launch, str) and launch.strip() else None
            return PlanStep(goal=goal[:400], done_when=str(item.get("done_when") or "")[:300],
                            launch=launch)
        raise ValueError("a step is a string or an object")

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
        if run.spend["llm_calls"] >= MAX_LLM_CALLS:
            self._event(run, "llm_cap", "the planning model was asked %d times; no more this run"
                        % MAX_LLM_CALLS)
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

    def _event(self, run: Run, kind: str, text: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if len(run.events) >= EVENT_LIMIT:
                return
            item: Dict[str, Any] = {"i": len(run.events), "t": round(time.time() - run.started_at, 2),
                                    "kind": kind, "text": text}
            if data:
                item["data"] = data
            run.events.append(item)


__all__ = ["CONFIRM_TIMEOUT_S", "CONSOLE_TITLE", "LAUNCH_ALLOW", "MAX_LLM_CALLS",
           "MAX_PLAN_STEPS", "MAX_REPLANS", "Operator", "OperatorBusy",
           "OperatorUnavailable", "PlanStep", "Run", "platform_status", "quoted_text"]

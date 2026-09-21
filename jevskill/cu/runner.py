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
from .killswitch import STOP_FILE_NAME, KillSwitch, Stopped
from .llm import LLMError, OpenRouterLLM, cached_models, pricing_table
from .types import Snapshot

CONSOLE_TITLE = "jev · console"
MAX_PLAN_STEPS = 8
MAX_REPLANS = 2
MAX_LLM_CALLS = 40
CONFIRM_TIMEOUT_S = 120.0
WINDOW_WAIT_S = 10.0
LAUNCH_SETTLE_S = 1.5
LAUNCH_WAIT_S = 5.0
ELEMENTS_FOR_LLM = 60
EVENT_LIMIT = 2000

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

    return snapshot()


def _default_foreground_title() -> str:
    from .observe import foreground_hwnd, window_title

    return window_title(foreground_hwnd())


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
                 launcher: Optional[Callable[[str], Any]] = None,
                 platform_check: Optional[Callable[[], Dict[str, Any]]] = None,
                 kill_switch: Optional[KillSwitch] = None,
                 ledger: Any = None) -> None:
        self.ledger_root = ledger_root
        self._client_getter = client_getter
        self._llm_factory = llm_factory or self._default_llm
        self._run_loop = run_loop
        self._observe = observe or _default_observe
        self._backend_factory = backend_factory or (lambda: act_module.UiaBackend())
        self._foreground_title = foreground_title or _default_foreground_title
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

    def plan(self, command: str, model: Optional[str]) -> Dict[str, Any]:
        """Plan without running. Calls the model; touches nothing."""
        command = (command or "").strip()
        if not command:
            raise ValueError("the command is empty")
        llm = self._make_llm(model)
        scratch = Run(id="plan", command=command, model=self._model_id(model))
        steps, note = self._plan(scratch, llm, command)
        return {"steps": [s.to_dict() for s in steps], "note": note,
                "model": scratch.model, "spend": scratch.spend,
                "events": scratch.events}

    def start(self, command: str, model: Optional[str] = None, *, dry_run: bool = False,
              plan: Optional[Sequence[Any]] = None, max_steps: int = 25,
              budget_s: float = 90.0, total_budget_s: float = 600.0,
              usd_cap: float = 0.5) -> str:
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
                      usd_cap=max(0.0, float(usd_cap)))
            if plan:
                run.plan = [self._coerce_step(item) for item in plan if item]
                if not run.plan:
                    raise ValueError("the supplied plan has no usable steps")
            self.run = run
            self._verify_cache = {}
            self._thread = threading.Thread(target=self._main, args=(run, llm),
                                            name="jev-operator", daemon=True)
            self._thread.start()
            return run.id

    def stop(self, reason: str = "STOP button") -> Dict[str, Any]:
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
            if result.stop_reason == "done":
                step.status = "done"
                self._event(run, "goal_done", "goal %d done after %d step%s" % (
                    index + 1, step.steps, "" if step.steps == 1 else "s"))
                index += 1
                continue
            step.note = (result.error or (result.steps[-1].note if result.steps else ""))[:200]
            self._event(run, "goal_stalled", "goal %d ended: %s%s" % (
                index + 1, result.stop_reason, (" — " + step.note) if step.note else ""))
            verdict = self._replan(run, llm, index, result)
            if verdict == "completed":
                step.status = "done"
                index += 1
                continue
            if verdict == "revised":
                step.status = "failed"
                index += 1
                continue
            step.status = "failed"
            self._finish(run, "failed", stop_reason=result.stop_reason,
                         error=step.note or result.stop_reason)
            return
        self._finish(run, "done")

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
        before = self._title_or_empty()
        self._event(run, "launch", "launching %s" % target)
        self._launcher(target)
        time.sleep(LAUNCH_SETTLE_S)
        deadline = time.time() + LAUNCH_WAIT_S
        while time.time() < deadline and self._title_or_empty() == before:
            self.kill.raise_if_tripped()
            time.sleep(0.25)

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
        if warned:
            self._event(run, "window", "working in: %s" % self._title_or_empty())
        self._set_state(run, "running")

    def _title_or_empty(self) -> str:
        try:
            return str(self._foreground_title() or "")
        except Exception:
            return ""

    def _run_goal(self, run: Run, llm: Any, step: PlanStep, index: int) -> RunResult:
        options = RunOptions(max_steps=run.max_steps, budget_s=run.budget_s,
                             dry_run=run.dry_run)
        hooks = dict(
            task_id="%s:%d" % (run.id, index), run=0,
            observe=self._observe_hook, ledger=self._ledger_or_none(),
            on_step=functools.partial(self._on_step, run, step),
            confirm=functools.partial(self._confirm_hook, run),
            compose_text=functools.partial(self._compose_hook, run, llm, step),
            escalate=(functools.partial(self._escalate_hook, run, llm, step) if llm else None),
            verify=(functools.partial(self._verify_hook, run, llm, step) if llm else None),
        )
        if run.dry_run:
            return self._simulate(step.goal, options, **hooks)
        loop = self._run_loop
        if loop is None:
            from .loop import run as loop  # type: ignore[no-redef]
        backend = self._backend_factory()
        return loop(step.goal, options, client=self._client(),
                    execute=functools.partial(self._execute_hook, backend=backend), **hooks)

    # ------------------------------------------------------------------- hooks
    def _observe_hook(self) -> Snapshot:
        self.kill.raise_if_tripped()
        return self._observe()

    def _execute_hook(self, action: Any, snapshot: Any, *, backend: Any = None,
                      dry_run: bool = False) -> Any:
        self.kill.raise_if_tripped()
        return act_module.execute(action, snapshot, backend=backend, dry_run=dry_run)

    def _on_step(self, run: Run, step: PlanStep, record: StepRecord) -> None:
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
        literal = quoted_text(step.goal) or quoted_text(run.command)
        if literal is not None:
            self._event(run, "text", "typing the quoted text from the goal")
            return literal
        if llm is None:
            from .loop import TextNeeded

            raise TextNeeded("the step needs text and no planning model was chosen")
        reply = self._llm(run, llm, "compose", (
            "You write the exact text a desktop agent types into one field. "
            "Reply with JSON only: {\"text\": \"...\"}. No explanation, no quotes around "
            "the value beyond JSON's own."),
            json.dumps({"command": run.command, "goal": step.goal,
                        "field": {"role": getattr(element, "role", ""),
                                  "name": getattr(element, "name", ""),
                                  "value": getattr(element, "value", None)}},
                       ensure_ascii=False))
        data = reply.json() if reply is not None else None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text:
            from .loop import TextNeeded

            raise TextNeeded("the planning model did not return text")
        self._event(run, "text", "text from %s (%d chars)" % (run.model, len(text)))
        return text

    def _verify_hook(self, run: Run, llm: Any, step: PlanStep, goal: str,
                     snapshot: Any) -> bool:
        from .hashing import tree_hash

        key = "%s|%s" % (step.goal, tree_hash(list(getattr(snapshot, "elements", []) or [])))
        if key in self._verify_cache:
            return self._verify_cache[key]
        reply = self._llm(run, llm, "verify", (
            "You judge whether a desktop sub-goal is complete from the UI Automation "
            "tree of the foreground window. Be strict: unsaved, half-typed or still-open "
            "dialogs are not done. Reply with JSON only: {\"done\": true|false, \"why\": \"...\"}."),
            json.dumps({"goal": step.goal, "done_when": step.done_when,
                        "screen": self._screen(snapshot)}, ensure_ascii=False))
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
        reply = self._llm(run, llm, "escalate", (
            "A fast decision model drives a Windows UI agent one step at a time and "
            "just failed on this step. Choose the single next action from the elements "
            "listed. Ops: click, type, select, scroll_up, scroll_down, key (give the chord "
            "in `key`, e.g. ctrl+s, enter, escape), wait, or none when nothing safe applies. "
            "`target` must be an element id from the list (null for key/scroll/wait). "
            "Never choose an action that deletes, sends, pays or overwrites unless the goal "
            "says so. Reply with JSON only: {\"op\": \"...\", \"target\": \"e3\"|null, "
            "\"text\": null, \"key\": null, \"why\": \"...\"}."),
            json.dumps({"command": run.command, "goal": step.goal, "done_when": step.done_when,
                        "reason": context.get("reason"), "step": context.get("step"),
                        "last_action": context.get("last_action"),
                        "model_said": {"op": getattr(decision, "op", None),
                                       "target": getattr(decision, "target", None),
                                       "confidence": round(float(getattr(decision, "confidence", 0) or 0), 3)},
                        "screen": self._screen(snapshot, candidates)}, ensure_ascii=False))
        data = reply.json() if reply is not None else None
        if not isinstance(data, dict):
            self._event(run, "escalate", "%s gave no usable answer" % run.model)
            return None
        op = str(data.get("op") or "none").strip().lower()
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
        if llm is None:
            return [PlanStep(goal=command, done_when="")], "no planning model: the command is the goal"
        context = {"command": command, "os": "Windows",
                   "foreground": (self._title_or_empty() if not run.dry_run else "")}
        reply = self._llm(run, llm, "plan", (
            "You plan desktop tasks for a UI agent on Windows. The agent sees the UI "
            "Automation tree of the foreground window (control roles, names, values) and "
            "performs one action per step: click, type, select, scroll, a key chord, wait. "
            "It cannot see pixels, cannot browse, and cannot run programs except a launch "
            "you name. Split the command into 1-%d sub-goals. Each goal: one short English "
            "imperative completable inside a single window or dialog. Text the agent must "
            "type goes inside double quotes, verbatim, in the user's language. `done_when` "
            "is an observable end state (a window title, a control's value, a control that "
            "exists). `launch` is the program to start before the goal — notepad.exe, "
            "calc.exe, mspaint.exe, explorer.exe, a ms-settings: URI — or null. Do not add "
            "steps that delete, send, pay or overwrite unless the command says so. Reply "
            "with JSON only: {\"steps\": [{\"goal\": \"...\", \"done_when\": \"...\", "
            "\"launch\": null}], \"note\": \"...\"}" % MAX_PLAN_STEPS),
            json.dumps(context, ensure_ascii=False))
        data = reply.json() if reply is not None else None
        steps = self._steps_from(data)
        if not steps:
            self._event(run, "plan_fallback", "the model returned no usable plan; "
                                              "running the command as one goal")
            return [PlanStep(goal=command)], ""
        note = str(data.get("note", ""))[:300] if isinstance(data, dict) else ""
        return steps, note

    def _replan(self, run: Run, llm: Any, index: int, result: RunResult) -> str:
        """``"completed"``, ``"revised"`` (plan changed, move on) or ``"give_up"``."""
        if llm is None or run.replans >= MAX_REPLANS:
            return "give_up"
        run.replans += 1
        step = run.plan[index]
        remaining = [s.to_dict() for s in run.plan[index + 1:]]
        screen = {}
        try:
            screen = self._screen(self._observe_hook()) if not run.dry_run else {"dry_run": True}
        except Stopped:
            raise
        except Exception:
            pass
        reply = self._llm(run, llm, "replan", (
            "A sub-goal of a desktop task ended without the agent reporting done. Decide: "
            "if the screen shows the sub-goal is in fact complete, answer completed=true. "
            "Otherwise return the remaining sub-goals, revised so the agent can continue "
            "from this screen (same rules: one window each, quoted text verbatim, optional "
            "`launch`). If the task cannot continue safely, set give_up to the reason. Reply "
            "with JSON only: {\"completed\": false, \"steps\": [...], \"give_up\": null}"),
            json.dumps({"command": run.command, "failed_goal": step.to_dict(),
                        "stop_reason": result.stop_reason, "error": result.error,
                        "remaining": remaining, "screen": screen}, ensure_ascii=False))
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

    def _llm(self, run: Run, llm: Any, purpose: str, system: str, user: str) -> Any:
        if run.spend["llm_calls"] >= MAX_LLM_CALLS:
            self._event(run, "llm_cap", "the planning model was asked %d times; no more this run"
                        % MAX_LLM_CALLS)
            return None
        self.kill.raise_if_tripped()
        try:
            reply = llm.chat(system, user)
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
        """What the model sees: title, app and up to 60 reduced elements."""
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

    def _finish(self, run: Run, state: str, *, stop_reason: str = "", error: str = "") -> None:
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

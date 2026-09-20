"""The contract between a computer-use agent and the benchmark harness.

`bench/cu_run.py` drives *an* agent through the tasks in `bench/cu_tasks.json`;
`bench/cu_report.py` turns the rows into a table. Neither imports the loop. The
loop (`jevskill/cu/loop.py`, plan items 4.1-4.6) is written separately and may not
exist yet, and the second agent under test — `kofanlabs/typesafe-computer-use-windows`
— is not this repo's code at all. What the harness and every agent share is this
module, and nothing else.

An agent is one callable::

    def run(goal: str, opts: RunOptions) -> RunResult: ...

The harness hands over the task's ``goal`` string verbatim (`act.md` §7: the goal
is a caller argument, re-supplied every step, never re-read from the screen) and
gets back what the agent did.

**`RunResult` has no `success` field, and must not grow one.** Success is the one
number this benchmark exists to produce, and it comes from the task's `oracle`,
evaluated by the harness after the agent stops — `bench/cu_tasks.md`, "Success is
judged by the oracle only". Two measurements in this repo say why an agent's own
verdict is not evidence: `jev-ultrafast`'s note that "A `DONE` choice still
requires independent outcome verification", and `act.md` §9, where `stuck` scored
0.31-0.60 on screens that had plainly changed. The oracle verdict therefore rides
on :class:`TaskRun`, which the harness builds, not on anything the agent returns.

Everything here is stdlib and pure: dataclasses, plus :func:`summarise`,
:func:`escalation_rate` and :func:`escalations_per_task`, which are functions of
their arguments and touch no file, clock or network. That is what makes the report
testable without a desktop.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "STAGES",
    "DECIDED_BY",
    "STOP_REASONS",
    "StepRecord",
    "RunResult",
    "RunOptions",
    "TaskRun",
    "Agent",
    "escalation_rate",
    "escalations_per_task",
    "summarise",
]

#: The six stages of one loop iteration, in the order `act.md` runs them. A step
#: record's ``stages_ms`` uses these keys; a loop that fuses two stages reports the
#: fused time under the later one and leaves the earlier at 0.0 rather than
#: inventing a seventh name, so the columns stay comparable across agents.
STAGES: Tuple[str, ...] = ("observe", "reduce", "decide", "validate", "act", "settle")

#: Who chose the step. ``jev`` is a model round trip; ``macro`` is a cache hit
#: (plan item 4.6 — 0 calls, still 1 step, which is exactly why steps and
#: decisions are counted separately); ``code`` is a deterministic override (the
#: destructive name list, a liveness failure, a forced stop); ``escalation`` is the
#: step handed to a VLM or a human (plan item 4.8).
#:
#: A sixth value exists in the code and deliberately **not** in this tuple yet:
#: ``speculation`` — a step filled from a prediction made during the previous
#: step's settle (plan item 5.1, :mod:`jevskill.cu.speculate`). It ships off by
#: default because the measurement in `references/speculate.md` did not earn it
#: one, and adding it here while no shipped configuration can produce it would
#: widen the contract `bench/cu_report.py` validates against for nothing. Add it
#: — here and in ``tests/test_cu_contract.py`` — in the same change that turns
#: speculation on.
DECIDED_BY: Tuple[str, ...] = ("jev", "macro", "code", "escalation")

#: How a run ended. ``done`` means the *agent* stopped believing it was finished —
#: it is not a success claim, see the module docstring.
STOP_REASONS: Tuple[str, ...] = (
    "done", "max_steps", "budget", "blocked", "escalated", "error",
)


def _median(values: Sequence[float]) -> float:
    """Median of ``values``, 0.0 when empty.

    ``statistics.median`` rather than a nearest-rank percentile, because that is
    what `bench/cu_bench.py` already uses for every published p50 in
    `skills/jev/references/hotloop.md`. One definition across both benches, so a
    p50 here and a p50 there mean the same thing.
    """
    return float(statistics.median(values)) if values else 0.0


@dataclass
class StepRecord:
    """One iteration of the loop: observe → reduce → decide → validate → act → settle.

    Written by the agent, read by the harness. Every field is a fact the agent
    already has; none of them requires an extra model call to produce.
    """

    #: 0-based position in the run. ``RunResult.problems`` checks these are dense.
    index: int
    #: Wall time of the whole step, including the parts no stage covers.
    t_ms: float
    #: Per-stage milliseconds, keyed by :data:`STAGES`. Missing keys read as 0.0.
    stages_ms: Dict[str, float] = field(default_factory=dict)
    #: Elements offered to Jev **after** reduction — the ≤ 60 of `act.md` §1. Not
    #: the raw accessibility-tree size, which is the observer's business.
    candidates: int = 0
    #: The element id Jev chose (``e11``), ``"none"`` when it chose none, or
    #: ``None`` when code overrode the choice and never asked.
    target: Optional[str] = None
    #: The operation Jev chose (``click``/``type``/``key``/…), or ``None`` as above.
    op: Optional[str] = None
    #: The top-1 probability the step was gated on. Per `act.md` §3 that is the
    #: **minimum** of the confidences the step depended on (`op` and `target`),
    #: never their product — "one wrong argument is enough to spoil the result". A
    #: loop that depends on `target` alone records `target`'s top-1.
    confidence: float = 0.0
    #: `target`'s top-1 minus its top-2. Confidence is concentration, not
    #: correctness: two controls named "Save" within ~0.10 is a narrow-and-re-ask,
    #: not an action (`act.md` §3).
    margin: float = 0.0
    #: The three Nouls of the bundle, as probabilities. Recorded even when the
    #: step did not branch on them — they ride free in the same call (`act.md` §2)
    #: and they are what a later analysis of *why* a run failed has to look at.
    goal_reached: float = 0.0
    needs_text: float = 0.0
    is_destructive: float = 0.0
    #: One of :data:`DECIDED_BY`, or ``"speculation"`` when the optional Phase 5
    #: hook filled the step from a prediction made during the previous settle.
    decided_by: str = ""
    #: Whether an action was actually performed. A validated-away step (stale
    #: element, illegal op for the role) is ``False`` and still costs a step.
    executed: bool = False
    #: Whether the tree hash differed after settle. Code's own judgement, never a
    #: model's — the `stuck` question measured 0.31-0.60 on changed screens.
    tree_changed: bool = False
    tokens_in: int = 0
    cost_usd: float = 0.0
    #: Free text for whatever the columns cannot hold: the escalation reason, the
    #: name that tripped the destructive list, the dialog that was not expressible.
    note: str = ""

    def stage_ms(self, name: str) -> float:
        """Milliseconds for one stage, 0.0 if the agent did not report it."""
        return float(self.stages_ms.get(name, 0.0) or 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StepRecord":
        """Rebuild from JSON, ignoring fields a newer agent added.

        Tolerant on purpose: the report must be able to read a results file
        written by an older or newer runner rather than crash on it.
        """
        return cls(
            index=int(data.get("index", 0)),
            t_ms=float(data.get("t_ms", 0.0) or 0.0),
            stages_ms={str(k): float(v) for k, v in (data.get("stages_ms") or {}).items()},
            candidates=int(data.get("candidates", 0) or 0),
            target=data.get("target"),
            op=data.get("op"),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            margin=float(data.get("margin", 0.0) or 0.0),
            goal_reached=float(data.get("goal_reached", 0.0) or 0.0),
            needs_text=float(data.get("needs_text", 0.0) or 0.0),
            is_destructive=float(data.get("is_destructive", 0.0) or 0.0),
            decided_by=str(data.get("decided_by", "")),
            executed=bool(data.get("executed", False)),
            tree_changed=bool(data.get("tree_changed", False)),
            tokens_in=int(data.get("tokens_in", 0) or 0),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            note=str(data.get("note", "")),
        )


@dataclass
class RunOptions:
    """Caps the harness imposes on one run. The agent is expected to honour them.

    ``budget_s`` is the agent's own wall-clock stop. The harness cannot preempt a
    Python callable, so it measures the run and *reports* an overrun rather than
    pretending it enforced one — see `bench/cu_run.py`.
    """

    max_steps: int = 25
    budget_s: float = 90.0
    #: True when the caller is a dry run: no desktop is touched, no key is needed,
    #: and every number produced is synthetic. An agent that cannot honour this
    #: must raise rather than act on the real machine.
    dry_run: bool = False
    #: Which endpoint the agent should use (``typesafe``/``openrouter``), or None
    #: for the package's normal resolution. Recorded in the results key, because
    #: `hotloop.md` measured the two routes 23-74 ms apart: two providers are two
    #: measurements, not one.
    provider: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    """What one agent did on one task, once. No success field — see the module docstring."""

    task_id: str
    run: int = 0
    #: One of :data:`STOP_REASONS`.
    stop_reason: str = ""
    steps: List[StepRecord] = field(default_factory=list)
    #: Goal handed over → agent stopped, by the agent's own clock.
    wall_ms: float = 0.0
    #: Model round trips. Differs from ``len(steps)``: a macro hit is a step with
    #: no decision, an escalation may be a decision with no step of its own.
    decisions: int = 0
    #: Times the loop handed the step to a VLM or a human (plan item 4.8).
    #: Should equal :attr:`escalation_steps`; :meth:`problems` says so when it does not.
    escalations: int = 0
    #: Times the deterministic destructive-name list fired and forced a
    #: confirmation, whatever `is_destructive` said (`act.md` §4).
    destructive_gates: int = 0
    tokens_in: int = 0
    cost_usd: float = 0.0
    error: str = ""

    # --- derived -----------------------------------------------------------

    @property
    def steps_count(self) -> int:
        return len(self.steps)

    @property
    def decide_ms_p50(self) -> float:
        """Median ``decide`` stage. The one stage a different model would move."""
        return _median([s.stage_ms("decide") for s in self.steps])

    @property
    def step_ms_p50(self) -> float:
        """Median whole step. What the user actually waits for."""
        return _median([s.t_ms for s in self.steps])

    @property
    def jev_share(self) -> float:
        """Decisions per step: 1.0 when every step asked, 0.0 on an all-macro replay."""
        return (self.decisions / len(self.steps)) if self.steps else 0.0

    @property
    def escalation_steps(self) -> int:
        """Steps whose ``decided_by`` is ``escalation``, counted from the steps."""
        return sum(1 for s in self.steps if s.decided_by == "escalation")

    @property
    def escalated(self) -> bool:
        """Whether the run *ended* in an escalation (distinct from escalating mid-run)."""
        return self.stop_reason == "escalated"

    def problems(self) -> List[str]:
        """Accounting complaints about this result, as plain sentences.

        The escalation rate is the metric plan item 4.8 exists to produce, so an
        agent that under-reports it must be visible rather than averaged in. The
        report prints whatever this returns.
        """
        out: List[str] = []
        if self.stop_reason not in STOP_REASONS:
            out.append(f"{self.task_id}/{self.run}: unknown stop_reason {self.stop_reason!r}")
        unknown = sorted({s.decided_by for s in self.steps} - set(DECIDED_BY))
        if unknown:
            out.append(f"{self.task_id}/{self.run}: unknown decided_by {unknown}")
        if self.escalations != self.escalation_steps:
            out.append(
                f"{self.task_id}/{self.run}: escalations={self.escalations} but "
                f"{self.escalation_steps} step(s) say decided_by='escalation'")
        expected = list(range(len(self.steps)))
        if [s.index for s in self.steps] != expected:
            out.append(f"{self.task_id}/{self.run}: step indexes are not 0..{len(self.steps) - 1}")
        if self.decisions > len(self.steps) and self.steps:
            out.append(
                f"{self.task_id}/{self.run}: {self.decisions} decisions over "
                f"{len(self.steps)} steps (jev_share {self.jev_share:.2f} > 1)")
        return out

    # --- json --------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Derived values are written too: the report reads the file, and a reader
        # that recomputes them has to agree with whoever wrote them.
        data["steps_count"] = self.steps_count
        data["decide_ms_p50"] = round(self.decide_ms_p50, 3)
        data["step_ms_p50"] = round(self.step_ms_p50, 3)
        data["jev_share"] = round(self.jev_share, 4)
        data["escalation_steps"] = self.escalation_steps
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunResult":
        return cls(
            task_id=str(data.get("task_id", "")),
            run=int(data.get("run", 0) or 0),
            stop_reason=str(data.get("stop_reason", "")),
            steps=[StepRecord.from_dict(s) for s in (data.get("steps") or [])],
            wall_ms=float(data.get("wall_ms", 0.0) or 0.0),
            decisions=int(data.get("decisions", 0) or 0),
            escalations=int(data.get("escalations", 0) or 0),
            destructive_gates=int(data.get("destructive_gates", 0) or 0),
            tokens_in=int(data.get("tokens_in", 0) or 0),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            error=str(data.get("error", "")),
        )


#: An agent under test. Goal text in, result out; the harness supplies nothing
#: else and accepts nothing else.
Agent = Callable[[str, RunOptions], RunResult]


@dataclass
class TaskRun:
    """One cell of the benchmark: what the agent reported **plus** the harness's verdict.

    The split is the point. :class:`RunResult` is the agent's; ``success`` is the
    oracle's, and ``None`` means the oracle could not be evaluated — which
    `bench/cu_tasks.md` says must be reported as a failure with a reason, never
    back-filled from how the agent stopped.
    """

    result: RunResult
    #: Oracle only. True/False, or None when it could not be evaluated.
    success: Optional[bool]
    #: ``dry-run`` or ``live``. A dry run's numbers are synthetic and are labelled
    #: as such everywhere they are printed.
    mode: str = "live"
    provider: Optional[str] = None
    #: Which agent produced ``result`` — an import path, or a tool name for a
    #: comparison run. The two columns of the comparison table are keyed on it.
    agent: str = ""
    oracle_detail: str = ""
    #: Goal handed over → agent returned, by the **harness's** clock. Kept beside
    #: the agent's own ``wall_ms`` rather than replacing it: when the two disagree
    #: the gap is the agent's own book-keeping error, and hiding it would hide that.
    harness_wall_ms: float = 0.0
    setup_ok: bool = True
    teardown_ok: bool = True
    notes: List[str] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.result.task_id

    @property
    def run(self) -> int:
        return self.result.run

    def key(self) -> Tuple[str, int, str, str]:
        """Identity of this cell: (task, run, mode, provider). Results merge on it."""
        return (self.task_id, self.run, self.mode, self.provider or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run": self.run,
            "mode": self.mode,
            "provider": self.provider,
            "agent": self.agent,
            "success": self.success,
            "oracle_detail": self.oracle_detail,
            "harness_wall_ms": round(self.harness_wall_ms, 3),
            "setup_ok": self.setup_ok,
            "teardown_ok": self.teardown_ok,
            "notes": list(self.notes),
            "synthetic": self.mode == "dry-run",
            "result": self.result.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRun":
        result = RunResult.from_dict(data.get("result") or {})
        if not result.task_id:
            result.task_id = str(data.get("task_id", ""))
        if not result.run:
            result.run = int(data.get("run", 0) or 0)
        return cls(
            result=result,
            success=data.get("success"),
            mode=str(data.get("mode", "live")),
            provider=data.get("provider"),
            agent=str(data.get("agent", "")),
            oracle_detail=str(data.get("oracle_detail", "")),
            harness_wall_ms=float(data.get("harness_wall_ms", 0.0) or 0.0),
            setup_ok=bool(data.get("setup_ok", True)),
            teardown_ok=bool(data.get("teardown_ok", True)),
            notes=list(data.get("notes") or []),
        )


# --------------------------------------------------------------------------- #
# Pure aggregation
#
# `summarise` and friends accept RunResult or TaskRun. A bare RunResult has no
# oracle verdict, so its success is `None` — unknown, never inferred from
# `stop_reason`. That is the "oracle only" rule made structural: there is no code
# path that can turn a `done` into a pass.
# --------------------------------------------------------------------------- #

Result = Union[RunResult, TaskRun]


def _as_pair(item: Result) -> Tuple[RunResult, Optional[bool]]:
    if isinstance(item, TaskRun):
        return item.result, item.success
    return item, None


def escalation_rate(results: Sequence[Result]) -> float:
    """Escalated **steps** over all steps — plan item 4.8's "odsetek kroków eskalowanych".

    Step-level, not run-level: a run that escalated once out of twenty steps and a
    run that escalated on its first step are not the same failure. The run-level
    figure is ``summarise(...)["totals"]["escalated_run_rate"]``.

    0.0 when there are no steps, which is also what an empty sequence gives — an
    unmeasured rate and a zero rate look the same here, so the report prints the
    step count next to it.
    """
    steps = 0
    escalated = 0
    for item in results:
        result, _ = _as_pair(item)
        steps += result.steps_count
        escalated += result.escalation_steps
    return (escalated / steps) if steps else 0.0


def escalations_per_task(results: Sequence[Result]) -> Dict[str, int]:
    """Total escalated steps per task id, every task that has a run included.

    Tasks with zero escalations stay in the mapping at 0. A task missing from an
    escalation table reads as "not measured"; a task at 0 reads as "measured, none".
    """
    out: Dict[str, int] = {}
    for item in results:
        result, _ = _as_pair(item)
        out[result.task_id] = out.get(result.task_id, 0) + result.escalation_steps
    return out


def _spread(values: Sequence[float], *, digits: int = 3) -> Dict[str, Any]:
    """Median plus every raw value.

    `bench/cu_tasks.md`: "Report all 3 raw values per cell, not just a mean" — at
    n = 3 a median hides exactly the flakiness the benchmark is there to see.
    """
    # digits=0 means a count, and a count reads as `3`, not `3.0`. The median of
    # an even number of counts is still rounded to an int, which at n=3 (the whole
    # design) never happens, and at n=4 is the honest thing to print in an integer
    # column rather than a 4.5 steps that no run took.
    cast = (lambda v: int(round(v))) if digits == 0 else (lambda v: round(float(v), digits))
    rounded = [cast(float(v)) for v in values]
    return {
        "median": cast(_median(rounded)),
        "min": cast(min(rounded)) if rounded else cast(0.0),
        "max": cast(max(rounded)) if rounded else cast(0.0),
        "sum": cast(sum(rounded)),
        "values": rounded,
    }


def summarise(results: Sequence[Result]) -> Dict[str, Any]:
    """Per task, the six metrics of `bench/cu_tasks.md`, medians over runs, plus totals.

    The six: ``wall_ms``, ``decisions`` (the doc's ``jev_calls``, named
    generically because the comparison agent calls a different model),
    ``tokens_in``, ``cost_usd``, ``escalations``, ``success``. Step count is
    reported separately from decisions on purpose: a macro hit (plan item 4.6)
    spends no call and still costs a step.

    Tasks come out in first-seen order, so a report follows the order the runner
    was given rather than an alphabetical one nobody chose.
    """
    order: List[str] = []
    buckets: Dict[str, List[Tuple[RunResult, Optional[bool]]]] = {}
    for item in results:
        result, success = _as_pair(item)
        if result.task_id not in buckets:
            buckets[result.task_id] = []
            order.append(result.task_id)
        buckets[result.task_id].append((result, success))

    tasks: Dict[str, Any] = {}
    problems: List[str] = []
    for task_id in order:
        pairs = buckets[task_id]
        runs = [r for r, _ in pairs]
        verdicts = [s for _, s in pairs]
        graded = [v for v in verdicts if v is not None]
        stop_reasons: Dict[str, int] = {}
        for result in runs:
            stop_reasons[result.stop_reason] = stop_reasons.get(result.stop_reason, 0) + 1
            problems.extend(result.problems())
        steps_total = sum(r.steps_count for r in runs)
        escalated_steps = sum(r.escalation_steps for r in runs)
        tasks[task_id] = {
            "task_id": task_id,
            "runs": len(runs),
            "success": {
                "passes": sum(1 for v in graded if v),
                "graded": len(graded),
                "unknown": len(verdicts) - len(graded),
                # None, not 0.0, when nothing was graded: an ungraded task is not
                # a failed task, and a 0% that means "no oracle ran" is a lie.
                "rate": (sum(1 for v in graded if v) / len(graded)) if graded else None,
                "values": list(verdicts),
            },
            "wall_ms": _spread([r.wall_ms for r in runs], digits=1),
            "decisions": _spread([r.decisions for r in runs], digits=0),
            "tokens_in": _spread([r.tokens_in for r in runs], digits=0),
            "cost_usd": _spread([r.cost_usd for r in runs], digits=8),
            "escalations": _spread([r.escalation_steps for r in runs], digits=0),
            "steps": _spread([r.steps_count for r in runs], digits=0),
            "step_ms_p50": _spread([r.step_ms_p50 for r in runs], digits=1),
            "decide_ms_p50": _spread([r.decide_ms_p50 for r in runs], digits=1),
            "jev_share": _spread([r.jev_share for r in runs], digits=4),
            "destructive_gates": _spread([r.destructive_gates for r in runs], digits=0),
            "steps_total": steps_total,
            "escalated_steps": escalated_steps,
            "escalation_rate": (escalated_steps / steps_total) if steps_total else 0.0,
            "escalated_runs": sum(1 for r in runs if r.escalated),
            "stop_reasons": stop_reasons,
            "errors": [r.error for r in runs if r.error],
        }

    all_pairs = [_as_pair(item) for item in results]
    all_runs = [r for r, _ in all_pairs]
    all_graded = [s for _, s in all_pairs if s is not None]
    steps_total = sum(r.steps_count for r in all_runs)
    escalated_steps = sum(r.escalation_steps for r in all_runs)
    totals = {
        "tasks": len(order),
        "runs": len(all_runs),
        "passes": sum(1 for v in all_graded if v),
        "graded": len(all_graded),
        "unknown": len(all_runs) - len(all_graded),
        "success_rate": (sum(1 for v in all_graded if v) / len(all_graded)) if all_graded else None,
        # Two sums, both named, because they answer different questions: one pass
        # over the list (the medians) versus everything that was actually spent.
        "wall_ms_sum_of_medians": round(
            sum(tasks[t]["wall_ms"]["median"] for t in order), 1),
        "wall_ms_sum_all_runs": round(sum(r.wall_ms for r in all_runs), 1),
        "cost_usd_sum_of_medians": round(
            sum(tasks[t]["cost_usd"]["median"] for t in order), 8),
        "cost_usd_sum_all_runs": round(sum(r.cost_usd for r in all_runs), 8),
        "tokens_in_sum_all_runs": sum(r.tokens_in for r in all_runs),
        "decisions_mean": round(
            sum(r.decisions for r in all_runs) / len(all_runs), 3) if all_runs else 0.0,
        "steps_mean": round(
            steps_total / len(all_runs), 3) if all_runs else 0.0,
        "steps_total": steps_total,
        "escalated_steps": escalated_steps,
        "escalation_rate": escalation_rate(results),
        "escalated_runs": sum(1 for r in all_runs if r.escalated),
        "escalated_run_rate": (
            sum(1 for r in all_runs if r.escalated) / len(all_runs)) if all_runs else 0.0,
        "destructive_gates": sum(r.destructive_gates for r in all_runs),
        # A 0/3 task is a finding, not noise — cu_tasks.md says keep it in the
        # table, so the summary names it rather than leaving it to be spotted.
        "failed_all_runs": [
            t for t in order
            if tasks[t]["success"]["graded"] and tasks[t]["success"]["passes"] == 0
        ],
        "stop_reasons": {
            reason: sum(tasks[t]["stop_reasons"].get(reason, 0) for t in order)
            for reason in sorted({r.stop_reason for r in all_runs})
        },
    }
    return {
        "tasks": tasks,
        "task_order": order,
        "totals": totals,
        "escalations_per_task": escalations_per_task(results),
        "problems": problems,
    }

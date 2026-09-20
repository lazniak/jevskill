"""The record shape a computer-use run produces, shared by the loop and the bench.

Two modules have to agree about this and neither owns it: :mod:`jevskill.cu.loop`
writes the records, ``bench/cu_run.py`` and ``bench/cu_report.py`` read them.
Putting the dataclasses in the loop would make the bench import the loop to parse
a JSON file it did not produce; putting them in the bench would make the package
depend on ``bench/``. So they live here, with no imports of their own.

``StepRecord`` is deliberately flat and JSON-shaped: a run is appended to disk
step by step, and a reader that has to reconstruct objects to answer "how many
steps escalated" will not be written. Every field is either a number, a string,
or a dict of numbers.

**What is not here.** No ``Decision``, no ``Action``, no ``Snapshot``: the report
does not re-derive judgements, it counts what happened. ``decided_by`` is the one
field that carries provenance, and it is a string precisely so that a new source
(a plan step, a second model) does not change this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StepRecord:
    """One step of one run, as the report reads it.

    ``confidence`` and ``margin`` are the ``target`` Choice's: top-1 probability
    and top-1 minus top-2. The three Nouls are stored raw rather than
    thresholded, so a later re-read can apply a different threshold to the same
    run — thresholds move, measurements must not.

    ``executed`` and ``tree_changed`` are separate on purpose: an action that was
    performed and changed nothing is the signature of a stale snapshot or a
    disabled-but-enabled-looking control, and collapsing them into one boolean
    hides exactly that case.
    """

    index: int
    t_ms: float
    #: observe / reduce / decide / validate / act / settle, in milliseconds.
    stages_ms: dict = field(default_factory=dict)
    candidates: int = 0
    target: str | None = None
    op: str | None = None
    #: Top-1 probability of ``target``.
    confidence: float = 0.0
    #: Top-1 minus top-2 of ``target``.
    margin: float = 0.0
    goal_reached: float = 0.0
    needs_text: float = 0.0
    is_destructive: float = 0.0
    #: ``"jev"`` | ``"macro"`` | ``"code"`` | ``"escalation"``.
    decided_by: str = "jev"
    executed: bool = False
    tree_changed: bool = False
    tokens_in: int = 0
    cost_usd: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index, "t_ms": round(self.t_ms, 3),
            "stages_ms": {k: round(float(v), 3) for k, v in self.stages_ms.items()},
            "candidates": self.candidates, "target": self.target, "op": self.op,
            "confidence": self.confidence, "margin": self.margin,
            "goal_reached": self.goal_reached, "needs_text": self.needs_text,
            "is_destructive": self.is_destructive, "decided_by": self.decided_by,
            "executed": self.executed, "tree_changed": self.tree_changed,
            "tokens_in": self.tokens_in, "cost_usd": self.cost_usd,
            "note": self.note,
        }


@dataclass
class RunResult:
    """One run of one task.

    ``stop_reason`` is closed: ``done`` | ``max_steps`` | ``budget`` |
    ``blocked`` | ``escalated`` | ``error``. A closed set is what lets a report
    say "3 of 10 runs escalated" without interpreting free text, and
    ``escalations`` counts every escalation *within* a run, which is a different
    number from "the run stopped escalated".
    """

    task_id: str
    run: int = 0
    stop_reason: str = ""
    steps: list = field(default_factory=list)
    wall_ms: float = 0.0
    decisions: int = 0
    escalations: int = 0
    destructive_gates: int = 0
    tokens_in: int = 0
    cost_usd: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "run": self.run,
            "stop_reason": self.stop_reason,
            "steps": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.steps],
            "wall_ms": round(self.wall_ms, 3), "decisions": self.decisions,
            "escalations": self.escalations,
            "destructive_gates": self.destructive_gates,
            "tokens_in": self.tokens_in, "cost_usd": self.cost_usd,
            "error": self.error,
        }


@dataclass
class RunOptions:
    """The caps a run must not exceed.

    Both are hard stops, and both exist because act.md §7's last row is real: a
    loop with no budget runs until the bill notices. ``dry_run`` resolves every
    action and performs none — the only way this package is exercised end to end
    today.
    """

    max_steps: int = 25
    budget_s: float = 90.0
    dry_run: bool = False
    provider: str | None = None


__all__ = ["RunOptions", "RunResult", "StepRecord"]

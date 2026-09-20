"""Staged timing — the measurement contract the skill promises.

The skill's own claim is that it measures *stage by stage, from the moment the
decision to use Jev is taken to the moment its output is consumed*. This module
is that promise in code.

Stages are the ones a harness actually experiences, timed inside our own
boundary so the breakdown is honest about where the milliseconds went::

    t_decision   the harness decided Jev was the right tool (stage begins)
    profile      inspect the data: size, shape, whether it even fits
    plan         choose the pattern and the questions
    build        construct state + question payloads (incl. chunking)
    serialize    JSON encoding of the request body
    http         network + provider inference (usually >95% of the total)
    parse        decode and type the response
    act          apply thresholds, sort, combine, validate
    report       render the result and write the ledger

``serialize``/``http``/``parse`` are handed to us by
:meth:`jevskill.client.JevClient.decide`. The rest are recorded by the harness
calling :meth:`Stages.mark`.

**Timing model.** Every mark stores the *absolute* elapsed time at that instant,
and durations are derived as the delta between consecutive marks. Two earlier
mistakes are worth remembering because they are easy to repeat:

* a stage must never be recorded as "time minus the sum of earlier stages" when
  the work it wraps contains those stages, or the total double-counts;
* a mark inside a measured region must be a *deltas-only* overlay, which is what
  :meth:`Stages.absorb` does — it attaches the client's own measured breakdown
  for reporting without inflating the wall clock.

If the reported total and the real wall clock disagree by more than a rounding
error, the ledger is lying, and the whole point of this skill is that it does not.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

#: Canonical stage order. Report in this order, always, so runs are comparable.
STAGE_ORDER: tuple[str, ...] = (
    "t_decision",
    "profile",
    "plan",
    "build",
    "http",
    "act",
    "report",
)

#: Stages the API client measures itself. These are an *overlay* inside `build`
#: (for `serialize`) and `http`, reported for insight but not summed into the
#: wall clock, because they are already contained in a marked region.
OVERLAY_STAGES: tuple[str, ...] = ("serialize", "parse")


@dataclass
class Stages:
    """Absolute-timestamp stopwatch with named marks and a delta-only overlay."""

    started_ns: int = field(default_factory=time.perf_counter_ns)
    marks: dict[str, float] = field(default_factory=dict)
    overlay: dict[str, float] = field(default_factory=dict)

    @classmethod
    def begin(cls) -> "Stages":
        return cls()

    def elapsed_ms(self) -> float:
        return (time.perf_counter_ns() - self.started_ns) / 1e6

    def mark(self, stage: str) -> float:
        """Record the absolute elapsed time at this instant."""
        value = round(self.elapsed_ms(), 3)
        self.marks[stage] = value
        return value

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Mark ``name`` at the end of the block (start is the previous mark)."""
        try:
            yield
        finally:
            self.mark(name)

    def absorb(self, timings: dict[str, float]) -> None:
        """Attach the client's internal breakdown as a non-summed overlay."""
        for key, value in timings.items():
            if key.endswith("_ms"):
                name = key[: -len("_ms")]
                if name in OVERLAY_STAGES:
                    self.overlay[name] = round(float(value), 3)
                elif name == "total":
                    self.overlay["client_total"] = round(float(value), 3)

    def ordered(self) -> dict[str, float]:
        """Stage durations, derived from consecutive absolute marks.

        The result sums to the real wall clock from the first mark to the last
        (give or take sub-millisecond rounding), so the ledger's total is
        trustworthy.
        """
        present = [s for s in STAGE_ORDER if s in self.marks]
        extra = [k for k in self.marks if k not in present]
        sequence = present + extra
        durations: dict[str, float] = {}
        previous = 0.0
        for name in sequence:
            absolute = self.marks[name]
            durations[name] = round(max(0.0, absolute - previous), 3)
            previous = absolute
        for name, value in self.overlay.items():
            durations[f"({name})"] = value
        return durations

    def total_ms(self) -> float:
        """True wall clock since the first mark."""
        if not self.marks:
            return round(self.elapsed_ms(), 3)
        return round(self.marks[max(self.marks, key=lambda k: self.marks[k])], 3)


def format_stages(stages: dict[str, float]) -> str:
    """Human-readable, aligned stage breakdown with each stage's share.

    Overlay stages are rendered in parentheses and excluded from the shares,
    because they are already inside a marked region.
    """
    if not stages:
        return "(no stages recorded)"
    real = {k: v for k, v in stages.items() if not k.startswith("(")}
    overlays = {k: v for k, v in stages.items() if k.startswith("(")}
    total = sum(real.values()) or 1.0
    width = max(len(name) for name in stages)
    lines = []
    for name, value in real.items():
        share = value / total * 100
        bar = "#" * max(1, int(round(share / 4))) if share >= 1 else ""
        lines.append(f"  {name.ljust(width)}  {value:9.1f} ms  {share:5.1f}%  {bar}")
    lines.append(f"  {'TOTAL'.ljust(width)}  {total:9.1f} ms  100.0%")
    for name, value in overlays.items():
        lines.append(f"  {name.ljust(width)}  {value:9.1f} ms  (inside a stage above)")
    return "\n".join(lines)
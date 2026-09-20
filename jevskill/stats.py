"""The effectiveness ledger.

The point of this module is to replace *belief* about Jev with *measurement*.
Every decision is appended as one JSON line containing:

* the pattern the harness chose (``which``),
* what the baseline would have cost had an LLM done the same judgement
  (``baseline`` — tokens the harness did **not** ship to the expensive model),
* stage-by-stage timing inside our own boundary (``stages_ms``),
* the API's own numbers (``tokens_in``, ``cost_usd``, ``latency_ms``),
* the model's self-reported confidence, and
* optionally, later, whether the decision actually turned out right
  (``outcome``), which is what makes calibration measurable.

Correlation is by ``decision_id``; the harness calls :func:`record_outcome` once
the downstream consequence is known. That is what turns "Jev is fast" into
"Jev was right 94% of the time on this pattern, with a p50 of 310 ms".

Ledgers are JSONL, appended, and never rewritten, so concurrent harnesses can
write to the same file without coordination.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

LEDGER_DIRNAME = ".jevskill"
LEDGER_FILENAME = "ledger.jsonl"

#: What an expensive-model path would plausibly have cost for the same decision.
#: Deliberately conservative and *stated*, never silently assumed: the README
#: quotes whichever baseline it used so the reader can redo the arithmetic.
#:
#: ``tokens`` is the context an LLM would have had to *read* to make the same
#: judgement (the raw data plus instructions); the output is the JSON/answer it
#: would have had to *write*. Both are counted, because both are billed.
DEFAULT_BASELINE = {
    "model": "a frontier chat model",
    "input_price_per_mtok": 3.00,
    "output_price_per_mtok": 15.00,
    "output_tokens": 60,
    "overhead_tokens": 350,
}


def ledger_path(root: Path | str | None = None) -> Path:
    """Where this project's ledger lives.

    Defaults to ``<cwd>/.jevskill/ledger.jsonl`` so stats travel with the repo
    that produced them. ``JEVSKILL_LEDGER_DIR`` overrides it, and the global
    ledger lives at ``~/.jevskill/ledger.jsonl``.
    """
    override = os.environ.get("JEVSKILL_LEDGER_DIR")
    if override:
        return Path(override) / LEDGER_FILENAME
    if root is not None:
        return Path(root) / LEDGER_DIRNAME / LEDGER_FILENAME
    return Path.cwd() / LEDGER_DIRNAME / LEDGER_FILENAME


def global_ledger_path() -> Path:
    return Path.home() / LEDGER_DIRNAME / LEDGER_FILENAME


def new_decision_id() -> str:
    return f"d_{uuid.uuid4().hex[:12]}"


@dataclass
class Record:
    """One decision, as written to the ledger."""

    decision_id: str
    ts: str
    which: str
    intent: str = ""
    stages_ms: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    questions: int = 0
    state_tokens: int = 0
    confidence: dict[str, float] = field(default_factory=dict)
    baseline_tokens: int = 0
    baseline_cost_usd: float = 0.0
    outcome: str = ""
    outcome_detail: str = ""
    session_id: str = ""
    version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _append(record: Record, *, root: Path | str | None = None) -> str:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json() + "\n")
    return record.decision_id


def record_decision(
    *,
    which: str,
    intent: str = "",
    stages_ms: dict[str, float] | None = None,
    latency_ms: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    questions: int = 0,
    state_tokens: int = 0,
    confidence: dict[str, float] | None = None,
    baseline_tokens: int = 0,
    baseline: dict | None = None,
    session_id: str = "",
    version: str = "",
    extra: dict | None = None,
    root: Path | str | None = None,
) -> str:
    """Append one decision. Returns its ``decision_id`` for later pairing."""
    baseline_cost = 0.0
    if baseline_tokens or baseline:
        spec = {**DEFAULT_BASELINE, **(baseline or {})}
        billed_tokens = baseline_tokens + int(spec["overhead_tokens"])
        baseline_cost = (
            billed_tokens / 1_000_000 * float(spec["input_price_per_mtok"])
            + int(spec["output_tokens"]) / 1_000_000 * float(spec["output_price_per_mtok"])
        )
    record = Record(
        decision_id=new_decision_id(),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        which=which,
        intent=intent,
        stages_ms=stages_ms or {},
        latency_ms=round(float(latency_ms), 3),
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
        cost_usd=float(cost_usd),
        questions=int(questions),
        state_tokens=int(state_tokens),
        confidence=confidence or {},
        baseline_tokens=int(baseline_tokens),
        baseline_cost_usd=round(baseline_cost, 8),
        session_id=session_id,
        version=version,
        extra=extra or {},
    )
    return _append(record, root=root)


def record_outcome(decision_id: str, outcome: str, detail: str = "", *, root: Path | str | None = None) -> bool:
    """Pair a decision with what actually happened.

    ``outcome`` is free-form but the reporting understands these:
    ``correct`` / ``incorrect`` / ``escalated`` / ``overridden`` / ``no_action``.

    Appends a small patch record rather than rewriting history, so the ledger
    stays append-only and safe for concurrent writers.
    """
    patch = {
        "kind": "outcome",
        "decision_id": decision_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "outcome": outcome,
        "outcome_detail": detail,
    }
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch, ensure_ascii=False) + "\n")
    return True


def _apply_outcomes(records: list[dict], patches: list[dict]) -> list[dict]:
    """Overlay outcome patches onto their decisions.

    Every row already carries ``outcome``/``outcome_detail`` because ``Record``
    defines them, so an unpaired decision is one with an *empty* outcome, and the
    key set is identical across the whole ledger. That uniformity is intentional:
    a downstream reader can read any row without checking which fields exist.
    """
    by_id = {r["decision_id"]: r for r in records if r.get("decision_id")}
    for patch in patches:
        target = by_id.get(patch.get("decision_id", ""))
        if target is not None:
            target["outcome"] = patch.get("outcome", "")
            target["outcome_detail"] = patch.get("outcome_detail", "")
    return records


def read_ledger(path: Path | str | None = None) -> tuple[list[dict], list[dict]]:
    """Return ``(decisions, patches)`` from one ledger file."""
    target = Path(path) if path is not None else ledger_path()
    decisions: list[dict] = []
    patches: list[dict] = []
    if not target.exists():
        return decisions, patches
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("kind") == "outcome":
            patches.append(item)
        else:
            decisions.append(item)
    return decisions, patches


def load_records(paths: Iterable[Path | str] | None = None, *, include_global: bool | None = None) -> list[dict]:
    """Load and outcome-resolve records.

    With no arguments, reads this project's ledger and (unless told otherwise)
    also the global one, so a harness sees decisions made from any directory it
    worked in.

    **When explicit ``paths`` are given, only those are read.** Merging the
    global ledger into an explicitly-scoped query silently contaminates it — a
    report about one repository would quietly include every other repository's
    decisions. That bug is easy to introduce and hard to notice, so the default
    is the safe one: explicit paths mean exactly those paths.
    """
    if paths is None:
        candidates = [ledger_path()]
        if include_global is not False:
            global_path = global_ledger_path()
            if global_path != candidates[0]:
                candidates.append(global_path)
    else:
        candidates = [Path(p) for p in paths]
        if include_global:
            global_path = global_ledger_path()
            if global_path not in candidates:
                candidates.append(global_path)
    all_decisions: list[dict] = []
    all_patches: list[dict] = []
    for path in candidates:
        decisions, patches = read_ledger(path)
        all_decisions.extend(decisions)
        all_patches.extend(patches)
    all_decisions.sort(key=lambda r: r.get("ts", ""))
    return _apply_outcomes(all_decisions, all_patches)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def summarize(records: list[dict]) -> dict:
    """Aggregate a ledger into the numbers the README quotes.

    Returns overall volume/cost/latency, the realised saving against the stated
    LLM baseline, per-pattern breakdowns, and per-intent accuracy for every
    decision that was paired with an outcome.
    """
    if not records:
        return {"decisions": 0}

    latencies = [float(r.get("latency_ms", 0) or 0) for r in records]
    jev_cost = sum(float(r.get("cost_usd", 0) or 0) for r in records)
    baseline_cost = sum(float(r.get("baseline_cost_usd", 0) or 0) for r in records)
    baseline_tokens = sum(int(r.get("baseline_tokens", 0) or 0) for r in records)
    input_tokens = sum(int(r.get("tokens_in", 0) or 0) for r in records)

    by_which: dict[str, dict[str, Any]] = {}
    by_intent: dict[str, dict[str, Any]] = {}
    for record in records:
        for key, bucket in ((record.get("which") or "unspecified", by_which), (record.get("intent") or "", by_intent)):
            if not key:
                continue
            entry = bucket.setdefault(
                key, {"n": 0, "latencies": [], "cost": 0.0, "baseline_cost": 0.0,
                      "baseline_tokens": 0, "outcomes": {}, "confidences": []}
            )
            entry["n"] += 1
            entry["latencies"].append(float(record.get("latency_ms", 0) or 0))
            entry["cost"] += float(record.get("cost_usd", 0) or 0)
            entry["baseline_cost"] += float(record.get("baseline_cost_usd", 0) or 0)
            entry["baseline_tokens"] += int(record.get("baseline_tokens", 0) or 0)
            outcome = record.get("outcome") or ""
            if outcome:
                entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
            for value in (record.get("confidence") or {}).values():
                entry["confidences"].append(float(value))

    def _finish(bucket: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in bucket.items():
            judged = entry["outcomes"].get("correct", 0) + entry["outcomes"].get("incorrect", 0)
            out[key] = {
                "n": entry["n"],
                "p50_ms": round(statistics.median(entry["latencies"]), 1) if entry["latencies"] else 0.0,
                "p95_ms": round(_pct(entry["latencies"], 0.95), 1),
                "cost_usd": round(entry["cost"], 6),
                "baseline_cost_usd": round(entry["baseline_cost"], 4),
                "baseline_tokens": entry["baseline_tokens"],
                "outcomes": entry["outcomes"],
                "accuracy": (
                    round(entry["outcomes"].get("correct", 0) / judged, 4) if judged else None
                ),
                "mean_confidence": (
                    round(statistics.fmean(entry["confidences"]), 3) if entry["confidences"] else None
                ),
            }
        return out

    judged_total = sum(
        1 for r in records if r.get("outcome") in ("correct", "incorrect")
    )
    correct_total = sum(1 for r in records if r.get("outcome") == "correct")

    return {
        "decisions": len(records),
        "questions_asked": sum(int(r.get("questions", 0) or 0) for r in records),
        "input_tokens": input_tokens,
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95": round(_pct(latencies, 0.95), 1),
            "min": round(min(latencies), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "cost_usd": round(jev_cost, 6),
        "cost_usd_per_decision": round(jev_cost / len(records), 8),
        "baseline_cost_usd": round(baseline_cost, 4),
        "baseline_tokens_avoided": baseline_tokens,
        "saved_usd": round(baseline_cost - jev_cost, 4),
        "saved_pct": (
            round((1 - jev_cost / baseline_cost) * 100, 1) if baseline_cost > 0 else None
        ),
        "by_pattern": _finish(by_which),
        "by_intent": _finish(by_intent),
        "accuracy": (round(correct_total / judged_total, 4) if judged_total else None),
        "judged": judged_total,
        "first_ts": records[0].get("ts", ""),
        "last_ts": records[-1].get("ts", ""),
    }
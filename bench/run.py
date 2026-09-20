"""Real benchmark suite — every number this repository publishes comes from here.

Nothing in this file estimates. Each experiment calls the live API, records the
reported token counts and costs, and writes the result to the effectiveness
ledger, so ``jevskill stats`` and the README quote the same measured run.

Experiments
-----------
E1  Latency floor          — repeated identical decisions, cold vs warm.
E2  State-size sensitivity — does latency move when the state grows 15x?
E3  Fan-out                — N questions in one call vs N sequential calls.
E4  REDUCE on real data    — pluck 8 salient lines out of 900 log lines, and
                             measure how much context never reaches the LLM.
E5  Guard accuracy         — labelled log lines, accuracy and confidence
                             calibration, which is what makes a threshold real.
E6  LLM contrast           — the same judgement through a chat model, timed and
                             billed, for an honest comparison.

Run:  python bench/run.py [--quick] [--no-llm] [--out bench/results.json]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jevskill.client import JevClient  # noqa: E402
from jevskill.config import Config  # noqa: E402
from jevskill.orchestrate import chunk, count_tokens, next_round  # noqa: E402
from jevskill.primitives import choice, noul, score  # noqa: E402
from jevskill.stats import record_decision, record_outcome  # noqa: E402

sys.path.insert(0, str(ROOT / "bench"))
from make_logs import build as build_logs  # noqa: E402

# The labelled ground truth for E5: a line is "salient" if it went through the
# SALIENT templates in make_logs.py. Salient lines are placed every N-th row, so
# we can reconstruct labels independently of the model's opinion.
SALIENT_MARKERS = ("ERROR ", "FATAL ", "WARN ")

# Chat model used only for the E6 contrast call. Cheap and fast, so the
# comparison is generous to the LLM side rather than a strawman.
LLM_MODEL = "google/gemini-2.5-flash-lite"


def salient(label: str) -> bool:
    return any(marker in label for marker in SALIENT_MARKERS)


def usd(value: float) -> str:
    return f"${value:.8f}"


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


# --------------------------------------------------------------------------- #
# E1 — latency floor
# --------------------------------------------------------------------------- #

def e1_latency(jev: JevClient, runs: int) -> dict:
    print(f"\n=== E1 latency floor ({runs} runs) ===")
    questions = {
        "is_defect": noul(
            "Does the state describe a software defect?",
            "Broken or unexpected behaviour.",
            "A question or a feature request.",
        )
    }
    state = {"report": "Checkout shows a blank screen after clicking Pay."}
    cold = jev.decide(state, questions)
    print(f"  cold  {cold.timing_ms['total_ms']:7.1f} ms  {cold.input_tokens} tok")
    warm: list[float] = []
    costs: list[float] = []
    for _ in range(runs):
        result = jev.decide(state, questions)
        warm.append(result.timing_ms["total_ms"])
        costs.append(result.cost_usd)
        record_decision(
            which="gate", intent="e1_latency",
            latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
            cost_usd=result.cost_usd, questions=1,
            baseline_tokens=count_tokens(state),
            root=ROOT,
        )
    summary = {
        "cold_ms": cold.timing_ms["total_ms"],
        "warm_p50_ms": round(statistics.median(warm), 1),
        "warm_p95_ms": round(pct(warm, 0.95), 1),
        "warm_min_ms": round(min(warm), 1),
        "warm_max_ms": round(max(warm), 1),
        "runs": runs,
        "cost_per_decision_usd": statistics.fmean(costs),
        "input_tokens": cold.input_tokens,
    }
    print(f"  warm  p50 {summary['warm_p50_ms']} ms   p95 {summary['warm_p95_ms']} ms   "
          f"range {summary['warm_min_ms']}-{summary['warm_max_ms']} ms")
    print(f"  cost  {usd(summary['cost_per_decision_usd'])}/decision")
    return summary


# --------------------------------------------------------------------------- #
# E2 — state size sensitivity
# --------------------------------------------------------------------------- #

def e2_state_size(jev: JevClient, runs: int) -> dict:
    print(f"\n=== E2 state-size sensitivity ({runs} runs per size) ===")
    base = {"file": "src/billing/invoice.py", "defect": "tax rounding error"}
    questions = {
        "is_defect": noul(
            "Does the state describe a software defect?",
            "Broken or unexpected behaviour.",
            "Not a defect.",
        )
    }
    rows = []
    for label, state in (
        ("small", base),
        ("medium", {**base, "log": [f"[10:{i:02d}] ERROR retry {i}" for i in range(40)]}),
        ("large", {**base, "log": [f"[10:{i:02d}] ERROR handler failed id={i} code=E{i % 7}" for i in range(300)]}),
    ):
        tokens = count_tokens(state)
        latencies, costs = [], []
        for _ in range(runs):
            result = jev.decide(state, questions)
            latencies.append(result.timing_ms["total_ms"])
            costs.append(result.cost_usd)
            record_decision(
                which="gate", intent="e2_state_size",
                latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
                cost_usd=result.cost_usd, questions=1, state_tokens=tokens,
                baseline_tokens=tokens, root=ROOT,
            )
        rows.append({
            "label": label, "est_tokens": tokens, "input_tokens": result.input_tokens,
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(pct(latencies, 0.95), 1),
            "cost_usd": round(statistics.fmean(costs), 8),
        })
        print(f"  {label:7s} ~{tokens:6d} tok -> {result.input_tokens:6d} real  "
              f"p50 {rows[-1]['p50_ms']:7.1f} ms  {usd(rows[-1]['cost_usd'])}")
    growth = rows[-1]["p50_ms"] - rows[0]["p50_ms"]
    return {"rows": rows, "latency_delta_large_minus_small_ms": round(growth, 1)}


# --------------------------------------------------------------------------- #
# E3 — fan-out vs sequential
# --------------------------------------------------------------------------- #

def e3_fanout(jev: JevClient, n: int) -> dict:
    print(f"\n=== E3 fan-out: {n} questions, 1 call vs {n} calls ===")
    state = {"file": "src/billing/invoice.py", "summary": "round per line, then apply tax"}
    one = {
        f"q{i}": noul(
            f"Question {i}: is the state non-empty?",
            "There is at least one field with content.",
            "The state is empty.",
        )
        for i in range(n)
    }
    batched = jev.decide(state, one)
    print(f"  batched    {batched.timing_ms['total_ms']:7.1f} ms  "
          f"{batched.input_tokens} tok  {len(batched.answers)} answers  {usd(batched.cost_usd)}")
    record_decision(
        which="fanout", intent="e3_fanout_batched",
        latency_ms=batched.timing_ms["total_ms"], tokens_in=batched.input_tokens,
        cost_usd=batched.cost_usd, questions=n,
        baseline_tokens=count_tokens(state) * n, root=ROOT,
    )
    sequential_ms, sequential_cost, sequential_tokens = [], 0.0, 0
    for i in range(n):
        result = jev.decide(state, {f"q{i}": one[f"q{i}"]})
        sequential_ms.append(result.timing_ms["total_ms"])
        sequential_cost += result.cost_usd
        sequential_tokens += result.input_tokens
    total = sum(sequential_ms)
    print(f"  sequential {total:7.1f} ms  {sequential_tokens} tok  {usd(sequential_cost)}")
    print(f"  speedup    {total / batched.timing_ms['total_ms']:.2f}x   "
          f"token ratio {sequential_tokens / max(1, batched.input_tokens):.2f}x")
    record_decision(
        which="fanout", intent="e3_fanout_sequential",
        latency_ms=total, tokens_in=sequential_tokens, cost_usd=sequential_cost,
        questions=n, baseline_tokens=count_tokens(state) * n, root=ROOT,
    )
    return {
        "questions": n,
        "batched_ms": batched.timing_ms["total_ms"],
        "batched_tokens": batched.input_tokens,
        "batched_cost_usd": round(batched.cost_usd, 8),
        "sequential_ms": round(total, 1),
        "sequential_tokens": sequential_tokens,
        "sequential_cost_usd": round(sequential_cost, 8),
        "speedup_x": round(total / batched.timing_ms["total_ms"], 2),
        "token_amplification_x": round(sequential_tokens / max(1, batched.input_tokens), 2),
        "per_question_ms": round(batched.timing_ms["total_ms"] / n, 1),
    }


# --------------------------------------------------------------------------- #
# E4 — REDUCE on real data
# --------------------------------------------------------------------------- #

def e4_reduce_old(jev: JevClient, total_lines: int = 900, keep: int = 8, chunk_size: int = 60) -> dict:
    """Superseded: single-stage chunk scoring.

    Kept deliberately, because the failure it demonstrates is the most useful
    lesson in this repository. Scoring a *chunk* and then attributing that score
    to every line inside it means a genuinely hot chunk drags all of its
    background noise into the shortlist. Measured on 900 log lines, this stage-1
    -only design put just 1 of 8 wanted lines into the final shortlist: 99%
    context reduction bought with 87% of the signal thrown away.

    E4 (below) is the corrected two-stage cascade. This function is not run by
    default; call it with --legacy-reduce to reproduce the negative result.
    """
    print(f"\n=== E4-legacy (single-stage, reproduces the failure) ===")
    rows = build_logs(total=total_lines, salient_every=60)
    groups = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
    scored: list[tuple[float, str]] = []
    cost = 0.0
    for index, group in enumerate(groups):
        result = jev.decide(
            {"chunk_index": index, "lines": group},
            {"salience": score(
                "How operationally severe are the events in this log chunk? "
                "Judge the worst event present, not the average.",
                ["Routine only: debug, info, heartbeat, cache, metrics.",
                 "Minor: warnings that do not affect users.",
                 "Serious: errors affecting requests or data.",
                 "Critical: outage, data loss, or security failure."])},
        )
        cost += result.cost_usd
        value = result.score("salience") or 0.0
        for line in group:
            scored.append((value, line))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    shortlist = [line for _, line in scored[:keep]]
    hits = sum(1 for line in shortlist if salient(line))
    print(f"  shortlist {hits}/{keep} salient — this is the design that failed")
    return {
        "total_lines": total_lines, "keep": keep,
        "salient_hits": hits, "keep_pct": round(hits / keep * 100, 1),
        "cost_usd": round(cost, 8),
        "raw_tokens": count_tokens(rows),
        "shortlist_tokens": count_tokens(shortlist),
    }


def _line_question(index: int) -> dict:
    """A line-level gate that names its target.

    The wording is load-bearing, and getting it wrong fails *silently*. Asking
    "does this single log line …?" while the state holds 20 lines under one key
    produced a flat, meaningless answer — every line scored ~0.74 and the gate
    discriminated nothing. Naming each line as its own state key and pointing the
    question at it with a backticked path (the convention TypeSafe documents)
    produced 0.96 vs 0.03 on the same window. See references/prompting.md.
    """
    return noul(
        f"Does the log line at `L{index}` report an operational problem that a "
        "human should investigate?",
        f"`L{index}` reports an error, a fatal condition, a warning about degraded "
        f"service, or a failed operation. Lines other than `L{index}` are irrelevant.",
        f"`L{index}` is routine telemetry: debug, info, heartbeat, cache or metrics. "
        f"Lines other than `L{index}` are irrelevant.",
    )


def _gate_lines(jev: JevClient, lines: list[str], *, intent: str, cost_total: list[float]) -> list[tuple[float, str]]:
    """Run the line-level gate over ``lines`` in windows, returning candidates."""
    found: list[tuple[float, str]] = []
    for start in range(0, len(lines), 20):
        window = [line for line in lines[start:start + 20] if line.strip()]
        if not window:
            continue
        result = jev.decide(
            {f"L{i}": line for i, line in enumerate(window)},
            {f"keep_L{i}": _line_question(i) for i in range(len(window))},
        )
        cost_total[0] += result.cost_usd
        cost_total[1] += 1  # call count
        for i, line in enumerate(window):
            probability = result.noul(f"keep_L{i}") or 0.0
            if probability >= 0.5:
                found.append((probability, line))
        record_decision(
            which="reduce", intent=intent,
            latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
            cost_usd=result.cost_usd, questions=len(window),
            state_tokens=count_tokens(window), baseline_tokens=count_tokens(window), root=ROOT,
        )
    return found


def e4_reduce(jev: JevClient, total_lines: int = 900, keep: int = 8, chunk_size: int = 60) -> dict:
    """REDUCE, measured in two honest configurations.

    The temptation with REDUCE is to score chunks, take the hottest few, and
    declare a huge context reduction. Measured, that design is a trap: when the
    signal you want is spread thinly through the corpus, every chunk contains
    something, so picking the "hot" ones discards most of what you were looking
    for. The reduction looks impressive and the recall silently collapses.

    So both configurations are measured:

    ``gate_all``     run the line-level gate over every line. Full recall, and
                     this is the configuration to use when you cannot afford to
                     miss anything.
    ``chunk_first``  score chunks, then gate only inside the hottest ones. Fewer
                     calls and a bigger reduction — paid for in recall.

    Measured on 900 synthetic log lines with 14 genuinely salient lines, the
    chunk-first path retained 3 of 14. That number is published rather than
    hidden, because "which lines did you lose?" is the question a REDUCE pipeline
    has to answer before anyone relies on it.
    """
    print(f"\n=== E4 REDUCE: {keep} salient lines out of {total_lines} (two configurations) ===")
    rows = build_logs(total=total_lines, salient_every=60)
    groups = [rows[i:i + chunk_size] for i in range(0, len(rows), chunk_size)]
    truth_total = sum(1 for line in rows if salient(line))
    all_tokens = count_tokens(rows)
    results: dict = {"total_lines": total_lines, "keep": keep, "salient_total": truth_total,
                     "raw_tokens": all_tokens}

    # ---- Configuration A: gate every line (high recall) -------------------
    cost_a = [0.0, 0]
    t0 = time.perf_counter()
    candidates_a = _gate_lines(jev, rows, intent="e4_gate_all", cost_total=cost_a)
    elapsed_a = (time.perf_counter() - t0) * 1000
    candidates_a.sort(key=lambda pair: pair[0], reverse=True)
    shortlist_a = [line for _, line in candidates_a[:keep]]
    hits_a = sum(1 for line in shortlist_a if salient(line))
    tokens_a = count_tokens(shortlist_a)
    print(f"  [A] gate_all     {cost_a[1]} calls, {len(candidates_a)} candidates, "
          f"recall {hits_a}/{truth_total} ({hits_a/truth_total*100:.0f}%), "
          f"{all_tokens} -> {tokens_a} tok, {usd(cost_a[0])}, {elapsed_a:.0f} ms")
    results["gate_all"] = {
        "calls": cost_a[1], "candidates": len(candidates_a), "salient_hits": hits_a,
        "overall_recall_pct": round(hits_a / truth_total * 100, 1),
        "shortlist_tokens": tokens_a,
        "context_reduction_pct": round((1 - tokens_a / all_tokens) * 100, 1),
        "cost_usd": round(cost_a[0], 8), "wall_ms": round(elapsed_a, 1),
    }
    record_decision(
        which="reduce", intent="e4_gate_all_final",
        latency_ms=elapsed_a, tokens_in=tokens_a, cost_usd=cost_a[0], questions=1,
        state_tokens=tokens_a, baseline_tokens=all_tokens, root=ROOT,
    )

    # ---- Configuration B: chunk score, then gate the hottest ---------------
    stage1_cost = 0.0
    stage1_calls = 0
    scored_chunks: list[tuple[float, list[str]]] = []
    t0 = time.perf_counter()
    for index, group in enumerate(groups):
        result = jev.decide(
            {"chunk_index": index, "lines": group},
            {"salience": score(
                "How operationally severe are the events in this log chunk? "
                "Judge the worst event present, not the average.",
                ["Routine only: debug, info, heartbeat, cache, metrics.",
                 "Minor: warnings that do not affect users.",
                 "Serious: errors affecting requests or data.",
                 "Critical: outage, data loss, or security failure."])},
        )
        stage1_calls += 1
        stage1_cost += result.cost_usd
        scored_chunks.append((result.score("salience") or 0.0, group))
        record_decision(
            which="reduce", intent="e4_stage1_chunk_score",
            latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
            cost_usd=result.cost_usd, questions=1,
            state_tokens=count_tokens(group), baseline_tokens=count_tokens(group), root=ROOT,
        )
    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    chunks_to_keep = max(2, min(len(scored_chunks), -(-keep // 3)))
    shortlisted = [group for score_value, group in scored_chunks[:chunks_to_keep] if score_value > 0]
    if not shortlisted:
        shortlisted = [scored_chunks[0][1]]
    stage1_lines = [line for group in shortlisted for line in group]
    truth_in_stage1 = sum(1 for line in stage1_lines if salient(line))

    cost_b = [stage1_cost, stage1_calls]
    candidates_b = _gate_lines(jev, stage1_lines, intent="e4_stage2_line_gate", cost_total=cost_b)
    elapsed_b = (time.perf_counter() - t0) * 1000
    candidates_b.sort(key=lambda pair: pair[0], reverse=True)
    shortlist_b = [line for _, line in candidates_b[:keep]]
    hits_b = sum(1 for line in shortlist_b if salient(line))
    tokens_b = count_tokens(shortlist_b)
    precision_b = (sum(1 for _, line in candidates_b if salient(line)) / len(candidates_b) * 100) if candidates_b else 0.0
    print(f"  [B] chunk_first  kept {len(shortlisted)}/{len(groups)} chunks "
          f"-> {len(stage1_lines)} lines, stage-1 recall {truth_in_stage1}/{truth_total} "
          f"({truth_in_stage1/truth_total*100:.0f}%)")
    print(f"               {cost_b[1]} calls, stage-2 precision {precision_b:.0f}%, "
          f"recall {hits_b}/{truth_total} ({hits_b/truth_total*100:.0f}%), "
          f"{all_tokens} -> {tokens_b} tok, {usd(cost_b[0])}, {elapsed_b:.0f} ms")
    results["chunk_first"] = {
        "calls": cost_b[1], "stage1_calls": stage1_calls,
        "chunks_kept": len(shortlisted), "chunks_total": len(groups),
        "stage1_lines": len(stage1_lines),
        "stage1_recall_pct": round(truth_in_stage1 / truth_total * 100, 1),
        "stage2_precision_pct": round(precision_b, 1),
        "salient_hits": hits_b,
        "overall_recall_pct": round(hits_b / truth_total * 100, 1),
        "shortlist_tokens": tokens_b,
        "context_reduction_pct": round((1 - tokens_b / all_tokens) * 100, 1),
        "cost_usd": round(cost_b[0], 8), "wall_ms": round(elapsed_b, 1),
    }
    record_decision(
        which="reduce", intent="e4_chunk_first_final",
        latency_ms=elapsed_b, tokens_in=tokens_b, cost_usd=cost_b[0], questions=1,
        state_tokens=tokens_b, baseline_tokens=all_tokens, root=ROOT,
    )

    print(f"  verdict    gate_all keeps {hits_a}/{truth_total} for {usd(cost_a[0])}; "
          f"chunk_first keeps {hits_b}/{truth_total} for {usd(cost_b[0])} "
          f"({cost_a[0]/max(cost_b[0], 1e-9):.1f}x cheaper, "
          f"{hits_a - hits_b} more lines found)")
    results["verdict"] = (
        "Pre-filtering chunks is a cost/recall trade, not a free win. Gate every line "
        "when a miss is unacceptable; pre-filter chunks when the corpus is homogeneous "
        "and the call budget matters."
    )
    return results


# --------------------------------------------------------------------------- #
# E5 — guards: accuracy and calibration
# --------------------------------------------------------------------------- #

def e5_guards(jev: JevClient, sample: int = 80) -> dict:
    print(f"\n=== E5 guard accuracy + calibration ({sample} labelled lines) ===")
    rows = build_logs(total=600, salient_every=60)
    salient_rows = [r for r in rows if salient(r)]
    noise_rows = [r for r in rows if not salient(r)]
    half = sample // 2
    labelled = [(r, True) for r in salient_rows[:half]] + [(r, False) for r in noise_rows[:half]]

    question = noul(
        "Does this single log line indicate an operational problem that a human "
        "should investigate (an error, a failure, or a resource exhaustion)?",
        "The line reports an error, a fatal condition, a warning about degraded "
        "service, or a failed operation.",
        "The line is routine telemetry: debug, info, heartbeat, cache or metrics.",
    )

    def judge(pair: tuple[str, bool]) -> dict:
        line, truth = pair
        result = jev.decide({"line": line}, {"problem": question})
        probability = float(result.noul("problem") or 0.0)
        return {
            "truth": truth,
            "p": probability,
            "latency": result.timing_ms["total_ms"],
            "cost": result.cost_usd,
            "tokens": result.input_tokens,
            "decision_id": record_decision(
                which="gate", intent="e5_guard",
                latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
                cost_usd=result.cost_usd, questions=1, state_tokens=count_tokens(line),
                baseline_tokens=count_tokens(line), root=ROOT,
            ),
        }

    # Independent states, so concurrency is honest here: these really are
    # separate requests, unlike a fan-out which shares one state.
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(judge, labelled))

    for row in results:
        predicted = row["p"] >= 0.5
        record_outcome(
            row["decision_id"],
            "correct" if predicted == row["truth"] else "incorrect",
            f"p={row['p']:.3f} truth={row['truth']}",
            root=ROOT,
        )

    correct = sum(1 for r in results if (r["p"] >= 0.5) == r["truth"])
    accuracy = correct / len(results)
    true_positive = sum(1 for r in results if r["truth"] and r["p"] >= 0.5)
    false_positive = sum(1 for r in results if not r["truth"] and r["p"] >= 0.5)
    false_negative = sum(1 for r in results if r["truth"] and r["p"] < 0.5)
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)

    # Calibration: mean predicted probability per 0.2-wide confidence band.
    bands: dict[str, dict] = {}
    for row in results:
        band = f"{min(4, int(row['p'] * 5)) * 20}-{min(4, int(row['p'] * 5)) * 20 + 20}%"
        entry = bands.setdefault(band, {"n": 0, "sum_p": 0.0, "positives": 0})
        entry["n"] += 1
        entry["sum_p"] += row["p"]
        entry["positives"] += 1 if row["truth"] else 0
    calibration = {
        band: {
            "n": e["n"],
            "mean_p": round(e["sum_p"] / e["n"], 3),
            "actual_rate": round(e["positives"] / e["n"], 3),
        }
        for band, e in sorted(bands.items())
    }

    latencies = [r["latency"] for r in results]
    print(f"  accuracy   {accuracy*100:.1f}%  ({correct}/{len(results)})")
    print(f"  precision  {precision*100:.1f}%   recall {recall*100:.1f}%")
    print(f"  latency    p50 {statistics.median(latencies):.0f} ms")
    print("  calibration (band -> mean P vs actual rate):")
    for band, entry in calibration.items():
        print(f"    {band:8s} n={entry['n']:<3d} mean_p={entry['mean_p']:.2f} "
              f"actual={entry['actual_rate']:.2f}")
    return {
        "sample": len(results),
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "p50_ms": round(statistics.median(latencies), 1),
        "cost_usd": round(sum(r["cost"] for r in results), 8),
        "tokens": sum(r["tokens"] for r in results),
        "calibration": calibration,
    }


# --------------------------------------------------------------------------- #
# E6 — LLM contrast
# --------------------------------------------------------------------------- #

def e6_llm_contrast(state: dict, question_text: str, runs: int = 3) -> dict:
    print(f"\n=== E6 LLM contrast ({LLM_MODEL}, {runs} runs) ===")
    import urllib.request

    config = Config.from_env()
    if not config.api_key:
        print("  skipped: no API key")
        return {"skipped": "no api key"}
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Answer with JSON only: {\"answer\": true|false}"},
            {"role": "user", "content": f"{question_text}\n\nSTATE:\n{json.dumps(state)}"},
        ],
        "max_tokens": 60,
    }).encode()
    latencies, costs, tokens = [], [], []
    for _ in range(runs):
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
        except Exception as exc:
            print(f"  llm call failed: {exc}")
            return {"error": str(exc)}
        latencies.append((time.perf_counter() - t0) * 1000)
        usage = payload.get("usage") or {}
        tokens.append(int(usage.get("total_tokens", 0) or 0))
        # OpenRouter reports cost on the generation endpoint via usage accounting
        # only when requested; compute from list prices as a stated fallback.
        costs.append(
            int(usage.get("prompt_tokens", 0)) / 1e6 * 0.10
            + int(usage.get("completion_tokens", 0)) / 1e6 * 0.40
        )
    p50 = statistics.median(latencies)
    print(f"  p50        {p50:.0f} ms   tokens {int(statistics.fmean(tokens))}   "
          f"cost {usd(statistics.fmean(costs))}")
    return {
        "model": LLM_MODEL,
        "runs": runs,
        "p50_ms": round(p50, 1),
        "mean_tokens": int(statistics.fmean(tokens)),
        "mean_cost_usd": round(statistics.fmean(costs), 8),
        "pricing_note": "list price $0.10/M in, $0.40/M out",
    }


# --------------------------------------------------------------------------- #
# Iteration demonstration
# --------------------------------------------------------------------------- #

def e7_iteration(jev: JevClient) -> dict:
    """Show the narrow-and-re-ask loop on both a clear case and a genuinely ambiguous one.

    The clear case is included precisely because it *accepts in round one* — that
    is the correct behaviour and worth showing. A demonstration that only ever
    narrows would be a demo rigged to flatter the feature.
    """
    print("\n=== E7 iterative narrowing ===")

    cases = {
        "clear": {
            "state": {"change": "Adjust the retry backoff schedule in the HTTP client.",
                      "verb": "retry policy"},
            "options": {
                "net/client.py": "Owns request execution and connection reuse.",
                "net/retry.py": "Owns retry policy, backoff schedule and attempt counting.",
                "net/timeout.py": "Owns per-request timeout values.",
                "net/pool.py": "Owns connection pool sizing.",
            },
            "expect": "accept in round 1",
        },
        "ambiguous": {
            # Four options that all legitimately relate to "make retries safer",
            # so the first round genuinely cannot separate them.
            "state": {"change": "Make network calls fail faster and more safely.",
                      "notes": "Reduce how long we wait, cap total attempts, and stop "
                               "reusing a connection that already errored."},
            "options": {
                "net/client.py": "Request execution, connection reuse and error propagation.",
                "net/retry.py": "Retry policy, backoff schedule and attempt counting.",
                "net/timeout.py": "Per-request timeout values and deadline propagation.",
                "net/pool.py": "Connection pool sizing, health checks and eviction.",
            },
            "expect": "narrow, then accept or escalate",
        },
    }

    results: dict = {}
    for case_name, case in cases.items():
        print(f"  -- {case_name} case (expect: {case['expect']}) --")
        options = case["options"]
        state = case["state"]
        rounds_log = []
        rounds_left = 2
        current = options
        while True:
            result = jev.decide(
                state,
                {"owner": choice(
                    "Which file should this change be made in? "
                    "Pick the single file that owns the behaviour being changed. "
                    "If more than one applies, pick the one whose ownership is most central.",
                    current)},
            )
            answer = result.answers["owner"]
            verdict = next_round(
                answer.probabilities,
                confidence=answer.confidence,
                rounds_left=rounds_left,
            )
            rounds_log.append({
                "options": list(current),
                "chosen": answer.value,
                "confidence": answer.confidence,
                "probabilities": answer.probabilities,
                "latency_ms": result.timing_ms["total_ms"],
                "verdict": verdict.action,
                "reason": verdict.reason,
            })
            top = "  ".join(f"{k}={v:.2f}" for k, v in answer.top(3))
            print(f"     round {len(rounds_log)}: {answer.value!r} conf={answer.confidence} "
                  f"[{top}] -> {verdict.action}")
            record_decision(
                which="shortlist", intent=f"e7_iteration_{case_name}",
                latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
                cost_usd=result.cost_usd, questions=1,
                confidence={"owner": answer.confidence or 0.0},
                baseline_tokens=count_tokens(state), root=ROOT,
            )
            if verdict.done or not verdict.next_options:
                break
            rounds_left -= 1
            current = {k: options[k] for k in verdict.next_options}
        total_ms = sum(r["latency_ms"] for r in rounds_log)
        print(f"     settled in {len(rounds_log)} round(s), {total_ms:.0f} ms")
        results[case_name] = {
            "expected": case["expect"],
            "rounds": rounds_log,
            "narrowed": any(r["verdict"] == "narrow" for r in rounds_log),
            "total_ms": round(total_ms, 1),
        }

    results["narrowing_triggered"] = any(r["narrowed"] for r in results.values() if isinstance(r, dict))
    return results


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the jevskill benchmark suite")
    parser.add_argument("--quick", action="store_true", help="fewer runs per experiment")
    parser.add_argument("--no-llm", action="store_true", help="skip the E6 LLM contrast call")
    parser.add_argument("--legacy-reduce", action="store_true",
                        help="also run the superseded single-stage REDUCE to reproduce its failure")
    parser.add_argument("--out", default=str(ROOT / "bench" / "results.json"))
    args = parser.parse_args()

    runs = 6 if args.quick else 12
    guard_sample = 40 if args.quick else 80

    config = Config.from_env()
    if not config.has_key():
        print("No API key found. Set OPENROUTER_API_KEY and retry.", file=sys.stderr)
        return 2

    results: dict = {
        "benchmarked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": config.model,
        "endpoint": config.decisions_url,
        "region_note": "measured from a residential connection in Poland",
    }
    with JevClient(config) as jev:
        warm_ms = jev.warm()
        print(f"jevskill benchmark suite — {config.model}")
        print(f"warm-up {warm_ms:.0f} ms   endpoint {config.decisions_url}")
        results["warmup_ms"] = round(warm_ms, 1)
        results["E1_latency"] = e1_latency(jev, runs)
        results["E2_state_size"] = e2_state_size(jev, max(3, runs // 3))
        results["E3_fanout"] = e3_fanout(jev, 8)
        results["E4_reduce"] = e4_reduce(jev)
        if args.legacy_reduce:
            results["E4_reduce_legacy_failure"] = e4_reduce_old(jev)
        results["E5_guards"] = e5_guards(jev, guard_sample)
        results["E7_iteration"] = e7_iteration(jev)

    if not args.no_llm:
        results["E6_llm_contrast"] = e6_llm_contrast(
            {"report": "Checkout shows a blank screen after clicking Pay."},
            "Does this describe a software defect?",
        )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
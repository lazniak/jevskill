"""Real A/B benchmark: does putting Jev in front of the model actually help?

This is the experiment the repository was missing. Every other number here
measures Jev in isolation (latency, cost, reduction). This measures the thing a
user actually cares about: **given a big pile of output, does a model answer a
question about it better, cheaper, or not at all when Jev picks what it reads?**

Method
------
Six workloads, each a large synthetic fixture plus a question with a
**checkable** answer (an exact string from the data). Three arms:

``direct``      the model gets the whole fixture and the question.
``jev``         Jev's REDUCE pattern picks a shortlist; the model gets only that.
``grep``        a deterministic keyword filter picks the shortlist (the cheap
                control that stops us claiming credit for "a filter helps").

Token counts are the **provider's own reported** ``usage``, not an estimate.
Answers are graded against the oracle by substring match. Arms run on the *same*
model, so the only variable is what the model reads.

Reported honestly, including the case where it loses. Workload design follows the
same idea as caveman's published suite so the two are at least conceptually
comparable — though the fixtures differ, so this is NOT a head-to-head.

Run:  python bench/ab.py [--runs 3] [--model google/gemini-2.5-flash-lite]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "jev" / "scripts"))

from jevskill.client import JevClient  # noqa: E402
from jevskill.config import Config  # noqa: E402
from jevskill.orchestrate import count_tokens  # noqa: E402
from jevskill.primitives import noul  # noqa: E402
from jevskill.stats import record_decision  # noqa: E402

import random  # noqa: E402

#: The cheap chat model used for the answering arms. Deliberately small: if Jev
#: only helps against a frontier model, it is not worth the complexity, and a
#: cheap model is the harder comparison.
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"

#: Published list price for the answering model, used ONLY to express the token
#: change in money. Tokens themselves are provider-reported.
MODEL_PRICE_IN = 0.10
MODEL_PRICE_OUT = 0.40

#: Per-1M price of Jev's own input.
JEV_PRICE_IN = 0.042


# --------------------------------------------------------------------------- #
# Workloads
#
# Each builder returns (fixture, model_question, oracle, filter):
#
#   model_question  the question the answering model is asked. Contains the
#                   oracle value ("which order id failed").
#   filter          an ANSWER-NEUTRAL description of what to keep, handed to the
#                   Jev gate and to the grep control. It must describe the *kind*
#                   of thing that matters without naming the answer.
#
# Separating those two is the difference between a benchmark and a trick. An
# earlier version of this file passed the model's question straight to the gate,
# so the gate was told to look for "payment gateway timeout" and then scored as
# if it had found the needle on its own merit. That inflates the Jev arm and
# makes the comparison worthless. This is the same class of defect as the
# un-named-value bug documented in references/prompting.md, and it is worth the
# same care.
# --------------------------------------------------------------------------- #

def _log_fixture(seed: int = 7, lines: int = 520):
    """A needle in a log haystack. Oracle: an order id unique to the needle."""
    rng = random.Random(seed)
    rows = []
    for i in range(lines):
        rows.append(
            f"2026-09-19T10:{i % 60:02d}:{i % 60:02d}Z INFO  request GET /api/v1/items "
            f"status=200 dur={rng.randint(5, 400)}ms trace={rng.randrange(16**8):08x}"
        )
    needle_at = 413
    order = "ZX-40217"
    rows[needle_at] = (
        f"2026-09-19T10:11:11Z ERROR payment gateway timeout after 30s "
        f"order={order} attempt=3/3 correlation={rng.randrange(16**10):010x}"
    )
    return ("\n".join(rows),
            "One payment failed. Which order id failed, and after how many attempts?",
            order,
            "keep lines at a severity above INFO, or lines naming a specific order")


def _csv_fixture(seed: int = 11, rows: int = 1100):
    """A numeric outlier. Oracle: the outlier's user id."""
    rng = random.Random(seed)
    lines = ["user_id,sessions,avg_minutes,country"]
    outlier_uid = "u_88214"
    for i in range(rows):
        uid = outlier_uid if i == 711 else f"u_{10000 + i}"
        sessions = 1847 if uid == outlier_uid else rng.randint(1, 40)
        avg = 312.7 if uid == outlier_uid else round(rng.uniform(0.5, 25.0), 1)
        lines.append(f"{uid},{sessions},{avg},{rng.choice(['PL', 'DE', 'US', 'FR'])}")
    return ("\n".join(lines),
            "One user is a massive outlier in session count. What is that user_id and how many sessions?",
            outlier_uid,
            "keep rows whose numbers are far outside the normal range")


def _test_output_fixture(seed: int = 13):
    """Failing test run. Oracle: the name of the test that fails on assertion."""
    rng = random.Random(seed)
    lines = []
    for i in range(700):
        lines.append(f"test_module_{i%40}.py::test_case_{i} PASSED [  {rng.randint(1,99)}%]")
    target = "test_invoice_rounding.py::test_tax_is_rounded_per_line"
    lines.insert(497, f"{target} FAILED")
    lines.insert(498, "E   AssertionError: Decimal('10.01') != Decimal('10.00')")
    lines.insert(499, "src/billing/invoice.py:42: AssertionError")
    return ("\n".join(lines),
            "One test failed. What is the fully qualified name of the failing test?",
            target,
            "keep lines reporting a failure, an error, or an assertion")


def _json_drift_fixture(seed: int = 17):
    """Deployment config drift. Oracle: which service is off the expected image."""
    rng = random.Random(seed)
    services = []
    for i in range(260):
        name = f"svc-{i:03d}"
        services.append({
            "name": name,
            "image": f"registry.internal/{name}:v{rng.randint(1, 9)}.{rng.randint(0, 9)}",
            "replicas": rng.choice([1, 2, 3, 4]),
            "env": rng.choice(["prod", "staging"]),
            "cpu": f"{rng.choice([100, 250, 500, 1000])}m",
            "memory": f"{rng.choice([256, 512, 1024, 2048])}Mi",
        })
    services[183]["image"] = "registry.internal/svc-183:v0.1.0-SNAPSHOT"
    services[183]["env"] = "prod"
    return (json.dumps({"services": services}, indent=1),
            "Which service in prod is running an image tagged SNAPSHOT? Answer with its name.",
            "svc-183",
            "keep entries whose image tag looks non-release, unusual, or out of pattern")


def _yaml_fixture(seed: int = 19):
    """Feature-flag drift. Oracle: the flag enabled in prod but disabled by default."""
    rng = random.Random(seed)
    lines = ["flags:"]
    target = "flag_0512"
    # NOTE: the flag number comes from the loop counter, not from len(lines), and
    # the loop must run past the target index. Two earlier versions were wrong
    # here: one used the line index as the flag number, the other generated only
    # 420 flags while targeting flag_0512 — so the control arm had no target to
    # find and would have scored as a genuine failure. The assert below is what
    # caught it, and it stays.
    for i in range(520):
        name = f"flag_{i:04d}"
        if name == target:
            lines += [f"  {name}:", "    default: false", "    prod: true", "    owner: team-7"]
            continue
        enabled = rng.choice(["true", "false"])
        lines.append(f"  {name}:")
        lines.append(f"    default: {enabled}")
        lines.append(f"    prod: {enabled}")
        lines.append(f"    owner: team-{rng.randint(1, 12)}")
        lines.append(f"    rollout: {rng.randint(0, 100)}")
    assert any(target in line for line in lines), "target flag was never generated"
    return ("\n".join(lines),
            "Which feature flag is enabled in prod but disabled by default? Answer with the flag name.",
            target,
            "keep lines where the prod value differs from the default value")


def _html_fixture(seed: int = 23):
    """Rendered dashboard alert. Oracle: the metric that breached its threshold."""
    rng = random.Random(seed)
    parts = ["<html><body><h1>Ops Dashboard</h1>"]
    for i in range(220):
        parts.append(
            f'<div class="metric" id="m{i}"><span class="name">metric_{i:03d}</span>'
            f'<span class="value">{rng.uniform(0, 100):.2f}</span>'
            f'<span class="threshold">{rng.randint(50, 99)}</span>'
            f'<span class="status">ok</span></div>'
        )
    parts.append(
        '<div class="alert alert-critical" id="m999">'
        '<span class="name">queue_depth_p99</span>'
        '<span class="value">4187</span>'
        '<span class="threshold">500</span>'
        '<span class="status">BREACHING</span></div>'
    )
    parts.append("</body></html>")
    return ("\n".join(parts),
            "One metric is breaching its threshold. Which metric, and what is its value?",
            "queue_depth_p99",
            "keep elements that signal an alert, a critical state, or a breach")


WORKLOADS = {
    "log_needle": _log_fixture,
    "csv_outlier": _csv_fixture,
    "test_output": _test_output_fixture,
    "json_drift": _json_drift_fixture,
    "yaml_drift": _yaml_fixture,
    "html_alert": _html_fixture,
}

#: A keyword regex per workload for the `grep` control arm — the deterministic
#: filter a competent engineer would use with no model at all. Jev has to beat
#: this to have earned its place, and note that it is *constructed from the same
#: filter description* the Jev gate receives, so the two arms get an equally
#: fair (equally answer-neutral) brief.
GREP_HINTS = {
    "log_needle": r"ERROR|WARN|FATAL",
    "csv_outlier": r",(?:[2-9]\d{2,}|1[0-9]{3,}),",
    "test_output": r"FAILED|Error|assert",
    "json_drift": r"SNAPSHOT|latest|dev|rc|nightly",
    "yaml_drift": None,   # structural, not keyword — see _grep_yaml below
    "html_alert": r"BREACHING|critical|alert",
}


def _grep_yaml(context: str) -> list[str]:
    """A real deterministic filter for the flag-drift case: prod != default.

    Keyword grep genuinely cannot express this, which is the point of including
    it — it is the workload where a *structural* judgement is required and a
    regex is not merely worse but unable to work at all.
    """
    lines = context.splitlines()
    hits: list[str] = []
    for i, line in enumerate(lines):
        # Flag headers sit at exactly two spaces; their properties at four.
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            block = lines[i:i + 6]
            default = prod = None
            for entry in block:
                stripped = entry.strip()
                if stripped.startswith("default:"):
                    default = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("prod:"):
                    prod = stripped.split(":", 1)[1].strip()
            if default is not None and prod is not None and default != prod:
                hits.extend([entry for entry in block[:5] if entry.strip()])
    return hits


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #

def ask_model(model: str, context: str, question: str, key: str, timeout: float = 120.0) -> dict:
    """One chat call. Returns the provider's own usage numbers."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": "Answer the question using only the DATA provided. "
                        "Be brief. Finish with a line: ANSWER: <your answer>"},
            {"role": "user", "content": f"DATA:\n{context}\n\nQUESTION: {question}"},
        ],
        "max_tokens": 300,
    }).encode()
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    elapsed = (time.perf_counter() - started) * 1000
    usage = payload.get("usage") or {}
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    return {
        "content": content,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "ms": elapsed,
    }


def arm_direct(context: str, question: str, model: str, key: str) -> dict:
    result = ask_model(model, context, question, key)
    result["arm"] = "direct"
    result["context_used"] = len(context)
    return result


def arm_grep(context: str, question: str, model: str, key: str,
             pattern: str | None, workload: str) -> dict:
    if workload == "yaml_drift":
        hits = _grep_yaml(context)
    else:
        try:
            regex = re.compile(pattern or r"$^", re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern or ""), re.IGNORECASE)
        hits = [line for line in context.splitlines() if regex.search(line)]
    reduced = "\n".join(hits) if hits else context[:2000]
    result = ask_model(model, reduced, question, key)
    result["arm"] = "grep"
    result["context_used"] = len(reduced)
    result["shortlist_lines"] = len(hits)
    return result


def arm_jev(jev: JevClient, context: str, question: str, filter_description: str,
            model: str, key: str, *, window: int = 40, keep: int = 8) -> dict:
    """REDUCE with Jev: chunk, gate every item, keep the hits.

    ``filter_description`` must be answer-neutral. The answering ``question`` is
    deliberately NOT passed to the gate: telling the gate to look for
    "payment gateway timeout" would hand it the needle and make the comparison
    meaningless.
    """
    lines = context.splitlines()
    kept: list[tuple[float, str]] = []
    calls = 0
    jev_cost = 0.0
    jev_tokens = 0
    started = time.perf_counter()
    for start in range(0, len(lines), window):
        chunk = [line for line in lines[start:start + window] if line.strip()]
        if not chunk:
            continue
        result = jev.decide(
            {f"L{i}": line for i, line in enumerate(chunk)},
            {
                f"keep_L{i}": noul(
                    f"Does the line at `L{i}` match this description: "
                    f"{filter_description}?",
                    f"`L{i}` matches. Lines other than `L{i}` are irrelevant.",
                    f"`L{i}` is routine filler that does not match. "
                    f"Lines other than `L{i}` are irrelevant.",
                )
                for i in range(len(chunk))
            },
        )
        calls += 1
        jev_cost += result.cost_usd
        jev_tokens += result.input_tokens
        for i, line in enumerate(chunk):
            probability = result.noul(f"keep_L{i}") or 0.0
            if probability >= 0.5:
                kept.append((probability, line))
        record_decision(
            which="reduce", intent="ab_benchmark",
            latency_ms=result.timing_ms["total_ms"], tokens_in=result.input_tokens,
            cost_usd=result.cost_usd, questions=len(chunk),
            state_tokens=count_tokens(chunk), baseline_tokens=count_tokens(chunk), root=ROOT,
        )
    selected = [line for _, line in sorted(kept, key=lambda kv: kv[0], reverse=True)[:keep]]
    reduced = "\n".join(selected)
    result = ask_model(model, reduced, question, key)
    result["arm"] = "jev"
    result["context_used"] = len(reduced)
    result["shortlist_lines"] = len(selected)
    result["jev_calls"] = calls
    result["jev_tokens"] = jev_tokens
    result["jev_cost_usd"] = jev_cost
    result["jev_ms"] = (time.perf_counter() - started) * 1000
    return result


def grade(content: str, oracle: str) -> bool:
    """Substring match on the oracle value. Deliberately strict."""
    return oracle.lower() in content.lower()


# --------------------------------------------------------------------------- #

def run_workload(name: str, build, model: str, key: str, runs: int) -> dict:
    context, question, oracle, filter_description = build()
    pattern = GREP_HINTS[name]
    print(f"\n--- {name} ---")
    print(f"  fixture {len(context):,} chars (~{count_tokens(context):,} tokens), "
          f"oracle {oracle!r}")
    print(f"  filter given to jev+grep: {filter_description!r}")
    arms: dict[str, list[dict]] = {"direct": [], "grep": [], "jev": []}

    for run in range(runs):
        with JevClient(Config.from_env()) as jev:
            jev.warm()
            for arm_name, fn in (
                ("direct", lambda: arm_direct(context, question, model, key)),
                ("grep", lambda: arm_grep(context, question, model, key, pattern, name)),
                ("jev", lambda: arm_jev(jev, context, question, filter_description, model, key)),
            ):
                try:
                    row = fn()
                except Exception as exc:
                    row = {"arm": arm_name, "error": str(exc), "content": "",
                           "prompt_tokens": 0, "completion_tokens": 0, "ms": 0.0,
                           "context_used": 0, "jev_cost_usd": 0.0}
                row["correct"] = grade(row.get("content", ""), oracle)
                arms[arm_name].append(row)
        got = "".join("Y" if arms[a][-1]["correct"] else "n" for a in ("direct", "grep", "jev"))
        print(f"  run {run + 1}/{runs}  direct/grep/jev correct: {got}")

    summary: dict = {"workload": name, "oracle": oracle, "runs": runs,
                     "filter_description": filter_description,
                     "fixture_tokens": count_tokens(context), "arms": {}}
    for arm_name, rows in arms.items():
        prompts = [r["prompt_tokens"] for r in rows if r.get("prompt_tokens")]
        completions = [r["completion_tokens"] for r in rows if r.get("prompt_tokens")]
        model_cost = sum(
            r["prompt_tokens"] / 1e6 * MODEL_PRICE_IN
            + r["completion_tokens"] / 1e6 * MODEL_PRICE_OUT
            for r in rows if r.get("prompt_tokens")
        )
        jev_cost = sum(float(r.get("jev_cost_usd", 0) or 0) for r in rows)
        summary["arms"][arm_name] = {
            "correct": sum(1 for r in rows if r["correct"]),
            "of": len(rows),
            "prompt_tokens_mean": round(statistics.fmean(prompts), 1) if prompts else 0,
            "completion_tokens_mean": round(statistics.fmean(completions), 1) if completions else 0,
            "total_tokens_mean": round(statistics.fmean([p + c for p, c in zip(prompts, completions)]), 1) if prompts else 0,
            "model_cost_usd": round(model_cost / max(1, len(rows)), 8),
            "jev_cost_usd": round(jev_cost / max(1, len(rows)), 8),
            "total_cost_usd": round((model_cost + jev_cost) / max(1, len(rows)), 8),
            "ms_mean": round(statistics.fmean([r["ms"] for r in rows]), 1) if rows else 0,
            "context_chars_mean": round(statistics.fmean([r.get("context_used", 0) for r in rows]), 1),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Jev A/B benchmark")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--only", nargs="+", choices=list(WORKLOADS))
    parser.add_argument("--out", default=str(ROOT / "bench" / "ab_results.json"))
    args = parser.parse_args()

    config = Config.from_env()
    if not config.has_key():
        print("No API key. Set OPENROUTER_API_KEY.", file=sys.stderr)
        return 2

    selected = args.only or list(WORKLOADS)
    results = {
        "benchmarked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "answering_model": args.model,
        "answering_model_price_per_mtok": {"input": MODEL_PRICE_IN, "output": MODEL_PRICE_OUT},
        "jev_price_per_mtok_input": JEV_PRICE_IN,
        "runs_per_arm": args.runs,
        "method": (
            "Same model in every arm; the only variable is what the model reads. "
            "Token counts are provider-reported usage. Answers graded by substring "
            "match against an exact oracle value in the fixture."
        ),
        "workloads": [],
    }
    print(f"Jev A/B benchmark — answering model {args.model}, {args.runs} run(s) per arm")

    for name in selected:
        summary = run_workload(name, WORKLOADS[name], args.model, config.api_key, args.runs)
        results["workloads"].append(summary)
        d = summary["arms"]["direct"]
        j = summary["arms"]["jev"]
        delta = (1 - j["total_tokens_mean"] / d["total_tokens_mean"]) * 100 \
            if d["total_tokens_mean"] else 0
        print(f"  => tokens {d['total_tokens_mean']:,.0f} -> {j['total_tokens_mean']:,.0f} "
              f"({delta:+.1f}%)   correct {d['correct']}/{d['of']} -> {j['correct']}/{j['of']}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # Roll-up across workloads.
    print("\n=== TOTAL ===")
    print(f"{'workload':<14} {'direct tok':>11} {'jev tok':>10} {'change':>9} "
          f"{'direct ok':>10} {'jev ok':>7} {'grep ok':>8}")
    tot_d = tot_j = 0
    ok_d = ok_j = ok_g = of = 0
    for w in results["workloads"]:
        d, j, g = w["arms"]["direct"], w["arms"]["jev"], w["arms"]["grep"]
        tot_d += d["total_tokens_mean"]
        tot_j += j["total_tokens_mean"]
        ok_d += d["correct"]; ok_j += j["correct"]; ok_g += g["correct"]; of += d["of"]
        change = (1 - j["total_tokens_mean"] / d["total_tokens_mean"]) * 100 if d["total_tokens_mean"] else 0
        print(f"{w['workload']:<14} {d['total_tokens_mean']:>11,.0f} {j['total_tokens_mean']:>10,.0f} "
              f"{change:>8.1f}% {d['correct']:>6}/{d['of']} {j['correct']:>4}/{j['of']} "
              f"{g['correct']:>5}/{g['of']}")
    overall = (1 - tot_j / tot_d) * 100 if tot_d else 0
    print(f"{'TOTAL':<14} {tot_d:>11,.0f} {tot_j:>10,.0f} {overall:>8.1f}% "
          f"{ok_d:>6}/{of} {ok_j:>4}/{of} {ok_g:>5}/{of}")
    results["totals"] = {
        "direct_tokens_mean": round(tot_d, 1), "jev_tokens_mean": round(tot_j, 1),
        "token_change_pct": round(overall, 1),
        "direct_correct": ok_d, "jev_correct": ok_j, "grep_correct": ok_g, "of": of,
    }
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

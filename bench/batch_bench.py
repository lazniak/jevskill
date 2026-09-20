"""Batch vs per-item, on a balanced fixture with known labels.

This is the reference measurement for `jevskill batch`. It builds a 50/50 salient
/ routine fixture, runs the same question template both ways, and reports tokens,
cost, wall clock, call count and correctness.

    python bench/batch_bench.py            # 60 items, default
    python bench/batch_bench.py --n 120 --window 12
    python bench/batch_bench.py --runs 3   # repeat and report the spread

Token counts and cost are the provider's own reported usage. Correctness is scored
against marker-based ground truth: a line containing ERROR/FATAL/WARN is "problem",
anything else is "routine".

Why a balanced fixture: the first attempt at this used 40 lines taken from the
generator's opening rows, which happen to be pure noise — so "all routine" was
correct and the run proved nothing about accuracy. Half-and-half makes both a
false positive and a missed line visible.
"""

from __future__ import annotations

import argparse
import importlib.util as ilu
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jevskill.client import JevClient  # noqa: E402
from jevskill.config import Config  # noqa: E402
from jevskill.jevtask import load_items, run_batch  # noqa: E402
from jevskill.primitives import choice  # noqa: E402
from jevskill.stats import record_decision  # noqa: E402

SALIENT_MARKERS = ("ERROR", "FATAL", "WARN")

TEMPLATE = {
    "kind": choice(
        "What kind of log line is `item`?",
        {
            "routine": "Debug, info, heartbeat, cache or metrics. Normal operation.",
            "problem": "Reports an error, an exception, a failure, or resource "
                       "exhaustion.",
            "unclear": "Cannot tell from this line alone.",
        },
    )
}


def _load_make_logs():
    spec = ilu.spec_from_file_location("mk", str(ROOT / "bench" / "make_logs.py"))
    module = ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_fixture(n: int, path: Path) -> tuple[list, list[str], list[bool]]:
    mk = _load_make_logs()
    half = n // 2
    salient = [mk.line(t, i) for i, t in enumerate((mk.SALIENT * 40)[:half])]
    noise = [mk.line(t, i) for i, t in enumerate((mk.NOISE * 40)[:n - half])]
    rows = [(t, True) for t in salient] + [(t, False) for t in noise]
    random.Random(11).shuffle(rows)
    with path.open("w", encoding="utf-8") as handle:
        for i, (text, _) in enumerate(rows):
            handle.write(json.dumps({"id": f"B{i}", "line": text}) + "\n")
    items = load_items(path, text_key="line")
    texts = [str(i.data) for i in items]
    truth = [any(m in t for m in SALIENT_MARKERS) for t in texts]
    return items, texts, truth


def score(labels: list, truth: list[bool], positive: str = "problem") -> dict:
    tp = sum(1 for l, t in zip(labels, truth) if t and l == positive)
    fp = sum(1 for l, t in zip(labels, truth) if not t and l == positive)
    fn = sum(1 for l, t in zip(labels, truth) if t and l != positive)
    tally: dict[str, int] = {}
    for label in labels:
        key = str(label)
        tally[key] = tally.get(key, 0) + 1
    return {
        "caught": tp,
        "of": sum(truth),
        "false_positives": fp,
        "missed": fn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "tally": tally,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch vs per-item benchmark")
    parser.add_argument("--n", type=int, default=60, help="items (default 60)")
    parser.add_argument("--window", type=int, default=8, help="items per call")
    parser.add_argument("--runs", type=int, default=1, help="repeat and report spread")
    parser.add_argument("--out", default=str(ROOT / "bench" / "batch_results.json"))
    args = parser.parse_args()

    config = Config.from_env()
    if not config.has_key():
        print("No API key. Set OPENROUTER_API_KEY.", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="jevbatch"))
    fixture = tmp / "balanced.jsonl"
    items, texts, truth = build_fixture(args.n, fixture)
    print(f"fixture: {len(items)} items, {sum(truth)} salient, "
          f"{len(truth) - sum(truth)} routine  ({fixture})")

    runs: dict[str, list[dict]] = {"windowed": [], "per-item": []}
    for run in range(args.runs):
        with JevClient(config) as jev:
            jev.warm()
            for strategy in ("windowed", "per-item"):
                result = run_batch(
                    jev, items, TEMPLATE, strategy=strategy,
                    window_size=args.window, concurrency=6,
                )
                labels = [o.values.get("kind") for o in result.outcomes]
                runs[strategy].append({
                    "tokens": result.input_tokens,
                    "cost_usd": result.cost_usd,
                    "wall_ms": result.wall_ms,
                    "calls": result.calls,
                    "errors": result.errors,
                    "score": score(labels, truth),
                })
                for _ in range(result.calls):
                    record_decision(
                        which="triage", intent=f"batch_{strategy}",
                        tokens_in=result.input_tokens // max(1, result.calls),
                        cost_usd=result.cost_usd / max(1, result.calls),
                        questions=args.window, root=ROOT,
                    )
        print(f"  run {run + 1}/{args.runs} done")

    def mean(strategy: str, key: str) -> float:
        return statistics.fmean(r[key] for r in runs[strategy])

    print()
    header = f"{'metric':<26} {'windowed':>12} {'per-item':>12}"
    print(header)
    print("-" * len(header))
    for label, key, fmt in (
        ("input tokens", "tokens", ",.0f"),
        ("cost (USD)", "cost_usd", ".8f"),
        ("wall clock (ms)", "wall_ms", ",.0f"),
        ("API calls", "calls", ",.0f"),
    ):
        print(f"{label:<26} {format(mean('windowed', key), fmt):>12} "
              f"{format(mean('per-item', key), fmt):>12}")
    print(f"{'salient found':<26} "
          f"{str(runs['windowed'][0]['score']['caught']) + '/' + str(sum(truth)):>12} "
          f"{str(runs['per-item'][0]['score']['caught']) + '/' + str(sum(truth)):>12}")

    ratio_tokens = mean("per-item", "tokens") / mean("windowed", "tokens")
    ratio_ms = mean("per-item", "wall_ms") / mean("windowed", "wall_ms")
    print()
    print(f"windowed uses {ratio_tokens:.2f}x fewer tokens and is {ratio_ms:.1f}x faster")

    payload = {
        "items": len(items),
        "salient": int(sum(truth)),
        "window": args.window,
        "runs": args.runs,
        "windowed": runs["windowed"],
        "per_item": runs["per-item"],
        "token_ratio": round(ratio_tokens, 2),
        "wall_ratio": round(ratio_ms, 2),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

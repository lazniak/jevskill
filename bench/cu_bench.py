"""Micro-benchmark: Jev as the per-step decision core of a computer-use loop.

State = a compact accessibility-tree snapshot (N elements), goal text, last action.
Questions (one call): target element (choice over N ids + none), action type
(choice), goal_reached (noul), stuck (noul). The bundle is deliberately unchanged
from the first run of this script (2026-09-20) so every later number stays
comparable with the ones already published in
`docs/research-2026-09-20-jev-cu.md` §3.

What it measures, and how to ask for it:

    python bench/cu_bench.py --provider typesafe   --n 12,30,60,120,240 --k 10
    python bench/cu_bench.py --provider openrouter --n 12,30,60,120,240 --k 10 --hedge
    python bench/cu_bench.py --provider typesafe   --warm-bench
    python bench/cu_bench.py --micro                    # offline, free

Every run merges into `bench/cu_results.json` (keyed by provider/N/hedge), which
is what `skills/jev/references/hotloop.md` quotes. Live runs cost real money:
~$0.0001 per call at N=12 and ~$0.001 at N=240, so a full sweep of one provider
(50 calls) is a few cents. `--micro` spends nothing.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevskill import __version__  # noqa: E402
from jevskill.client import HAS_HTTPX, JevClient  # noqa: E402
from jevskill.config import Config  # noqa: E402

# Provider comes from the normal resolution (JEVSKILL_PROVIDER, then the key found
# by name, vendor names first) unless --provider says otherwise.

GOAL = "Save the current document and close the dialog"
DEFAULT_OUT = Path(__file__).resolve().parent / "cu_results.json"

def make_tree(n: int) -> dict:
    els = {}
    kinds = ["button", "textbox", "menuitem", "checkbox", "link", "tab"]
    names = ["File", "Edit", "View", "Search", "Font", "Bold", "Italic", "Zoom", "Help",
             "Cancel", "Don't Save", "Save", "Save As", "Print", "Close", "Settings",
             "Undo", "Redo", "Cut", "Copy", "Paste", "Find", "Replace", "Insert",
             "Table", "Image", "Link", "Comment", "Share", "Export"]
    for i in range(n):
        els[f"e{i}"] = {
            "role": kinds[i % len(kinds)] if names[i % len(names)] not in ("Save", "Cancel", "Don't Save", "Close") else "button",
            "name": names[i % len(names)] + ("" if i < len(names) else f" {i}"),
            "bbox": [40 + (i % 8) * 120, 60 + (i // 8) * 48, 110, 32],
            "enabled": True,
            "focused": i == 3,
        }
    return els

def questions(ids: list[str]) -> dict:
    crit = {eid: f"`elements.{eid}` is the control whose activation most directly advances `goal`." for eid in ids}
    crit["none"] = "No listed element advances `goal`; a different action (key, scroll, wait) is needed."
    return {
        "target": {"type": "choice",
                   "instructions": {"question": "Which element in `elements` should be acted on next to advance `goal`?",
                                    "focus": "Pick the single control whose activation is the most direct next step, given `last_action`."},
                   "criteria": crit},
        "action": {"type": "choice",
                   "instructions": "What kind of input advances `goal` from this screen?",
                   "criteria": {"click": "Activate a visible control.",
                                "type": "Enter text into a focused text field.",
                                "key": "Press a keyboard shortcut (Enter, Esc, Ctrl+S).",
                                "scroll": "Content needed is not visible yet.",
                                "wait": "The UI is still loading or animating.",
                                "done": "`goal` is already satisfied by the visible state."}},
        "goal_reached": {"type": "noul",
                         "instructions": "Is `goal` already satisfied by the state in `elements` and `window_title`?",
                         "criteria": {"true": "The visible state shows `goal` completed; nothing more to do.",
                                      "false": "At least one step of `goal` is still pending."}},
        "stuck": {"type": "noul",
                  "instructions": "Does `last_action` together with the current `elements` indicate the previous step had no effect?",
                  "criteria": {"true": "The screen is unchanged from before `last_action`, or an error dialog appeared.",
                               "false": "The screen progressed as expected after `last_action`."}},
    }


def make_state(n: int) -> dict:
    return {"goal": GOAL, "window_title": "Untitled - Editor - Save changes?",
            "last_action": {"type": "key", "key": "Ctrl+W"}, "elements": make_tree(n)}


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile — the same definition `jevskill.stats` uses, so a
    p95 here and a p95 in the ledger mean the same thing."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def run(n: int, k: int, jev: JevClient, *, quiet: bool = False) -> dict:
    """K identical decisions at tree size N. Returns one JSON-ready record."""
    state = make_state(n)
    qs = questions(list(state["elements"]))
    lat: list[float] = []
    http: list[float] = []
    actions: dict[str, int] = {}
    targets: dict[str, int] = {}
    goal_reached: list[float] = []
    stuck: list[float] = []
    target_conf: list[float] = []
    hedge_fired = 0
    winners: dict[str, int] = {}
    errors: list[str] = []
    cost_total = 0.0
    hedge_cost = 0.0
    first = None
    for _ in range(k):
        try:
            r = jev.decide(state, qs, session_id="cu-bench")
        except Exception as exc:  # a hot client with retries=0 surfaces these
            errors.append(f"{type(exc).__name__}: {exc}"[:200])
            continue
        lat.append(r.timing_ms.get("total_ms", 0.0))
        http.append(r.timing_ms.get("http_ms", 0.0))
        if "hedged" in r.timing_ms:
            hedge_fired += 1 if r.timing_ms["hedged"] else 0
            if r.timing_ms["hedged"]:
                winners[r.winner] = winners.get(r.winner, 0) + 1
        cost_total += float(r.usage.get("cost", 0.0) or 0.0)
        hedge_cost += float(r.usage.get("hedge_cost_usd_est", 0.0) or 0.0)
        action = r.choice("action") or "?"
        actions[action] = actions.get(action, 0) + 1
        target = r.choice("target") or "?"
        targets[target] = targets.get(target, 0) + 1
        conf = r.confidence("target")
        if conf is not None:
            target_conf.append(float(conf))
        value = r.noul("goal_reached")
        if value is not None:
            goal_reached.append(value)
        value = r.noul("stuck")
        if value is not None:
            stuck.append(value)
        if first is None:
            first = r
    record = {
        "provider": jev.config.provider,
        "model": first.model if first else jev.config.model,
        "n": n,
        "k": k,
        "ok": len(lat),
        "errors": errors,
        "hot": bool(getattr(jev, "hot", False)),
        "hedge": bool(jev.config.hedge),
        "hedge_after_ms": jev.config.hedge_after_ms if jev.config.hedge else None,
        "timeout_read_s": jev.config.timeout_read_s,
        "tokens_in": first.input_tokens if first else 0,
        "cost_per_call_usd": round(float(first.usage.get("cost", 0.0)), 9) if first else 0.0,
        "cost_total_usd": round(cost_total, 8),
        "hedge_cost_est_usd": round(hedge_cost, 8),
        "http_ms": {
            "p50": round(statistics.median(http), 1) if http else None,
            "p95": round(pct(http, 0.95), 1) if http else None,
            "min": round(min(http), 1) if http else None,
            "max": round(max(http), 1) if http else None,
        },
        "total_ms": {
            "p50": round(statistics.median(lat), 1) if lat else None,
            "p95": round(pct(lat, 0.95), 1) if lat else None,
        },
        "hedged_calls": hedge_fired,
        "winners": winners,
        "target_top3": [[a, round(b, 4)] for a, b in (first.top("target", 3) if first else [])],
        "target_confidence_p50": round(statistics.median(target_conf), 3) if target_conf else None,
        "target_choices": targets,
        "action_distribution": actions,
        "goal_reached_p50": round(statistics.median(goal_reached), 3) if goal_reached else None,
        "stuck_p50": round(statistics.median(stuck), 3) if stuck else None,
    }
    if not quiet:
        mode = "hedge" if record["hedge"] else "plain"
        print(f"N={n:3d} K={k} {record['provider']}/{mode}  tokens_in={record['tokens_in']}  "
              f"cost/call=${record['cost_per_call_usd']:.7f}  ok={record['ok']}/{k}")
        print(f"   http p50={record['http_ms']['p50']} ms  p95={record['http_ms']['p95']}  "
              f"min={record['http_ms']['min']}  max={record['http_ms']['max']}  "
              f"total p50={record['total_ms']['p50']} ms")
        print(f"   target top3={record['target_top3']} conf_p50={record['target_confidence_p50']}")
        print(f"   action={record['action_distribution']}  goal_reached={record['goal_reached_p50']}  "
              f"stuck={record['stuck_p50']}")
        if record["hedge"]:
            print(f"   hedged {hedge_fired}/{record['ok']} calls, winners={winners}, "
                  f"hedge cost est ${record['hedge_cost_est_usd']:.6f}")
        if errors:
            print(f"   errors: {errors[:2]}")
    return record


def warm_bench(provider: str | None, *, repeats: int = 3, n: int = 12) -> list[dict]:
    """HEAD warm-up vs a real mini-decision warm-up, and what each buys.

    Each repeat builds a **fresh client**, because a warm-up can only be measured
    once per connection pool — the second one is warming an already-warm socket
    and measures nothing. ``none`` is the control: no warm-up at all, so the
    first decision pays whatever the warm-up would have paid.

    ``warm_tokens_in_median`` / ``warm_cost_usd_median`` come from the warm-up
    decision itself (``JevClient.last_warm_decision``), so the price of
    ``mode="decision"`` is a measurement in this file rather than a number
    quoted from memory in the docs. ``head`` and ``none`` record zero because
    they really do cost nothing.

    Rows are keyed by the provider actually used, not by whatever was asked for:
    a run without ``--provider`` used to file itself under ``"resolved"``, which
    names no endpoint and silently shadowed nothing.
    """
    out: list[dict] = []
    state = make_state(n)
    qs = questions(list(state["elements"]))
    resolved = provider or ""
    for mode in ("none", "head", "decision"):
        warms: list[float] = []
        firsts: list[float] = []
        seconds: list[float] = []
        warm_tokens: list[int] = []
        warm_costs: list[float] = []
        for _ in range(repeats):
            jev = JevClient(Config.from_env(provider))
            resolved = jev.config.provider
            try:
                warm_ms = 0.0 if mode == "none" else jev.warm(mode=mode)
                if jev.last_warm_decision is not None:
                    warm_tokens.append(jev.last_warm_decision.input_tokens)
                    warm_costs.append(jev.last_warm_decision.cost_usd)
                first = jev.decide(state, qs, session_id="cu-warm")
                second = jev.decide(state, qs, session_id="cu-warm")
                warms.append(warm_ms)
                firsts.append(first.timing_ms["total_ms"])
                seconds.append(second.timing_ms["total_ms"])
            finally:
                jev.close()
        record = {
            "provider": resolved,
            "mode": mode,
            "repeats": repeats,
            "n": n,
            "warm_ms_median": round(statistics.median(warms), 1) if warms else 0.0,
            "warm_tokens_in_median": int(statistics.median(warm_tokens)) if warm_tokens else 0,
            "warm_cost_usd_median": round(statistics.median(warm_costs), 8) if warm_costs else 0.0,
            "warm_cost_usd_total": round(sum(warm_costs), 8),
            "first_decision_ms_median": round(statistics.median(firsts), 1),
            "first_decision_ms": [round(v, 1) for v in firsts],
            "second_decision_ms_median": round(statistics.median(seconds), 1),
            "warm_plus_first_ms": round(
                statistics.median(warms) + statistics.median(firsts), 1) if warms
            else round(statistics.median(firsts), 1),
        }
        out.append(record)
        print(f"warm mode={mode:8s} warm={record['warm_ms_median']:7.1f} ms  "
              f"first decision={record['first_decision_ms_median']:7.1f} ms  "
              f"(next {record['second_decision_ms_median']:7.1f} ms)  "
              f"warm+first={record['warm_plus_first_ms']:7.1f} ms  "
              f"warm cost=${record['warm_cost_usd_median']:.6f} "
              f"({record['warm_tokens_in_median']} tok)")
    return out


def micro(reps: int = 20) -> dict:
    """Offline costs that sit *inside* the step budget: redaction and the ledger.

    Both are things the hot loop pays on every step whether or not the model is
    fast, which is why they are measured here rather than assumed small.
    """
    import tempfile

    from jevskill.orchestrate import count_tokens
    from jevskill.redact import redact_state
    from jevskill.stats import Ledger, build_record, record_decision

    state = make_state(60)
    blob = json.dumps(state, ensure_ascii=False)

    def timed(fn, n: int) -> list[float]:
        out = []
        for _ in range(n):
            t = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t) * 1000.0)
        return out

    red = timed(lambda: redact_state(state), reps)
    red_mail = timed(lambda: redact_state(state, redact_emails=True), reps)

    row = build_record(which="act", intent="hot-loop step", latency_ms=312.0,
                       tokens_in=6041, cost_usd=0.000254, questions=4,
                       state_tokens=6041, confidence={"target": 0.99},
                       baseline_tokens=6041)
    ledger = Ledger(root=Path(tempfile.mkdtemp()), flush_every=10_000,
                    flush_interval_s=60.0, start_thread=False)
    buffered = timed(lambda: ledger.write(row), 1000)
    ledger.close()
    direct_root = Path(tempfile.mkdtemp())
    direct = timed(lambda: record_decision(which="act", root=direct_root, latency_ms=312.0,
                                           tokens_in=6041, cost_usd=0.000254, questions=4,
                                           state_tokens=6041, baseline_tokens=6041), 200)

    out = {
        "state": {
            "elements": len(state["elements"]),
            "chars": len(blob),
            "estimated_tokens": count_tokens(blob),
            "api_tokens_in_for_same_state_plus_questions": 6041,
        },
        "redact_state_ms": {
            "reps": reps,
            "median": round(statistics.median(red), 3),
            "min": round(min(red), 3),
            "max": round(max(red), 3),
            "median_with_emails": round(statistics.median(red_mail), 3),
            "cache_added": False,
            "cache_threshold_ms": 2.0,
        },
        "ledger_write_ms": {
            "reps": 1000,
            "buffered_median": round(statistics.median(buffered), 4),
            "buffered_p95": round(pct(buffered, 0.95), 4),
            "buffered_max": round(max(buffered), 4),
            "unbuffered_median": round(statistics.median(direct), 4),
            "unbuffered_p95": round(pct(direct, 0.95), 4),
            "target_ms": 0.5,
        },
        "host": host_facts(),
    }
    print(json.dumps(out, indent=2))
    return out


def host_facts() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "httpx": HAS_HTTPX,
        "jevskill": __version__,
    }


def merge(out_path: Path, *, runs: list[dict] | None = None,
          warm: list[dict] | None = None, micro_data: dict | None = None,
          probes: list[dict] | None = None) -> None:
    """Merge into the results file instead of overwriting it.

    Two providers and two modes are four separate invocations; a writer that
    truncated would leave the file describing whichever one ran last, and the doc
    quoting it would be quoting a fragment.
    """
    data: dict = {}
    if out_path.exists():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.setdefault("about", (
        "Per-step decision benchmark for a computer-use loop. Written by "
        "bench/cu_bench.py; every number in skills/jev/references/hotloop.md "
        "comes from this file."
    ))
    data["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["host"] = host_facts()
    if micro_data is not None:
        data["micro"] = micro_data
    if warm:
        existing = {(w["provider"], w["mode"]): w for w in data.get("warm", [])}
        for record in warm:
            existing[(record["provider"], record["mode"])] = record
        data["warm"] = [existing[k] for k in sorted(existing)]
    if probes:
        # A probe is the same measurement with a deliberately different
        # `hedge_after_ms`, kept apart from `runs` so the main table keeps
        # comparing like with like.
        pkey = lambda r: (r["provider"], r["hedge_after_ms"] or 0, r["n"])  # noqa: E731
        existing = {pkey(r): r for r in data.get("hedge_probes", [])}
        for record in probes:
            existing[pkey(record)] = record
        data["hedge_probes"] = [existing[k] for k in sorted(existing)]
    if runs:
        key = lambda r: (r["provider"], bool(r["hedge"]), r["n"])  # noqa: E731
        existing = {key(r): r for r in data.get("runs", [])}
        for record in runs:
            existing[key(record)] = record
        data["runs"] = [existing[k] for k in sorted(existing)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path}")


def table(out_path: Path) -> None:
    """Print what the results file now says — plain vs hedged, per provider."""
    if not out_path.exists():
        return
    data = json.loads(out_path.read_text(encoding="utf-8"))
    runs = data.get("runs", [])
    if not runs:
        return
    by = {(r["provider"], bool(r["hedge"]), r["n"]): r for r in runs}
    providers = sorted({r["provider"] for r in runs})
    sizes = sorted({r["n"] for r in runs})
    print("\nhttp latency, ms (p50 / p95)            hedged = duplicate after "
          f"{runs[0].get('hedge_after_ms') or 400:.0f} ms")
    header = f"{'provider':<11}{'N':>5}{'tokens':>8}{'plain p50':>11}{'plain p95':>11}" \
             f"{'hedge p50':>11}{'hedge p95':>11}{'fired':>7}{'won':>6}"
    print(header)
    print("-" * len(header))
    for provider in providers:
        for n in sizes:
            plain = by.get((provider, False, n))
            hedged = by.get((provider, True, n))
            if not plain and not hedged:
                continue
            source = plain or hedged
            fired = f"{hedged['hedged_calls']}/{hedged['ok']}" if hedged else "-"
            won = str(hedged["winners"].get("hedge", 0)) if hedged else "-"
            def cell(record, field):
                return f"{record['http_ms'][field]:.0f}" if record and record["http_ms"][field] is not None else "-"
            print(f"{provider:<11}{n:>5}{source['tokens_in']:>8}"
                  f"{cell(plain, 'p50'):>11}{cell(plain, 'p95'):>11}"
                  f"{cell(hedged, 'p50'):>11}{cell(hedged, 'p95'):>11}{fired:>7}{won:>6}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("k", nargs="?", type=int, default=None,
                        help="legacy positional K (same as --k)")
    parser.add_argument("--provider", choices=("typesafe", "openrouter"), default=None,
                        help="endpoint to measure; default is the normal resolution")
    parser.add_argument("--n", default=None,
                        help="comma-separated tree sizes, default 12,30,60 "
                             "(240 is the Choice-option ceiling: 240 ids + none = 241 of 255)")
    parser.add_argument("--k", dest="k_opt", type=int, default=10,
                        help="calls per size (default 10)")
    parser.add_argument("--hedge", action="store_true",
                        help="hot client with hedging on (duplicate after hedge_after_ms)")
    parser.add_argument("--no-hot", action="store_true",
                        help="use the default client instead of hot mode")
    parser.add_argument("--hedge-after-ms", type=float, default=None)
    parser.add_argument("--hedge-probe", action="store_true",
                        help="record this sweep under `hedge_probes` instead of "
                             "`runs` — for trying a different --hedge-after-ms "
                             "without disturbing the comparable table")
    parser.add_argument("--warm-mode", default="head",
                        choices=("none", "head", "decision"),
                        help="warm-up before the sweep. Default head, which is what "
                             "the first published run of this script did, so the "
                             "rows stay comparable. Use --warm-bench to measure "
                             "the warm-up itself.")
    parser.add_argument("--warm-bench", action="store_true",
                        help="measure HEAD vs decision warm-up on this provider")
    parser.add_argument("--warm-repeats", type=int, default=5,
                        help="fresh clients per warm mode (default 5)")
    parser.add_argument("--micro", action="store_true",
                        help="offline: redaction and ledger cost only, no API calls")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-write", action="store_true", help="do not touch --out")
    args = parser.parse_args()

    if args.micro:
        data = micro()
        if not args.no_write:
            merge(args.out, micro_data=data)
        return

    k = args.k if args.k is not None else args.k_opt
    # --warm-bench on its own measures warm-up only; asking for sizes as well
    # runs both, which is what a full sweep of one provider does.
    sizes_arg = args.n if args.n is not None else ("" if args.warm_bench else "12,30,60")
    sizes = [int(x) for x in str(sizes_arg).split(",") if x.strip()]

    warm_records: list[dict] | None = None
    if args.warm_bench:
        warm_records = warm_bench(args.provider, repeats=args.warm_repeats)

    runs: list[dict] = []
    if sizes:
        client = JevClient(
            Config.from_env(args.provider),
            hot=not args.no_hot,
            hedge=args.hedge,
            hedge_after_ms=args.hedge_after_ms,
        )
        try:
            if args.warm_mode != "none":
                t0 = time.perf_counter()
                client.warm(mode=args.warm_mode)
                print(f"warm ({args.warm_mode}): {(time.perf_counter() - t0) * 1000:.0f} ms")
            for n in sizes:
                runs.append(run(n, k, client))
        finally:
            client.close()

    if not args.no_write:
        if args.hedge_probe:
            merge(args.out, probes=runs, warm=warm_records)
        else:
            merge(args.out, runs=runs, warm=warm_records)
            table(args.out)


if __name__ == "__main__":
    main()

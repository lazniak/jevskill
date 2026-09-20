"""The command-line surface. This is what a harness shells out to.

A billing harness does not import a Python library — it runs a command and reads
JSON. Every command therefore:

* prints a single machine-readable JSON object when ``--json`` is given, and a
  human report otherwise,
* writes one row to the effectiveness ledger (except for the pure planning
  commands, which spend nothing and are therefore free to call often),
* times its own stages from process start, so ``t_decision`` genuinely covers
  "the moment the harness decided to use the skill".

Run ``jevskill --help`` for the surface, or ``jevskill patterns`` to see the
palette of coding-workflow uses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Captured at import: the earliest honest timestamp for "the harness decided to
# use this skill". Every command measures its first stage from here.
_T_IMPORT_NS = time.perf_counter_ns()

from . import __version__
from .client import JevClient
from .config import PROVIDERS, Config
from .errors import JevApiError, JevConfigError, JevError, JevQuestionError
from .jevtask import (
    estimate_separate_tokens,
    load_items,
    measure_separate_cost,
    run_batch,
    save_results,
    summarize_outcomes,
)
from .orchestrate import (
    PATTERNS,
    choose_pattern,
    count_tokens,
    estimate_saving,
    plan_for,
    profile,
    should_use_jev,
)
from .primitives import choice, noul, score, validate_questions
from .stages import Stages, format_stages
from .stats import (
    advise,
    format_advice,
    ledger_path,
    load_records,
    record_decision,
    record_outcome,
    summarize,
)

_PRIMITIVE_BUILDERS = {"noul": noul, "choice": choice, "score": score}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _emit(payload: dict, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


def _load_state(args: argparse.Namespace) -> object:
    if args.state_file:
        path = Path(args.state_file)
        if not path.exists():
            raise JevError(f"state file not found: {path}")
        text = path.read_text(encoding="utf-8")
        if args.state_file.lower().endswith(".json"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text
    if args.state is not None:
        return args.state
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            return text
    raise JevError("no state supplied — pass --state, --state-file, or pipe it on stdin")


def _build_questions(args: argparse.Namespace) -> dict[str, dict]:
    """Build question bundle from --questions JSON, or from single-question flags."""
    if args.questions:
        try:
            questions = json.loads(args.questions)
        except json.JSONDecodeError as exc:
            raise JevQuestionError(f"--questions is not valid JSON: {exc}") from exc
        validate_questions(questions)
        return questions

    if not args.question_type:
        raise JevQuestionError(
            "no questions given — use --questions '<json>' or "
            "--question-type {noul,choice,score} with --name/--instructions"
        )

    kind = args.question_type
    name = args.name or "q0"
    instructions = args.instructions or "Answer the question."
    if kind == "noul":
        question = noul(instructions, args.true_text, args.false_text)
    elif kind == "choice":
        options = [o for o in (args.options or []) if o]
        if len(options) < 2:
            raise JevQuestionError("choice needs at least 2 --options")
        question = choice(instructions, {o: o for o in options})
    else:
        levels = [l for l in (args.levels or []) if l]
        if len(levels) < 2:
            raise JevQuestionError("score needs at least 2 --levels")
        question = score(instructions, levels)
    questions = {name: question}
    validate_questions(questions)
    return questions


def _baseline_tokens(state: object, questions: dict) -> int:
    """Estimate the context an LLM would have needed for the same judgement.

    This is the number the ledger stores as ``baseline_tokens`` and the number the
    README's savings figure is built from, so it is stated plainly rather than
    buried: the *data itself*, plus the question text an LLM would have needed.
    (The ~350 tokens of scaffolding — system prompt, output-format instructions —
    are added later by `stats.DEFAULT_BASELINE`, so they are not counted here, to
    avoid double-counting.)

    Routed through the single calibrated `count_tokens` helper on purpose. An
    earlier version open-coded its own chars/3.6 division while the rest of the
    codebase used a calibrated constant, so the ledger and the budget check
    disagreed about how big the same data was.
    """
    import json as _json

    state_text = state if isinstance(state, str) else _json.dumps(state, ensure_ascii=False)
    question_text = _json.dumps(questions, ensure_ascii=False)
    return count_tokens(state_text) + count_tokens(question_text)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    stages = Stages.begin()
    # `t_decision` is the instant the harness committed to Jev, so it must be the
    # first mark. Resolving the config is setup, not the decision, and belongs to
    # `profile` — marking it first (as this command once did) reported the config
    # lookup as the decision time.
    stages.mark("t_decision")
    config = Config.from_env(provider=getattr(args, "provider", None))
    report: dict = {
        "version": __version__,
        "ok": False,
        "provider": config.provider,
        "provider_options": sorted(PROVIDERS),
        "key_found": config.has_key(),
        "key_source_hint": config.api_key[:12] + "..." if config.api_key else None,
        "model": config.model,
        "endpoint": config.decisions_url,
        "context_tokens": config.context_tokens,
        "cost_reported_by_provider": config.reports_cost,
        "ledger": str(ledger_path()),
        "ledger_exists": ledger_path().exists(),
    }
    stages.mark("profile")
    if not config.has_key():
        spec = PROVIDERS[config.provider]
        report["error"] = (
            f"No API key for provider '{config.provider}'. Set one of "
            f"{', '.join(spec['key_env'])}, or write {{\"api_key\": \"...\"}} to "
            f"~/.jevskill/config.json. Use --provider to switch endpoints."
        )
        _emit(report, args.json, f"NOT OK: {report['error']}")
        stages.mark("report")
        report["stages_ms"] = stages.ordered()
        return 2

    stages.mark("plan")
    # Doctor's payload is a fixed probe, so there is nothing to build: `build` is
    # marked before the client exists and stays ~0 ms. Marking it *inside* the
    # `with` block (as this command once did) attributed the TCP/TLS handshake to
    # `build` — 814.7 ms of "query construction" for a two-key payload — and left
    # the declared `warm` stage unmarked, which pushed the handshake into `http`
    # and made the reported inference time exceed the real decision latency.
    stages.mark("build")
    try:
        with JevClient(config) as client:
            warm_ms = client.warm()
            # Same rationale as `cmd_ask`: the handshake is real work a harness
            # pays once, and it must not be charged to the model.
            stages.mark("warm")
            started = time.perf_counter()
            result = client.decide(
                {"probe": "connectivity check"},
                {"alive": noul("Is this request working?", "Yes.", "No.")},
            )
            stages.mark("http")
            stages.absorb(result.timing_ms)
            report.update(
                {
                    "ok": True,
                    "warm_ms": round(warm_ms, 1),
                    "probe_noul": result.noul("alive"),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "resolved_model": result.model,
                    "input_tokens": result.input_tokens,
                    "cost_usd": result.cost_usd,
                }
            )
    except JevApiError as exc:
        report["error"] = str(exc)
        report["hint"] = exc.hint
    except JevError as exc:
        report["error"] = str(exc)

    stages.mark("report")
    report["stages_ms"] = stages.ordered()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status = "OK" if report["ok"] else "NOT OK"
        lines = [
            f"jevskill {__version__} — {status}",
            f"  key       : {'found' if report['key_found'] else 'MISSING'}",
            f"  model     : {report['model']}",
            f"  endpoint  : {report['endpoint']}",
            f"  ledger    : {report['ledger']}",
        ]
        if report["ok"]:
            lines += [
                f"  warm      : {report['warm_ms']} ms",
                f"  probe     : {report['latency_ms']} ms  (noul={report['probe_noul']})",
                f"  tokens    : {report['input_tokens']}  cost ${report['cost_usd']:.8f}",
            ]
        else:
            lines.append(f"  error     : {report.get('error')}")
        lines.append("  stages    :")
        lines.append(format_stages(report["stages_ms"]))
        print("\n".join(lines))
    return 0 if report["ok"] else 2


def cmd_ask(args: argparse.Namespace) -> int:
    """One decision: state + questions -> typed answers, timed and ledgered."""
    stages = Stages.begin()
    stages.mark("t_decision")

    state = _load_state(args)
    questions = _build_questions(args)
    stages.mark("profile")
    stages.mark("plan")

    shape = profile(state, max_state_tokens=args.max_state_tokens)
    if not shape.fits and not args.force:
        payload = {
            "ok": False,
            "error": "state exceeds budget",
            "shape": shape.to_dict(),
            "advice": shape.note,
        }
        stages.mark("report")
        payload["stages_ms"] = stages.ordered()
        _emit(payload, args.json, f"REFUSED: {shape.note}")
        return 3

    config = Config.from_env(
        provider=getattr(args, "provider", None),
        max_state_tokens=args.max_state_tokens,
    )
    baseline_tokens = _baseline_tokens(state, questions)
    stages.mark("build")

    with JevClient(config) as client:
        if args.warm:
            client.warm()
        # `warm` gets its own stage: folding the connection handshake into `http`
        # would report a first-call latency several times the real inference time
        # (measured 1189 ms of "http" against a 345 ms decision).
        stages.mark("warm")
        started = time.perf_counter()
        result = client.decide(state, questions, session_id=args.session_id)
        wall_ms = (time.perf_counter() - started) * 1000
        stages.mark("http")
        stages.absorb(result.timing_ms)

    confidences = {
        name: answer.confidence
        for name, answer in result.answers.items()
        if answer.confidence is not None
    }

    payload = {
        "ok": True,
        "provider": config.provider,
        "answers": {name: answer.to_dict() for name, answer in result.answers.items()},
        "values": {name: result.value(name) for name in result.answers},
        "model": result.model,
        "usage": result.usage,
        "timing_ms": result.timing_ms,
        "wall_ms": round(wall_ms, 1),
        "shape": shape.to_dict(),
        "baseline_tokens": baseline_tokens,
    }

    # The ledger should capture the stage breakdown *including* the work of
    # applying and recording the decision, so `act` is marked before the write
    # and the snapshot is taken for the row.
    decision_id = ""
    stages.mark("act")
    recorded_stages = stages.ordered()
    if not args.no_ledger:
        decision_id = record_decision(
            which=args.pattern or choose_pattern(args.intent or args.instructions or "")[0],
            intent=args.intent or "",
            stages_ms=recorded_stages,
            latency_ms=result.timing_ms.get("total_ms", wall_ms),
            tokens_in=result.input_tokens,
            tokens_out=int(result.usage.get("output_tokens", 0) or 0),
            cost_usd=result.cost_usd,
            questions=len(questions),
            state_tokens=shape.tokens,
            confidence=confidences,
            baseline_tokens=baseline_tokens,
            session_id=args.session_id or "",
            version=__version__,
            root=args.ledger_root,
        )
        payload["decision_id"] = decision_id
    payload["ledger"] = str(ledger_path(args.ledger_root))

    stages.mark("report")
    stages_dict = stages.ordered()
    payload["stages_ms"] = stages_dict

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        lines = [f"JEV decided ({result.model}) in {result.timing_ms.get('total_ms', wall_ms):.0f} ms"]
        for name, answer in result.answers.items():
            lines.append(f"  {name}: {_render_answer(answer)}")
        lines.append("")
        lines.append(
            f"  tokens {result.input_tokens}  cost ${result.cost_usd:.8f}"
            f"  (LLM baseline ~{baseline_tokens} tok)"
        )
        if decision_id:
            lines.append(f"  decision_id {decision_id}")
        lines.append("  stages:")
        lines.append(format_stages(stages_dict))
        print("\n".join(lines))
    return 0


def _render_answer(answer) -> str:
    if answer.kind == "noul":
        return f"P(true)={answer.value}"
    if answer.kind == "choice":
        top = answer.top(3)
        detail = "  ".join(f"{k}={v:.2f}" for k, v in top)
        return f"{answer.value!r} (conf {answer.confidence})  [{detail}]"
    if answer.kind == "score":
        return f"{answer.value} (conf {answer.confidence})"
    return json.dumps(answer.raw, ensure_ascii=False)[:120]


def cmd_plan(args: argparse.Namespace) -> int:
    """Zero-cost planning: should Jev be used, which pattern, how many calls."""
    stages = Stages.begin()
    stages.mark("t_decision")
    data = None
    if args.state_file or args.state:
        data = _load_state(args)
    verdict = should_use_jev(args.problem or "", data)
    stages.mark("profile")
    stages.mark("plan")
    saving = estimate_saving(state=data, questions=args.questions) if data is not None else None
    if saving:
        verdict["saving"] = saving
    stages.mark("report")
    verdict["stages_ms"] = stages.ordered()

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        if not verdict["use_jev"]:
            print(f"DO NOT USE JEV ({verdict['reason']})\n  {verdict['why']}")
        else:
            plan = verdict["plan"]
            print(f"USE JEV — pattern '{plan['pattern']}'")
            print(f"  why    : {plan['why']}")
            print(f"  layers : {plan['layers']}")
            print(f"  calls  : {plan['calls']}")
            print(f"  data   : {plan['shape']['tokens']} tok — {plan['note']}")
            if saving:
                print(
                    f"  cost   : ${saving['jev_cost_usd']:.8f} (Jev) vs "
                    f"${saving['llm_cost_usd']:.6f} (LLM)  = {saving['ratio']}x"
                )
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    """The palette. Free to call; this is the discovery surface for a harness."""
    payload = {"version": __version__, "patterns": PATTERNS}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"JEV usage palette for coding harnesses (jevskill {__version__})\n")
    for name, info in PATTERNS.items():
        print(f"  {name}")
        print(f"      shape   : {info['shape']}")
        print(f"      why     : {info['why']}")
        print(f"      example : {info['example']}")
        print(f"      layers  : {info['layers']}")
    print("\nRule of thumb: if the answer is prose, code, or an open-ended set,")
    print("Jev cannot do it. If the answer is one of N things you can list up")
    print("front, Jev does it in ~300 ms for about $0.00002.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Report the measured record of this skill's own effectiveness.

    Scope is deliberate: this project's ledger only, unless asked otherwise. An
    earlier version always merged ``~/.jevskill/ledger.jsonl``, which made the
    output depend on whatever the machine had ever run and made a per-project
    report quietly include every other project's decisions. Merging is now opt-in
    via ``--include-global``.
    """
    if args.ledger:
        paths = [Path(args.ledger)]
    elif args.global_only:
        from .stats import global_ledger_path

        paths = [global_ledger_path()]
    else:
        paths = [ledger_path()]
    records = load_records(paths, include_global=bool(args.include_global))
    summary = summarize(records)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not summary.get("decisions"):
        print(f"No decisions recorded yet. Ledger: {ledger_path()}")
        print("Run 'jevskill ask ...' or 'jevskill bench' to populate it.")
        return 0

    lat = summary["latency_ms"]
    print(f"JEV effectiveness ledger — {summary['decisions']} decisions")
    print(f"  window     : {summary['first_ts']} .. {summary['last_ts']}")
    print(f"  questions  : {summary['questions_asked']}")
    print(f"  latency    : p50 {lat['p50']} ms   p95 {lat['p95']} ms   min {lat['min']}   max {lat['max']}")
    print(f"  jev cost   : ${summary['cost_usd']:.6f}  (${summary['cost_usd_per_decision']:.8f}/decision)")
    print(f"  input tok  : {summary['input_tokens']}")
    if summary["saved_pct"] is not None:
        print(
            f"  vs LLM     : ${summary['baseline_cost_usd']:.4f} baseline -> "
            f"saved ${summary['saved_usd']:.4f} ({summary['saved_pct']}%), "
            f"{summary['baseline_tokens_avoided']} tokens kept out of LLM context"
        )
    if summary["accuracy"] is not None:
        print(f"  accuracy   : {summary['accuracy']*100:.1f}% over {summary['judged']} judged decisions")
    else:
        print("  accuracy   : no outcomes paired yet — use 'jevskill outcome <decision_id> correct'")
    print("\n  by pattern:")
    for name, entry in sorted(summary["by_pattern"].items(), key=lambda kv: -kv[1]["n"]):
        acc = f"  acc {entry['accuracy']*100:.0f}%" if entry["accuracy"] is not None else ""
        print(
            f"    {name:12s} n={entry['n']:<4d} p50={entry['p50_ms']:7.1f} ms  "
            f"${entry['cost_usd']:.6f}{acc}"
        )
    if summary["by_intent"]:
        print("\n  by intent:")
        for name, entry in sorted(summary["by_intent"].items(), key=lambda kv: -kv[1]["n"])[:12]:
            acc = f"  acc {entry['accuracy']*100:.0f}%" if entry["accuracy"] is not None else ""
            print(f"    {name[:24]:24s} n={entry['n']:<4d} p50={entry['p50_ms']:7.1f} ms{acc}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Apply one set of questions to many items, and report the measured saving.

    This is the large-dataset path: triage, classify, label or filter N things in
    as few calls as possible. The token saving is reported from the API's own
    usage, not projected.
    """
    stages = Stages.begin()
    stages.mark("t_decision")

    template = _build_questions(args)
    stages.mark("plan")

    try:
        items = load_items(args.input, text_key=args.text_key)
    except (ValueError, OSError) as exc:
        raise JevError(f"could not read items: {exc}") from exc
    if args.limit:
        items = items[:args.limit]
    stages.mark("profile")

    config = Config.from_env(provider=getattr(args, "provider", None),
                             max_state_tokens=args.max_state_tokens)
    stages.mark("build")

    ledger_rows: list[tuple] = []

    def on_call(result, mapping):
        # One ledger row per call, labelled with the batch intent so `stats` and
        # `advice` can judge the batch path on its own terms.
        covered = sorted({index for index, _ in mapping.values()})
        baseline = sum(count_tokens(items[i].as_state()) for i in covered)
        record_decision(
            which=args.pattern or "triage",
            intent=args.intent or "batch",
            stages_ms=result.timing_ms,
            latency_ms=result.timing_ms.get("total_ms", 0.0),
            tokens_in=result.input_tokens,
            tokens_out=int(result.usage.get("output_tokens", 0) or 0),
            cost_usd=result.cost_usd,
            questions=len(mapping),
            state_tokens=baseline,
            confidence=_confidences(result),
            baseline_tokens=baseline,
            session_id=args.session_id or "",
            version=__version__,
            root=args.ledger_root,
        )
        ledger_rows.append((result, len(covered)))

    with JevClient(config) as client:
        if args.warm:
            client.warm()
        stages.mark("warm")
        # Measure the one-item-per-call cost for real rather than assuming it.
        # Costs one extra call and removes the possibility of a flattering ratio.
        if args.measure_baseline:
            try:
                base_tokens, base_cost, n = measure_separate_cost(
                    client, items, template, timeout_s=args.timeout)
                measured = (base_tokens, base_cost, n)
            except Exception:
                measured = None
        else:
            measured = None

        result = run_batch(
            client, items, template,
            strategy=args.strategy,
            window_size=args.window,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
            session_id=args.session_id,
            on_call=None if args.no_ledger else on_call,
        )
        stages.mark("http")

    if measured:
        result.separate_tokens, result.separate_cost_usd, probed = measured
        result.baseline_measured = True
        result.baseline_probe_items = probed
        result.separate_calls = len(items)
    else:
        result.separate_tokens = estimate_separate_tokens(items)
        result.separate_calls = len(items)
    stages.mark("act")

    payload = result.to_dict()
    payload.update({
        "ok": True,
        "provider": config.provider,
        "source": str(args.input),
        "tally": summarize_outcomes(result),
        "ledger": str(ledger_path(args.ledger_root)),
        "stages_ms": stages.ordered(),
    })

    if args.out:
        save_results(result, args.out)
        payload["out_file"] = str(args.out)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_batch(result, payload["tally"], args.quiet))
    return 0


def _confidences(result) -> dict:
    out = {}
    for name, answer in result.answers.items():
        if answer.confidence is not None:
            out[name] = answer.confidence
    return out


def _render_batch(result, tally: dict, quiet: bool) -> str:
    lines = [
        f"Jev batch [{result.strategy}] — {len(result.outcomes)} items "
        f"in {result.calls} call(s)",
    ]
    saving = result.token_saving_pct
    if saving is not None:
        how = (f"measured on {result.baseline_probe_items} item(s), then scaled"
               if result.baseline_measured
               else "estimated from item text — a lower bound")
        lines.append(
            f"  reading  {result.input_tokens:,} tokens batched vs "
            f"{result.separate_tokens:,} one-per-call  ({saving:.0f}% fewer)"
        )
        lines.append(
            f"  cost     ${result.cost_usd:.8f} batched vs "
            f"${result.separate_cost_usd:.8f} one-per-call"
            f"   ({result.calls} call(s) vs {result.separate_calls})"
        )
        lines.append(f"  baseline {how}")
    else:
        lines.append(f"  cost     ${result.cost_usd:.8f}   {result.calls} call(s)")
    lines.append(f"  wall     {result.wall_ms:.0f} ms")
    if result.errors:
        lines.append(f"  errors   {result.errors}")
    if not quiet:
        lines.append("")
        lines.append("  tally:")
        for name, counts in tally.items():
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            rendered = "  ".join(f"{k}={v}" for k, v in ranked[:6])
            lines.append(f"    {name}: {rendered}")
    return "\n".join(lines)


def cmd_advice(args: argparse.Namespace) -> int:
    """Turn the ledger into decisions about where to keep using Jev.

    `stats` reports what happened. This says what to do about it — which patterns
    pay for themselves, which should be escalated, and which are not worth the
    round trip at all. It is the reading layer over the measurements the rest of
    the tool already collects.
    """
    if args.ledger:
        paths = [Path(args.ledger)]
    elif args.global_only:
        from .stats import global_ledger_path

        paths = [global_ledger_path()]
    else:
        paths = [ledger_path()]
    records = load_records(paths, include_global=bool(args.include_global))
    report = advise(records)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_advice(report, limit_unproven=args.limit_unproven))
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    """Pair a decision with what actually happened — this is what makes accuracy real."""
    record_outcome(args.decision_id, args.result, args.detail or "", root=args.ledger_root)
    payload = {
        "ok": True,
        "decision_id": args.decision_id,
        "outcome": args.result,
        "ledger": str(ledger_path(args.ledger_root)),
    }
    _emit(payload, args.json, f"recorded outcome '{args.result}' for {args.decision_id}")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jevskill",
        description=(
            "Use the Jev decision model (typesafe/jev-1.13 via OpenRouter) for bounded "
            "decisions inside a coding workflow: routing, triage, gating, grading, "
            "ranking, reducing large data. Emits no text — only typed decisions."
        ),
        epilog="Start with 'jevskill patterns' then 'jevskill plan \"<your problem>\"'.",
    )
    parser.add_argument("--version", action="version", version=f"jevskill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_state_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--state", help="state inline (text; JSON strings are fine)")
        p.add_argument("--state-file", help="path to the state (.json is parsed as JSON)")
        p.add_argument("--max-state-tokens", type=int, default=8000,
                       help="refuse (with advice) if the state exceeds this (default 8000)")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    def add_provider_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--provider", choices=sorted(PROVIDERS), default=None,
            help=(
                "which endpoint serves the model: 'openrouter' "
                "(POST /api/alpha/decisions, model typesafe/jev-1.13, reports cost) or "
                "'typesafe' (the vendor's POST /v1/systemone, model jev-latest, 64k "
                "context, cost computed from the $0.042/Mtok rate). Default: detected "
                "from the key shape, else openrouter."
            ),
        )

    # doctor
    p = sub.add_parser("doctor", help="check key, connectivity, latency and cost")
    add_provider_flag(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    # ask
    p = sub.add_parser("ask", help="ask one decision (state + questions)")
    add_state_flags(p)
    add_provider_flag(p)
    p.add_argument("--questions", help="full questions JSON: {name: {type, instructions, criteria}}")
    p.add_argument("--question-type", choices=["noul", "choice", "score"],
                   help="build a single question from flags instead of --questions")
    p.add_argument("--name", help="question name for single-question mode")
    p.add_argument("--instructions", help="question instructions")
    p.add_argument("--true-text", help="noul: what counts as true")
    p.add_argument("--false-text", help="noul: what counts as false")
    p.add_argument("--options", nargs="+", help="choice: option keys")
    p.add_argument("--levels", nargs="+", help="score: ordered levels, lowest first")
    p.add_argument("--pattern", help="pattern label for the ledger (see 'jevskill patterns')")
    p.add_argument("--intent", help="free-text label for the ledger")
    p.add_argument("--session-id", help="group related decisions in the ledger")
    p.add_argument("--warm", action="store_true", default=True, help="pre-open the connection (default)")
    p.add_argument("--force", action="store_true", help="send even if over the state budget")
    p.add_argument("--no-ledger", action="store_true", help="do not write a ledger row")
    p.add_argument("--ledger-root", help="directory holding .jevskill/ledger.jsonl")
    p.set_defaults(func=cmd_ask)

    # batch
    p = sub.add_parser(
        "batch",
        help="apply one question set to many items (large-dataset triage)",
        description=(
            "Apply one set of questions to many items and report the measured token "
            "saving. Items come from JSONL, a JSON array, or a plain line-per-item "
            "file. By default several items share one call, which measured 2.69x "
            "fewer input tokens and 25.6x faster than one call per item, with "
            "identical labels."
        ),
        epilog=(
            "Write questions naming the item as `item`, e.g. "
            "--question-type choice --instructions 'Which team should own `item`?'. "
            "The backticked reference is rewritten per item automatically."
        ),
    )
    p.add_argument("input", help="path to JSONL / JSON array / line-per-item text")
    p.add_argument("--questions", help="full questions JSON template, naming `item`")
    p.add_argument("--question-type", choices=["noul", "choice", "score"])
    p.add_argument("--name")
    p.add_argument("--instructions")
    p.add_argument("--true-text")
    p.add_argument("--false-text")
    p.add_argument("--options", nargs="+")
    p.add_argument("--levels", nargs="+")
    p.add_argument("--text-key", help="pull this field out of each JSON object")
    p.add_argument("--strategy", choices=["windowed", "per-item"], default="windowed",
                   help="windowed (default) puts several items in one call and is far "
                        "cheaper; per-item isolates each decision at higher cost")
    p.add_argument("--window", type=int, default=8,
                   help="items per call in windowed mode (default 8)")
    p.add_argument("--concurrency", type=int, default=4,
                   help="parallel calls; only helps --strategy per-item (default 4)")
    p.add_argument("--limit", type=int, help="only process the first N items")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="read timeout per call (default 120s; batches are large)")
    p.add_argument("--out", help="write one JSON object per item to this file")
    p.add_argument("--quiet", action="store_true", help="skip the tally")
    p.add_argument("--measure-baseline", action="store_true", default=True,
                   help="measure the one-item-per-call cost with a real probe call "
                        "(default on: costs one extra call and removes any chance of "
                        "a flattering ratio)")
    p.add_argument("--no-measure-baseline", dest="measure_baseline",
                   action="store_false", help="estimate the baseline instead")
    p.add_argument("--pattern", help="pattern label for the ledger (default triage)")
    p.add_argument("--intent", help="intent label for the ledger (default batch)")
    p.add_argument("--session-id")
    p.add_argument("--max-state-tokens", type=int, default=8000)
    p.add_argument("--warm", action="store_true", default=True)
    p.add_argument("--no-ledger", action="store_true")
    p.add_argument("--ledger-root")
    add_provider_flag(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_batch)

    # plan
    p = sub.add_parser("plan", help="free go/no-go check: should Jev be used, which pattern")
    p.add_argument("problem", help="plain-language description of the task")
    p.add_argument("--state", help="optional data to size")
    p.add_argument("--state-file", help="optional data file to size")
    p.add_argument("--questions", type=int, default=3, help="assumed question count for the estimate")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    # patterns
    p = sub.add_parser("patterns", help="show the usage palette for coding workflows")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_patterns)

    # stats
    p = sub.add_parser("stats", help="report measured effectiveness from the ledger")
    p.add_argument("--ledger", help="specific ledger file")
    p.add_argument("--global-only", action="store_true", help="use ~/.jevskill/ledger.jsonl only")
    p.add_argument("--include-global", action="store_true",
                   help="also merge ~/.jevskill/ledger.jsonl (off by default: an "
                        "explicit ledger must mean exactly that ledger)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    # advice
    p = sub.add_parser(
        "advice",
        help="what to do about the ledger: which patterns pay off, which to escalate",
        description=(
            "Reads the effectiveness ledger and says where Jev is worth using, where "
            "it saves too little to bother, and where it is cheap but too often wrong. "
            "Pair decisions with `jevskill outcome` so it can measure accuracy, not "
            "just cost."
        ),
    )
    p.add_argument("--ledger", help="specific ledger file")
    p.add_argument("--global-only", action="store_true", help="use ~/.jevskill/ledger.jsonl only")
    p.add_argument("--include-global", action="store_true",
                   help="also merge ~/.jevskill/ledger.jsonl")
    p.add_argument("--limit-unproven", type=int, default=5,
                   help="how many 'unproven' groups to print (default 5; use --json for all)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_advice)

    # outcome
    p = sub.add_parser("outcome", help="pair a decision with what actually happened")
    p.add_argument("decision_id")
    p.add_argument("result", choices=["correct", "incorrect", "escalated", "overridden", "no_action"])
    p.add_argument("--detail", help="free-text note")
    p.add_argument("--ledger-root")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_outcome)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (JevError, JevApiError, JevConfigError, JevQuestionError) as exc:
        message = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        if getattr(exc, "hint", None):
            message["hint"] = exc.hint
        if getattr(args, "json", False):
            print(json.dumps(message, ensure_ascii=False, indent=2), file=sys.stdout)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
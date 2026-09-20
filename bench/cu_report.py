"""Print the computer-use benchmark table from `bench/cu_runs.json` (plan item 4.7).

    python bench/cu_report.py
    python bench/cu_report.py --compare bench/cu_runs_kofanlabs.json
    python bench/cu_report.py --json            # the summarise() dict, for a diff

Reads rows written by `bench/cu_run.py` and renders the per-task table of
`bench/cu_tasks.md` plus a totals line, in Markdown. **Every number printed is
computed from the file.** There is no literal figure in this script and there must
never be one: `AGENTS.md` says a published number has to be reproducible, and a
table with one hard-coded cell is no longer a report of anything.

Two rules this file exists to enforce:

* **Success comes from `success`, which is the oracle's.** Never from
  `stop_reason`. An agent's `done` is a proposal (`act.md` §3), and a row whose
  oracle could not be evaluated prints `?` and is counted as ungraded, not as a
  pass and not as a failure.
* **A dry run is labelled in the title of every table it appears in.** The runner
  can write synthetic rows into the same file as real ones, which is what makes it
  testable on a machine where the benchmark must not run; the label is the other
  half of that bargain. Mixed files say so and give the count.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_contract():
    """Import `jevskill.cu.contract`, by path if the package init is missing.

    Same `sys.modules` caching as `bench/cu_run.py`: the report and the runner must
    end up with one contract module, not two copies whose dataclasses are
    different types.
    """
    try:
        return importlib.import_module("jevskill.cu.contract")
    except ImportError:
        cached = sys.modules.get("jevskill_cu_contract")
        if cached is not None:
            return cached
        path = ROOT / "jevskill" / "cu" / "contract.py"
        spec = importlib.util.spec_from_file_location("jevskill_cu_contract", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["jevskill_cu_contract"] = module
        spec.loader.exec_module(module)
        return module


contract = _load_contract()
TaskRun = contract.TaskRun
summarise = contract.summarise

DEFAULT_IN = ROOT / "bench" / "cu_runs.json"

#: The per-task columns, as `bench/cu_tasks.md` asks for them. `calls` is the
#: doc's `jev_calls`, named generically because the comparison agent calls a
#: different model; `steps` is beside it because a macro hit (plan item 4.6)
#: spends no call and still costs a step.
COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("task", "<"), ("success", ">"), ("steps", ">"), ("step p50 ms", ">"),
    ("decide p50 ms", ">"), ("calls", ">"), ("Jev share", ">"),
    ("tokens in", ">"), ("cost $", ">"), ("esc", ">"),
)


def load_rows(path: Path) -> Tuple[List[Any], dict]:
    """Rows as `TaskRun` objects, plus the file's own metadata block."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = [TaskRun.from_dict(row) for row in data.get("runs", [])]
    meta = {k: v for k, v in data.items() if k != "runs"}
    return rows, meta


def mode_label(rows: Sequence[Any]) -> str:
    """The loudest honest description of what these rows are.

    Returned for the *table title*, not a footnote: someone who screenshots one
    table must not be able to lose the fact that it is synthetic.
    """
    modes = sorted({row.mode for row in rows})
    if not modes:
        return "NO ROWS"
    if modes == ["dry-run"]:
        return "DRY RUN - synthetic, not a measurement"
    if "dry-run" in modes:
        dry = sum(1 for row in rows if row.mode == "dry-run")
        return f"MIXED - {dry} of {len(rows)} rows are DRY RUN (synthetic)"
    return "live"


def _agent_label(rows: Sequence[Any], fallback: str) -> str:
    names = sorted({row.agent for row in rows if row.agent})
    if len(names) == 1:
        return names[0]
    if names:
        return f"{len(names)} agents: " + ", ".join(names)
    return fallback


def _success_cell(entry: dict) -> str:
    success = entry["success"]
    if not entry["runs"]:
        return "-"
    cell = f"{success['passes']}/{success['graded']}" if success["graded"] else "0/0"
    if success["unknown"]:
        cell += f" (+{success['unknown']}?)"
    return cell


def _render_table(headers: Sequence[str], aligns: Sequence[str],
                  rows: Sequence[Sequence[str]]) -> List[str]:
    """A Markdown table, padded so the raw text is readable too.

    The file is quoted into `bench/cu_tasks.md` and read in a terminal at least as
    often as it is rendered, so the columns line up in both.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"]
    out.append("|" + "|".join(
        ("-" * (widths[i] + 1) + ":") if aligns[i] == ">" else ("-" * (widths[i] + 2))
        for i in range(len(headers))) + "|")
    for row in rows:
        out.append("| " + " | ".join(
            cell.rjust(widths[i]) if aligns[i] == ">" else cell.ljust(widths[i])
            for i, cell in enumerate(row)) + " |")
    return out


def _task_cells(entry: dict, *, label: Optional[str] = None) -> List[str]:
    return [
        label or entry["task_id"],
        _success_cell(entry),
        str(entry["steps"]["median"]),
        f"{entry['step_ms_p50']['median']:.1f}",
        f"{entry['decide_ms_p50']['median']:.1f}",
        str(entry["decisions"]["median"]),
        f"{entry['jev_share']['median']:.2f}",
        str(entry["tokens_in"]["median"]),
        f"{entry['cost_usd']['median']:.6f}",
        str(entry["escalations"]["sum"]),
    ]


def render(rows: Sequence[Any], *, source: Path, meta: dict,
           label: Optional[str] = None) -> str:
    """One agent's table: per task, medians over that task's runs, then totals."""
    if not rows:
        return f"No runs in {source}.\n"
    report = summarise(rows)
    totals = report["totals"]
    agent = label or _agent_label(rows, source.stem)
    lines = [f"## {agent} ({mode_label(rows)})", ""]
    lines.append(
        f"Source: `{source.as_posix()}` - {totals['runs']} runs over "
        f"{totals['tasks']} tasks, {totals['steps_total']} steps"
        + (f", generated {meta['generated']}" if meta.get("generated") else "")
        + (f", provider {sorted({r.provider or 'default' for r in rows})}"
           if {r.provider for r in rows} != {None} else ""))
    lines.append("")
    lines.append("Success is the task oracle's verdict only; medians are over that "
                 "task's runs, `esc` is the sum of escalated steps.")
    lines.append("")

    headers = [c[0] for c in COLUMNS]
    aligns = [c[1] for c in COLUMNS]
    body = [_task_cells(report["tasks"][task_id]) for task_id in report["task_order"]]

    rate = totals["success_rate"]
    body.append([
        "**totals**",
        (f"{totals['passes']}/{totals['graded']}"
         + (f" = {rate:.0%}" if rate is not None else "")
         + (f" (+{totals['unknown']}?)" if totals["unknown"] else "")),
        f"{totals['steps_mean']:.1f} mean",
        "",
        "",
        f"{totals['decisions_mean']:.1f} mean",
        "",
        str(totals["tokens_in_sum_all_runs"]),
        f"{totals['cost_usd_sum_all_runs']:.6f}",
        str(totals["escalated_steps"]),
    ])
    lines.extend(_render_table(headers, aligns, body))
    lines.append("")
    lines.extend(_escalation_lines(report))
    lines.extend(_caveat_lines(report, rows, meta))
    return "\n".join(lines) + "\n"


def _escalation_lines(report: dict) -> List[str]:
    """Plan item 4.8's own output: the rate, and who contributed to it.

    Printed unconditionally, including when it is zero. An escalation rate that
    only appears when it is non-zero is a rate nobody checks.
    """
    totals = report["totals"]
    per_task = {t: n for t, n in report["escalations_per_task"].items() if n}
    lines = [
        f"- **Escalations:** {totals['escalated_steps']} of {totals['steps_total']} "
        f"steps = {totals['escalation_rate']:.1%}; "
        f"{totals['escalated_runs']} of {totals['runs']} runs ended escalated "
        f"({totals['escalated_run_rate']:.1%}).",
    ]
    lines.append("  - per task: " + (", ".join(f"{t} {n}" for t, n in sorted(per_task.items()))
                                     if per_task else "none"))
    if totals["destructive_gates"]:
        lines.append(f"- **Destructive gate fired:** {totals['destructive_gates']} times "
                     "(the deterministic name list, not the `is_destructive` Noul).")
    return lines


def _caveat_lines(report: dict, rows: Sequence[Any], meta: dict) -> List[str]:
    totals = report["totals"]
    lines: List[str] = []
    if totals["failed_all_runs"]:
        lines.append("- **Failed on every run:** " + ", ".join(totals["failed_all_runs"])
                     + " - a 0/n task is a finding, not noise (`cu_tasks.md`).")
    if totals["unknown"]:
        lines.append(f"- **Ungraded:** {totals['unknown']} run(s) had no oracle verdict; "
                     "they count as neither pass nor fail.")
    stops = {k: v for k, v in totals["stop_reasons"].items() if k != "done"}
    if stops:
        lines.append("- **Stop reasons other than `done`:** "
                     + ", ".join(f"{k} {v}" for k, v in sorted(stops.items())))
    bad_teardown = [f"{r.task_id}/{r.run}" for r in rows if not r.teardown_ok]
    if bad_teardown:
        lines.append("- **Teardown failed:** " + ", ".join(bad_teardown)
                     + " - the next run did not start from a clean state.")
    if report["problems"]:
        lines.append(f"- **Accounting problems ({len(report['problems'])}):** "
                     + "; ".join(report["problems"][:5])
                     + (" ..." if len(report["problems"]) > 5 else ""))
    syntax = meta.get("powershell_syntax") or {}
    if syntax and not syntax.get("available"):
        lines.append(f"- **PowerShell snippets unchecked:** {syntax.get('reason', '')}")
    if any(r.mode == "dry-run" for r in rows):
        lines.append("- **DRY RUN rows are synthetic**: a fake agent against a fake "
                     "oracle. Nothing above is evidence about any agent.")
    return lines


def render_compare(left: Sequence[Any], right: Sequence[Any], *,
                   left_source: Path, right_source: Path,
                   left_label: Optional[str] = None,
                   right_label: Optional[str] = None) -> str:
    """Two result files side by side, one row per (task, agent).

    The shape `bench/cu_tasks.md` asks for: the same ten tasks graded by the same
    ten oracles, one block per agent, so the two columns are measuring the same
    thing. Tasks present in only one file still get a row, with `-` in the other -
    dropping them would quietly compare different task lists.
    """
    name_l = left_label or _agent_label(left, left_source.stem)
    name_r = right_label or _agent_label(right, right_source.stem)
    report_l, report_r = summarise(left), summarise(right)
    modes = f"{mode_label(left)} vs {mode_label(right)}"
    lines = [f"## {name_l} vs {name_r} ({modes})", ""]
    lines.append(f"Left: `{left_source.as_posix()}` ({report_l['totals']['runs']} runs). "
                 f"Right: `{right_source.as_posix()}` ({report_r['totals']['runs']} runs).")
    lines.append("")

    headers = ["task", "agent"] + [c[0] for c in COLUMNS][1:]
    aligns = ["<", "<"] + [c[1] for c in COLUMNS][1:]
    order = list(report_l["task_order"])
    order += [t for t in report_r["task_order"] if t not in order]

    body: List[List[str]] = []
    for task_id in order:
        for name, report in ((name_l, report_l), (name_r, report_r)):
            entry = report["tasks"].get(task_id)
            if entry is None:
                body.append([task_id, name] + ["-"] * (len(headers) - 2))
            else:
                body.append([task_id, name] + _task_cells(entry)[1:])
    for name, report in ((name_l, report_l), (name_r, report_r)):
        totals = report["totals"]
        rate = totals["success_rate"]
        body.append([
            "**totals**", name,
            (f"{totals['passes']}/{totals['graded']}"
             + (f" = {rate:.0%}" if rate is not None else "")),
            f"{totals['steps_mean']:.1f} mean", "", "",
            f"{totals['decisions_mean']:.1f} mean", "",
            str(totals["tokens_in_sum_all_runs"]),
            f"{totals['cost_usd_sum_all_runs']:.6f}",
            str(totals["escalated_steps"]),
        ])
    lines.extend(_render_table(headers, aligns, body))
    lines.append("")
    for name, report in ((name_l, report_l), (name_r, report_r)):
        lines.append(f"**{name}**")
        lines.extend(_escalation_lines(report))
    lines.append("")
    lines.append("Both columns are graded with the same `bench/cu_tasks.json` oracles, "
                 "never with either tool's own reported success. If the two agents ran "
                 "against different backing models, that variable moved too - say which.")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="?", type=Path, default=DEFAULT_IN,
                        help=f"results file (default {DEFAULT_IN.name})")
    parser.add_argument("--compare", type=Path, default=None, metavar="OTHER.json",
                        help="a second results file in the same schema, side by side")
    parser.add_argument("--label", default=None, help="name for the first file's agent")
    parser.add_argument("--compare-label", default=None,
                        help="name for the second file's agent")
    parser.add_argument("--task", action="append", default=None, metavar="ID",
                        help="only this task id; repeatable")
    parser.add_argument("--mode", choices=("dry-run", "live"), default=None,
                        help="only rows from this mode")
    parser.add_argument("--json", action="store_true",
                        help="print the summarise() dict instead of the table")
    return parser


def _filter(rows: Sequence[Any], args) -> List[Any]:
    out = list(rows)
    if args.mode:
        out = [r for r in out if r.mode == args.mode]
    if args.task:
        wanted = set(args.task)
        out = [r for r in out if r.task_id in wanted]
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.results.exists():
        print(f"{args.results} does not exist - run `python bench/cu_run.py --dry-run` "
              "first.", file=sys.stderr)
        return 2
    rows, meta = load_rows(args.results)
    rows = _filter(rows, args)

    if args.json:
        payload = {"source": args.results.as_posix(), "mode": mode_label(rows),
                   **summarise(rows)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if args.compare:
        if not args.compare.exists():
            print(f"{args.compare} does not exist", file=sys.stderr)
            return 2
        other, _ = load_rows(args.compare)
        other = _filter(other, args)
        print(render_compare(rows, other, left_source=args.results,
                             right_source=args.compare,
                             left_label=args.label, right_label=args.compare_label))
        return 0

    print(render(rows, source=args.results, meta=meta, label=args.label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

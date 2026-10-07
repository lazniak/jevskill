"""Live: does the operator's memory make the second run of a command faster?

Runs one command template on THIS desktop twice — or more — with different
values each time, through the same :class:`jevskill.cu.runner.Operator` the
console drives. The first run plans with the model and is performed by Jev; it
teaches the plan, the recipes and the lessons. Every later run should come out
of memory: no planning call, goals replayed, the end checked in code. The script
writes the runs' status payloads and the derived numbers to
``bench/cu_learn_live.json`` so every figure quoted from it can be regenerated.

It moves the mouse, types and saves files on the desktop it runs on. It refuses
to start without ``--live``; nobody should run it by accident, and the first
run of each release is watched by a person with a hand near Ctrl+Alt+Esc.

Memory lives in a fresh directory for the bench (``--work-dir``, a temporary one
by default), so the user's own ``cu_experience.json`` is neither read nor
written. Files the runs save stay on the Desktop — the script prints their
names and deletes nothing.

    python bench/cu_learn_live.py --live --model anthropic/claude-sonnet-5
    python bench/cu_learn_live.py --live --model anthropic/claude-sonnet-5 --mission
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jevskill.cu.runner import Operator  # noqa: E402

OUT = ROOT / "bench" / "cu_learn_live.json"

#: The command the 0.14.0 panel saved once in 60.1 s (bench/cu_live_save_run.json),
#: with its values as slots so each run saves a new file.
SAVE = "Otwórz Notatnik, wpisz „{text}” i zapisz jako {name} na pulpicie"
#: A longer one, for the plan tree: two documents, each written and saved.
MISSION = ("Otwórz Notatnik, wpisz „{text}” i zapisz jako {name} na pulpicie, potem otwórz "
           "nowy Notatnik, wpisz „{text} — kopia” i zapisz jako kopia-{name} na pulpicie")
VALUES = [("hello world", "jev-learn-1.txt"), ("lista zakupów", "jev-learn-2.txt"),
          ("trzeci raz", "jev-learn-3.txt")]


def confirm_policy(operator: Operator, stop: threading.Event, log: list) -> None:
    """Allow a confirm that is about saving, deny anything else — the policy of
    the 0.14.0 live runs. Denials are recorded with the run."""
    while not stop.is_set():
        pending = (operator.status().get("pending_confirm") or {})
        if pending:
            text = " ".join(str(pending.get(k) or "") for k in ("prompt", "name", "op")).lower()
            allow = "save" in text or "zapisz" in text
            log.append({"t": time.time(), "prompt": pending.get("prompt"), "allowed": allow})
            try:
                operator.confirm(allow)
            except LookupError:
                pass
        time.sleep(0.2)


def summary(status: dict) -> dict:
    """What a run did, from its status payload — the page's own numbers."""
    return {
        "run_id": status.get("run_id"), "state": status.get("state"),
        "stop_reason": status.get("stop_reason"), "error": status.get("error"),
        "elapsed_s": status.get("elapsed_s"), "spend": status.get("spend"),
        "memory": status.get("memory"), "progress": status.get("progress"),
        "goals": [[s.get("goal"), s.get("status"), s.get("evidence"), s.get("steps")]
                  for s in status.get("plan") or []],
        "llm_purposes": [e["text"].split(" · ")[0] for e in status.get("events") or []
                         if e.get("kind") == "llm"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true",
                        help="required: this drives the real desktop")
    parser.add_argument("--model", required=True, help="planning model (OpenRouter id)")
    parser.add_argument("--runs", type=int, default=2, help="runs of the template (2-3)")
    parser.add_argument("--mission", action="store_true",
                        help="the longer two-document command (a plan tree)")
    parser.add_argument("--usd-cap", type=float, default=0.30, help="per run")
    parser.add_argument("--total-budget-s", type=float, default=300.0, help="per run")
    parser.add_argument("--work-dir", default="", help="ledger + memory for the bench "
                                                       "(default: a new temporary directory)")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    if not args.live:
        print("refusing: this moves the mouse and types on this desktop. Pass --live, and "
              "keep Ctrl+Alt+Esc (or `jevskill cu stop`) within reach.", file=sys.stderr)
        return 2
    runs = max(1, min(len(VALUES), int(args.runs)))
    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="jev-learn-"))
    template = MISSION if args.mission else SAVE
    operator = Operator(ledger_root=work)
    results, denials = [], []
    for number, (text, name) in enumerate(VALUES[:runs], start=1):
        command = template.format(text=text, name=name)
        print("run %d/%d: %s" % (number, runs, command), flush=True)
        stop = threading.Event()
        watcher = threading.Thread(target=confirm_policy, args=(operator, stop, denials),
                                   daemon=True)
        watcher.start()
        try:
            operator.start(command, args.model, usd_cap=args.usd_cap,
                           total_budget_s=args.total_budget_s)
            operator.wait()
        except KeyboardInterrupt:
            operator.stop("Ctrl+C")
            operator.wait(10)
        finally:
            stop.set()
        status = operator.status()
        results.append(dict(summary(status), command=command, values=[text, name]))
        print("  %s in %.1f s · llm %d calls · memory %s" % (
            status.get("state"), float(status.get("elapsed_s") or 0),
            int((status.get("spend") or {}).get("llm_calls") or 0),
            json.dumps(status.get("memory"), ensure_ascii=False)), flush=True)
        if status.get("state") != "done":
            break
        time.sleep(2.0)
    first = results[0] if results else {}
    later = [r for r in results[1:] if r.get("state") == "done"]
    derived = {}
    if first.get("state") == "done" and later:
        derived = {
            "first_elapsed_s": first["elapsed_s"],
            "later_elapsed_s": [r["elapsed_s"] for r in later],
            "speedup": [round(float(first["elapsed_s"]) / max(0.1, float(r["elapsed_s"])), 2)
                        for r in later],
            "first_llm_calls": first["spend"]["llm_calls"],
            "later_llm_calls": [r["spend"]["llm_calls"] for r in later],
        }
    payload = {
        "what": "Live runs of one command template with different values through the "
                "operator, memory on, a fresh memory file for the bench. The first run "
                "learns; later runs should replay. Status payloads are the page's own.",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"), "model": args.model,
        "template": template, "work_dir": str(work), "confirm_log": denials,
        "runs": results, "derived": derived,
        "files_left_on_desktop": [v[1] for v in VALUES[:len(results)]] +
                                 (["kopia-" + v[1] for v in VALUES[:len(results)]]
                                  if args.mission else []),
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print("wrote %s%s" % (args.out, (" · speed-up %s" % derived["speedup"]) if derived else ""))
    print("files left on the Desktop (delete them yourself if you like): %s"
          % ", ".join(payload["files_left_on_desktop"]))
    return 0 if all(r.get("state") == "done" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

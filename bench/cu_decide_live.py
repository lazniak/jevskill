"""Live validation of `jevskill.cu.decide` against the two real UIA fixtures.

Four calls, well under $0.01, writing ``bench/cu_decide_results.json``:

1. Notepad (real snapshot, Polish UI), goal "Open the File menu"
2. Calculator (real snapshot, Polish UI), goal "Compute 7 times 8"
3. a 60-candidate screen with ``destructive_scope="names"``  — the token baseline
4. the same screen with ``destructive_scope="commands"``      — what §2's fix costs

Calls 3 and 4 exist to answer one question with a number instead of an opinion:
act.md §4 says the per-element Noul "catches what the list misses", but §2 only
generated it for elements whose name *already* matched the list, so it could
never catch anything new. Asking it for every command control fixes that and
costs tokens; this script measures how many, on the same state, in the same
session, so the two numbers are comparable.

Nothing here touches the desktop: both fixtures are JSON snapshots recorded
earlier by ``bench/cu_observe_bench.py --save-fixtures``.

    python bench/cu_decide_live.py                # vendor or OpenRouter, per the usual resolution
    python bench/cu_decide_live.py --dry          # print the bundles, spend nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jevskill.client import JevClient  # noqa: E402
from jevskill.cu import candidates, tree_hash  # noqa: E402
from jevskill.cu.act import risky_ids  # noqa: E402
from jevskill.cu.decide import (DESTRUCTIVE_QUESTION_CAP, build_bundle,  # noqa: E402
                                build_state, from_decisions)
from jevskill.cu.types import Snapshot  # noqa: E402
from jevskill.orchestrate import count_tokens  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "cu"
OUT = Path(__file__).with_name("cu_decide_results.json")

CASES = [
    ("notepad", "Open the File menu",
     {"type": None, "target": None, "outcome": None}),
    ("calculator", "Compute 7 times 8",
     {"type": None, "target": None, "outcome": None}),
]


def load(name: str) -> Snapshot:
    data = json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))
    return Snapshot.from_dict(data["snapshot"])


def summarise(name: str, goal: str, cands, decision, extra=None) -> dict:
    row = {
        "case": name,
        "goal": goal,
        "candidates": len(cands),
        "tree_hash": tree_hash(cands),
        "target": decision.target,
        "target_name": next((el.name for el in cands if el.id == decision.target), None),
        "target_confidence": round(decision.target_confidence, 4),
        "margin": round(decision.margin, 4),
        "op": decision.op,
        "op_confidence": round(decision.op_confidence, 4),
        "top3_target": [[k, round(v, 4)] for k, v in
                        sorted(decision.probs.items(), key=lambda kv: -kv[1])[:3]],
        "goal_reached": round(decision.goal_reached, 4),
        "needs_text": round(decision.needs_text, 4),
        "is_destructive": round(decision.is_destructive, 4),
        "per_element_destructive": {k: round(v, 4) for k, v in
                                    sorted(decision.per_element_destructive.items())},
        "questions": decision.questions,
        "tokens_in": decision.tokens_in,
        "state_tokens_estimated": decision.state_tokens,
        "cost_usd": round(decision.cost_usd, 8),
        "http_ms": round(float(decision.timing.get("http_ms", 0.0)), 1),
    }
    row.update(extra or {})
    return row


def report(row: dict) -> None:
    print("\n===== %s: %r =====" % (row["case"], row["goal"]))
    print("  candidates=%d questions=%d tokens_in=%d cost=$%.6f http=%.0f ms"
          % (row["candidates"], row["questions"], row["tokens_in"],
             row["cost_usd"], row["http_ms"]))
    print("  target=%s (%r) conf=%.3f margin=%.3f | op=%s conf=%.3f"
          % (row["target"], row["target_name"], row["target_confidence"],
             row["margin"], row["op"], row["op_confidence"]))
    print("  top3=%s" % row["top3_target"])
    print("  goal_reached=%.2f needs_text=%.2f is_destructive=%.2f"
          % (row["goal_reached"], row["needs_text"], row["is_destructive"]))
    if row["per_element_destructive"]:
        print("  destructive_*: %s" % row["per_element_destructive"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true",
                        help="build the bundles and print sizes; make no calls")
    args = parser.parse_args()

    rows = []
    scope_rows = {}
    big = candidates(load("synthetic_500").elements)

    if args.dry:
        for name, goal, last in CASES:
            snap = load(name)
            cands = candidates(snap.elements)
            bundle = build_bundle(cands, risky_ids=risky_ids(cands))
            state = build_state(goal, snap, last, elements=cands)
            print("%-12s candidates=%2d questions=%2d state~%d tok bundle~%d tok"
                  % (name, len(cands), len(bundle), count_tokens(state),
                     count_tokens(bundle)))
        for scope in ("names", "commands"):
            bundle = build_bundle(big, risky_ids=risky_ids(big),
                                  destructive_scope=scope)
            print("scope=%-9s candidates=%d questions=%d bundle~%d tok"
                  % (scope, len(big), len(bundle), count_tokens(bundle)))
        return 0

    total = 0.0
    with JevClient(hot=True) as jev:
        jev.warm()
        for name, goal, last in CASES:
            snap = load(name)
            cands = candidates(snap.elements)
            risky = risky_ids(cands)
            bundle = build_bundle(cands, risky_ids=risky)
            state = build_state(goal, snap, last, elements=cands)
            decision = from_decisions(jev.decide(state, bundle))
            decision.questions = len(bundle)
            decision.state_tokens = count_tokens(state)
            row = summarise(name, goal, cands, decision,
                            {"window_title": snap.window_title, "app": snap.app,
                             "risky_ids": risky, "destructive_scope": "commands",
                             "destructive_cap": DESTRUCTIVE_QUESTION_CAP})
            rows.append(row)
            report(row)
            total += decision.cost_usd

        # The §2-vs-§4 measurement: same state, same session, two scopes.
        state = build_state("Save the current document and close the dialog",
                            load("synthetic_500"),
                            {"type": "key", "target": None, "outcome": "new_window"},
                            elements=big)
        for scope in ("names", "commands"):
            bundle = build_bundle(big, risky_ids=risky_ids(big),
                                  destructive_scope=scope)
            decision = from_decisions(jev.decide(state, bundle))
            decision.questions = len(bundle)
            decision.state_tokens = count_tokens(state)
            row = summarise("synthetic_500:%s" % scope,
                            "Save the current document and close the dialog",
                            big, decision,
                            {"destructive_scope": scope,
                             "destructive_cap": DESTRUCTIVE_QUESTION_CAP,
                             "risky_ids": risky_ids(big)})
            rows.append(row)
            scope_rows[scope] = row
            report(row)
            total += decision.cost_usd

    delta = None
    if len(scope_rows) == 2:
        base, wide = scope_rows["names"], scope_rows["commands"]
        delta = {
            "tokens_names": base["tokens_in"],
            "tokens_commands": wide["tokens_in"],
            "extra_tokens": wide["tokens_in"] - base["tokens_in"],
            "extra_pct": round(100.0 * (wide["tokens_in"] - base["tokens_in"])
                               / max(1, base["tokens_in"]), 2),
            "extra_cost_usd": round(wide["cost_usd"] - base["cost_usd"], 8),
            "questions_names": base["questions"],
            "questions_commands": wide["questions"],
        }
        print("\n===== destructive scope: names vs commands =====")
        print("  %d -> %d tokens (+%d, +%.1f%%), +$%.6f per step, %d -> %d questions"
              % (delta["tokens_names"], delta["tokens_commands"],
                 delta["extra_tokens"], delta["extra_pct"],
                 delta["extra_cost_usd"], delta["questions_names"],
                 delta["questions_commands"]))

    payload = {
        "note": "Live validation of jevskill/cu/decide.py against real UIA "
                "fixtures. Regenerate with `python bench/cu_decide_live.py`.",
        "source": "bench/cu_decide_live.py",
        "cases": rows,
        "destructive_scope_delta": delta,
        "total_cost_usd": round(total, 8),
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print("\nTOTAL COST: $%.6f  (%d calls)  -> %s" % (total, len(rows), OUT.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Retrieve what the REDUCE pattern threw away.

Reduction is only safe if it is **reversible**. This script is the other half of
``jev_query.py --reduce``: every rejected item is stored locally on disk, and this
reads it back. Standard library only.

Why this matters: an agent that keeps only 1% of a log has made an irreversible
bet that the 1% was the right 1%. Measured on 900 synthetic log lines with evenly
scattered signal, the gate found 8 of 14 genuinely salient lines — good, not
perfect. A miss is only survivable if the agent can go back and look.

    # what was dropped, and how much
    python scripts/jev_recovery.py rc_1a2b3c4d5e6f --summary

    # search the rejected items
    python scripts/jev_recovery.py rc_1a2b3c4d5e6f --grep "payment"

    # get specific rejected items back
    python scripts/jev_recovery.py rc_1a2b3c4d5e6f --index 42 43

    # everything, to a file
    python scripts/jev_recovery.py rc_1a2b3c4d5e6f --all --out rejected.txt

    # every handle created on this machine, newest first
    python scripts/jev_recovery.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def recovery_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    if os.environ.get("JEVSKILL_LEDGER_DIR"):
        return Path(os.environ["JEVSKILL_LEDGER_DIR"]) / "recovery"
    return Path.cwd() / ".jevskill" / "recovery"


def load(handle: str, override: str | None) -> dict:
    path = recovery_dir(override) / f"{handle}.json"
    if not path.is_file():
        # A handle created in another directory is common; say where we looked.
        print(f"error: no recovery record {handle!r} at {path}\n"
              f"       run with --list to see handles, or --recovery-dir to point "
              f"at the right store.", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_list(override: str | None) -> int:
    store = recovery_dir(override)
    if not store.is_dir():
        print(f"no recovery store at {store}")
        return 0
    records = sorted(store.glob("rc_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not records:
        print(f"no recovery records in {store}")
        return 0
    print(f"recovery store: {store}")
    for path in records:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        print(f"  {data.get('handle'):<18} {data.get('created'):<20} "
              f"{data.get('total_items'):>6} items -> {len(data.get('kept', []))} kept, "
              f"{data.get('rejected_count')} recoverable   ({data.get('source')})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jev_recovery.py",
        description="Read back the items a REDUCE run rejected.")
    parser.add_argument("handle", nargs="?", help="recovery handle, e.g. rc_1a2b3c4d5e6f")
    parser.add_argument("--list", action="store_true", help="list every stored handle")
    parser.add_argument("--summary", action="store_true", help="counts and token math only")
    parser.add_argument("--grep", help="regex to search the rejected items")
    parser.add_argument("--index", nargs="+", type=int, help="original indices to return")
    parser.add_argument("--all", action="store_true", help="return every rejected item")
    parser.add_argument("--limit", type=int, default=50, help="max items to print (default 50)")
    parser.add_argument("--out", help="write the selection to a file instead of stdout")
    parser.add_argument("--recovery-dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.list or not args.handle:
        if not args.list and not args.handle:
            parser.error("give a handle, or --list")
        return cmd_list(args.recovery_dir)

    record = load(args.handle, args.recovery_dir)
    rejected = record.get("rejected", [])
    kept = record.get("kept", [])

    if args.summary and not (args.grep or args.index or args.all):
        summary = {
            "handle": record.get("handle"),
            "source": record.get("source"),
            "created": record.get("created"),
            "total_items": record.get("total_items"),
            "kept": len(kept),
            "rejected_recoverable": len(rejected),
            "raw_tokens": record.get("raw_tokens"),
            "kept_tokens": record.get("kept_tokens"),
            "calls": record.get("calls"),
            "cost_usd": record.get("cost_usd"),
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            reduction = (1 - summary["kept_tokens"] / summary["raw_tokens"]) * 100 \
                if summary["raw_tokens"] else 0
            print(f"handle    {summary['handle']}  ({summary['created']}, from {summary['source']})")
            print(f"items     {summary['total_items']} total -> {summary['kept']} kept, "
                  f"{summary['rejected_recoverable']} recoverable")
            print(f"tokens    {summary['raw_tokens']} -> {summary['kept_tokens']} "
                  f"({reduction:.1f}% reduction)")
            print(f"cost      ${summary['cost_usd']:.8f} over {summary['calls']} calls")
            print("\nThe rejected items are on disk, not gone. That is the point:")
            print("a reduction you cannot reverse is a bet, not an optimisation.")
        return 0

    if args.index:
        selected = [row for row in rejected if row["index"] in set(args.index)]
    elif args.grep:
        try:
            pattern = re.compile(args.grep, re.IGNORECASE)
        except re.error as exc:
            print(f"error: bad --grep regex: {exc}", file=sys.stderr)
            return 1
        selected = [row for row in rejected if pattern.search(row["text"])]
    elif args.all:
        selected = rejected
    else:
        # No selector: show the rejected items so the caller can see what exists.
        selected = rejected

    if args.json:
        print(json.dumps(selected[:args.limit], ensure_ascii=False, indent=2))
        return 0

    lines = [row["text"] for row in selected[:args.limit]]
    if args.out:
        Path(args.out).write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {len(lines)} item(s) to {args.out}")
    else:
        for row in selected[:args.limit]:
            print(f"[{row['index']:>6}] {row['text']}")
        if len(selected) > args.limit:
            print(f"\n... {len(selected) - args.limit} more (raise --limit)")
    if not selected:
        print(f"no rejected items matched in {record.get('handle')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

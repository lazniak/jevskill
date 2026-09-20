#!/usr/bin/env python3
"""Bundled zero-dependency Jev caller — the skill works with nothing installed.

Standard library only. No ``pip install``, no ``httpx``, no ``orjson``. This is
what makes the skill usable straight out of an installed skill folder, which is
the Agent Skills expectation: a skill should carry the scripts it needs.

    # a gate
    python scripts/jev_query.py --state-file diff.txt \
      --question-type noul --name breaks_api \
      --instructions "Does the diff change a public API signature?" \
      --true-text "A public name or signature changes." \
      --false-text "Only internals, comments or formatting change."

    # several questions at once (cheaper and far faster than one per call)
    python scripts/jev_query.py --state-file log.txt --questions '{
      "owner": {"type":"choice","instructions":"Which module owns this?",
                "criteria":{"api":"HTTP layer.","db":"Schema.","unclear":"Not enough info."}},
      "risk":  {"type":"score","instructions":"How risky is this?",
                "criteria":["Trivial","Low","Moderate","High","Critical"]}}'

    # reduce a big payload, and keep the rejected part retrievable
    python scripts/jev_query.py --state-file build.log --reduce \
      --instructions "Does the log line at `L{i}` report a problem worth investigating?"

For the ledger, per-stage statistics and `stats`/`outcome`, install the package
(``pip install -e .``) and use the full CLI. This script exists so the skill is
useful without that install.

Exit codes: 0 success, 1 error, 3 refused (state over budget).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DECISIONS_PATH = "/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_BASE = "https://openrouter.ai"
KEY_ENV_VARS = (
    "JEVSKILL_API_KEY",
    "OPENROUTER_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "JEVUSE_API_KEY",
)
RETRYABLE = {0, 429, 500, 502, 503, 504, 520, 522, 524, 529}

# Calibrated against the live API: a prose rule of thumb (3.6 chars/token)
# under-counted log lines and code by 2.15x. See jevskill/config.py.
CHARS_PER_TOKEN = 1.68


def _registry_env(name: str) -> str:
    """Read ``HKCU\\Environment`` — `setx` values are not inherited by running shells."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip() if value else ""
    except Exception:
        return ""


def find_api_key() -> str:
    for name in KEY_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for name in KEY_ENV_VARS:
        value = _registry_env(name)
        if value:
            return value
    try:
        data = json.loads((Path.home() / ".jevskill" / "config.json").read_text("utf-8"))
        return str(data.get("api_key", "")).strip()
    except Exception:
        return ""


def count_tokens(text) -> int:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def build_questions(args) -> dict:
    if args.questions:
        questions = json.loads(args.questions)
    elif args.question_type:
        name = args.name or "q0"
        instructions = args.instructions or "Answer the question."
        if args.question_type == "noul":
            question = {"type": "noul", "instructions": instructions}
            if args.true_text or args.false_text:
                criteria = {}
                if args.true_text:
                    criteria["true"] = args.true_text
                if args.false_text:
                    criteria["false"] = args.false_text
                question["criteria"] = criteria
        elif args.question_type == "choice":
            options = [o for o in (args.options or []) if o]
            if len(options) < 2:
                raise SystemExit("error: choice needs at least 2 --options")
            question = {"type": "choice", "instructions": instructions,
                        "criteria": {o: o for o in options}}
        elif args.question_type == "score":
            levels = [l for l in (args.levels or []) if l]
            if len(levels) < 2:
                raise SystemExit("error: score needs at least 2 --levels")
            question = {"type": "score", "instructions": instructions, "criteria": levels}
        else:
            raise SystemExit(f"error: unknown question type {args.question_type!r}")
        questions = {name: question}
    else:
        raise SystemExit("error: pass --questions '<json>' or --question-type {noul,choice,score}")

    for qname, question in questions.items():
        kind = question.get("type")
        if kind not in ("noul", "choice", "score"):
            raise SystemExit(f"error: question {qname!r} has type {kind!r}; "
                             "must be 'noul', 'choice' or 'score'")
        if not question.get("instructions"):
            raise SystemExit(f"error: question {qname!r} is missing 'instructions'")
    return questions


def post(body: bytes, key: str, base: str, retries: int, timeout: float):
    """One POST with bounded retries on the transient status family."""
    url = base.rstrip("/") + DECISIONS_PATH
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://github.com/lazniak/jevskill",
               "X-Title": "jevskill"}
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            last = f"HTTP {status}: {payload[:300].decode('utf-8', 'replace')}"
            if status not in RETRYABLE:
                raise SystemExit(f"error: {last}")
        except Exception as exc:  # transport
            last = f"transport: {exc}"
    raise SystemExit(f"error: failed after {retries + 1} attempts — {last}")


def render(result: dict) -> list[str]:
    lines = [f"JEV decided ({result.get('model')}) in {result['_ms']:.0f} ms"]
    for name, answer in result.get("answers", {}).items():
        kind = answer.get("type")
        if kind == "noul":
            lines.append(f"  {name}: P(true)={answer.get('noul')}")
        elif kind == "choice":
            probs = answer.get("probabilities") or {}
            top = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:3]
            detail = "  ".join(f"{k}={v:.2f}" for k, v in top)
            lines.append(f"  {name}: {answer.get('choice')!r} "
                         f"(conf {answer.get('confidence')})  [{detail}]")
        elif kind == "score":
            lines.append(f"  {name}: {answer.get('score')} (conf {answer.get('confidence')})")
        else:
            lines.append(f"  {name}: {json.dumps(answer)[:120]}")
    usage = result.get("usage") or {}
    lines.append("")
    lines.append(f"  tokens {usage.get('input_tokens')}  cost ${float(usage.get('cost', 0)):.8f}")
    return lines


def reduce_state(data, args, key: str, base: str) -> dict:
    """REDUCE: gate each item, keep the hits, store the rest for retrieval."""
    if isinstance(data, dict):
        items = list(data.values())[0] if len(data) == 1 else None
    else:
        items = data
    if not isinstance(items, list):
        raise SystemExit("error: --reduce needs a JSON array (or a single-key object "
                         "whose value is an array)")

    window = args.window
    instructions = args.instructions or "Does the item at `L{i}` matter?"
    kept, calls, cost = [], 0, 0.0
    started = time.perf_counter()
    for start in range(0, len(items), window):
        chunk = [str(x) for x in items[start:start + window]]
        questions = {
            f"keep_L{i}": {
                "type": "noul",
                # Naming the value with a backticked path is load-bearing: without
                # it the model returns a flat, meaningless answer for every line.
                "instructions": instructions.replace("{i}", str(i)),
                "criteria": {
                    "true": f"`L{i}` matches. Other items are irrelevant.",
                    "false": f"`L{i}` does not match. Other items are irrelevant.",
                },
            }
            for i in range(len(chunk))
        }
        body = json.dumps({"model": args.model, "state": {f"L{i}": t for i, t in enumerate(chunk)},
                           "questions": questions}).encode()
        result = post(body, key, base, args.retries, args.timeout)
        calls += 1
        cost += float((result.get("usage") or {}).get("cost", 0) or 0)
        for i, text in enumerate(chunk):
            probability = ((result.get("answers") or {}).get(f"keep_L{i}") or {}).get("noul") or 0.0
            if probability >= 0.5:
                kept.append({"index": start + i, "p": probability, "text": text})

    kept.sort(key=lambda row: row["p"], reverse=True)
    selected = kept[:args.keep]
    kept_indexes = {row["index"] for row in selected}
    rejected = [{"index": i, "text": str(t)} for i, t in enumerate(items) if i not in kept_indexes]

    elapsed = (time.perf_counter() - started) * 1000
    raw_tokens = count_tokens(items)
    kept_tokens = count_tokens([row["text"] for row in selected])

    handle = None
    if not args.no_recovery:
        handle = f"rc_{uuid.uuid4().hex[:12]}"
        store = recovery_dir(args.recovery_dir)
        store.mkdir(parents=True, exist_ok=True)
        record = {
            "handle": handle,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": args.state_file or "stdin",
            "total_items": len(items),
            "kept": selected,
            "rejected_count": len(rejected),
            "rejected": rejected,
            "raw_tokens": raw_tokens,
            "kept_tokens": kept_tokens,
            "calls": calls,
            "cost_usd": cost,
        }
        (store / f"{handle}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "mode": "reduce",
        "handle": handle,
        "calls": calls,
        "items": len(items),
        "kept": len(selected),
        "rejected": len(rejected),
        "raw_tokens": raw_tokens,
        "kept_tokens": kept_tokens,
        "reduction_pct": round((1 - kept_tokens / raw_tokens) * 100, 1),
        "ms": round(elapsed, 1),
        "cost_usd": round(cost, 8),
        "lines": [row["text"] for row in selected],
        "recovery_hint": (
            f"rejected {len(rejected)} items stored; retrieve with: "
            f"python scripts/jev_recovery.py {handle} --grep <pattern>"
        ) if handle else None,
    }


def recovery_dir(override: str | None) -> Path:
    if override:
        return Path(override)
    if os.environ.get("JEVSKILL_LEDGER_DIR"):
        return Path(os.environ["JEVSKILL_LEDGER_DIR"]) / "recovery"
    return Path.cwd() / ".jevskill" / "recovery"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="jev_query.py",
        description="Zero-dependency Jev caller bundled with the skill. "
                    "Decisions, not text: noul (yes/no), choice (one of N), "
                    "score (rubric grade).")
    parser.add_argument("--state")
    parser.add_argument("--state-file")
    parser.add_argument("--questions", help="full questions JSON")
    parser.add_argument("--question-type", choices=["noul", "choice", "score"])
    parser.add_argument("--name")
    parser.add_argument("--instructions")
    parser.add_argument("--true-text")
    parser.add_argument("--false-text")
    parser.add_argument("--options", nargs="+")
    parser.add_argument("--levels", nargs="+")
    parser.add_argument("--reduce", action="store_true",
                        help="REDUCE pattern: gate every item, keep the hits, store the rest")
    parser.add_argument("--keep", type=int, default=8)
    parser.add_argument("--window", type=int, default=20)
    parser.add_argument("--no-recovery", action="store_true")
    parser.add_argument("--recovery-dir")
    parser.add_argument("--session-id")
    parser.add_argument("--model", default=os.environ.get("JEVSKILL_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.environ.get("JEVSKILL_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-state-tokens", type=int, default=8000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    key = find_api_key()
    if not key:
        print("error: no OpenRouter API key. Set OPENROUTER_API_KEY, or write "
              "{'api_key': '...'} to ~/.jevskill/config.json", file=sys.stderr)
        return 2

    if args.state_file:
        text = Path(args.state_file).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
    elif args.state is not None:
        data = args.state
    else:
        data = sys.stdin.read()

    if args.reduce:
        out = reduce_state(data, args, key, args.base_url)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"REDUCE: {out['items']} items -> {out['kept']} kept "
                  f"({out['reduction_pct']}% fewer tokens: "
                  f"{out['raw_tokens']} -> {out['kept_tokens']})")
            for line in out["lines"]:
                print(f"  {line[:150]}")
            print(f"\n  {out['calls']} calls, {out['ms']:.0f} ms, ${out['cost_usd']:.8f}")
            if out["recovery_hint"]:
                print(f"  {out['recovery_hint']}")
        return 0

    try:
        questions = build_questions(args)
    except json.JSONDecodeError as exc:
        print(f"error: --questions is not valid JSON: {exc}", file=sys.stderr)
        return 1

    size = count_tokens(data)
    if size > args.max_state_tokens:
        print(f"error: state is ~{size} tokens, over the {args.max_state_tokens} budget.\n"
              "Do not just raise it — accuracy degrades with irrelevant state. "
              "Use --reduce, or cut the data with code first (grep for the region "
              "that matters).", file=sys.stderr)
        return 3

    body = json.dumps({"model": args.model, "state": data, "questions": questions}).encode()
    started = time.perf_counter()
    result = post(body, key, args.base_url, args.retries, args.timeout)
    result["_ms"] = (time.perf_counter() - started) * 1000

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "_ms"},
                         ensure_ascii=False, indent=2))
    else:
        print("\n".join(render(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

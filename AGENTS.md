# Working in this repository

Guidance for an AI agent (or a person) changing `jevskill`. Read this before
editing; the conventions here are load-bearing, not decoration.

## What this project is

A portable **Agent Skill** that lets a coding harness use the Jev decision model
(TypeSafe System One, via OpenRouter) for bounded decisions — routing, triage,
gating, grading, ranking, reducing large data — plus an effectiveness ledger so
the skill's own value is measured rather than asserted.

Two halves, deliberately separate:

| Path | What it is |
|---|---|
| `skills/jev/` | the Skill: `SKILL.md`, `references/`, and stdlib-only `scripts/` |
| `jevskill/` | the Python package: client, primitives, orchestration, ledger, CLI |

The Skill must work with **no install** (stdlib only). The package adds the
measurement half (ledger, `stats`, `outcome`, `plan`). Keep that boundary: if you
add a feature, decide which half it belongs to and do not make the Skill depend on
the package.

## Running things

```bash
python -m pytest -q                       # 211 tests, offline, must stay green
python bench/run.py --legacy-reduce       # live API: E1-E7, writes bench/results.json
python bench/ab.py --runs 3               # live API: the A/B evaluation, writes bench/ab_results.json

python skills/jev/scripts/jev_query.py --help       # the bundled, zero-install caller
python skills/jev/scripts/jev_recovery.py --list    # read back what REDUCE rejected
python -m jevskill patterns                         # the usage palette
```

The benchmark scripts spend real money (a few cents) and need
`OPENROUTER_API_KEY`. Tests never touch the network.

## Conventions that matter

**Every published number must be reproducible.** If a figure appears in `README.md`
or `skills/jev/references/`, some script in this repo must produce it. Never round
a number up, never quote a figure you cannot regenerate, and never replace a
measured value with an estimate.

**Negative results stay published.** `bench/run.py --legacy-reduce` deliberately
reproduces a design that failed (99% context reduction while keeping 1 of 8 wanted
lines). The `shortlist` narrowing path did not trigger in testing and the README
says so. If you find a case where Jev loses, add it rather than removing the test.

**Name the target value in every question.** The single most expensive mistake in
this codebase's history. A question saying "does this line report an error?" while
20 lines sit in one state returns a flat, meaningless ~0.74 for every line. Point
at the value with a backticked path (`` `L7` ``) in *both* instructions and
criteria. This is measured and documented in `references/prompting.md` §1 — do not
"simplify" it away.

**Fan out, do not loop.** Questions sharing one state run in parallel: 8 in one
call was 12.4× faster and used 4.03× fewer tokens than 8 sequential calls. Any
loop that calls `decide()` once per question is a bug.

**One chars-per-token constant.** `CHARS_PER_TOKEN` in `jevskill/config.py` is
calibrated against the live API (a prose rule of thumb under-counted logs by
2.15×). Route all sizing through `count_tokens()`; do not open-code a division.

**Ledger writes are append-only.** Outcomes are stored as separate patch records
keyed by `decision_id`, never by rewriting a decision line.

**Explicit paths mean exactly those paths.** `load_records([p])` must not merge
the global ledger; that silently contaminated a per-project report once.

## Style

- Python 3.9+, standard library by default. `httpx`/`orjson` are optional speedups.
- Docstrings explain **why**, including the mistake that motivated the code. This
  codebase's comments are its memory; a comment restating the code is noise.
- Tests derive from constants rather than hard-coding them, so re-tuning a
  constant does not require editing assertions.
- `SKILL.md` is a decision guide for an agent, not documentation for a human. Keep
  it imperative and under 500 lines (it is ~300).
- Commit messages: conventional commits, and explain the reasoning and the
  measurement, not just the change.

## Before you push

1. `python -m pytest -q` is green.
2. If you changed a measured behaviour, re-run the relevant benchmark and update
   `README.md`, `CHANGELOG.md` and the reference doc together.
3. If you changed a published number, say so explicitly in the CHANGELOG and name
   the superseded figure.
4. Bump the version in `pyproject.toml` and `jevskill/__init__.py` together.

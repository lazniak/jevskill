# Design

Why `jevskill` is built the way it is, including the mistakes that shaped it.

## The problem

A coding harness makes hundreds of small judgements per session: which module
owns a failure, whether a diff is safe, which log lines matter, whether a test
covers the bug, what a stack trace says. Doing these with a chat model means
paying frontier prices, waiting seconds, and parsing JSON that is occasionally
malformed. Doing them by hand costs human attention.

Jev is built exactly for this: a *System One* model that returns typed decisions
with calibrated probabilities, no text, no reasoning, output free.

So the interesting question is not "is Jev fast" — it is **"when is it worth
reaching for, and how do you know it worked?"** This repository is an answer to
that second question, and the answer is mostly measurement infrastructure.

## Layer map

```
skill/SKILL.md          the decision guide a harness actually reads
        │
        ├── references/         api · patterns · prompting · benchmarks
        │
jevskill/cli.py         the command surface (doctor · plan · patterns ·
        │               ask · outcome · stats)
        ├── orchestrate.py      planning: pattern, size, iteration, savings
        ├── primitives.py       noul / choice / score + fail-fast validation
        ├── client.py           Decisions API: warm connection, retries, timings
        ├── stages.py           staged timing
        └── stats.py            the effectiveness ledger
```

The split matters: `client.py` knows the API, `orchestrate.py` knows the
*strategy*, and neither knows about the other. Swapping the provider would touch
one file.

## Design decisions worth defending

### 1. Publishing the measurement, not just the tool

A skill that helps you use a model is only as good as the reader's belief that it
works. So the ledger, the benchmark suite and the negative results ship with the
code:

* `bench/results.json` is generated, not written by hand.
* `bench/run.py --legacy-reduce` reproduces a design that **failed**, so the
  negative result is checkable.
* The README reports that a small chat model was faster and cheaper on a trivial
  call, because that is what the measurement said.

### 2. Absolute timestamps, derived deltas

The first version of `stages.py` recorded each stage as "elapsed minus the sum of
previous stages", and separately absorbed the client's own
`serialize`/`http`/`parse` timings. That double-counted: the doctor command once
reported `act: -612 ms` and a 1047 ms total for a 45 ms script.

The fix is structural. `Stages.mark()` stores the **absolute** elapsed time and
`ordered()` derives durations from consecutive marks, so the parts sum to the real
wall clock. Client-side timings are attached as an **overlay** in parentheses —
visible, but never summed, because they sit *inside* an already-measured region.

### 3. Validation before the network

The documented 400 causes are almost always a `choice` with one option or an
empty `criteria`. `primitives.py` refuses to build those, and
`validate_questions()` re-checks bundles that arrived as JSON, so a malformed
question costs zero round trips and zero cents.

### 4. A schema-stable ledger

Every row carries the same keys because `Record` defines them, including
`outcome: ""` for decisions not yet paired. A reader can therefore consume any row
without probing which fields exist. Outcomes are appended as separate patch
records rather than rewriting the decision line, which keeps the ledger
append-only and safe for concurrent writers.

### 5. Errors that tell you what to do

`JevApiError` maps each documented status to the action a harness should take —
413 means "chunk it", 402 means "fall back to the LLM path", 404 means "you used
the chat endpoint". A bare status code makes a model guess.

## The bug that shaped the prompting rules

The REDUCE pattern was implemented as: score each 60-line chunk, keep the three
hottest, gate the lines inside. It reported a **99.1% context reduction while
keeping 1 of 8** wanted lines. Adding a line-level second stage made it *worse* —
0 of 8 — and the reason turned out to be the most valuable finding in the
project.

Measured on one 20-line window containing a single real `ERROR`:

| Variant | Real `ERROR` | Noise line | Useful |
|---|---|---|---|
| A: `{"lines": {L0…L19}}`, question says "this single log line" | 0.72 | 0.75 | ❌ |
| B: one line per state, one call per line | 0.98 | 0.02 | ✅ but 20 calls |
| C: per-line keys, question points at `` `L0` `` | **0.96** | **0.03** | ✅ batched |

Variant A fails **silently**: the numbers look like answers. Naming the target
with a backticked path — a documented TypeSafe convention this project had read
and not applied — restores discrimination at no cost.

Two consequences, both now enforced:

* `references/prompting.md` opens with this rule and the table above.
* `bench/run.py` keeps the failing single-stage design as
  `--legacy-reduce`, so the lesson is reproducible rather than folklore.

## Windows notes

A harness runs in whichever shell a human opened. `setx` changes are **not**
inherited by processes already running, which is the most common reason an API key
"disappears". `config.py` therefore reads `HKCU\Environment` directly with
`winreg` after the environment variables come up empty, so a key set yesterday
works in a terminal opened before it.

The registry is only consulted on `os.name == "nt"` and every failure is
swallowed into an empty string, so the fallback can never break a working setup.

## Why stdlib-only

`httpx[http2]` and `orjson` are meaningfully faster, and the client uses them when
they are importable. But a harness shelling out to a tool should not fail because
a virtualenv lacks a dependency, so `urllib` and `json` are the fallback and
`pip install -e .` has no requirements at all. The measured penalty is inside the
noise, because `http` is 92–96% of wall clock regardless.

## What is deliberately not here

* **No MCP server.** A CLI keeps the integration surface small and lets any
  harness — Claude Code, DSH, a shell script — use it identically.
* **No caching.** Jev is deterministic per state, but caching decisions would make
  the ledger lie about what was actually measured.
* **No attempt to outperform an LLM.** The README says so explicitly. Jev wins on
  structure, latency and fan-out; on raw accuracy against a frontier model it does
  not, and pretending otherwise would discredit the parts that are true.

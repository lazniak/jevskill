# jevskill

**Stop paying a frontier model to make a decision.**

`jevskill` is a portable Skill that lets a coding harness use
[Jev](https://openrouter.ai/typesafe/jev-1.13) — TypeSafe's *System One* decision
model — for the decisions a coding session is full of: which module owns this
failure, which 8 of 900 log lines matter, is this diff safe to apply, does this
test actually cover the bug.

Jev returns **typed decisions with real probability distributions**, not text. It
answers in ~325 ms for about **$0.000013**. So the question is never "can we
afford a decision" — it is "which decisions should we stop making by hand, or by
paying a chat model to emit fragile JSON?"

```
Without jevskill   →  paste 16 289 tokens of logs into the model, hope it finds the 8 that matter
With jevskill      →  Jev reads the logs for $0.006, model sees 201 tokens
```

**It also measures itself.** Every decision is written to a local ledger with its
own stage timings, token cost, confidence, and — once you pair it with what
actually happened — whether it was *right*. The statistics below are that ledger,
not a pitch.

---

## The measured record

Live API, 148 decisions, residential connection in Poland, 2026-09-20.
Reproduce it yourself: `python bench/run.py`.

### Latency — p50 **325 ms**, and it barely cares how big your data is

| Experiment | Result |
|---|---|
| Cold first request | 485 ms |
| Warm p50 / p95 | **325 ms / 427 ms** |
| State grown 324 → 7 020 tokens (**22×**) | p50 353 → 468 ms |
| **8 questions, one call** | **288 ms** |
| 8 questions, 8 sequential calls | 3 564 ms |

That last row is the whole argument for batching: **12.4× faster**, because
questions sharing one state are evaluated in parallel.

### Cost — **$0.000013** per decision

| | Input tokens | Cost |
|---|---|---|
| 1 gate, small state | 317 | $0.0000133 |
| 1 gate, 7K-token state | 7 020 | $0.0002948 |
| 8 questions, one call | 663 | $0.0000279 |
| 8 questions, 8 calls | 2 672 | $0.0001122 |

Output is free — Jev generates none. Batching is a **token** optimisation too:
sequential calls re-send the same state, measured at **4.03× amplification**.

### Where the real savings are — context you never ship

900 log lines (**16 289 tokens**) reduced to the **8 that matter (201 tokens)**:

| | Reduction | Recall | Cost |
|---|---|---|---|
| Gate every line (no misses) | **98.8%** | 8/14 found | $0.0061 |
| Chunk-prefilter first (cheaper) | **99.6%** | 3/14 found | $0.0029 |

99% of the corpus never reaches your expensive model. Read the recall column
before you choose a configuration — the cheap one finds less than half of what
the thorough one does.

### Accuracy — and where Jev is *not* better

| | Result |
|---|---|
| Guard: is this log line a problem to investigate? (80 labelled lines) | **100%** (49 judged) |
| Calibration: answers at P≈0.03 | actual rate **0%** |
| Calibration: answers at P≈0.98 | actual rate **100%** |
| Same question through `gemini-2.5-flash-lite` | 429 ms, $0.0000057 |

**Read that last row honestly.** A small chat model was *faster and cheaper* than
Jev on a trivial one-question call. Jev's advantage is not raw speed or price
against a flash-lite model — it is that you get a **probability distribution and
a confidence value with no parsing, no retries and no malformed JSON**, and that
fan-out makes large context reductions possible at all.

We publish this because a repository that only reports wins is not a measurement.

---

## What it actually looks like

```bash
# Should I even use Jev here?  (free — no API call)
$ jevskill plan "900 build log lines, keep the 8 that matter" --state-file build.log
USE JEV — pattern 'reduce'
  why    : Cut what the expensive model has to read.
  calls  : 11
  data   : 16289 tok — 3 chunk call(s) + 1 final call over the shortlist

# One decision
$ jevskill ask --state-file defect.json --questions '{
    "owner": {"type":"choice","instructions":"Which subsystem owns the fix?",
              "criteria":{"billing":"…","api":"…","db":"…","unclear":"…"}},
    "risk":  {"type":"score","instructions":"How risky is deploying unreviewed?",
              "criteria":["Trivial","Low","Moderate","High","Critical"]},
    "needs_test":{"type":"noul","instructions":"Does this need a new test?"}
  }'
JEV decided (typesafe/jev-1.13-20260917) in 311 ms
  owner: 'billing' (conf 0.88)  [billing=0.88  api=0.12]
  risk: 1.4 (conf 0.7)
  needs_test: P(true)=0.91
  tokens 500  cost $0.00002100  (LLM baseline ~520 tok)
  stages: t_decision 12.4 ms · build 8.9 ms · http 371.0 ms · act 7.2 ms

# Did it turn out right?
$ jevskill outcome d_1a2b3c4d5e6f correct --detail "reviewer agreed"
$ jevskill stats
```

## The nine patterns

`jevskill patterns` prints the palette; `skill/references/patterns.md` has the
full treatment.

| Pattern | Coding example |
|---|---|
| **gate** | Does this diff break a public API? |
| **triage** | Which module owns this failing test? |
| **reduce** | Which 8 of these 900 log lines matter? |
| **rank** | Rank these 12 lint findings by user impact |
| **route** | One-line fix, or architectural change? |
| **verify** | Does this test actually exercise the bug? |
| **guard** | Does this command touch anything outside the repo? |
| **shortlist** | Top two at 0.51/0.44 → re-ask over just those two |
| **extract** | Stack trace → language, exception type, failing frame |

## Four rules that decide whether this works

**1. Fan out.** One call, many questions — 12.4× faster than looping, and 4× fewer
tokens. If you are looping one question per call, you are paying for the same
state over and over.

**2. Decompose, then combine in code.** One broad "analyse this" question
underperforms several atomic gates weighted in plain Python. Independent evidence:
a single Jev verdict lost to a small chat model, while five cheap signals combined
in a logistic regression reached **95.1%**.

**3. Budget the state, and reduce before you paste.** Latency is nearly flat with
size, so the constraint is *accuracy and cost*. Irrelevant context is a
distractor that you also pay for. Default budget 8 000 tokens; `jevskill ask`
refuses bigger states and tells you what to do instead.

**4. Route on uncertainty.** A `choice` is a distribution, not a label. When the
top two are close, narrow and re-ask with the context that discriminates them —
or escalate. Never accept a 0.51 winner because it happened to come first.

## The single most expensive mistake

Asking a question that does not **name the value it is about**.

Measured on one window of 20 log lines containing one genuine `ERROR`:

| How the question was asked | The real ERROR | Routine noise |
|---|---|---|
| "Does this single log line report a problem?" (20 lines in one state) | 0.72 | 0.75 |
| Same, but pointing at `` `L0` `` with a backtick path | **0.96** | **0.03** |

The first version produces numbers that look like answers and discriminate
nothing. It cost this repository an entire benchmark run that reported a 99%
context reduction while keeping **zero** of the eight lines it was looking for.
Backticked paths are a documented convention and they are worth 23 points of
probability mass. Details: `skill/references/prompting.md`.

## Install

```bash
git clone https://github.com/lazniak/jevskill && cd jevskill
python -m pip install -e .            # stdlib only
python -m pip install -e ".[fast]"    # + httpx[http2] and orjson

export OPENROUTER_API_KEY=sk-or-v1-...   # Windows: setx OPENROUTER_API_KEY "..."

jevskill doctor      # verifies key, endpoint, latency and live cost
```

The key is read from `JEVSKILL_API_KEY`, `OPENROUTER_API_KEY`,
`OPEN_ROUTER_API_KEY`, `JEVUSE_API_KEY`, the Windows user registry, or
`~/.jevskill/config.json`. See the Windows reverse-engineering note in
`docs/DESIGN.md` for why the registry is consulted.

### Install it as a Skill for your harness

```bash
pwsh -File install.ps1          # Claude Code, DSH and generic skills dirs
```

This copies `skill/` into the harness skill directories and leaves the CLI on
your `PATH` via the `jevskill` console script.

## When **not** to use this

Jev emits no text. It cannot write a summary, generate code, explain its
reasoning, or choose from options you could not enumerate up front. It has a 32K
context, text-only input, and no self-hosting. It is **not more accurate than a
frontier model** on published evidence.

`jevskill plan "<task>"` says no for free, and says why:

```bash
$ jevskill plan "write documentation for this module"
DO NOT USE JEV (prose)
  The answer is text, code, or an unbounded set. Jev emits no text and can only
  choose from options you enumerate up front — use the LLM for this, and consider
  a Jev gate in front of it if the call is expensive.
```

## How the measurement works

Every `ask` records a ledger row — `docs/DESIGN.md` explains the design.

**Stage timing** is split so the claim is auditable, from the moment the harness
decides to use the skill to the moment output is consumed:

```
t_decision   12.4 ms    3.1%   ← deciding to use the skill
profile       0.3 ms    0.1%
build         8.9 ms    2.2%   ← building state and questions
http        371.0 ms   92.4%   ← network + inference
act           7.2 ms    1.8%
report        1.4 ms    0.3%
```

`http` is 92–96% of wall clock. The skill's own overhead is ~4%, so there is no
client-side optimisation left worth chasing — the leverage is in *better
questions* and *less state*.

**Accuracy** comes from pairing decisions with reality, which is why
`jevskill outcome` exists. Without it a ledger can tell you Jev was fast; only a
paired one tells you it was **right**, and therefore whether your confidence
threshold is correct. You can see the effect in the table above: every confidence
below 0.2 really was wrong, every one above 0.9 really was right.

**Every published figure is reproducible.** `bench/run.py` calls the live API and
writes `bench/results.json`; `python bench/run.py --legacy-reduce` reproduces the
REDUCE design that failed, so the negative result can be checked rather than
taken on trust.

## Repository layout

```
jevskill/          the CLI and library (stdlib only)
  client.py        Decisions API client — retries, warm connection, stage timings
  primitives.py    noul / choice / score, with validation that prevents 400s
  orchestrate.py   pattern selection, profiling, chunking, iteration rules
  stats.py         the effectiveness ledger
  stages.py        staged timing
  cli.py           doctor · plan · patterns · ask · outcome · stats
skill/
  SKILL.md         the Skill a harness loads
  references/      api · patterns · prompting · benchmarks
bench/run.py       the benchmark suite that produced every number above
tests/             211 tests, offline
docs/DESIGN.md     architecture and the Windows notes
CHANGELOG.md       versioned history
```

## Status

**v0.1.0** — the CLI, the ledger, the Skill and the reference docs are complete
and tested (211 offline tests, green). The benchmark suite runs end to end against
the live API.

Known limits, stated plainly:

* Latency is dominated by provider inference and network distance from Poland
  (~325 ms here; TypeSafe quotes 70–500 ms depending on where you are).
* The `shortlist` narrowing loop has not yet fired on a real case: both test
  cases resolved at ≥0.92 confidence in round one. The logic is tested, but the
  *empirical* value of narrowing is unproven here.
* E4's recall figures depend on how the signal is distributed through the corpus;
  900 synthetic log lines are not a production log.
* Cost comparison against a chat model favours the chat model on trivial calls.
  Jev wins on structure and on not shipping context — not on price per token.

## Licence

MIT — see `LICENSE`.

Jev and TypeSafe are products of TypeSafe AI. This project is an independent
client and is not affiliated with them.

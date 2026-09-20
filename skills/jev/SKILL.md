---
name: jev
description: >-
  Use the Jev decision model (TypeSafe System One, via OpenRouter) for bounded
  decisions inside coding workflows — routing, triage, classification, gating,
  rubric grading, ranking, and reducing large data before it reaches the main
  model. Use when a decision has a fixed set of possible answers, when data is
  too large or too noisy to put in context, when the same judgement must be made
  many times, or when an irreversible action needs a cheap safety check. Jev
  emits no text: never use it for prose, code generation, summaries or reasoning.
  Triggers: "classify", "categorize", "which of these", "route", "triage",
  "gate", "should we", "rank", "prioritize", "grade", "too many logs",
  "reduce the data", "save tokens", "batch decisions", "is it safe to".
  Advisory, never an authorization boundary: Jev can be wrong, manipulated or
  overconfident, so do not map a returned label straight to an irreversible or
  destructive action without your own deterministic check.
license: MIT
allowed-tools: Bash(python:*)
metadata:
  version: "0.8.0"
  requirements: >-
    Python 3.9+ (standard library only — no install, no dependencies) and network
    access to OpenRouter or api.typesafe.ai. Needs OPENROUTER_API_KEY or
    TYPESAFE_API_KEY; calls are billed (about $0.000013 per decision, output
    free). Secret-shaped strings in the state are redacted before sending by
    default. Without a key, ask the user before judging in Jev's place — never
    simulate silently.
---

# Jev: decisions, not text

Jev is a **System One** model. You give it one *state* (the data) and any number
of typed *questions*, and it returns typed decisions with real probability
distributions — in ~325 ms (p50), for about **$0.000013**.

It cannot write. Not a summary, not a line of code, not an explanation. What it
does instead is answer **"which of these N things is it?"**, **"is this true?"**,
and **"how much, on this scale?"** — with a calibrated confidence you can branch
on, and with a **full probability distribution** no chat model hands you.

It is cheap because output is free and questions to one state run in parallel —
eight questions in one call cost $0.0000279, where asking them separately cost 4×
that. It is **not** universally cheaper than a small chat model on a single trivial
question; it wins on *structure*, on *fan-out*, and where the alternative is
pasting a large corpus into a chat context. That is the whole trade: use it where
the answer is a decision, the LLM where the answer is text.

> Measured, not estimated. Every number here comes from `bench/run.py` against the
> live API and is recorded in the ledger. Run `jevskill stats` for this machine's
> own record.

---

## 0. Run this first

**No install needed.** This skill ships scripts that use only the Python standard
library, so they run straight out of the skill folder:

```bash
python scripts/jev_query.py --state-file diff.txt --question-type noul --name breaks_api \
  --instructions "Does the diff change a public API signature?" \
  --true-text "A public name or signature changes." \
  --false-text "Only internals, comments or formatting change."
```

That is the whole dependency: Python 3.9+, a key, network access. For reduced data
use `scripts/jev_recovery.py` (see §3).

**Install the package for the measurement half** — the ledger, stage timings,
`plan`, `patterns`, `stats` and `outcome`:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...   # Windows: setx OPENROUTER_API_KEY "..."
python -m pip install -e .               # or: pip install -e ".[fast]"

jevskill doctor                              # key, endpoint, warm latency, live cost
jevskill plan "<what you are about to do>"   # free: is Jev even right here?
```

### Two endpoints serve this model — pick one

The same model is served through OpenRouter and through TypeSafe's own API. Both
work identically from this skill; the provider is chosen for you, or explicitly:

```bash
jevskill doctor                        # auto-detect from the key shape
jevskill doctor --provider typesafe    # the vendor's endpoint
jevskill doctor --provider openrouter  # the aggregator
```

| | OpenRouter | TypeSafe (vendor) |
|---|---|---|
| Model field | `typesafe/jev-1.13` | `jev-latest` |
| Key | `OPENROUTER_API_KEY` | `TYPESAFE_API_KEY` |
| Context | 32,000 tokens | **64,000** (32,000 for state + longest question) |
| Price | $0.042/Mtok | **identical** |
| Response `cost` | ✅ reported | ❌ absent — computed from the rate |

Prefer **OpenRouter** if you already have a key — it also reports the billed cost,
so the ledger needs no arithmetic. Prefer **TypeSafe** for double the context: the
price is identical, so going direct is a dependency question, not a cost one.
Model names are translated automatically; the wrong one is a 404 or a 422.

`setx` does not affect shells that are already open. On Windows the client also
reads the user registry, so a key set yesterday works in a terminal opened before
it. Without the install, `scripts/jev.py` delegates to the package if it can find
it, so either path works.

Do **not** reach for Jev before reading §2: the most common waste is using it on a
task whose answer is text.

### No key? Ask — never simulate silently

Check only the **presence** of a key (`jevskill doctor` reports `key_found`); never
print a key's value. If there is none, warn the user and ask in their language:

> No Jev key was found, so I cannot call the model. Which do you prefer?
> **A — get a key:** create one at <https://openrouter.ai/settings/keys> (or
> <https://console.typesafe.ai/keys>) and configure it locally; I will use real Jev.
> **B — I judge it myself:** I classify using the same state, options and criteria,
> without calling Jev.

**Wait for an explicit A or B.** The rules that keep this honest:

- Consent covers **the current task**, not a permanent default. Do not re-ask for
  every record in that task; do not silently carry it to the next one.
- A key appearing **later** does not authorize switching an approved B task to A.
- **API errors are not consent to simulate** — report them instead.
- In B, label every result `mode: agent_simulation`, `jev_called: false`, set
  `probability`/`confidence` to `null`, and use `needs_review: true` rather than
  inventing values or distributions. Never apply confidence thresholds to
  simulated judgements, or mix them into Jev's measured numbers.
- B promises nothing about cost, locality or speed: your own model's terms apply.
  Help with key setup without ever collecting the secret in chat.

### Exit codes are a contract

| Code | Meaning | What to do |
|---|---|---|
| `0` | Decided / scored | act on it |
| `2` | **At least one answer needs review** | treat as "not confident enough to automate": escalate, widen the state, or re-ask |
| `3` | State over the token budget | nothing was sent — cut the data first (§4 Rule 3) |
| `1` | Input, key, API or protocol error | fix the call |

`2` is deliberately not an error: a harness must tell *"the model hesitated"* apart
from *"the call failed"* without parsing output. The thresholds behind it
(`--review-below 0.75`, `--review-margin 0.10`) are **illustrative heuristics, not
calibrated guarantees** — tune them on held-out data via `jevskill outcome`.

---

## 1. The three primitives

Everything Jev does is built from three question types. Pick by the *shape* of the
answer you need — never by topic.

| You need | Primitive | Returns | Reach for it when |
|---|---|---|---|
| yes / no | `noul` | `P(true)` as a float 0–1 | filtering, guardrails, verification |
| one of N things | `choice` | winner + **full distribution** + confidence | routing, triage, classification, tool pick |
| how much on a scale | `score` | weighted mean (e.g. `1.4`), legend, distribution | grading, priority, severity, quality |

```bash
# a gate
jevskill ask --state "$DIFF" --question-type noul --name breaks_api \
  --instructions "Does this diff change a public API signature?" \
  --true-text "A public name, signature or return type changes." \
  --false-text "Only internals, comments, tests or formatting change."

# a route
jevskill ask --state "$ERROR_REPORT" --question-type choice --name owner \
  --instructions "Which subsystem owns the fix?" \
  --options billing api ui db unclear

# a grade
jevskill ask --state "$PR_DESCRIPTION" --question-type score --name risk \
  --instructions "How risky is deploying this unreviewed?" \
  --levels Trivial Low Moderate High Critical
```

All three at once, in **one call**, is the normal case:

```json
{
  "owner":     {"type": "choice", "instructions": "Which subsystem owns the fix?",
                "criteria": {"billing": "Invoices, tax, payments.",
                             "api": "HTTP layer, serialization.",
                             "db": "Schema, migrations, queries.",
                             "unclear": "Not enough information to decide."}},
  "risk":      {"type": "score", "instructions": "How risky is deploying this unreviewed?",
                "criteria": ["Trivial", "Low", "Moderate", "High", "Critical"]},
  "needs_test":{"type": "noul", "instructions": "Does this change require a new automated test?"}
}
```

## 2. Before anything: is Jev the right tool?

Ask this first. It is free, instant, and it is the single most common mistake.

```bash
jevskill plan "classify 900 build log lines and keep the 8 that matter" --state-file build.log
```

**Use Jev when the answer is one of N things you can list up front.**

| Signal | Verdict |
|---|---|
| You can enumerate the possible answers | ✅ Jev |
| The same judgement repeats over many items | ✅ Jev |
| The decision gates something expensive | ✅ Jev |
| The data is large and mostly irrelevant | ✅ Jev (REDUCE) |
| The answer is a sentence, summary or explanation | ❌ LLM |
| The answer is code | ❌ LLM |
| The answer set is open-ended ("find all possible…") | ❌ LLM |
| You need step-by-step reasoning to justify the answer | ❌ LLM |

If Jev is wrong for the task, say so and use the LLM. Reaching for Jev out of
novelty costs a round trip and produces nothing usable.

## 3. The usage palette

Nine patterns cover essentially every legitimate use in a coding workflow. Full
detail, worked questions and pitfalls: `references/patterns.md`.

| Pattern | Shape | Coding example | Layers |
|---|---|---|---|
| **gate** | one boolean about one text | "Does this diff break a public API?" | two-stage: cheap signals → code-combined policy |
| **triage** | many items → one bounded category each | "Which module owns this failing test?" | two-stage: coarse bucket → fine bucket |
| **reduce** | large sequence → small shortlist | "Which 8 of these 900 log lines matter?" | two-stage: chunk-score → final question on survivors |
| **rank** | several items → an order | "Rank these 12 lint findings by user impact" | two-stage: score-all → re-score top-k with more context |
| **route** | one task → one tier of effort | "One-line fix or architectural change?" | two-stage: route → verify the route |
| **verify** | one artifact → a rubric score | "Does this test actually exercise the bug?" | two-stage: atomic Nouls → combine in code |
| **guard** | one proposed action → safe / unsafe | "Does this command delete data outside the repo?" | two-stage: guard → require human confirmation |
| **shortlist** | a close call → ask again, narrower | top two at 0.51/0.44 → re-ask over those two | **iterative** |
| **extract** | unstructured text → fixed fields | stack trace → language, exception, frame | two-stage: presence gate per field → Choice over candidates |

## 4. The four rules that decide whether this works

### Rule 1 — Fan out. One call, many questions.

Questions share one state and are evaluated **in parallel**. Adding a question
costs a few input tokens and almost no latency.

Measured: **8 questions in one call = 1 call's worth of latency.** Eight
sequential calls cost **9.4×** the time and **~1.9×** the tokens, because you pay
for the same state eight times.

```
GOOD:  1 call  {q1, q2, q3, q4, q5, q6}
BAD:   6 calls, each with one question and the same state
```

If you catch yourself writing a loop that calls Jev once per question, stop —
restructure into a dict of questions and make one call.

### Rule 2 — Decompose into atomic signals, combine in code.

This is the highest-leverage habit, and it is the one with independent evidence
behind it. One broad question hides several judgements and gets *worse* as it
gets broader. Several narrow gates, weighted in plain code, beat it.

```python
signals = jev.decide(state, {
    "claims_tests_pass":  noul("Does the summary claim the tests pass?"),
    "cite_diff_line":     noul("Does it reference a specific changed line?"),
    "contradicts_diff":   noul("Does the claim contradict the diff?"),
})
risk = (0.45 * signals.noul("claims_tests_pass")
        + 0.30 * (1 - signals.noul("cite_diff_line"))
        + 0.25 * signals.noul("contradicts_diff"))
```

An independent 2,000-email study found Jev's *single* verdict statistically worse
than a small chat model — and the same study found that **five cheap signals
combined in a logistic regression reached 95.1%**. Treat Jev as a signal
generator you query broadly, not an oracle you query once.

### Rule 3 — Budget the state, and cut it with code first.

Latency is essentially **flat** with respect to state size — measured, growing the
state from ~330 to ~5,000 tokens moved p50 by *less than 10 ms*. Cost is not:
it scales linearly with input tokens.

So the constraint is **accuracy and cost, not speed**. Irrelevant state is a
distractor that degrades the decision, and it is billed. Default budget: 8,000
tokens per state (the hard ceiling is 32K on OpenRouter, and accuracy falls off
before the limit).

If your data does not fit, do **not** just raise the budget. Use REDUCE:

```bash
# profile it first
jevskill plan "keep the salient lines" --state-file build.log
#   -> 18000 tokens exceeds the 8000 budget: split into ~3 chunks of 6000 tokens
#      and use the REDUCE pattern — score or filter each chunk, keep the
#      shortlist, then ask a final question over the survivors.
```

### Rule 4 — Route on uncertainty. Iterate instead of accepting.

A `choice` answer is not a label, it is a **distribution**. When the top two
options are close, the honest move is to narrow and ask again — not to accept the
winner because it happened to come first.

```bash
# round 1: four candidates
#   net/client.py 0.51   net/retry.py 0.44   net/timeout.py 0.05   net/pool.py 0.00
#   -> gap 0.07, too close: NARROW, do not accept
# round 2: only the two leaders, plus the context that discriminates them
#   net/retry.py 0.91   net/client.py 0.09
#   -> gap 0.82: ACCEPT net/retry.py
```

```python
round_one = jev.decide(state, {"owner": choice(..., all_four)})
verdict = next_round(round_one.probs("owner"), confidence=round_one.confidence("owner"))
if verdict.action == "narrow":
    round_two = jev.decide(
        {**state, "discriminator": extract_only_the_difference()},
        {"owner": choice("...", verdict.next_options)},
    )
elif verdict.action == "escalate":
    hand_to_llm_or_human()   # never guess
```

Two rounds is the useful budget. Beyond that you are paying for a coin flip.

## 5. Iterative design: how to plan multi-call work

Design the *sequence* before the first call. Three reusable shapes:

**Cascade — coarse, then fine.** Cheap broad buckets first, then a second call
over only the winner's children. Turns 40 options into 5+5 and keeps both calls
accurate.

**Reduce — score everything, keep the survivors.** Chunk the data, score each
chunk for relevance, keep the top-k, then ask the real question over just those.
This is where the token savings live: 900 log lines (~15k tokens) collapse to a
shortlist of ~8 lines (~90 tokens) — **99% of the data never reaches the LLM.**

**Fan-out then combine — many gates, one decision in code.** Ask every independent
signal about the same state in one call, then weight them yourself. Use this when
no single question captures the judgement.

**Batch — the same questions over many items.** When you have N things to decide
about rather than one thing to decide repeatedly, put several items in one state
with one question per item. This is the large-dataset path, and it is the cheapest
shape of all.

```bash
jevskill batch build.log --text-key line \
  --question-type choice --name owner \
  --instructions 'Which team should own `item`?' \
  --options backend frontend infra unclear \
  --intent ci-triage --out triaged.jsonl
# Jev batch [windowed] — 60 items in 8 call(s)
#   reading  10,674 tokens batched vs 23,288 one-per-call  (54% fewer)
#   cost     $0.00044831 batched vs $0.00097810 one-per-call   (8 call(s) vs 60)
```

Measured on 60 log lines, half of them genuinely salient: **2.18× fewer input
tokens and 12× faster than one call per item, with no accuracy cost** — windowed
found 30/30 salient lines while one per-item call returned no answer at all.
Reproduce with `python bench/batch_bench.py`.

**Name the item as `` `item` `` and the tool rewrites it per item.** This is not
cosmetic: batching puts many items in one state, which makes the "question does not
name its value" failure *more* likely, and that failure is silent. The rewrite is
mechanical so it cannot be forgotten.

**`--strategy per-item`** trades cost for isolation — one item per call, so a long
or ambiguous item cannot influence a neighbour. Budget the calls before starting:
`jevskill plan "<problem>"` returns the pattern, layer strategy and expected call
count for free.

## 6. Measure it — the skill's own statistics

Every `ask` writes a ledger row: pattern, stages, tokens, cost, confidence, and
context kept out of the LLM. Pair a decision with reality and accuracy becomes
real, not asserted.

```bash
jevskill ask --state-file diff.txt --questions @"q.json" --intent "pre-commit API gate"
#   ... decision_id d_1a2b3c4d5e6f
jevskill outcome d_1a2b3c4d5e6f correct --detail "reviewer agreed, API break confirmed"

jevskill stats
#   JEV effectiveness ledger — 128 decisions
#     latency  : p50 311 ms   p95 402 ms   jev cost : $0.004210
#     vs LLM   : saved 94.3%, 412k tokens kept out of LLM context
#     accuracy : 94.1% over 118 judged decisions
```

An unpaired ledger can tell you Jev was fast; only a paired one tells you it was
**right**, and therefore whether your threshold is the right one.

### Ask the ledger what to do next

`stats` reports what happened. `advice` says what to do about it — and this is the
command to run before deciding whether to keep using Jev for something:

```bash
jevskill advice
# JEV advice — 1311 decisions, 49 judged
#   [KEEP       ] pattern:gate  (n=73, saved 99%  acc 100%/49)
#   [STOP       ] intent:tiny-diff  (n=40, saved 3%)
#   [ESCALATE   ] intent:log-triage  (n=200, saved 95%  acc 71%/60)
#   [UNPROVEN   ] pattern:reduce  (n=1234, saved 95%)
```

Six verdicts, each with the numbers that produced it:

| Verdict | Means |
|---|---|
| `KEEP` | saves tokens at measured accuracy — keep doing this |
| `STOP` | the state is too small; the round trip costs more than it saves |
| `ESCALATE` | cheap but too often wrong — gate on confidence, escalate the rest |
| `UNPROVEN` | the saving is real but no outcomes are paired yet |
| `NO BASELINE` | nothing to compare against — pass a `--state` so it can size the data |
| `MARGINAL` | saves tokens; accuracy not yet established |

**The `STOP` verdict is the one to look for first.** A reduction that saves 95% of
a state that only cost $0.00006 has saved nothing and added a network round trip.
The percentages look impressive and the absolute numbers do not — which is exactly
the trap this command exists to catch. Thresholds live in `stats.ADVICE` and are
printed with every report, because they are **policy, not fact**.

Reports carry a full per-stage breakdown so the timing claim is auditable:

```
  profile         6.7 ms    0.5%         <- inspecting the data
  build           0.3 ms    0.0%         <- building state and questions
  warm           74.0 ms    5.4%         <- handshake (first call only)
  http          386.5 ms   93.6%         <- network + inference
  act             0.2 ms    0.0%         <- thresholds, ledger write
  report          1.6 ms    0.1%
  TOTAL         469.3 ms  100.0%
  (client_total) 386.5 ms  (inside a stage above)
```

`http` is normally 92–96% of the wall clock, and it matches the client's own
measured `http_ms` — that agreement is how you know the breakdown is not inflated.
The skill's own overhead is ~4%, so effort belongs in *choosing good questions*,
not in the client. `warm` appears only on the first call of a process. Full
breakdowns, including the cold-versus-warm measurement: `references/benchmarks.md`.

## 7. Choosing a confidence threshold

There is no universal number, and copying one from a blog post is the most common
way to ship a bad gate. **Measure it** against your own labelled cases:

```bash
jevskill stats --json          # confidence and accuracy per pattern and intent
jevskill advice                # which patterns are KEEP / STOP / ESCALATE
```

A guard in front of `rm -rf` deserves a different threshold than a routing hint.
Full method, including reading a distribution instead of a bare confidence:
`references/prompting.md`.

## 8. Hard constraints (do not fight these)

- **No text output.** No prose, code, summaries, explanations. Ever.
- **Options are fixed per request.** It picks from the set you send; it cannot
  propose an option you did not think of.
- **Text input only.** No images or audio.
- **Context is 32K** on OpenRouter, 64K on the vendor endpoint — and accuracy
  degrades before the limit.
- **Not OpenAI-compatible**, and hosted only: no self-hosting, no VPC, no air-gap.
  Full list: `references/api.md`.

## 9. Failure modes to avoid

The four that cost the most, in this codebase's own history:

| Anti-pattern | Do instead |
|---|---|
| Looping one question per call | one call, many questions |
| Not naming the target value in the question (`` `L7` ``, `` `state.diff` ``) | name it — the silent failure that returns a flat 0.74 for everything |
| Accepting a 0.51 winner | narrow and re-ask |
| No `unclear` / `none` option | always include an escape hatch |

The other six, with the measurement behind each: `references/patterns.md`.

## 10. Read only the slice you need

| Need | Read |
|---|---|
| Exact request/response shapes, fields, error codes | `references/api.md` |
| Every command, flag and script invocation | `references/commands.md` |
| The nine patterns with full worked questions | `references/patterns.md` |
| How to write instructions and criteria that discriminate | `references/prompting.md` |
| Every measurement, with method and honesty notes | `references/benchmarks.md` |

Do not load all five. Pick the one the task needs; the pattern work above already
shows the common shapes inline.

Official documentation: <https://docs.typesafe.ai> ·
Model card: <https://openrouter.ai/typesafe/jev-1.13>
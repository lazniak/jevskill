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
license: MIT
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
one call with eight questions cost $0.0000279, while the same eight questions
asked separately cost 4× that. It is **not** universally cheaper than a small chat
model on a single trivial question; it wins on *structure*, on *fan-out*, and on
decisions where the alternative is pasting a large corpus into a chat context.

That is the whole trade. Use it where the answer is a decision. Use the LLM where
the answer is text.

> Measured, not estimated. Every number in this skill comes from
> `bench/run.py` against the live API and is recorded in the effectiveness
> ledger. Run `jevskill stats` to see this machine's own record.

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

That is the whole dependency: Python 3.9+, a key, and network access. For the
recovery of reduced data use `scripts/jev_recovery.py` (see §3).

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

Prefer **OpenRouter** if you already have a key; it also reports the billed cost,
so the ledger needs no arithmetic. Prefer **TypeSafe** for double the context — it
is the same price, so going direct is a dependency question, not a cost one.

Model names are translated automatically: `typesafe/jev-1.13` ↔ `jev-latest`.
Passing the wrong one to the wrong endpoint is a 404 or a 422.

`setx` does not affect shells that are already open. On Windows the client also
reads the user registry, so a key set yesterday works in a terminal opened before
it.

Without the install, `scripts/jev.py` delegates to the package if it can find it,
so either path works.

Do **not** reach for Jev before reading §2. The most common way to waste a round
trip is using it on a task whose answer is text.

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
#   wall     667 ms
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
or ambiguous item cannot influence a neighbour. Use it when that matters more than
tokens.

Budget the calls before starting: `jevskill plan "<problem>"` returns the pattern,
the layer strategy and the expected call count for free.

## 6. Measure it — the skill's own statistics

Every `ask` writes a row to the effectiveness ledger with the pattern used, the
stages, the tokens, the cost, the confidence, and how much context was kept out
of the LLM. Pair a decision with reality and the accuracy becomes real rather
than asserted.

```bash
jevskill ask --state-file diff.txt --questions @"q.json" --intent "pre-commit API gate"
#   ... decision_id d_1a2b3c4d5e6f
jevskill outcome d_1a2b3c4d5e6f correct --detail "reviewer agreed, API break confirmed"

jevskill stats
#   JEV effectiveness ledger — 128 decisions
#     latency  : p50 311 ms   p95 402 ms
#     jev cost : $0.004210
#     vs LLM   : saved 94.3%, 412k tokens kept out of LLM context
#     accuracy : 94.1% over 118 judged decisions
```

Pair outcomes whenever the consequence is observable. An unpaired ledger can
tell you Jev was fast; only a paired one can tell you it was **right**, and
therefore whether the threshold you chose is the correct one.

### Ask the ledger what to do next

`stats` reports what happened. `advice` says what to do about it — and this is the
command to run before deciding whether to keep using Jev for something:

```bash
jevskill advice
# JEV advice — 1311 decisions, 49 judged
#   saved $5.3239 (95.3%), accuracy 100%
#
#   [KEEP       ] pattern:gate  (n=73, saved 99%  acc 100%/49)
#                 Saves 98.8% at 100% accuracy over 49 judged decisions.
#   [STOP       ] intent:tiny-diff  (n=40, saved 3%)
#                 Saves 3.0%. The state is too small for reduction to pay: JEV
#                 costs a round trip to save almost nothing. Answer this directly.
#   [ESCALATE   ] intent:log-triage  (n=200, saved 95%  acc 71%/60)
#                 Accuracy 71% over 60 judged. It saves tokens but is wrong too
#                 often to act on unattended — gate on confidence and escalate.
#   [UNPROVEN   ] pattern:reduce  (n=1234, saved 95%)
#                 Saves 95.1% but 0 decisions paired with an outcome.
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
the trap this command exists to catch.

Thresholds live in `stats.ADVICE` and are printed with every report, because they
are **policy, not fact**: 85% accuracy, 20% minimum saving, 5 judged decisions
before accuracy counts. Change them to match what a wrong answer costs you.

Reports carry a full per-stage breakdown so the timing claim is auditable:

```
  t_decision      0.0 ms    0.0%
  profile         6.7 ms    0.5%         <- inspecting the data
  plan            0.0 ms    0.0%         <- choosing the pattern
  build           0.3 ms    0.0%         <- building state and questions
  warm           74.0 ms    5.4%         <- connection handshake (first call only)
  http          386.5 ms   93.6%  #######################  <- network + inference
  act             0.2 ms    0.0%         <- applying thresholds, writing the ledger
  report          1.6 ms    0.1%
  TOTAL         469.3 ms  100.0%
  (serialize)     0.0 ms  (inside a stage above)
  (client_total) 386.5 ms  (inside a stage above)
```

`http` is normally 92–96% of the wall clock — the client's own measured
`http_ms` matches the `http` stage, which is how you can tell the breakdown is
not inflated. The skill's own overhead is ~4%, so there is no client-side
optimisation left worth chasing: effort belongs in *choosing good questions* and
*reducing the state*.

`warm` appears only on the first call of a process (and is ~0 when the connection
is already open). It is kept on by default because it is a net win: on the
measurement machine the handshake costs 74–100 ms and saves ~100 ms on the first
real call (437 ms cold vs 340 ms warm).

## 7. Choosing a confidence threshold

There is no universal number, and copying one from a blog post is the most common
way to ship a bad gate. **Measure it.** Use your ledger:

```bash
jevskill stats --json | jq '.by_intent'
```

Then plot confidence against accuracy for your own labelled cases and pick the
threshold where the accuracy is good enough for what a wrong answer costs you. A
guard in front of `rm -rf` deserves a different threshold than a routing hint.

Escalate — to the LLM or to a human — when confidence is low, rather than
accepting an answer the model already told you it was unsure about.

## 8. Hard constraints (do not fight these)

- **No text output.** No prose, code, summaries, explanations. Ever.
- **Options are fixed per request.** Jev picks from the set you send; it cannot
  propose an option you did not think of.
- **Text input only.** No images or audio.
- **32K context** on OpenRouter, and accuracy degrades before the limit.
- **Not OpenAI-compatible.** It uses `/api/alpha/decisions`, never
  `/api/v1/chat/completions`. Chat SDKs will not work.
- **Hosted only.** No self-hosting, no VPC, no air-gap.

## 9. Failure modes to avoid

| Anti-pattern | Why it hurts | Do instead |
|---|---|---|
| Looping one question per call | 9.4× slower, ~2× tokens | one call, many questions |
| One giant "analyse everything" question | Broad questions underperform | atomic gates, combine in code |
| Accepting a 0.51 winner | It is a coin flip | narrow and re-ask |
| Dumping raw data "just in case" | Distracts and bills | 8k budget; REDUCE |
| Asking Jev to write a summary | It returns no text | use the LLM |
| Choosing options on the fly per call | Unstable, uncacheable | fixed question bundles |
| No `unclear` / `none` option | Forces a wrong answer | always include an escape hatch |
| Assuming Jev is more accurate than an LLM | It is not, on published evidence | use it for speed/cost, combine signals |
| Threshold copied from a doc | Your risks are not theirs | measure against your ledger |

## 10. Command reference

```
jevskill doctor                # key, connectivity, warm latency, live cost
jevskill patterns              # the palette, with shapes and examples
jevskill plan "<problem>"      # FREE: should Jev be used? which pattern? how many calls?
jevskill batch items.jsonl --text-key line --question-type choice --name owner --options a b unclear
jevskill ask --state ... --questions '<json>'
jevskill ask --state-file diff.txt --question-type choice --name owner --options a b c unclear
jevskill outcome <decision_id> correct|incorrect|escalated|overridden|no_action
jevskill stats                 # measured latency, cost, savings, accuracy per pattern
jevskill advice                # what to do about it: KEEP / STOP / ESCALATE / UNPROVEN
```

Add `--json` to any command for machine-readable output. State comes from
`--state`, `--state-file`, or stdin. Every command reports its own stage timings.

## 11. Further reading

- `references/api.md` — exact request/response shapes, all fields, error codes
- `references/patterns.md` — the nine patterns with full worked questions
- `references/prompting.md` — how to write instructions and criteria that work
- `references/benchmarks.md` — every measurement, with method and honesty notes
- Official docs: <https://docs.typesafe.ai> · Model:
  <https://openrouter.ai/typesafe/jev-1.13>
# The usage palette — nine patterns for coding workflows

Each pattern is defined by the **shape** of the problem, not by its topic. Apply
the structural test before reaching for the model at all. If a pattern's shape
does not match your problem, the pattern will not work, however well you write
the questions.

The palette is also machine-readable:

## Contents

| | Pattern | Shape | Evidence |
|---|---|---|---|
| [1](#1-gate--one-boolean-about-one-text) | **gate** | one boolean about one text | measured |
| [2](#2-triage--many-items-to-one-bounded-category-each) | **triage** | many items → one bounded category each | inferred |
| [3](#3-reduce--a-large-sequence-to-a-small-shortlist) | **reduce** | large sequence → small shortlist | measured, incl. a published failure |
| [4](#4-rank--several-items-to-an-order) | **rank** | several items → an order | inferred |
| [5](#5-route--one-task-to-one-tier-of-effort) | **route** | one task → one tier of effort | inferred |
| [6](#6-verify--one-artifact-to-a-rubric-score) | **verify** | one artifact → a rubric score | measured |
| [7](#7-guard--one-proposed-action-to-safe--unsafe) | **guard** | one proposed action → safe/unsafe | measured |
| [8](#8-shortlist--a-close-call-becomes-a-narrower-question) | **shortlist** | a close call → ask again, narrower | **logic tested, value unproven** |
| [9](#9-extract--unstructured-text-to-fixed-fields) | **extract** | unstructured text → fixed fields | inferred |

**What the labels mean.** `measured` — a script in this repo produces a number for
it, and the number is in `benchmarks.md`. `inferred` — the pattern follows from the
model's shape and is exercised in tests, but it has **not** been benchmarked on its
own workload, so treat the shape as sound and the effect size as unknown.
`unproven` — the code path is tested; the behaviour it exists for did not occur in
testing. Numbers, method and threats to validity: `benchmarks.md`.

Then: [the vendor's own patterns](#0-the-same-nine-in-typesafes-vocabulary) ·
[composition](#composition-how-patterns-stack) ·
[choosing between patterns](#choosing-between-patterns) ·
[anti-patterns](#anti-patterns)

```bash
jevskill patterns            # human summary
jevskill patterns --json     # the same data, for a harness
jevskill plan "<problem>"    # picks a pattern and costs it, for free
```

---

## 0. The same nine, in TypeSafe's vocabulary

These nine are a taxonomy of **problem shapes**. TypeSafe publishes four named
**architectural patterns** and a cookbook library, and an agent that has read the
vendor's skill will use those names. They are the same ideas at a different
granularity, so here is the translation — use it, and cite the vendor's numbers
rather than re-deriving them.

The four named patterns ([index](https://docs.typesafe.ai/patterns.md)):

| Vendor pattern | Its one-line definition | Nearest of ours |
|---|---|---|
| [Speculative Fan-Out](https://docs.typesafe.ai/patterns/fan-out.md) | "Send many questions in a single call, including speculative ones" | the mechanism under **every** pattern here |
| [Confidence-Gated Routing](https://docs.typesafe.ai/patterns/confidence-routing.md) | "The answer tells you what; confidence tells you whether to act." | **guard**, **shortlist**, **route** |
| [Composite Scoring](https://docs.typesafe.ai/patterns/composite-scoring.md) | "Break a complex judgment into atomic scores, combine with weights you control in code." | **verify**, **rank** |
| [Intent Routing](https://docs.typesafe.ai/patterns/intent-routing.md) | "Classify incoming requests and route each to the optimal handler" | **triage**, **route** |

And the cookbooks that back each of ours (all under
`https://docs.typesafe.ai/cookbooks/<name>.md`):

| Ours | Vendor pattern | Cookbooks |
|---|---|---|
| 1 **gate** | Speculative Fan-Out | [`llm_guardrails`](https://docs.typesafe.ai/cookbooks/llm_guardrails.md), [`function_calling`](https://docs.typesafe.ai/cookbooks/function_calling.md) |
| 2 **triage** | Intent Routing | [`hierarchical_classification`](https://docs.typesafe.ai/cookbooks/hierarchical_classification.md), [`skill_suggestion`](https://docs.typesafe.ai/cookbooks/skill_suggestion.md) |
| 3 **reduce** | Speculative Fan-Out | [`semantic_find`](https://docs.typesafe.ai/cookbooks/semantic_find.md), [`sde_cascade`](https://docs.typesafe.ai/cookbooks/sde_cascade.md) |
| 4 **rank** | Composite Scoring | [`rerank_typesafe`](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md) |
| 5 **route** | Intent Routing + Confidence-Gated Routing | [`skill_suggestion`](https://docs.typesafe.ai/cookbooks/skill_suggestion.md), [`sde_cascade`](https://docs.typesafe.ai/cookbooks/sde_cascade.md) |
| 6 **verify** | Composite Scoring | [`function_calling`](https://docs.typesafe.ai/cookbooks/function_calling.md), [`llm_guardrails`](https://docs.typesafe.ai/cookbooks/llm_guardrails.md) |
| 7 **guard** | Confidence-Gated Routing | [`llm_guardrails`](https://docs.typesafe.ai/cookbooks/llm_guardrails.md) |
| 8 **shortlist** | Confidence-Gated Routing | [`consistency_choice_cookbook`](https://docs.typesafe.ai/cookbooks/consistency_choice_cookbook.md), [`hierarchical_classification`](https://docs.typesafe.ai/cookbooks/hierarchical_classification.md) |
| 9 **extract** | — (no named pattern; the docs treat it as a cascade) | [`function_calling`](https://docs.typesafe.ai/cookbooks/function_calling.md), [`semantic_find`](https://docs.typesafe.ai/cookbooks/semantic_find.md) |

**The cookbooks that publish a number.** These are the vendor's measurements, not
this repository's — quote them as such.

| Cookbook | What it measured | Reported |
|---|---|---|
| [`parallel_questions`](https://docs.typesafe.ai/cookbooks/parallel_questions.md) | 13 questions over one article, one call vs 13 | `0.27s` / `$0.000497` vs `2.71s` / `$0.006090` — "batching: 12.2x cheaper, 10.0x faster" |
| [`skill_suggestion`](https://docs.typesafe.ai/cookbooks/skill_suggestion.md) | picking one skill from a 182-skill catalogue | "Wrong loads fell from 16.8% to 7.3%", needless loads 9.8% → 4.0% |
| [`hierarchical_classification`](https://docs.typesafe.ai/cookbooks/hierarchical_classification.md) | beam search (K=3) vs greedy over a taxonomy | "Beam search matched 4 of 4 expected leaves; greedy search matched 2 of 4." |
| [`rerank_typesafe`](https://docs.typesafe.ai/cookbooks/rerank_typesafe.md) | one question per BM25 candidate, 40 legal queries | "raise top-1 accuracy from 5% to 18%", top-10 38% → 62%; 1,200 calls cost `$0.0645` |
| [`consistency_choice_cookbook`](https://docs.typesafe.ai/cookbooks/consistency_choice_cookbook.md) | label agreement across 15 repeats, then a `0.60` floor | 90.8% raw; "agreement then rises to 99.2%, with automatic labels on 74.2%" |

The full index of pages is [`llms.txt`](https://docs.typesafe.ai/llms.txt) — fetch
that first if you need a cookbook this table does not list.

---

## 1. gate — one boolean about one text

**Shape:** you need a yes/no before doing something expensive.
**Layers:** two-stage — cheap atomic signals, then a code-combined policy.

The simplest pattern and the one to reach for first. A gate is a tripwire: it
either lets work through or stops it, and it costs about $0.00001.

```jsonc
{"breaks_api": {"type": "noul",
  "instructions": "Does this diff change the signature or return type of a public function?",
  "criteria": {
    "true":  "A name, parameter list, return type or exported constant changes.",
    "false": "Only internal helpers, comments, tests, formatting or private members change."
  }}}
```

A good gate names **both** sides. "Does it break the API?" invites a coin flip;
"here is what counts as breaking, here is what does not" does not.

**Exit hatch:** when you need several gates, do not chain them as separate calls.
Ask all of them in one call and combine the results in code — see pattern 6.

---

## 2. triage — many items to one bounded category each

**Shape:** each item needs exactly one label from a fixed set.
**Layers:** two-stage — coarse bucket, then fine bucket.

```jsonc
{"owner": {"type": "choice",
  "instructions": "Which module should own the fix for this failure?",
  "criteria": {
    "billing": "Invoices, tax, pricing, payments.",
    "api":     "HTTP layer, request/response serialization, auth middleware.",
    "ui":      "Components, rendering, styles, templates.",
    "db":      "Schema, migrations, queries, indexes.",
    "unclear": "Not enough information in the report to decide."
  }}}
```

**Always include an escape hatch** (`unclear`, `none`, `other`). Without one, an
item that fits nothing forces the model to pick a wrong answer, and you cannot
tell that it happened.

**Coarse-then-fine beats one wide question.** 40 options in one call degrade
accuracy on adjacent options. Two calls of five options stay sharp:

```
round 1:  {area: [backend, frontend, infra, data, unclear]}
round 2:  over only `backend`: {module: [billing, api, auth, jobs, other]}
```

That is 10 options split across two calls instead of 40 in one — and it is also
cheaper in tokens than the wide call, because the second call sees five criteria
rather than forty.

**Iterate on the distribution.** If round 1 returns `backend 0.51, data 0.44`,
do not accept `backend`. Narrow to those two and re-ask — see pattern 8.

---

## 3. reduce — a large sequence to a small shortlist

**Shape:** the data is far too big and mostly irrelevant.
**Layers:** two-stage — a chunk-level pass to find where activity is, then a
line-level gate that actually discriminates.

This is the pattern where the token savings live, and the one most likely to be
implemented wrongly.

### The wrong way (measured, and it failed)

Score each chunk, then attribute the chunk's score to all of its lines, sort, keep
the top 8. Because a genuinely hot chunk contains one salient line and 59 noise
lines, the shortlist is mostly noise. Measured on 900 log lines: **99.1% context
reduction, but only 1 of 8 kept lines was actually salient.** A reduction that
discards what you were looking for is worse than no reduction, because it looks
like it worked.

```bash
python bench/run.py --legacy-reduce   # reproduces this failure on demand
```

### The right way — and the trade you are choosing

```
stage 1  COARSE   score each chunk for where activity is
                 900 lines / 15 chunks
stage 2  FINE     a line-level gate (see the warning below)

Then pick a configuration:
  A) gate every line      98.8% reduction, 8/14 salient found, $0.0061
  B) chunk-prefilter first 99.6% reduction, 3/14 salient found, $0.0029
```

**Measured, on 900 synthetic log lines with 14 genuinely salient events:**

| Configuration | Reduction | Recall | Cost |
|---|---|---|---|
| **A** — gate every line | 98.8% (→201 tok) | **8/14 (57%)** | $0.0061 |
| **B** — chunk-prefilter, then gate | 99.6% (→67 tok) | 3/14 (21%) | $0.0029 |
| ✗ legacy single-stage chunk scoring | 99.0% (→156 tok) | **1/8** | $0.0017 |

Configuration B is 2.1× cheaper and reduces slightly more, but finds **five fewer**
lines. Pre-filtering chunks is a genuine cost/recall trade, **not a free win**:
when salient events are spread thinly, every chunk looks somewhat hot, so "keep
the hot chunks" mostly keeps everything's neighbours.

**Choose by what a miss costs you**, not by the reduction percentage. Gate every
line when a miss is unacceptable; pre-filter when the corpus is homogeneous and
the call budget matters.

### When a line is the wrong unit: `--blocks`

A line-level gate cannot answer a question that compares two lines. Measured, and
published as a loss for several releases: asked to keep lines where `prod` differs
from `default` in a feature-flag file, Jev **correctly** flagged `prod: true` at
p=0.92 — and the arm still scored **0/3**, because

* no single line can satisfy a comparison between two lines, and
* the flag's *name*, which the test asks for, sits on the line **above**.

`--blocks` gates a **header plus the scalars nested under it** instead. The pair
lands in one unit, and the kept text still carries the header — so the answer
survives the reduction. It took that workload from 0/3 to 3/3 with no change to the
other five, and the same scaffolding is now what the benchmark's gate uses.

```bash
python scripts/jev_query.py --state-file flags.yaml --blocks --reduce --keep 8 \
  --instructions 'Does `B{i}` drift from its default?'
```

The rules, which matter for getting it right:

- **A block is a header (`key:`) plus following lines indented deeper that are not
  themselves headers.** A nested header starts its own block, so a container like
  `flags:` stays a one-line block instead of swallowing every flag under it.
- **Windows are packed by block and never split one.** Slicing raw lines separates a
  header from its values, which is unanswerable for a reason that has nothing to do
  with the model.
- **Flat text has no headers**, so every line stays its own block: a log or CSV
  behaves exactly as before. Block mode is a generalisation, not a YAML special case.
- **`--blocks` accepts a raw text file**, not only a JSON array, so you can gate a
  YAML file directly.
- The question names the unit as `` `B{i}` `` — the same rule as `` `L{i}` `` in flat
  mode. Unnamed units return a flat, meaningless answer for everything.

Not established: behaviour on **deep** nesting, and on formats where a "block" is not
delimited by indentation (XML without pretty-printing, minified JSON). One workload
proves the fix, not the generality.

### Always keep the rejected part retrievable

Jev is not an oracle. Measured on 900 synthetic log lines with evenly scattered
signal, the gate found **8 of 14** genuinely salient lines. Good, not perfect.

So reduction must be **reversible**. `--reduce` writes every rejected item to a
local store and prints a handle:

```bash
python scripts/jev_query.py --state-file build.log --reduce \
  --instructions 'Does the item at `L{i}` report a problem worth investigating?'
# REDUCE: 900 items -> 8 kept (98.8% fewer tokens: 34906 -> 414)
#   rejected 892 items stored; retrieve with:
#   python scripts/jev_recovery.py rc_92110309381a --grep <pattern>
```

```bash
python scripts/jev_recovery.py rc_92110309381a --summary
python scripts/jev_recovery.py rc_92110309381a --grep "WARN|ERROR|FATAL"
python scripts/jev_recovery.py rc_92110309381a --index 42 43
python scripts/jev_recovery.py --list
```

On the measurement above, recovery surfaced the **6 salient lines the gate had
missed** — which turns a 57% recall gate into a 100% recoverable pipeline. That is
the difference between an optimisation and a bet: **a reduction you cannot reverse
is a bet.**

This is the one structural advantage a mechanical compressor has over a decision
model (it can always hand back the original bytes). Keeping the rejected items
costs disk and nothing else, and closes the gap.

### The stage-2 question must name its target

This is the defect that made the first two-stage attempt *worse* (0 of 8):

```jsonc
// ❌ ambiguous — which of the 20 lines?
{"keep_L7": {"type": "noul", "instructions": "Does this single log line report an error?"}}

// ✅ exactly one line, named in instructions and criteria
{"keep_L7": {"type": "noul",
  "instructions": "Does the log line at `L7` report an error?",
  "criteria": {
    "true":  "`L7` reports an error or failure. Lines other than `L7` are irrelevant.",
    "false": "`L7` is routine telemetry. Lines other than `L7` are irrelevant."
  }}}
```

Measured on one 20-line window with a single real `ERROR`: the ambiguous form
scored the error line 0.72 and its neighbours 0.75 — plausible numbers that
discriminate nothing. The named form scored **0.96 vs 0.03**. Full detail:
`prompting.md` §1.

### Worked example

```jsonc
// stage 1 — one call per chunk
{"salience": {"type": "score",
  "instructions": "How operationally severe are the events in this log chunk? Judge the worst event present, not the average.",
  "criteria": ["Routine only: debug, info, heartbeat, cache, metrics.",
               "Minor: warnings that do not affect users.",
               "Serious: errors affecting requests or data.",
               "Critical: outage, data loss, or security failure."]}}

// stage 2 — one call per window of ~20 lines, each line named and pointed at
{"keep_L0": {"type": "noul",
  "instructions": "Does the log line at `L0` report an operational problem a human should investigate?",
  "criteria": {"true":  "`L0` reports an error, a fatal condition, or a failed operation. Lines other than `L0` are irrelevant.",
               "false": "`L0` is routine telemetry. Lines other than `L0` are irrelevant."}},
 "keep_L1": {"type": "noul", "instructions": "Does the log line at `L1` report an operational problem a human should investigate?"}}
```

Twenty `noul` questions over one window is **one** call, because questions share a
state — which is what makes the fine-grained stage affordable.

**When to use reduce:**

* logs, stack traces, test output, dependency trees, search results;
* "which of these N should I look at?", where N is in the hundreds or thousands;
* any time you were about to paste a large blob into a model's context.

**When not to:** if you can filter deterministically with code (`grep ERROR`,
`status != ok`), do that instead. Jev earns its place on the residue that code
cannot classify — the difference between an error that matters and an error that
is routine.

---

## 4. rank — several items to an order

**Shape:** you need a priority order across items.
**Layers:** two-stage — score all, then re-score the top-k with more context.

Do **not** ask one question to rank N items against each other. Use one `score`
per item, all in a single call, then sort in code.

```jsonc
{"impact_0": {"type": "score", "instructions": "How much user impact does finding 0 have?", "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"]},
 "impact_1": {"type": "score", "instructions": "How much user impact does finding 1 have?", "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"]},
 "impact_2": {"type": "score", "instructions": "How much user impact does finding 2 have?", "criteria": ["Cosmetic", "Minor", "Moderate", "Serious", "Critical"]}}
```

```python
ranking = sorted(
    ((result.score(f"impact_{i}"), item) for i, item in enumerate(findings)),
    key=lambda pair: pair[0], reverse=True,
)
```

One call, N scores, and the ordering is computed by deterministic arithmetic
rather than by the model — which is exactly where ordering belongs.

**Second layer:** re-score only the top 5 with a fuller state (the actual diff,
the surrounding code). Scores from a thin state are useful for trimming, not for
a final decision.

---

## 5. route — one task to one tier of effort

**Shape:** one task, and you must decide how much machinery it deserves.
**Layers:** two-stage — route, then verify the route was right.

```jsonc
{"tier": {"type": "choice",
  "instructions": "What level of effort does this change require?",
  "criteria": {
    "trivial":  "A local edit: constant, string, comment, obvious typo.",
    "local":    "One function or file, clear intent, no design decision.",
    "design":   "Touches several files or needs an interface decision.",
    "research": "Requires reading unfamiliar code or an external spec first."
  }}}
```

This is the pattern that pays for itself, because it decides whether to spend
frontier-model tokens. A `trivial` route can go to a small model or to a
mechanical fix; a `research` route genuinely needs the expensive one.

**Always verify the route cheaply.** A mis-route is the expensive failure: a
`trivial` label on a `design` problem produces a confidently wrong fix, and the
cost of discovering that exceeds everything saved. A second `noul` —
"Did this change touch more than one file?" — is often enough to catch it.

---

## 6. verify — one artifact to a rubric score

**Shape:** you have produced something and want it checked before a human sees it.
**Layers:** two-stage — several atomic Noul checks, combined in code.

Do not ask "is this good?". Ask the several specific things that make it good,
then combine.

```jsonc
{"tests_actually_fail": {"type": "noul",
  "instructions": "Would this test fail if the fix were reverted?",
  "criteria": {"true": "The test asserts the specific behaviour the fix changed.",
               "false": "The test would pass with or without the fix."}},
 "covers_reported_case": {"type": "noul",
  "instructions": "Does the test exercise the exact case from the bug report?"},
 "asserts_side_effects": {"type": "noul",
  "instructions": "Does the test assert observable behaviour rather than implementation details?"}}
```

```python
confidence = combine_weighted(
    {name: r.noul(name) for name in ("tests_actually_fail", "covers_reported_case",
                                     "asserts_side_effects")},
    {"tests_actually_fail": 0.5, "covers_reported_case": 0.3, "asserts_side_effects": 0.2},
)
```

**This is the strongest pattern in the palette, and it has independent evidence —
which also says something uncomfortable about Jev.** The source is
[`anisselbd/jev-phishing-bench`](https://github.com/anisselbd/jev-phishing-bench)
(2,000 emails of PhishNChips v5.2, 17 September 2026). Read all four rows before
you quote any of them:

| On the same 2,000 emails | Accuracy |
|---|---|
| Jev, one verdict question | **62.6%** (AUROC 0.689, ECE 0.154) |
| Claude Haiku 4.5, one prompt | **81.3%** (AUROC 0.837, ECE 0.097) |
| Jev's five signal Nouls + logistic regression | **95.1%** (AUROC 0.988, ECE 0.027); **95.0%** [93.5, 96.2] on the held-out half |
| Haiku asked the *same* five signals + the same regression | 93.2% [91.5, 94.6] — a statistical tie with Jev's (McNemar p = 0.063) |
| A hand-written regex on links and sender domains | **91.8%** on the held-out half |

Three conclusions, and only the first is flattering:

1. **Decomposition is where the accuracy is.** One broad verdict scored 62.6%; the
   same call's five atomic signals, combined in code, scored 95.1%. That is the
   pattern on this page, measured by someone else.
2. **Jev alone is markedly worse than a small chat model on this task** — 62.6% vs
   81.3%, "McNemar p < 0.0001". Do not sell a single Jev verdict as an accuracy win.
3. **The decomposition is not Jev's alone.** Haiku given the same five questions
   ties it, and a two-feature regex already reaches 91.8%. What Jev keeps is the
   price: the benchmark reports it "about 27 times cheaper and 5 times faster than
   Haiku" for signals of comparable quality ($0.038 vs $0.462 per 1,000 emails,
   p50 239 ms vs 687 ms).

So reach for this pattern when you want many cheap signals per second, and check a
deterministic baseline first. This repository's own calibration run is consistent
with the decomposition result, not with a claim of superior accuracy.

---

## 7. guard — one proposed action to safe / unsafe

**Shape:** an action is about to be taken and it is irreversible.
**Layers:** two-stage — guard, then require human confirmation regardless.

```jsonc
{"outside_repo": {"type": "noul",
  "instructions": "Does this shell command write, move or delete anything outside the repository directory?",
  "criteria": {"true":  "Any path outside the repo, a wildcard that could escape it, or a recursive delete.",
               "false": "All paths are relative, inside the repo, and non-destructive."}},
 "targets_secrets": {"type": "noul",
  "instructions": "Does this command read or transmit credentials, tokens, keys or .env files?"}}
```

**A guard is a tripwire, never an authorisation.** It is cheap and it catches the
obvious mistake; it does not make an action safe. Pair it with a human
confirmation for anything irreversible, and set the threshold high — a guard in
front of `rm -rf` deserves a stricter threshold than a routing hint. Measure the
threshold on your own labelled cases (see `references/benchmarks.md`); do not copy
a number from any document, including this one.

---

## 8. shortlist — a close call becomes a narrower question

**Shape:** the first answer was not confident.
**Layers:** **iterative** — loop until confident, or escalate.

The pattern that most harnesses miss, and the one that makes Jev's probability
distributions worth having.

```
round 1   {owner: [client, retry, timeout, pool]}
          -> retry 0.51, client 0.44, timeout 0.05, pool 0.00
          gap 0.07  ->  NARROW, do not accept

round 2   state += the context that discriminates retry from client
          {owner: [retry, client]}
          -> retry 0.91, client 0.09
          gap 0.82  ->  ACCEPT retry
```

```python
verdict = next_round(result.probs("owner"), confidence=result.confidence("owner"))
if verdict.action == "narrow":
    second = jev.decide({**state, **discriminating_context()},
                        {"owner": choice("...", verdict.next_options)})
elif verdict.action == "escalate":
    hand_to_llm_or_human()   # never guess
```

**Adding the discriminating context in round 2 is the part that matters.** Asking
the same question over a shorter list with the same state often reproduces the
same ambiguity. Feed it the thing that separates the two candidates.

**Budget two rounds.** Beyond that you are paying repeatedly for a coin flip, and
escalation is cheaper and more honest.

---

## 9. extract — unstructured text to fixed fields

**Shape:** messy text must become values your code can read.
**Layers:** two-stage — presence gate per field, then a Choice over candidates.

Jev cannot produce free text, so extraction is done by asking *closed* questions
about each field. State is `{"trace": "<the raw traceback>"}`, and every question
names that key with a backticked path — the rule from `prompting.md` §1:

```jsonc
{"lang_is_python":  {"type": "noul",
   "instructions": "Is `trace` a Python traceback?",
   "criteria": {"true": "`trace` contains Python traceback lines (File \"…\", line N).",
                "false": "`trace` is not a Python traceback."}},
 "has_exception":   {"type": "noul",
   "instructions": "Does `trace` name an exception type on its final line?"},
 "exc_type":        {"type": "choice",
   "instructions": "Which exception type does `trace` raise?",
   "criteria": {"ValueError": "`trace` ends in ValueError.",
                "KeyError": "`trace` ends in KeyError.",
                "TypeError": "`trace` ends in TypeError.",
                "other": "`trace` ends in a different exception type, or none is named."}},
 "is_timeout":      {"type": "noul",
   "instructions": "Does `trace` implicate a timeout or a deadline being exceeded?"},
 "user_code_frame": {"type": "noul",
   "instructions": "Does `trace` include a frame in application code rather than only library or framework code?"}}
```

**For genuinely open-ended values** (a filename, a numeric id), do not use Jev —
use a regex or an LLM. Use `choice` only when the candidate set is finite; when it
is not, enumerate the likely candidates with code or retrieval first and let Jev
pick among them.

---

## Composition: how patterns stack

Real work composes two or three patterns into a pipeline. Three shapes cover most
cases:

```
CASCADE        triage(coarse) -> triage(fine)
REDUCE         reduce(chunk score) -> reduce(line gate) -> LLM(shortlist)
FAN-OUT+COMBINE gate x N (one call) -> combine_weighted(...) -> act
```

Cost the pipeline before running it:

```bash
jevskill plan "keep the 8 lines that matter from this log" --state-file build.log
#   pattern 'reduce', calls 16, 16289 tok -> 2 chunk stage(s) + 1 final call
```

## Choosing between patterns

| If your problem is… | Pattern | Because |
|---|---|---|
| "should we…?" | gate | boolean, cheap, gates the expensive step |
| "which one of these…?" | triage | bounded category per item |
| "there are too many…" | reduce | the data, not the question, is the problem |
| "what order…?" | rank | N independent scores, sorted in code |
| "how much effort…?" | route | decides which model to spend |
| "is this good enough…?" | verify | atomic rubric checks, combined |
| "is this safe…?" | guard | tripwire before irreversible action |
| "I'm not sure…" | shortlist | iterate on the distribution |
| "turn this into fields…" | extract | closed questions per field |

## Anti-patterns

One table, deliberately: this list used to be two overlapping ones.

| Anti-pattern | Fix |
|---|---|
| Looping one question per call | one call, many questions — measured **12.4× slower and 4.03× more tokens** for 8 questions in 8 calls (`bench/results.json`, `E3_fanout.speedup_x`, `E3_fanout.token_amplification_x`) |
| One broad "analyse this / analyse everything" question | atomic gates and signals, combined in code — that is where the accuracy is (§6) |
| Chunk score attributed to every line | real two-stage cascade with a line-level gate |
| A line-gate question that does not name its line | point at it with a backticked path |
| Assuming pre-filtering is free | measure recall, not just reduction |
| Ranking N items in one question | one `score` per item, sort in code |
| Asking whether the state changed, or how many there are | hash, diff and count in **code**; a `stuck` Noul scored 0.42–0.60 on a screen that had plainly changed (`prompting.md` §11) |
| Accepting a 0.51 winner | narrow and re-ask |
| No `unclear`/`none` option | always include an escape hatch |
| Asking for prose or code | use the LLM |
| Guard used as authorisation | guard + human confirmation |
| Threshold copied from a doc | measure on your own labelled cases |
| Sending the whole corpus | reduce first; 8K budget |
| Choosing options on the fly per call | fixed bundles: unstable and uncacheable otherwise |
| Assuming Jev is more accurate than an LLM | it is not, on published evidence — 62.6% vs 81.3% on [jev-phishing-bench](https://github.com/anisselbd/jev-phishing-bench); use it for speed and cost, or to combine signals |

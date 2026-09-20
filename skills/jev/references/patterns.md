# The usage palette — nine patterns for coding workflows

Each pattern is defined by the **shape** of the problem, not by its topic. Apply
the structural test before reaching for the model at all. If a pattern's shape
does not match your problem, the pattern will not work, however well you write
the questions.

The palette is also machine-readable:

## Contents

| | Pattern | Shape |
|---|---|---|
| [1](#1-gate--one-boolean-about-one-text) | **gate** | one boolean about one text |
| [2](#2-triage--many-items-to-one-bounded-category-each) | **triage** | many items → one bounded category each |
| [3](#3-reduce--a-large-sequence-to-a-small-shortlist) | **reduce** | large sequence → small shortlist |
| [4](#4-rank--several-items-to-an-order) | **rank** | several items → an order |
| [5](#5-route--one-task-to-one-tier-of-effort) | **route** | one task → one tier of effort |
| [6](#6-verify--one-artifact-to-a-rubric-score) | **verify** | one artifact → a rubric score |
| [7](#7-guard--one-proposed-action-to-safe--unsafe) | **guard** | one proposed action → safe/unsafe |
| [8](#8-shortlist--a-close-call-becomes-a-narrower-question) | **shortlist** | a close call → ask again, narrower |
| [9](#9-extract--unstructured-text-to-fixed-fields) | **extract** | unstructured text → fixed fields |

Then: [composition](#composition-how-patterns-stack) ·
[choosing between patterns](#choosing-between-patterns) ·
[anti-patterns](#anti-patterns)

```bash
jevskill patterns            # human summary
jevskill patterns --json     # the same data, for a harness
jevskill plan "<problem>"    # picks a pattern and costs it, for free
```

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

**This is the strongest pattern in the palette, and it has independent evidence.**
A 2,000-email study found Jev's *single* verdict statistically worse than a small
chat model (McNemar p < 0.0001) — but the same study found that **five cheap
signal questions combined in a plain logistic regression reached 95.1%**. Atomic
signals composed in code beat one broad judgement. This repository's own
calibration run is consistent with that.

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

| Anti-pattern | Fix |
|---|---|
| One question per call in a loop | one call, many questions |
| One broad "analyse this" question | atomic signals, combine in code |
| Chunk score attributed to every line | real two-stage cascade with a line-level gate |
| A line-gate question that does not name its line | point at it with a backticked path |
| Assuming pre-filtering is free | measure recall, not just reduction |
| Ranking N items in one question | one `score` per item, sort in code |
| Accepting a 0.51 winner | narrow and re-ask |
| No `unclear`/`none` option | always include an escape hatch |
| Asking for prose or code | use the LLM |
| Guard used as authorisation | guard + human confirmation |
| Threshold copied from a doc | measure on your own labelled cases |
| Sending the whole corpus | reduce first; 8K budget |
| Looping one question per call | one call, many questions (9.4× slower, ~2× tokens) |
| One giant "analyse everything" question | atomic gates, combine the answers in code |
| Choosing options on the fly per call | fixed bundles: unstable and uncacheable otherwise |
| Assuming Jev is more accurate than an LLM | it is not, on published evidence — use it for speed and cost, or to combine signals |

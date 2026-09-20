# Writing questions that work

Jev is not prompted like a chat model. There is no persona, no chain of thought,
no "you are an expert". What exists is a state, a question, and a set of criteria
— and the way you shape those three decides whether the answer is signal or noise.

This file collects the rules that measurably changed outcomes while building this
skill, with the measurements attached.

---

## 1. Point at the value — with a backtick path

**This is the highest-impact rule in this document, and violating it fails
silently.**

When several values sit in one state, a question that says "this line" or "the
message" without naming which one produces a *flat, meaningless* answer. Not an
error — a plausible-looking number that discriminates nothing.

Measured on the same 20-line log window, with one genuine `ERROR` line and 19
noise lines, asked as one batched call:

| Variant | `L0` (the real ERROR) | `L1` (routine noise) | Useful? |
|---|---|---|---|
| **A** — `{"lines": {...}}`, question says "this single log line" | 0.72 | 0.75 | ❌ no discrimination |
| **B** — one line per state, one call per line | 0.98 | 0.02 | ✅ but 20 calls |
| **C** — per-line keys `` `L{i}` ``, question names the path | **0.96** | **0.03** | ✅ **and batched** |

Variant A is the trap: the numbers look like answers, so nothing warns you. It
cost this repository a whole benchmark run — the REDUCE pipeline reported a 99%
context reduction while keeping **zero** of the eight lines it was looking for.

```jsonc
// ❌ ambiguous — which line?
{"keep_L7": {"type": "noul", "instructions": "Does this single log line report an error?"}}

// ✅ unambiguous — exactly one line, named
{"keep_L7": {"type": "noul",
  "instructions": "Does the log line at `L7` report an error?",
  "criteria": {
    "true":  "`L7` reports an error or failure. Lines other than `L7` are irrelevant.",
    "false": "`L7` is routine telemetry. Lines other than `L7` are irrelevant."
  }}}
```

Two habits follow from this:

* **Name each item as its own state key** (`L0`, `L1`, …) rather than nesting them
  under one key.
* **Reference it in both `instructions` and `criteria`**, and repeat that the
  other values are irrelevant. Saying it once is not always enough.

Use backticked dot-and-index paths (`` `ticket.message` ``, `` `config.retry.max` ``)
— that is the documented convention.

---

## 2. Write criteria as contrasts

A criterion that only says what an option *is* leaves the boundary undefined. Give
each option the same fields, including what it is **not** for and a couple of
examples:

```jsonc
"criteria": {
  "billing": {
    "what": "Charges, invoices, refunds, subscriptions",
    "not_for": "Order tracking or account access",
    "examples": ["I was charged twice", "Where is my refund?"]
  },
  "orders": {
    "what": "Order status, delivery, cancellation, returns",
    "not_for": "Charges or account access",
    "examples": ["Where is my order?", "Cancel my shipment"]
  }
}
```

**Use identical field names across options.** The model compares options field by
field; matching names make that comparison direct instead of inferred. `not_for`
does most of the work — the confusable cases are almost always the ones a criterion
failed to exclude.

For `noul`, always supply both sides. A gate with only a `true` description has no
defined boundary and drifts.

---

## 3. Be atomic

One question, one judgement. Broad questions hide several decisions behind one
number, and you can neither inspect nor tune them.

```jsonc
// ❌ three judgements wearing one question
{"is_good_test": {"type": "noul",
  "instructions": "Is this a good test that covers the bug and won't break later?"}}

// ✅ three questions, one call, combined in code
{"would_fail_on_revert": {"type": "noul",
  "instructions": "Would this test fail if the fix were reverted?"},
 "covers_reported_case": {"type": "noul",
  "instructions": "Does the test exercise the exact case from the bug report?"},
 "asserts_behaviour": {"type": "noul",
  "instructions": "Does the test assert observable behaviour rather than implementation details?"}}
```

Atomicity does **not** cost extra round trips — questions in one call run in
parallel. It costs a few input tokens and buys inspectability, tunability and
better accuracy. The independent evidence on this is unusually strong: a single
Jev verdict did not beat a small chat model, while five atomic signals combined in
a logistic regression reached 95.1% accuracy. Decomposition is where the accuracy
comes from.

---

## 4. Keep instructions short and put the structure elsewhere

Long prose instructions get worse, not better. Move context, examples and
code-supplied values into their own named fields:

```jsonc
{"duplicate_name": {"type": "noul",
  "instructions": {"question": "Does `resume.name` match a name in `existing_candidates`?",
                   "focus": "Match on the person, not on shared surname alone.",
                   "compare": ["`resume.name`", "`existing_candidates`"]}}}
```

This is also how you make questions **reusable**: if a value comes from your
database, keeping it in its own field means the question text does not change
between calls, so you can cache the compiled questions and diff them across runs.

---

## 5. Always include an escape hatch

Every `choice` needs a way out. Without one, an item that fits no option is forced
into a wrong one and you cannot detect it.

```jsonc
"criteria": {
  "billing": "...", "api": "...", "ui": "...",
  "unclear": "The report does not contain enough information to choose any of the above."
}
```

Name it after the *reason* (`unclear`, `insufficient_context`, `none_of_these`),
not a catch-all (`other`). `other` attracts items that belong somewhere specific;
`unclear` attracts only the genuinely undecidable ones, and that is the signal you
want to route to a human.

---

## 6. Choose option keys that your code can read

Option keys are identifiers your program branches on, not prose for a human.

```jsonc
// ❌ forces string matching later
"criteria": {"The payments and billing team": "..."}

// ✅ the value is already a decision
"criteria": {"payments": "..."}
```

For an item-level choice, use stable indices (`e0`, `e7`) and keep the mapping in
your own data structure. That keeps the question bundle identical across
iterations — which matters, because a stable bundle can be cached and its results
compared over time.

---

## 7. Calibrate your thresholds; do not import them

There is no universal confidence threshold. `0.7` is not a fact; it is a policy
that depends on what a wrong answer costs you.

```bash
jevskill stats --json | jq '.by_intent, .by_pattern'
```

Then measure on your own labelled data: bucket the answers by confidence and look
at the actual accuracy in each bucket. If every answer lands at 0.03 or 0.98 — as
they did on the 80-line guard benchmark in this repository — the model is not
uncertain on your data and the threshold barely matters. If answers cluster
between 0.4 and 0.6, the threshold is doing real work and choosing it carelessly
is expensive.

**Route on uncertainty rather than ignoring it.** Low confidence is information:
escalate to the LLM or to a human.

---

## 8. Iterate on the distribution, not on a re-roll

`choice` returns the full distribution. Use it:

* leader above your accept threshold → accept;
* leader ahead of the runner-up by a clear margin → accept;
* leaders close → **narrow to the leaders and re-ask**, adding the context that
  discriminates them;
* still close after narrowing → escalate. Do not re-roll the same question hoping
  for a different answer; the same state will produce roughly the same judgement.

```python
verdict = next_round(result.probs("owner"), confidence=result.confidence("owner"))
if verdict.action == "narrow":
    second = jev.decide(
        {**state, "discriminator": extract_the_deciding_detail()},
        {"owner": choice("...", verdict.next_options)},
    )
```

The **added context** is what makes round two different. Shortening the option
list alone often reproduces the same ambiguity.

---

## 9. Compose many signals instead of asking once

When a judgement is genuinely multi-factorial, ask every factor and combine them in
code. Weights belong to you, not to the model.

```python
weights = {"requests_credentials": 0.45,
           "sender_mismatch": 0.30,
           "unexpected_reward": 0.25}
risk = combine_weighted({k: r.noul(k) for k in weights}, weights)

if 0.4 < risk < 0.6:
    escalate()          # uncertain band, by construction
elif risk >= 0.6:
    quarantine()
```

Explicit weights are auditable and tunable. If you have labels, replace the code
with a small logistic regression over the same probabilities — that is precisely
what the 95.1% result did.

---

## 10. State hygiene

* **Include only what the question needs.** Irrelevant state is a distractor and
  it is billed. This is the documented reason accuracy falls off before the context
  limit.
* **Structure beats prose.** Use nested JSON, and point questions at specific
  values rather than describing them in the instructions.
* **Keep values stable across iterations** when you intend to compare answers.
* **Never paste a whole file** when a `grep` result plus the relevant function will
  do. If you cannot cut it with code, use the REDUCE pattern
  (`references/patterns.md`, §3).
* **Stay under the budget** (8,000 tokens by default). `jevskill ask` refuses
  larger states and tells you what to do instead — read the advice rather than
  reaching for `--force`.

---

## Checklist before you ship a question bundle

- [ ] Does every question name the exact value it is about, with a backtick path?
- [ ] Does every `noul` describe both `true` **and** `false`?
- [ ] Does every `choice` have an escape hatch named after the reason?
- [ ] Does each option's criteria say what it is **not** for?
- [ ] Are the options atomic — one judgement each?
- [ ] Are the option keys identifiers your code can branch on?
- [ ] Are the questions in **one** call rather than a loop?
- [ ] Is the state under budget, and free of irrelevant context?
- [ ] Is the confidence threshold measured on your own labelled data?
- [ ] Are you iterating on the distribution instead of re-rolling?

---

## Choosing a confidence threshold

There is no universal number, and copying one from a blog post is the most common
way to ship a bad gate. The defaults in this skill (`--review-below 0.75`,
`--review-margin 0.10`) are **illustrative heuristics, not calibrated guarantees**.

Measure yours against your own labelled cases:

```bash
jevskill stats --json    # confidence and accuracy per pattern and intent
jevskill advice          # KEEP / STOP / ESCALATE / UNPROVEN
```

Then pick the threshold where accuracy is good enough for what a wrong answer
costs you. A guard in front of `rm -rf` deserves a different threshold than a
routing hint.

Three things worth knowing before you tune anything:

- **`confidence` is concentration, not correctness.** A `choice` can report a high
  confidence while the top two options are separated by noise, which is why the
  review rule also requires a margin between them. Read the `probabilities` when
  the decision matters.
- **Escalate rather than accept.** When confidence is low, hand the case to the LLM
  or to a human — do not act on an answer the model already told you it was unsure
  about. That is what exit code `2` is for.
- **Pair outcomes or you cannot tune.** `jevskill outcome <decision_id> …` is what
  turns the ledger into accuracy; without it `advice` can only report UNPROVEN.

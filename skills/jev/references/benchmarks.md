# Benchmarks — method and every number

Two suites. `bench/run.py` measures Jev **in isolation** (latency, cost, fan-out,
reduction, accuracy). `bench/ab.py` measures the thing a user actually cares
about: **with Jev in front of the model, does the model answer better, cheaper,
or not at all?**

Both write a machine-readable artifact that the README quotes:
`bench/results.json` and `bench/ab_results.json`.

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python bench/run.py --legacy-reduce    # E1-E7 microbenchmarks
python bench/ab.py --runs 3            # the A/B evaluation
python -m jevskill stats               # the same runs, from the ledger's point of view
```

**Conditions.** `typesafe/jev-1.13` via `https://openrouter.ai/api/alpha/decisions`,
from a residential connection in Poland, 2026-09-20. Client under test:
`jevskill` with `httpx[http2]` and `orjson`.

> Latency is dominated by network distance and provider inference. TypeSafe quotes
> 70–500 ms end-to-end depending on where the caller is; these numbers reflect one
> location. **Measure your own round trip before promising a figure** — the
> limitations page of the independent guide makes the same point.

---

# Part 1 — A/B evaluation (`bench/ab.py`)

The experiment that answers "should I actually use this?"

**Method.** Six workloads, each a large fixture plus a question with a
**checkable oracle** (an exact string from the data). Three arms:

| Arm | What the model reads |
|---|---|
| `direct` | the whole fixture |
| `grep` | a **deterministic** filter — the thing a competent engineer does with no model at all |
| `jev` | Jev's REDUCE shortlist (8 lines) |

The answering model is **identical in every arm** (`google/gemini-2.5-flash-lite`),
so the only variable is what the model reads. The `direct` and `jev` columns are the
**provider's own reported `usage`**; the `fixture` column is this repo's own estimate
(`count_tokens`, calibrated on logs and code) and is shown separately because the two
are different measurements — on CSV rows the estimate under-counts by 59%. Answers are
graded by substring match against the oracle. Three runs per arm.

## Results — 3 runs × 6 workloads × 3 arms

| Workload | fixture (est.) | direct tokens | direct | grep | **jev** | Token change |
|---|---:|---:|---:|---:|---:|---:|
| log_needle | 27,472 | 27,049 | 3/3 | 3/3 | **3/3** | −99.5% |
| csv_outlier | 12,065 | 19,223 | 3/3 | 3/3 | **3/3** | −98.9% |
| test_output | 19,882 | 16,562 | 3/3 | 3/3 | **3/3** | −99.1% |
| json_drift | 23,888 | 20,542 | 3/3 | 3/3 | **3/3** | −99.5% |
| yaml_drift | 25,133 | 18,134 | **0/3** | 3/3 | **3/3** | −99.5% |
| html_alert | 22,064 | 12,120 | 3/3 | 3/3 | **3/3** | −98.8% |
| **TOTAL** | | **113,632** | **15/18** | **18/18** | **18/18** | **−99.3%** |

**Provider-reported mean tokens: 113,632 → 831.**

Superseded figures, named rather than quietly replaced: the previous run of this suite
recorded **113,352 → 801**, **direct 12/18**, **grep 17/18**, **jev 15/18**, with
`json_drift` at direct **0/3** / grep **2/3** and `yaml_drift` at jev **0/3**. The
`yaml_drift` change is the one this release caused; the `json_drift` movement is not —
see the variance note below.

## Reading the result honestly

**Jev cut the model's input by 99.3% and scored 18/18 — level with the deterministic
filter, and 137× less context than the direct arm.**

The clearest explainable case remains `json_drift`: handed 20,542 tokens of service
definitions the model has to work, but given 111 tokens of shortlist it finds the one
SNAPSHOT tag. **A reduction is not only a cost optimisation — it is a signal-to-noise
improvement.**

**But the deterministic filter ties it, 18/18 to 18/18.** So the honest conclusion is
still not "Jev wins":

1. **If a regex or a structural text pass can answer it, use that.** It is free,
   instantaneous and deterministic. Jev is for the residue that code cannot classify.
2. **Jev's advantage over `grep` is on semantic anomalies** ("looks non-release,
   unusual, or out of pattern") which no keyword expresses — although in *this* run
   the filter happened to catch `json_drift` too.
3. **Gate blocks, not lines, when the answer spans lines.** This was a real loss until
   v0.10.0 and is now fixed; the diagnosis below is kept because the reasoning
   generalises.

### The yaml_drift loss: diagnosed, then fixed

This row scored **0/3 against the filter's 3/3** for several releases, and stayed
published. A failure without an explanation is just an anecdote, so here is both.

The target flag's block is four lines:

```yaml
  flag_0512:          <- the line holding the ANSWER (the name)
    default: false
    prod: true        <- the line holding the SIGNAL
    owner: team-7
```

Asked to "keep lines where the prod value differs from the default value", Jev picked
**`prod: true` at p=0.92** — a correct judgement of the description it was given. But
the test asks for the *flag name*, and the name lives on the line above. Jev's
shortlist was two lines (`prod: true`, `owner: team-7`) and contained no name at all.
The structural filter in the other arm emitted the whole four-line block, so the name
survived.

**Two things were wrong, and neither was the model:**

1. **The gate was asked about a line.** "Prod differs from default" is a comparison
   *between* two lines; no single line can satisfy it. The model answered the only
   question it could.
2. **The window sliced raw lines**, so a header could be separated from its own values
   by a window boundary — unanswerable for a reason with nothing to do with the model.

**The fix** is to gate blocks — a header plus the scalars nested under it, packed into
windows that never split one — which is what `jevskill/blocks.py` does and what the
arm now uses. The pair lands in one unit, and the kept text still carries the flag's
name. Measured after the change:

| | before | after |
|---|---:|---:|
| yaml_drift, jev arm | 0/3 | **3/3** |
| yaml_drift, jev tokens | 82 | 87 |
| other five workloads | — | unchanged |

Flat text has no headers, so block mode degrades to one block per line: a log file
behaves exactly as it did. That is why the change is a generalisation rather than a
special case for YAML, and why the other five rows did not move.

**The question-design lesson still stands.** The description asks about a *value* when
the answer is a *name*. Scaffolding rescued it here, but asking for the thing that
carries the identifier is still the better question — the same class of error as the
un-named-value bug in `prompting.md` §1, arriving from the other direction: there the
question was too vague, here it was precise about the wrong attribute.

## External corroboration

Three independent sources bear on what this suite measures, which is worth
recording because a single author's numbers deserve scepticism.

**The vendor, on fan-out.** TypeSafe's own
[parallel-questions cookbook](https://docs.typesafe.ai/cookbooks/parallel_questions)
runs 13 questions over a regulatory briefing and reports that batching every
question into one call is **12.2× cheaper on input tokens and 10.0× faster, with no
change in answers**.

This repository measured the same pattern independently in E3: **12.4× faster on
latency and 4.03× fewer tokens** for 8 questions in one call versus 8 sequential
calls. Different workloads, different question counts, different providers — and
the direction and rough magnitude agree. The token multiple differs because their
workload is 13 questions against one large state, where the shared state dominates;
the latency multiple is nearly identical.

**The vendor, on their own model's limits.** TypeSafe publishes a
[Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13) page
listing known weaknesses of this model version. Worth reading before trusting it
with anything consequential — a vendor documenting its model's failure modes is a
better signal than any benchmark.

**A third party, on accuracy — and it is not flattering.**
[`anisselbd/jev-phishing-bench`](https://github.com/anisselbd/jev-phishing-bench)
ran 2,000 phishing/legitimate emails through both Jev and Claude Haiku 4.5
(17 September 2026). Jev's **single verdict scored 62.6%** against Haiku's
**81.3%**, "McNemar p < 0.0001". Five atomic signal Nouls **from the same call**,
combined in a logistic regression, scored **95.1%** (AUROC 0.988, ECE 0.027) —
statistically tied with Haiku given the same five questions (93.2%), and above a
hand-written regex (91.8% on the held-out half). Jev's measured advantage there was
price and latency: "about 27 times cheaper and 5 times faster than Haiku".

That is the only external *accuracy* figure this repository cites, and it is the
reason nothing in this skill claims Jev out-judges an LLM. It is one dataset, with
synthetic email bodies, which the authors say up front.

**Where no external check exists.** Our REDUCE recall figures (`8/14`, `3/14`) and
the guard accuracy run have no independent replication. Treat them as one careful
measurement on synthetic data, not as established properties of the model.

## Threats to validity

1. **Synthetic fixtures.** Generated, not harvested. Real logs have messier
   signal distribution; `log_needle` in particular has exactly one needle.
2. **One answering model**, and a cheap one. The `direct` and `grep` arms feed that
   same sampled model, so **they wobble between runs with no code change**: between
   the v0.9.0 and v0.10.0 runs, `json_drift` moved from direct 0/3 to 3/3 and grep
   2/3 to 3/3 while neither arm was touched. Their token counts, by contrast, were
   reproducible to within a couple of tokens. Read arm *accuracy* differences of one
   or two runs as noise; read the `jev` changes against a fixed oracle as signal.
3. **One filter description per workload**, written by the same author as the
   gate question. A different phrasing could move either arm.
4. **`n=3` per cell.** Directional, not statistical. No confidence interval is
   claimed; with 18 observations per arm a two-point difference is noise.
5. **Block mode is demonstrated on one workload.** `yaml_drift` is the only fixture
   with a two-line comparison, so 0/3 → 3/3 is one case, on synthetic YAML, at one
   nesting depth. It is a fix for a diagnosed defect, not evidence that block gating
   generalises to every structured format.
6. **Not a head-to-head with other token-reduction tools.** Caveman publishes
   −33.2% on a 54-run Claude Code suite with provider-reported tokens and 18/18
   oracles. The fixtures, model and harness differ, so the percentages are **not**
   comparable. What is comparable is the *method*, and this suite now matches it.

---

# Part 2 — Microbenchmarks (`bench/run.py`)

All figures below come from `bench/run.py` against the live OpenRouter Decisions
API, written to `bench/results.json`.

**Conditions for Part 2.** 148 decisions / 1 188 questions.

---

## E1 — Latency floor

Twelve repeats of an identical single-question decision, plus a cold first call.

| Metric | Value |
|---|---|
| Cold first request | 485 ms |
| Warm p50 | **325 ms** |
| Warm p95 | 427 ms |
| Warm range | 299–683 ms |
| Cost per decision | $0.0000133 |
| Input tokens | 317 |

**Reading it.** The cold/warm gap (~160 ms) is the DNS+TCP+TLS+HTTP/2 handshake,
which is why `jevskill ask` warms the connection first by default. The warm range
is wide — 299 to 683 ms across twelve calls with *identical* input — so treat any
single latency measurement as noise and compare medians.

## E2 — State-size sensitivity

| State | Estimated tokens | Real input tokens | p50 | p95 | Cost |
|---|---|---|---|---|---|
| small | 18 | 324 | 353 ms | 471 ms | $0.0000136 |
| medium | 306 | 920 | 370 ms | 395 ms | $0.0000386 |
| large | 3 962 | 7 020 | 468 ms | 601 ms | $0.0002948 |

**Reading it.** A **22× increase in state** moved p50 by 115 ms — far less than
proportional, and much smaller than the 299–683 ms spread between identical calls
in E1. The earlier run of this same experiment measured a 9 ms delta. The honest
conclusion is that latency is **weakly** dependent on state size and heavily
dominated by network variance; cost, by contrast, is strictly linear in input
tokens (22× state → 22× cost).

The practical consequence: **size your state by accuracy and cost, not by
latency.** Irrelevant context is a distractor you also pay for.

## E3 — Fan-out versus sequential

Eight questions over one shared state.

| | Latency | Tokens | Cost |
|---|---|---|---|
| Batched (1 call) | **288 ms** | **663** | $0.0000279 |
| Sequential (8 calls) | 3 564 ms | 2 672 | $0.0001122 |
| **Ratio** | **12.4× faster** | **4.03× fewer** | 4.03× cheaper |

Per question, batched: **36 ms**.

**Reading it.** This is the strongest and most reliable result in the suite, and
it is structural rather than incidental: questions in one call are evaluated in
parallel, while sequential calls re-send the same state every time. The 4.03×
token amplification is exactly 8 calls' worth of one shared state. An earlier run
measured 7.4× on latency; the direction is consistent even as absolute network
time varies.

**Never loop one question per call.**

## E4 — REDUCE: 900 log lines to the 8 that matter

Corpus: 900 synthetic log lines, 14 genuinely salient (real `ERROR`/`FATAL`/`WARN`
events), 16 289 tokens.

| Configuration | Calls | Reduction | Recall | Cost |
|---|---|---|---|---|
| **A** gate every line | 45 | 98.8% (→201 tok) | **8/14 (57%)** | $0.0061 |
| **B** chunk-prefilter, then gate | 24 | 99.6% (→67 tok) | 3/14 (21%) | $0.0029 |
| ✗ legacy single-stage chunk scoring | 15 | 99.0% (→156 tok) | **1/8** | $0.0017 |

**Reading it — the most important negative result here.**

The *legacy* design scored each 60-line chunk and attributed the chunk's score to
every line inside it. It reported a 99% context reduction while keeping **one** of
the eight lines it was looking for. A reduction that discards the signal you were
searching for is worse than none, because it looks like it worked.

The corrected design adds a **line-level gate**, which required fixing the
prompting bug documented below. With the fix, configuration A finds 8 of 14
salient lines (57% recall) at 100% precision on what it returns — the 8 it kept
were all genuine.

Configuration B is 2.1× cheaper and reduces slightly more, but finds **5 fewer**
lines. Pre-filtering chunks is a genuine cost/recall trade, not a free win: when
salient events are spread thinly, every chunk looks somewhat hot, so "keep the hot
chunks" mostly keeps *everything*'s neighbours.

**Choose by what a miss costs you**, not by the reduction percentage.

### The prompting bug behind variant A

Measured on one 20-line window containing exactly one real `ERROR`:

| How the question named its target | Real `ERROR` | Routine noise |
|---|---|---|
| `{"lines": {L0…L19}}`, "does this single log line report a problem?" | 0.72 | 0.75 |
| One line per state, one call per line | 0.98 | 0.02 |
| Per-line state keys, question points at `` `L0` `` | **0.96** | **0.03** |

The first variant produces plausible numbers that discriminate nothing. Naming the
value with a backticked path (a documented TypeSafe convention) restores it, and
stays batched. Full detail: `skills/jev/references/prompting.md` §1.

## E5 — Guard accuracy and calibration

80 labelled log lines (40 salient, 40 routine), each judged independently by a
`noul` gate at the 0.5 threshold.

| Metric | Value |
|---|---|
| Judged at a decisive probability | 49 |
| Accuracy | **100%** |
| Precision / recall | 100% / 100% |
| p50 latency | 380 ms |
| Total cost | $0.00079 |

**Calibration** — the property that makes a probability worth acting on:

| Confidence band | n | Mean P | Actual rate |
|---|---|---|---|
| 0–20% | 40 | 0.03 | **0.00** |
| 80–100% | 9 | 0.98 | **1.00** |

**Reading it.** The model was decisive (and correct) on 49 of 80 lines and quiet
on the rest — 31 answers fell outside the 0–20% and 80–100% bands, so the
distribution is not simply two spikes. Where it was confident, it was right; where
it said "probably not", it was never wrong.

**Treat this as a weak signal, not a headline.** The corpus is synthetic and the
task is deliberately easy. The *method* — pair outcomes, bucket by confidence,
compare to the actual rate — is the transferable part; the 100% is not.

## E6 — LLM contrast

The same single question through `google/gemini-2.5-flash-lite`:

| | Latency | Tokens | Cost |
|---|---|---|---|
| Jev | 325 ms | 317 | $0.0000133 |
| gemini-2.5-flash-lite | 429 ms | 42 | **$0.0000057** |

**Reading it.** The small chat model was **cheaper** on this trivial call, and
within the noise on latency. This is published deliberately.

Jev's case is not "cheaper than a chat model per call":

* the output is **typed and constrained** — no parsing, no malformed JSON, no
  retry logic to maintain;
* every answer carries a **calibrated probability distribution**, so code can
  branch on uncertainty, which a chat model's single label does not offer;
* **fan-out** makes it practical to ask 20 atomic questions for the price of one
  round trip (E3: eight questions answered in one 288 ms call), which is what
  enables the large context reductions in E4 at all.

Against a frontier model on a large state the token arithmetic changes sharply in
Jev's favour, because Jev never needs the corpus pasted into a chat context — that
is the saving E4 measures.

## E7 — Iterative narrowing

The `next_round` rule: accept on confidence ≥ 0.7 or a probability gap > 0.15;
narrow to the leaders when they are close; escalate when the round budget is out.

| Case | Round 1 confidence | Verdict |
|---|---|---|
| Clear ("retry backoff") | 1.00 | accept |
| Deliberately ambiguous | 0.92 | accept |

**Reading it — a negative result.** The narrowing path **did not fire** in either
case: Jev was more confident than expected even on a case designed to be
ambiguous. The logic is covered by unit tests, but its *empirical* value is
**unproven** in this repository. A genuine ambiguity would need a harder case, or
options that overlap more deliberately.

This is reported rather than quietly omitted, because "we implemented narrowing"
and "narrowing demonstrably helps" are different claims.

## Cost model

```
cost = (state_tokens + question_tokens) / 1_000_000 × $0.042
```

Output is free because none is generated. At the measured $0.0000133 per small
decision, one million decisions cost about **$13**.

---

## Threats to validity

Stated so the numbers are not over-read:

1. **One location, one day.** Latency is network-bound; a caller nearer the
   provider would see lower figures, and an earlier run of this suite measured
   p50 304 ms against 325 ms now.
2. **Synthetic corpus.** E4 and E5 use generated log lines with clean labels.
   Production logs have correlated, messier signal distribution.
3. **E4 recall is distribution-dependent.** The 3/14 versus 8/14 gap reflects
   *uniformly scattered* signal. Clustered signal would flatter the cheap
   configuration.
4. **E5's 100% is not a general accuracy claim**, for the reason in E5 above.
5. **E7 does not support its own feature** at the time of writing.
6. **The baseline used for ledger savings is stated, not measured.** It assumes a
   frontier chat model at $3.00/M input, $15.00/M output, 60 output tokens and
   350 tokens of scaffolding — deliberately conservative, and printed in
   `stats.DEFAULT_BASELINE` so the arithmetic can be redone. It is a *counterfactual*,
   unlike every other number here, which comes from the API's own reported usage.

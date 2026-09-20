# Benchmarks — method and every number

All figures come from `bench/run.py` against the live OpenRouter Decisions API,
written to `bench/results.json`. Reproduce with:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python bench/run.py --legacy-reduce
python -m jevskill stats          # the same run, from the ledger's point of view
```

**Conditions.** `typesafe/jev-1.13` via `https://openrouter.ai/api/alpha/decisions`,
from a residential connection in Poland, 2026-09-20, 148 decisions / 1 188
questions. Client under test: `jevskill` with `httpx[http2]` and `orjson`.

> Latency is dominated by network distance and provider inference. TypeSafe quotes
> 70–500 ms end-to-end depending on where the caller is; these numbers reflect one
> location. **Measure your own round trip before promising a figure** — the
> limitations page of the independent guide makes the same point.

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
* **fan-out** makes it practical to ask 20 atomic questions in one 300 ms call,
  which is what enables the large context reductions in E4 at all.

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

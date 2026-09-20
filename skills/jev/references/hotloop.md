# Hot loop — a decision per step, not a burst

The default client is tuned for a *burst*: a handful of decisions inside one
harness turn, where a 60 s timeout and two retries are cheap insurance. An agent
loop is the other shape — one decision per step, every few hundred milliseconds —
and there a request still waiting after a second is already useless, because the
screen it was asked about has moved on.

```python
from jevskill.client import JevClient

with JevClient(hot=True) as jev:          # 1.5 s read, 2 s connect, 0 retries
    jev.warm()                            # HEAD /v1/models (see below)
    for step in loop:
        result = jev.decide(ui_tree, questions)
```

`hot` is a **constructor flag, not a `Config.hot()` classmethod**. A `Config`
answers *where do I send this, with which key*; it is resolved once from the
environment and may be shared by several clients and by the CLI. Hot is not a
fact about the endpoint, it is the call pattern of one client — so it lives where
the client is built, and the caller's config object is never mutated.

Hot mode retunes **silence, not a number you stated**. `Config(timeout_read_s=8)`
keeps 8 seconds — and so does `Config(timeout_read_s=60.0)`, the documented
default said out loud. That distinction is real rather than cosmetic: the three
retuned fields arrive as `None` and are resolved in `Config.__post_init__`, which
records what it resolved (`config.is_defaulted("retries")`). An earlier version
compared each field to its dataclass default, so stating the documented number
was indistinguishable from saying nothing and was silently retuned to 1.5 s / 0
retries. `dataclasses.replace` produces a config that reports nothing as
defaulted, so a copy is never re-tuned behind whoever made it.

| knob | default | hot | why |
|---|---|---|---|
| `timeout_read_s` | 60 | **1.5** | ~4x the measured p50 (284–503 ms). Long enough for a slow answer, short enough that a dead one does not own the step. |
| `timeout_connect_s` | 5 | **2.0** | Only paid on the first call of a pool. |
| `retries` | 2 | **0** | A retry costs a full round trip *after* the failure is known — past a 1.5 s ceiling the step is gone either way. |
| `hedge` | off | **off** | Measured: it loses. See below. |
| `hedge_after_ms` | 400 | 400 | Above the p95 of a healthy small call, so a duplicate fires on the tail, not the body. Below `MIN_HEDGE_AFTER_MS` (50) it is a `ValueError`: at 0 the twin goes out before the primary can answer, so *every* call is sent twice. |
| `warm_mode` | `head` | `head` | Measured: free and sufficient on both providers. |

`AsyncJevClient` is the same thing on `httpx.AsyncClient` — same request bytes,
same parsing, plus a genuinely concurrent `decide_many`. It needs `httpx` and
says so (`JevConfigError`) instead of faking a loop over threads.

## Warm-up: measured, and the research note was wrong

`python bench/cu_bench.py --provider <p> --warm-bench --warm-repeats 5`, a fresh
client per repeat (a warm-up can only be measured once per pool), medians of 5,
re-run 2026-09-20 with the warm-up's own tokens and cost recorded per row:

| provider | warm-up | warm-up costs | first decision | next decision |
|---|---|---:|---:|---:|
| typesafe | none | – | **693 ms** | 304 ms |
| typesafe | `head` | 583 ms, $0 | **293 ms** | 285 ms |
| typesafe | `decision` | 693 ms, 310 tok, $0.000013 | **266 ms** | 310 ms |
| openrouter | none | – | **342 ms** | 301 ms |
| openrouter | `head` | 136 ms, $0 | **314 ms** | 315 ms |
| openrouter | `decision` | 358 ms, 310 tok, $0.000013 | **304 ms** | 299 ms |

The warm-up decision's price is now a recorded number rather than an estimate:
`warm_tokens_in_median` and `warm_cost_usd_median` in `bench/cu_results.json`
say 310 input tokens and $0.000013 on both providers, which is 310 / 1M x
$0.042 to the cent-millionth. `head` and `none` record a true zero.

The cold penalty is real and provider-specific: **400 ms on the vendor**
(693 → 293 with `HEAD`), 28 ms on OpenRouter (342 → 314). The vendor's figure is
stable across both runs of this bench (399 ms, then 400); OpenRouter's is small
and moves — it was 70 ms in the first run — which is what a one-hop-closer
aggregator with a warm edge looks like. **`HEAD` removes it on both**, costs
nothing, and finishes sooner than a warm-up decision does (`warm+first` 876 ms
vs 960 on the vendor, 450 vs 662 on OpenRouter) — so it is the default, against
the expectation in `docs/research-2026-09-20-jev-cu.md` §3 that the vendor's slow
`/v1/models` made `HEAD` useless. It warms the pool perfectly well; it is only
slow itself.

`warm(mode="decision")` is kept because it exercises what `HEAD` cannot — key,
decisions endpoint, parser — so a 401 surfaces at warm-up, not on the first step.
It is **one attempt** whatever `retries` says: a warm-up that inherited two
retries spent 751 ms failing against a dead endpoint before returning the shrug
its contract promises. The decision it made is kept on
`client.last_warm_decision`, which is where the cost above comes from.

## Hedging: implemented, measured, and off — it never won

The idea: after `hedge_after_ms` with no answer, send the identical request
again on the same warm pool; the first 200 wins, the loser is abandoned. The
result carries `timing_ms["hedged"]`, `timing_ms["winner"]`, and — because the
abandoned leg is still answered and still billed — an estimate of its cost.

**The cost rule.** The loser's `usage` never comes back, but the two requests are
the same bytes, so the winner's `input_tokens` is what the provider billed for
both. `usage["hedge_cost_usd_est"] = input_tokens x $0.042/Mtok`, marked
`hedge_cost_source: "estimated"`, and `Decisions.cost_usd` returns winner +
estimate. `usage["cost"]` still holds the winner alone, so the halves stay
separable. Recording a zero would have made hedging look free in the very ledger
this project uses to claim savings.

**The measurement.** 100 hedged calls (both providers, N=12…240) plus a 60-call
probe at a 320 ms delay: **66 duplicates fired, 0 won**, and the calls that fired
one got *slower*.

| provider | N | fired | won | plain p50 | hedged p50 |
|---|---:|---:|---:|---:|---:|
| typesafe | 240 | 7/10 | 0 | 429 ms | 435 ms |
| openrouter | 120 | 3/10 | 0 | 369 ms | 385 ms |
| openrouter | 240 | 10/10 | 0 | 503 ms | **876 ms** |
| openrouter (probe, 320 ms) | 12 | 16/20 | 0 | 298 ms | **642 ms** |

Two reasons, both structural. The twin goes out on the *same multiplexed HTTP/2
connection to the same provider*, so it cannot escape a slow connection and it
makes the server do the work twice — which delays the leg being waited on. And it
starts `hedge_after_ms` late, so it can only win against a primary that is
**stuck**, not merely slow; the tail at N>=120 is a property of the request (a
23 081-token state is slow on every attempt), not independent noise.

Kept, off: a stuck leg is a real failure mode this sample never produced, and a
hedge over a *separate* connection or a second provider is an experiment nobody
has run yet. `JevClient(hot=True, hedge=True)` turns it on; the loser is costed.

**Three bounds on what "on" can cost you**, each of which was unbounded and is
now not:

* *Hedging does not multiply with retries.* Only attempt 0 hedges; retries go
  single-leg. `retries=2, hedge=True` used to put **six** requests on the wire
  for one decision, and now puts at most `retries + 2`.
* *Abandoned legs cannot starve the pool.* A sync loser is abandoned, not
  cancelled, so it holds one of the pool's 8 connections until it answers or
  times out. At most `MAX_ABANDONED_HEDGE_LEGS` (2) may be outstanding; past
  that the call declines to hedge and records
  `timing_ms["note"] = "hedge_skipped_saturated"`. The pool timeout is also
  stated explicitly (`max(read, connect) + 1`) instead of silently inheriting
  the read timeout, which made a queued request fail before the request ahead of
  it had finished. The async client needs neither: cancellation really does
  return the connection.
* *The estimate is marked in the ledger.* `Record` carries
  `hedge_cost_usd_est` and `hedge_cost_source` beside `cost_usd`, so a row that
  blends a billed figure with an estimated one says so.

## In-loop costs that are not the network

`python bench/cu_bench.py --micro` (offline, free), 60-element UI tree, 6 727
characters — the same state the API bills at 6 041 input tokens with the
four-question bundle:

| what | median | p95 | note |
|---|---:|---:|---|
| `redact_state` | **0.642 ms** | – | 20 reps; 0.699 ms with `redact_emails=True` |
| `Ledger.write` (buffered) | **0.016 ms** | 0.018 ms | 1 000 reps |
| `record_decision` (unbuffered) | 0.616 ms | 0.976 ms | 200 reps |

Redaction is under the 2 ms at which a hash-keyed cache would have been worth its
invalidation bugs, so there is no cache. The ledger is the opposite: an
open/write/close per decision costs more than the whole 0.5 ms `act` budget, so
`jevskill.stats.Ledger` buffers in memory and a background thread flushes every
64 rows or 1 s, and on `close()`/`atexit`. Append-only and ordered — 1 000 rows,
no loss, original order (`tests/test_stats.py::TestBufferedLedger`).

"No loss" now survives a *failed* write as well. The buffer used to be emptied
before the write, so one `OSError` discarded the batch and the background
thread's bare `except` said nothing: 5 accepted rows, 0 on disk, `pending == 0`.
A failed batch goes back at the front of the buffer, `flush_errors` counts it,
and `close()` raises rather than letting a loop end believing its rows are safe.
Each batch is one `os.write` to an `O_APPEND` descriptor — 64 rows is ~22 KB,
which an 8 KiB buffered writer splits into three or more writes that another
process can append between. On POSIX that makes a batch uninterruptible; on
Windows `O_APPEND` is seek-then-write, so the window narrows rather than closing.
Nothing is `fsync`-ed either way.

## Latency vs state size, both providers

`python bench/cu_bench.py --provider {typesafe,openrouter} --n 12,30,60,120,240
--k 10 [--hedge]`, hot client, `HEAD` warm-up, one connection, Poland,
2026-09-20. Every number here is a row in `bench/cu_results.json`.

| provider | N | tokens in | plain p50 | plain p95 | hedged p50 | hedged p95 |
|---|---:|---:|---:|---:|---:|---:|
| typesafe | 12 | 1 723 | 304 | 344 | 295 | 424 |
| typesafe | 30 | 3 308 | 284 | 324 | 298 | 391 |
| typesafe | 60 | 6 041 | 292 | 402 | 309 | 338 |
| typesafe | 120 | 11 588 | 325 | 402 | 343 | 387 |
| typesafe | 240 | 23 081 | 429 | 462 | 435 | 612 |
| openrouter | 12 | 1 723 | 298 | 485 | 337 | 408 |
| openrouter | 30 | 3 308 | 307 | 437 | 310 | 389 |
| openrouter | 60 | 6 041 | 350 | 459 | 345 | 800 |
| openrouter | 120 | 11 588 | 369 | 490 | 385 | 789 |
| openrouter | 240 | 23 081 | 503 | 538 | 876 | 967 |

Latency is flat to about N=30 and then climbs — +125 ms on the vendor and
+205 ms on OpenRouter between N=12 and N=240 — so "adding state is free" holds
only to roughly 6 000 tokens. The vendor is faster from N=30 up, by 23–74 ms
(one hop fewer); at N=12 the two were within 6 ms of each other. Cost scales
with the state: $0.000072 per call at N=12, $0.00097 at N=240.

Answer quality did not degrade: `target` picked `e11` ("Save") at 0.97–0.99
confidence at every size including 241 options, `action` was `click` on 10/10
calls, `goal_reached` 0.04. `stuck` stayed at 0.455–0.605 — the flat, useless
answer already documented in the research; compare two state hashes in code
instead of asking. Zero errors and zero timeouts in the 260 sweep and probe
calls, so the hot path's 1.5 s ceiling was never reached. Sweeps and probes
together: **$0.1111**.

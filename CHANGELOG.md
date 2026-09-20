# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this project's value is its *measurements*, entries that change published
numbers say so explicitly, and superseded figures are named rather than quietly
replaced.

## [Unreleased]

### Planned
- A genuinely ambiguous case for the `shortlist` pattern, so narrowing can be
  demonstrated rather than only unit-tested.
- `jevskill batch` — read a JSONL/CSV of items and run one pattern across them
  with progress and a resumable ledger.
- Per-repository ledger merging (`jevskill stats --merge`).
- MCP wrapper, if harnesses turn out to want one.

## [0.2.0] — 2026-09-20

A measurement fix with a version bump, because it changed published numbers: the
token estimator was under-counting by **2.15×**, which had inflated every
savings figure in the ledger and could let an oversized state past the budget
check silently.

### Fixed
- **`CHARS_PER_TOKEN` was a prose rule of thumb (3.6) applied to logs and code.**
  Measured against the live API, a state estimated at 3 896 tokens really cost
  11 484 at one size and 22 861 at another — under-counting by 2.114–2.196×,
  because log lines and code tokenise far less efficiently than prose. The
  constant is now derived from that measurement (3.6 / 2.148 = **1.68**), with
  the measurement table preserved in the docstring so it can be re-derived.
  Post-fix check: 200 log lines estimate 7 669 vs 7 685 actual — **ratio 1.00**.
- **`cli._baseline_tokens` open-coded its own `/ 3.6` division** while the rest of
  the codebase used the constant, so the ledger and the state-budget check could
  disagree about the size of the same data. It now routes through the single
  `count_tokens` helper. Every `baseline_tokens` figure recorded before this
  release was roughly half its true value.
- Tests no longer hard-code the constant; they derive from it, and one asserts it
  stays conservative (≤ 2.0) so an under-counting regression fails loudly.

### Added
- README rewritten around the benefit a harness user actually feels — context
  growth — rather than the API surface: the 900-line CI log (34 989 tokens into
  the harness vs 320 after Jev), a session context-growth table at 1/3/5/10 large
  logs (**73.8%–95.8% less**), per-session and extrapolated savings at published
  list rates for Sonnet-, GPT-5- and Opus-class models, a head-to-head table
  against a frontier chat model, the nine patterns as reflexes, and an inline cost
  sanity check (50 000 decisions = $0.66).
- An explicit **honesty section** in the README: the flash-lite cost result, Jev's
  published 67.8% accuracy, the failed first REDUCE design, and the fact that
  savings figures are modelled from list prices rather than billed.
- Repository description and 20 GitHub topics for discoverability
  (`agentic-ai`, `claude-code`, `codex`, `context-window`, `token-efficiency`,
  `cost-optimization`, `decision-model`, `jev`, `typesafe`, `openrouter`, …).

### Measured
- Token-reduction basis re-derived: 900 log lines = **34 989 tokens** raw
  (16289 chars-estimate × 2.148) reduced to **320 tokens** = **99.1%**.
- Jev's own cost for the 5-log REDUCE pipeline: **$0.0061** — counted against the
  modelled savings rather than ignored.

### Notes
- The published savings are **modelled**, not billed: they assume published list
  prices and a 12 000-token baseline session. The token reduction (99.1%) and all
  Jev costs are measured. The README labels the difference rather than blurring it.

## [0.1.0] — 2026-09-20

First complete release: CLI, ledger, Skill, reference docs, 211 tests, and a
benchmark suite whose numbers are all reproduced from live API calls.

### Added
- **`jevskill` CLI** with six commands: `doctor`, `plan`, `patterns`, `ask`,
  `outcome`, `stats`. Every command reports its own stage timings; every command
  accepts `--json`.
- **Decisions API client** (`client.py`): warm HTTP/2 connection, precompiled
  question bytes, per-stage timing, typed answers (`noul`/`choice`/`score`) with
  probability distributions intact, exponential-backoff retries, and error hints
  that name the correct action for each documented status.
- **Effectiveness ledger** (`stats.py`): one append-only JSONL row per decision
  carrying pattern, intent, stage timings, tokens, cost, confidence, and the
  context kept out of the LLM. Outcomes are paired later by `decision_id`, which
  is what turns "Jev was fast" into "Jev was right 100% of the time on this
  pattern, at p50 380 ms".
- **Staged timing** (`stages.py`): nine canonical stages from the decision to use
  the skill through to the report that consumes its output.
- **Planning layer** (`orchestrate.py`): a nine-pattern coding palette, keyword
  routing, data profiling with prescriptive advice, token-budget-aware chunking,
  the `next_round` iteration rule, weighted signal combination, and a free
  cost/saving estimate.
- **Question primitives** (`primitives.py`): `noul`/`choice`/`score` with
  validation that refuses the shapes the API rejects, so a malformed question
  costs zero round trips.
- **`skills/jev/SKILL.md`** and four references: `api.md` (exact shapes, every field,
  error codes, cost arithmetic), `patterns.md` (nine patterns with worked
  questions), `prompting.md` (ten rules, each backed by a measurement),
  `benchmarks.md` (every number with method and threats to validity).
- **Benchmark suite** (`bench/run.py`): E1 latency, E2 state-size sensitivity,
  E3 fan-out, E4 REDUCE (two configurations plus the reproducible failure),
  E5 guard accuracy and calibration, E6 LLM contrast, E7 iteration.
- **211 offline tests** covering primitives, client parsing and error handling,
  planning, statistics, the CLI, and staged timing.
- `install.ps1` for wiring the Skill into Claude Code, DSH and generic skill
  directories.

### Measured
Baseline established from 148 decisions / 1 188 questions on 2026-09-20, from a
residential connection in Poland:

| Metric | Value |
|---|---|
| Warm p50 / p95 latency | 325 ms / 427 ms |
| Cost per small decision | $0.0000133 |
| Fan-out (8 questions, one call) | 12.4× faster, 4.03× fewer tokens |
| State grown 22× (324 → 7 020 tokens) | p50 moved 353 → 468 ms |
| REDUCE 900 log lines → 8 (gate all) | 98.8% reduction, 8/14 recall |
| REDUCE 900 log lines → 8 (chunk-first) | 99.6% reduction, 3/14 recall |
| Guard accuracy (80 labelled lines) | 100% of 49 judged |

### Fixed
Bugs found by the benchmark suite and by its own tests, each now covered by a
regression test:

- **`http` stage absorbed the connection handshake.** On a first call, `ask`
  reported 1 189 ms of "http" against a 345 ms decision, because the `warm()`
  HEAD request was inside the same marked region. Connection setup now has its
  own `warm` stage, so the inference number is honest. Measured on this machine:
  `warm()` costs ~74–100 ms and saves ~100 ms on the first real call (437 ms cold
  vs 340 ms warm), so warming remains on by default.
- **Stage timing double-counted.** `Stages` computed a stage as "elapsed minus the
  sum of earlier stages" while also absorbing the client's own `serialize`/`http`/
  `parse` measurements, which sit inside an already-marked region. `doctor`
  reported an `act` stage of **-612 ms** and a 1 047 ms total for a ~45 ms script.
  Replaced with absolute timestamps and derived deltas, plus a delta-only
  "overlay" for client-side timings that is never summed.
- **HTTP 520 was not retried.** A live benchmark run died mid-suite on
  Cloudflare-style 520. The retryable set is now explicit
  (`{0, 429, 500, 502, 503, 504, 520, 522, 524, 529}`) and the whole family is
  parametrically tested.
- **`stages_dict` was read before assignment** in `cmd_ask`, so every `ask` that
  wrote a ledger row raised `UnboundLocalError`.
- **Explicit ledger paths pulled in the global ledger.** `load_records([path])`
  also read `~/.jevskill/ledger.jsonl`, so a report about one project silently
  included every other project's decisions. Explicit paths now mean exactly those
  paths.
- **Pattern keyword collisions.** "big" matched "logs" no better than it matched
  "ambiguous", `route` matched `troubleshoot`, and `triage`/`gate` collided on
  "which"/"whether", silently mislabelling ledger rows. Match order is now part
  of the contract and covered by tests, including one asserting every pattern is
  reachable.
- **`ask` crashed when constructing a single `choice` or `score` question** from
  flags, because `_build_questions` referenced an undefined local.

### Changed
- The REDUCE pattern's published design. The first implementation (chunk-level
  scoring only) reported a 99.1% context reduction while keeping **1 of 8** wanted
  log lines. Two-stage gating with a properly-targeted line question now reaches
  98.8% reduction at 8/14 recall. The failing design is retained behind
  `bench/run.py --legacy-reduce` so the negative result stays reproducible, and
  the prompting defect that caused it is documented in `prompting.md` §1.
- Published figures superseded within the same day: E3 latency speedup was
  **7.4×** in an intermediate run and **12.4×** in the final one; E1 p50 was
  **304 ms**, then **310 ms**, then **325 ms**. Absolute latency varies with the
  network; the ratios are stable.

### Notes
- Latency figures reflect one location. TypeSafe quotes 70–500 ms depending on
  distance from the provider; measure your own before committing to a number.
- E7's narrowing path did not trigger on either test case (both resolved at
  ≥0.92 confidence), so its empirical value is **unproven** at this version.

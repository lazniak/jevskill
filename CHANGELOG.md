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
- **`skill/SKILL.md`** and four references: `api.md` (exact shapes, every field,
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

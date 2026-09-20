# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this project's value is its *measurements*, entries that change published
numbers say so explicitly, and superseded figures are named rather than quietly
replaced.

## [0.7.0] — 2026-09-21

Safety and contract work, prompted by reading three competing Jev skills
(`oomol-lab/skills`, `wuyoscar/jev-skill`, `reachjalil/jevlogs`). Two of them were
doing something this skill was not, and both were right.

### Changed — **breaking-ish: redaction is now on by default**

`ask`, `batch` and the bundled `jev_query.py` now scrub credential-shaped strings
from the state before it is sent, and report what they scrubbed:

```
$ jevskill ask --state "ERROR auth failed; key sk-or-v1-0123…; password=hunter2"
  redacted: openrouter_key, password_param
```

This is a **behaviour change**: the state that leaves the machine is no longer
byte-identical to the state you passed. It is also the right default — this skill
sends your logs, diffs and tickets to a third party by design, and a decision
rarely needs live credentials. `--no-redact` restores the old behaviour, and is
tested.

Patterns cover private keys, OpenRouter/`sk-`/AWS/GitHub/Slack tokens, JWTs,
`Bearer` headers and `password=`/`token=`-style parameters. Email addresses are
**not** redacted by default (`--redact-emails` opts in) because for triage the
address is often the signal. What fired is recorded under `"redactions"` in the
output and in the ledger row, so a redacted run stays interpretable.

This is not a general PII policy, and the docs say so.

### Added — exit code 2 means "the model hesitated"

`ask` and `batch` now return the review contract: `0` decided, `2` at least one
answer needs review, `3` over budget, `1` error. `2` is deliberately distinct from
`1` so a harness can tell *"not confident enough to automate"* from *"the call
failed"* without parsing output. `--needs-review` (per answer) is reported in JSON
and in the ledger.

The rules: a `noul` inside `(1-below, below)`, a `choice` whose top probability is
below the floor **or** whose top-two margin is under `--review-margin`, a `score`
whose reported confidence is below the floor. Defaults `0.75` / `0.10` are
documented as **illustrative heuristics, not calibrated guarantees** — tune them
on held-out data, which is what `outcome` and `stats` exist for.

### Added — keyless choreography in `SKILL.md`

Lifted from `wuyoscar/jev-skill`, which handles the no-key case better than we did:
the skill now tells the agent to check only the *presence* of a key, ask the user
A (get a key) or B (judge it yourself) in their language, and wait. Consent is
per-task, a key appearing later does **not** authorise switching an approved B task
to A, API errors are not consent to simulate, and simulation must be labelled
`mode: agent_simulation` / `jev_called: false` with `probability` and `confidence`
set to `null` rather than invented.

### Added — frontmatter metadata

`allowed-tools: Bash(python:*)`, `metadata.version`, and `metadata.requirements`
(Python 3.9+ and stdlib only, which endpoints, which key variables, that calls are
billed, that redaction is on, and the keyless rule). The description now also says
Jev is **advisory, never an authorization boundary**.

### Changed — `SKILL.md` restructured to stay under 500 lines

It had reached 492 lines with the additions, against a hard 500-line ceiling. The
command reference moved to the new `references/commands.md`; the confidence-threshold
method moved to `references/prompting.md`; the failure-mode table was merged into
the one already in `references/patterns.md`; and §10 is now a "read only the slice
you need" router. Nothing was dropped — the detail moved, and every pointer is
checked to resolve. `AGENTS.md`'s claim that the file was "~300" lines was stale and
is corrected.

### Fixed — stale documentation

`AGENTS.md` said 344 tests (424 now) and "~300 lines" (499 now).

### Verified

- 424 tests green, from 361. New: `test_redact.py`, `test_review.py`, plus CLI
  contract tests, plus drift guards asserting the bundled zero-install script's
  *copies* of the redaction and review rules still match the package.
- Live: state containing a real-shaped key and `password=hunter2` went out with both
  scrubbed and the model still answered (p=0.87); a genuinely borderline item
  (p=0.36) exited `2` while p=0.10 and p=0.77 exited `0`; tightening the floor
  flagged an otherwise-confident answer, proving the band is live and not inert.
- No published number changes.

## [Unreleased]

### Planned
- A genuinely ambiguous case for the `shortlist` pattern, so narrowing can be
  demonstrated rather than only unit-tested.
- Block-level REDUCE, so a gate can keep a parent key together with its values.
  The `yaml_drift` loss in the A/B suite is exactly this gap.
- Per-repository ledger merging (`jevskill stats --merge`).
- Live verification of the vendor endpoint. It is verified by 51 unit tests plus
  endpoint existence, but not by a real call — no TypeSafe key was available.

## [0.6.2] — 2026-09-21

Auditing the recovery handle end to end — the one feature whose entire promise is
that a reduction is reversible — found that it had **no tests at all**, and one
message that undermined the promise it was making.

### Fixed

**`jev_recovery.py --index` reported a kept item as missing.** Asking for an index
that the gate *kept* printed `no rejected items matched`, which reads as data loss.
That is precisely the fear the handle exists to remove, so the one case where the
user most needs reassurance was the case that lied. Requested indices that are not
in the rejected set are now labelled on **stderr**, so `--json` and `--out` stdout
stays purely data:

```
$ jev_recovery.py rc_45ea207c8ca1 --index 0 2 99
note: 2=kept, 99=out of range; only rejected items are retrievable
[     0] INFO  healthcheck ok in 2ms
```

### Added

**`tests/test_recovery.py`** — 14 tests, including a partition check that the kept
and rejected index sets together equal the original input, so "nothing was lost" is
asserted rather than assumed. Four of them fail on 0.6.1.

### Verified

A live REDUCE over 10 log lines (3 genuinely salient) kept exactly those 3 and
issued a handle; the rejected 7 came back with indices `[0,1,3,4,6,7,9]`, leaving
`{2,5,8}` kept — 10 of 10 accounted for. `--list`, `--summary`, `--grep`, `--index`,
`--all`, `--out` and `--json` all round-tripped. No published number changes.

## [0.6.1] — 2026-09-21

A measurement bug in `jevskill doctor`, found by running the released 0.6.0
build rather than by any test. No published number changes: the stage breakdown
quoted in the README comes from `ask`, which was always correct. `doctor` was
wrong, and `doctor` is the command a new user runs first — so the first timing
breakdown anyone saw was misattributed.

### Fixed

**`doctor` charged the connection handshake to `build`.** `stages.mark("build")`
sat *inside* the `with JevClient(config)` block, so the delta it recorded was
`JevClient` construction plus `__enter__` — TCP and TLS setup — rather than query
construction. Doctor's payload is a static two-key probe, so its honest `build`
is ~0 ms:

```
before                              after
  build           814.7 ms          build             0.0 ms
  (no warm stage)                   warm            796.4 ms
  http            748.5 ms          http            564.8 ms
```

Three consequences, all fixed by moving `build` before the client exists and
marking the `warm` stage that `STAGE_ORDER` already declared:

- the largest stage in the output was labelled "query construction" when it was
  network setup;
- `warm` never appeared at all, so the handshake a harness pays once was not
  separated from inference;
- `http` included the 81.6 ms warm-up, so the reported inference time exceeded
  the real decision latency. `http` now equals the client's own `client_total`
  (564.8 ms against 564.7 ms), which is the check that proves the split.

This is the same defect that was fixed in `ask` in 0.4.0 — *"1189 ms of `http`
against a 345 ms decision"* — fixed there but not here, because `doctor` had no
test. `t_decision` was also marked after configuration resolution instead of
before it, so the config lookup was reported as the decision instant.

### Added
- `TestDoctorStages`, three regression tests that fail on 0.6.0. They need a fake
  client whose construction and warm-up cost real milliseconds: with an instant
  fake the misattribution is arithmetically invisible, which is how it survived.

## [0.6.0] — 2026-09-20

The large-dataset path. The objective named *"large datasets"* as the case that
matters most, and every command until now handled one decision at a time.

### Added

**`jevskill batch`** — apply one set of questions to many items, and report the
token saving the API actually reported rather than a projection.

```bash
jevskill batch build.log --text-key line \
  --question-type choice --name owner \
  --instructions 'Which team should own `item`?' \
  --options backend frontend infra unclear \
  --intent ci-triage --out triaged.jsonl
```

Measured on 60 log lines, half genuinely salient, both strategies on identical
items (`python bench/batch_bench.py`):

| | windowed (default) | per-item |
|---|---:|---:|
| Input tokens | **10,674** | 23,288 |
| Cost | **$0.000448** | $0.000978 |
| Wall clock | **667 ms** | 7,979 ms |
| API calls | **8** | 60 |
| Salient lines found | **30/30** | 29/30 |

**2.18× fewer input tokens and 12× faster, with no accuracy cost.** Windowed found
every salient line while one per-item call returned no answer at all.

Two strategies: ``windowed`` puts several items in one call; ``per-item`` isolates
each decision at higher cost, for when a long or ambiguous item must not influence
a neighbour. Both write ledger rows, so ``stats`` and ``advice`` see them.

**Items from JSONL, a JSON array, or a plain line-per-item file.** Anything that is
not JSON is treated as a string, because the commonest input in practice is a file
of log lines.

**The `item` reference is rewritten mechanically.** `_retarget` rewrites a
backticked `` `item` `` (and `` `item.path` ``) to the item's actual state key in
instructions, criteria, and structured objects. This is not cosmetic: batching puts
many items in one state, which makes the *"question does not name its value"*
failure **more** likely, and that failure is silent — it returns a plausible number
for every candidate. The rewrite cannot be forgotten because it is not manual.

**`--measure-baseline`** (on by default) makes one real probe call to measure the
one-item-per-call cost, then scales it. The obvious alternative — summing the
items' own tokens — is wrong in the direction that flatters batching: a per-item
call also pays for question text and per-request framing, which on small items
exceeds the item itself. A first cut compared a batched payload against item text
alone and printed **"-313% fewer"** on a run that was in fact 61% cheaper.

### Fixed
- `JevClient.decide` accepts a per-call `timeout_s`. Batches send larger payloads
  than a single decision, so the read timeout must be raisable without changing the
  default that suits small calls.
- `JevClient._post` honours that timeout on both the httpx and the stdlib
  transport paths.

### Documented
- `SKILL.md` §5 gains **batch** as a fourth reusable shape, alongside cascade,
  reduce and fan-out — with the caveat that this is the shape where the
  name-the-value rule bites hardest.
- README documents the command with the measured table.

### Tests
51 new tests. `tests/test_jevtask.py` (36) covers the template rewrite in every
position it can appear, the loader's three formats, both strategies, attribution of
answers to the right item, and that **a failed call loses only its own items**.
`tests/test_cli.py` (15) covers the command surface, ledger rows, `--no-ledger`,
and the measured baseline. 293 → 344.

## [0.5.0] — 2026-09-20

The layer the original objective asked for and the project did not have: not only
using the model and measuring it, but **learning when using it pays off**.

### Added

**`jevskill advice`** — reads the effectiveness ledger and says what to do about
it, per pattern and per intent. `stats` reported numbers; nothing turned them into
decisions. Six verdicts, each carrying the numbers that produced it:

| Verdict | Means |
|---|---|
| `worth_it` | saves tokens at measured accuracy — keep doing this |
| `not_worth_it` | the state is too small; the round trip costs more than it saves |
| `escalate` | cheap but too often wrong — gate on confidence, escalate the rest |
| `no_baseline` | nothing to compare against; pass a `--state` so it can size the data |
| `marginal` | saves tokens; accuracy not yet established |
| `unproven` | the saving is real but no outcomes are paired yet |

`not_worth_it` is evaluated **before** accuracy on purpose: "this replaces nothing"
is more actionable than an accuracy figure on a call that should not be happening.
A state that costs less to read than the decision costs to make has not been
optimised, it has been ritualised.

Nothing new had to be collected for this. Baseline tokens, input tokens, cost,
confidence and paired outcomes were already in every ledger row; the *reading*
layer was missing.

**The worth-it comparison is token-for-token**, and getting there took three
attempts, each wrong in a different way:

1. A percentage against the full LLM counterfactual. The 350-token scaffolding in
   `DEFAULT_BASELINE` is a *constant*, so it dominated the ratio and every state
   reported a ~99% win — `not_worth_it` was unreachable.
2. The state priced at a frontier model's $3/Mtok against Jev's $0.042/Mtok. Jev is
   ~71× cheaper per token, so its cost could never look significant. Every state
   passed again.
3. Costs compared per decision, across a pipeline that records many decisions per
   document. A per-chunk cost ratio compared a chunk-sized read against a
   document-sized baseline.

The final form compares **Jev's own billed input tokens against the tokens it
replaced**. Both sides are the same currency, so no price, no scaffolding and no
unit conversion can distort it. The crossover sits at roughly 310 tokens of state:
below that, a decision reads more than it replaces.

`data_cost_at_jev_rates_usd` and `baseline_data_cost_usd` are recorded alongside
the full baseline so the intermediate numbers stay inspectable, and rows written
before the field existed are handled by recomputing from `baseline_tokens` rather
than being reported as "never sized" — which is what happened on first
introduction and made the command useless on exactly the data it was built to
learn from.

**Granularity caveat, stated in the command's own help.** The verdict is computed
from *recorded decisions*, and a REDUCE run records one decision per chunk, so its
ratio is chunk-against-chunk. That can look unfavourable even when the pipeline
replaces a large document with a short list. Judge the pipeline from pipeline
totals, not from per-chunk rows.

Thresholds (85% accuracy, 20% minimum saving, 5 judged decisions before accuracy
counts) live in `stats.ADVICE` and are printed with every report, because they are
policy rather than fact and a reader must be able to disagree with them.

`--limit-unproven` caps the unproven rows, defaulting to 5: on a real ledger most
intents are unproven, and a command that prints a hundred near-identical lines is
one nobody reads.

### Fixed
- **`worth_it` sorted last.** The first implementation ordered verdicts
  worst-first, which buried the one finding a reader most wants — where this thing
  is actually paying off. Confirmed wins now lead, followed by confirmed waste,
  with `unproven` last because it is the least actionable.

### Documented
- `SKILL.md` §6 gained "Ask the ledger what to do next", including the advice
  output and the note that `STOP` is the verdict to look for first.
- README documents the command as the payoff of the measuring loop.

### Tests
23 new tests in `tests/test_advice.py` pin the policy boundaries explicitly: a
saving too small to matter is `not_worth_it` **even at 100% accuracy**, an
unmeasured intent is never a win, `escalated` outcomes do not count as accuracy,
and the unproven output is capped and says how many were withheld. 266 → 289.

## [0.4.0] — 2026-09-20

Support for the vendor's own endpoint, so the skill is not tied to one gateway.

### Added

**Both official endpoints.** The same model is served through OpenRouter and
through TypeSafe's first-party API. `--provider {openrouter,typesafe}` selects one;
unanchored, the provider is detected from the key shape (`sk-or-…` is OpenRouter).

| | OpenRouter | TypeSafe |
|---|---|---|
| Endpoint | `/api/alpha/decisions` | `/v1/systemone` |
| Model field | `typesafe/jev-1.13` | `jev-latest` |
| Context | 32,000 | **64,000** (32,000 for state + longest question) |
| Choice options | — | documented max 255 |
| Score levels | — | documented 2–10 |
| `usage.cost` | reported | **absent** |
| Price | $0.042/Mtok | **identical** |

Verified live: both `/v1/systemone` and `/v1/models` exist and return a structured
`401` for an invalid key (`{"detail": {"error_type": "authentication_error", …}}`).

Provider choice resolves in this order: `--provider`, then `JEVSKILL_PROVIDER`,
then a `"provider"` field in `~/.jevskill/config.json`, then the key shape, else
OpenRouter.

**Confidence semantics, sharpened against the vendor's own guidance.** Three
clarifications that change how answers should be read, now in `api.md`:

- `confidence` measures **distribution concentration**, not correctness, and is not
  permission to act. Several acceptable alternatives also spread probability, so low
  confidence on a harmless preference choice is expected.
- A `noul` near 0.5 means "as likely yes as no", **not** medium intensity. Reading a
  0.5 gate as a middling severity is wrong; it is a coin flip, and the response is
  to escalate or supply more state.
- A `Choice` **cannot select a value you never offered**, so check *candidate
  coverage* first. An omitted value is unreachable and the model will confidently
  pick the nearest option it was given — the same failure as a missing `unclear`
  option, one level up.

**External corroboration** (`benchmarks.md`). The vendor's parallel-questions
cookbook reports 13 questions in one call as **12.2× cheaper and 10.0× faster with
no change in answers**; this repo measured **12.4× faster and 4.03× fewer tokens**
on 8 questions. Different workloads and providers, same effect, near-identical
latency multiple; the token multiple differs because their state is large and
shared. The vendor's own [Jev 1.13 jaggedness][jagged] page — documenting this model
version's known weaknesses — is also linked.

[jagged]: https://docs.typesafe.ai/model-jaggedness/jev-1.13

**The vendor's own agent skill**, documented as *complementary* rather than
competing: theirs teaches an agent to build applications with Jev via docs and
cookbook routing; this one uses Jev during a session with a running CLI, reversible
REDUCE and a measured ledger. README and `api.md` both recommend installing both.

### Fixed
- **A missing `cost` field would have silently zeroed the ledger.** TypeSafe
  returns `input_tokens` and `output_tokens` and no `cost`. Recording that as `0`
  would make every vendor-endpoint decision look free and inflate the reported
  savings — the exact class of error this project exists to prevent. The client
  now computes it from the documented $0.042/Mtok rate and marks the provenance
  with `usage.cost_source` = `"computed"` (or `"provider"` when reported).
- **Model names are translated per provider**, both directions. Passing
  `typesafe/jev-1.13` to the vendor endpoint is a 404 and `jev-latest` to
  OpenRouter is a 422; that is the easiest mistake to make when switching, so it
  is handled rather than documented-and-hoped.
- **Payload-size errors now name the provider's actual ceiling** — 32,000 on
  OpenRouter, 64,000 (and 32,000 for state plus the longest question) on TypeSafe.
  A caller inside an error handler cannot look that up.
- **422 is handled as TypeSafe's equivalent of a 400**: validation failure, not
  retryable, and the hint says so. Previously only OpenRouter's 400 was covered.
- **Provider-scoped key lookup.** Asking for one provider no longer returns the
  other's key; a machine holding both is the normal case, and picking the wrong one
  sends traffic to the wrong endpoint.
- Unknown provider names are rejected loudly rather than falling back silently.
- **A documented invocation that argparse rejects.** The docs showed
  `jevskill --provider typesafe doctor`; the flag must follow the subcommand
  (`jevskill doctor --provider typesafe`). Corrected in README, `SKILL.md` and
  `api.md`.

### Changed
- `scripts/jev_query.py` (the bundled zero-install caller) gained the same
  `--provider` support, so the dependency-free path is not OpenRouter-only.
- `SKILL.md` §0 documents both endpoints and when to prefer each.
- `references/api.md` opens with a full provider delta table, including error-code
  differences and the resolved 32K/64K context question.

### Notes
- Going direct to the vendor is **not** a cost saving: the rate is identical. It
  buys double the context, documented rate limits and option ceilings, at the cost
  of gatekept access.
- The vendor endpoint was probed with an invalid key only. No valid TypeSafe key
  was available, so the vendor path is verified by unit tests (51 of them) plus
  endpoint existence, **not** by a live end-to-end call. The OpenRouter path remains
  the one exercised live.
- Measurements with **no external replication** are named as such: the REDUCE recall
  figures (`8/14` in the A/B suite, `3/14` pre-filter) and the guard accuracy run.


## [0.3.0] — 2026-09-20

Distribution and reversibility: the skill installs into 20+ agents with one
command, reduction can be undone, and the repository finally contains the
experiment that answers "should I use this?"

### Added

**One-command install into 20+ agents.** `npx skills add lazniak/jevskill -g`
now works, verified against the live registry:

```
Found 1 skill: jev
universal: Antigravity, Cline, Codex, Cursor, Gemini CLI +15 more
symlinked: Claude Code, Kiro CLI, Qwen Code, Windsurf, ZCode
```

Also `.claude-plugin/marketplace.json`, so Claude Code users can
`/plugin marketplace add lazniak/jevskill`.

**Bundled zero-install scripts.** The skill carries its own dependency-free
caller, so it is useful the moment it is installed, with no `pip install`:

| Script | What it does |
|---|---|
| `scripts/jev_query.py` | decisions (`noul`/`choice`/`score`) and reversible REDUCE |
| `scripts/jev_recovery.py` | read back everything REDUCE rejected |
| `scripts/jev.py` | delegates to the full CLI when the package is installed |

**Reversible REDUCE.** `--reduce` stores every rejected item locally and returns a
handle. Measured on the 900-line log benchmark: the gate kept 8 of 14 salient
lines, and recovery surfaced the **6 it had missed** — a 57%-recall gate becomes a
100%-recoverable pipeline. This closes the one structural advantage mechanical
compressors had over a decision model.

**A/B evaluation** (`bench/ab.py`). Six workloads with checkable oracles, three
arms (direct / deterministic filter / Jev), identical answering model,
provider-reported token counts, 3 runs per arm:

| Workload | direct | filter | Jev | Token change |
|---|---:|---:|---:|---:|
| log_needle | 3/3 | 3/3 | 3/3 | −99.5% |
| csv_outlier | 3/3 | 3/3 | 3/3 | −98.9% |
| test_output | 3/3 | 3/3 | 3/3 | −99.1% |
| json_drift | **0/3** | 2/3 | **3/3** | −99.6% |
| yaml_drift | **0/3** | **3/3** | **0/3** | −99.5% |
| html_alert | 3/3 | 3/3 | 3/3 | −98.8% |
| **TOTAL** | **12/18** | **17/18** | **15/18** | **−99.3%** |

Tokens 113,352 → 801. Accuracy rose from 12/18 to 15/18.

**`AGENTS.md`** — the conventions that keep this repo honest: every published
number reproducible, negative results stay published, name the target value, fan
out instead of looping, one chars-per-token constant.

**CI** (`.github/workflows/tests.yml`) — pytest on Python 3.9/3.11/3.13, plus a
smoke test that the bundled skill scripts still work without the package.

### Changed
- **Skill moved from `skill/` to `skills/jev/`.** The Agent Skills convention is a
  root-level `skills/<name>/` with `<name>` matching the frontmatter `name`. The
  old layout could not be mapped by any installer. `install.ps1` and every
  cross-reference updated.
- Table of contents added to `references/patterns.md` (328 lines; the guidance
  suggests one past 300).
- `SKILL.md` documents the zero-install path, the recovery workflow, and the
  package-vs-bundled boundary.

### Fixed
Two defects in the A/B harness, both of which would have produced a flattering
and false result:

- **The reduce gate was handed the model's own question**, so it was told to look
  for "payment gateway timeout" and then credited with finding the needle on its
  own merit. The gate now receives an **answer-neutral filter description**, and
  the deterministic control arm is built from that same description so both arms
  get an equally fair brief.
- **`_yaml_fixture` generated only 420 flags while targeting `flag_0512`**, so the
  control arm had no target to find and would have been scored as a genuine
  failure. A guard assertion now makes that class of bug impossible to miss.

Also: `scripts/jev.py` must not be named `jevskill.py` — a script's own directory
leads `sys.path`, so the name shadows the `jevskill` package and the import
resolves to the script itself. The first version made exactly this mistake.

### Measured
- A/B: **−99.3%** model input tokens (113,352 → 801), accuracy 12/18 → 15/18.
- Reversible REDUCE: 900 items → 8 kept (−98.8%), 892 recoverable, and the 6
  missed salient lines were recovered.
- `json_drift` is the clearest single case for the technique: direct 0/3, Jev 3/3.
- `yaml_drift` is the clearest case against it: Jev 0/3, deterministic filter 3/3.

### Notes
- The published microbenchmark figures in Part 2 of `benchmarks.md` are unchanged
  from 0.2.0.
- The A/B percentages are **not** comparable to other token-reduction tools'
  published numbers: different fixtures, model and harness. The method is
  comparable; the numbers are not.

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

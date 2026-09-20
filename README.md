<div align="center">

# jevskill

**Your agent's context window is full of logs it didn't need to read.**

A Skill that makes Claude Code, Codex, DSH and 20+ other agents stop burning
tokens on decisions — and start making them for **$0.000013** in **325 ms**.

**A/B tested: 99.3% fewer input tokens, and accuracy went *up* (12/18 → 15/18).**

[![tests](https://img.shields.io/badge/tests-344%20passing-brightgreen)](#-does-it-actually-help-ab-tested)
[![A/B](https://img.shields.io/badge/A%2FB-99.3%25%20fewer%20tokens-blue)](#-does-it-actually-help-ab-tested)
[![cost](https://img.shields.io/badge/decision-%240.000013-success)](#-cost-per-decision)
[![license](https://img.shields.io/badge/license-MIT-informational)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)

```bash
npx skills add lazniak/jevskill -g     # nothing to compile, no account
```

</div>

---

## The problem nobody budgets for

You paste a failing CI log into your agent. 900 lines. **~35,000 tokens.**

Those tokens don't just cost money once. They sit in the context window **for the
rest of the session** — re-sent on every single turn, until your agent hits the
limit, autocompacts, and forgets what it was doing.

```
900-line CI log        ████████████████████████████████████████  34,989 tokens
JEV shortlist (3 lines) █                                              320 tokens
                                                                   ↑ 99.1% gone
```

`jevskill` asks **Jev** — TypeSafe's *System One* decision model — to find the 3
lines that matter. Your expensive model never sees the other 897.

```
Without jevskill   5 logs into the session   ████████████████████████  186,945 tok
With jevskill      5 logs into the session   ██                        13,600 tok
                                                                  ↓ 92.7% less
```

## 🚀 The slupek (74–96% less context growth)

Context growth across a real session that pulls in large tool output. Baseline
session = 12,000 tokens; each log = 34,989 tokens raw vs 320 tokens after JEV.

| Big logs consumed | Without jevskill | With jevskill | Context saved |
|---|---:|---:|---:|
| 1 | ████████████████ 46,989 | ████ 12,320 | **73.8%** |
| 3 | ████████████████████████████████████████ 116,967 | ████ 12,960 | **88.9%** |
| 5 | ████████████████████████████████████████████████████████████████ 186,945 | █████ 13,600 | **92.7%** |
| 10 | ████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████████ 361,890 | █████ 15,200 | **95.8%** |

> These are **modelled** from a stated baseline and list prices. The 99.3% figure
> in the A/B section below is **measured** — provider-reported tokens. Trust that
> one more.

## 💸 What that saves per session

Same session, priced at published list rates. JEV's whole pipeline for 5 logs
costs **$0.0061** — already subtracted below.

| Main model | Without JEV | With JEV | **You save** |
|---|---:|---:|---:|
| Claude Sonnet-class ($3/M) | $0.5608 | $0.0408 | **$0.5200** |
| GPT-5-class ($1.25/M) | $0.2337 | $0.0170 | **$0.2167** |
| Claude Opus-class ($15/M) | $2.8042 | $0.2040 | **$2.6002** |

Extrapolated to **20 sessions/day** — roughly one focused agent session per
working hour:

| | Sonnet-class | GPT-5-class | Opus-class |
|---|---:|---:|---:|
| Daily | **$10.40** | **$4.33** | **$51.99** |
| Monthly | **$312** | **$130** | **$1,560** |
| Yearly | **$3,795** | **$1,581** | **$18,977** |

> 💡 **The real win isn't the money — it's that your agent stops hitting the
> context limit.** Fewer tokens in context means fewer autocompactions, fewer
> "I forgot what we were doing", and longer sessions before quality degrades.

---

## ⚡ What one decision costs

| | Jev | Frontier chat model |
|---|---:|---:|
| Latency (p50, measured) | **325 ms** | 2,000–6,000 ms |
| Cost per decision | **$0.000013** | ~$0.001+ |
| Output to parse | **none — typed decision** | JSON that sometimes breaks |
| Malformed output | **impossible** | retry logic you maintain |
| Probability distribution | **✅ full distribution** | ❌ one label |
| Output billing | **$0 — generates nothing** | per token written |

**8 questions in one call = 288 ms.** The same 8 questions asked separately =
3,564 ms. That's **12.4× faster and 4.03× fewer tokens** — because questions
sharing one state run in parallel.

---

## 🧠 What the hell is Jev?

A model that **doesn't write text**. You give it data plus typed questions; it
returns typed answers with real probability distributions.

Three primitives, and that's the whole language:

| You need | Primitive | Returns |
|---|---|---|
| yes / no | `noul` | `P(true)` — a float, you pick the threshold |
| one of N things | `choice` | winner + **full distribution** + confidence |
| how much on a scale | `score` | weighted mean (e.g. `1.4`) + per-level probabilities |

If the answer is **one of N things you can list up front** → Jev does it in
325 ms for $0.000013.
If the answer is prose, code, or an open-ended set → use your LLM.

**That's the entire decision rule.** And `jevskill plan` enforces it for free.

---

## 🔥 Install (30 seconds)

### One command, into 20+ agents

```bash
npx skills add lazniak/jevskill -g
```

That installs the skill for **Claude Code, Codex, Cursor, Gemini CLI, Windsurf,
Cline, Copilot, OpenCode, Amp, Goose, Zed, VS Code** and more, via the open
[Agent Skills](https://agentskills.io) registry. No account, no key, nothing to
compile — the bundled scripts use only the Python standard library.

Claude Code plugin marketplace users can instead do:

```bash
/plugin marketplace add lazniak/jevskill
/plugin install jev@jevskill
```

### Or from source

```bash
git clone https://github.com/lazniak/jevskill && cd jevskill
python -m pip install -e ".[fast]"          # the measurement half (ledger, stats)
export OPENROUTER_API_KEY=sk-or-v1-...      # Windows: setx OPENROUTER_API_KEY "..."

jevskill doctor          # verifies key, endpoint, latency, live cost
```

### Both official endpoints — use whichever key you have

The model is served through OpenRouter **and** through TypeSafe's own API. Same
price. The skill detects which one your key belongs to, or you can say:

```bash
jevskill doctor                        # auto-detect from the key shape
jevskill doctor --provider typesafe    # POST api.typesafe.ai/v1/systemone
jevskill doctor --provider openrouter  # POST openrouter.ai/api/alpha/decisions
```

| | OpenRouter | TypeSafe (vendor) |
|---|---|---|
| Model field | `typesafe/jev-1.13` | `jev-latest` |
| Key variable | `OPENROUTER_API_KEY` | `TYPESAFE_API_KEY` |
| Context | 32,000 tokens | **64,000** (32,000 for state + longest question) |
| Choice options | — | documented max 255 |
| Score levels | — | documented 2–10 |
| `usage.cost` | ✅ reported | ❌ absent |
| Price | $0.042 / Mtok | **identical** |
| Access | immediate with credits | waitlist, keys in batches |

Model names are **translated automatically** in both directions, because passing
`typesafe/jev-1.13` to the vendor endpoint is a 404 and passing `jev-latest` to
OpenRouter is a 422.

Two things worth knowing about the vendor endpoint:

- **It returns no `cost` field.** Recording that as zero would make every decision
  look free and inflate the savings, so the client computes it from the published
  rate and marks the provenance (`cost_source: "computed"`).
- **`jev-latest` is a moving alias.** The response's `model` field reports the
  versioned id that answered — log it, and pin a version if you have tuned a
  confidence threshold against it.

Full delta table, including error codes: [`api.md`](skills/jev/references/api.md).

### Ask a decision immediately (zero install)

The skill carries its own stdlib-only caller, so this works right after
`npx skills add`:

```bash
cd ~/.agents/skills/jev

# a gate
python scripts/jev_query.py --state-file diff.txt \
  --question-type noul --name breaks_api \
  --instructions "Does the diff change a public API signature?"

# reduce 900 log lines to the 8 that matter, reversibly
python scripts/jev_query.py --state-file build.log --reduce \
  --instructions 'Does the item at `L{i}` report a problem worth investigating?'
# REDUCE: 900 items -> 8 kept (98.8% fewer tokens: 34906 -> 414)
#   rejected 892 items stored; retrieve with:
#   python scripts/jev_recovery.py rc_92110309381a --grep <pattern>
```

Pass `--json` for machine-readable output.

Then your agent just... uses it. `SKILL.md` teaches it when to reach for Jev and
— just as importantly — when **not** to.

---

## 🧪 Does it actually help? A/B tested

Six workloads. One question each with a **checkable answer**. Same answering model
in every arm, so the only variable is what the model reads. Token counts are the
**provider's own reported usage**. 3 runs per arm.

| Workload | Tokens direct | Tokens **with Jev** | direct | grep filter | **with Jev** |
|---|---:|---:|---:|---:|---:|
| Log needle in haystack | 27,472 | 126 | 3/3 | 3/3 | **3/3** |
| CSV outlier hunt | 12,065 | 221 | 3/3 | 3/3 | **3/3** |
| Test-output failure | 19,882 | 142 | 3/3 | 3/3 | **3/3** |
| Deployment JSON drift | 23,888 | 83 | **0/3** | 2/3 | **3/3** |
| YAML config drift | 25,133 | 83 | **0/3** | **3/3** | 0/3 |
| Dashboard HTML alert | 22,064 | 147 | 3/3 | 3/3 | **3/3** |
| **TOTAL** | **113,352** | **801** | **12/18** | **17/18** | **15/18** |

**99.3% fewer tokens — and accuracy went *up*, from 12/18 to 15/18.**

Look at the JSON row: handed 23,888 tokens of service definitions, the model
found the one bad image tag **zero times out of three**. Given 83 tokens of
shortlist, it found it every time. Reduction is not only a cost optimisation —
it is a **signal-to-noise improvement**.

### And here's where it loses

**The plain regex filter beat both arms (17/18).** On YAML config drift Jev scored
**0/3** where a 20-line structural pass scored 3/3.

The post-mortem is in the repo because a failure without an explanation is just an
anecdote: Jev **correctly** flagged `prod: true` at p=0.92, but the test asks for
the *flag name*, which sits on the line **above**. A line-level gate is the wrong
granularity when the unit of meaning is a block — and the filter description asked
about a value when the answer was a name.

So, plainly:

1. **If a regex or a structural pass can answer it, use that.** Free, instant,
   deterministic.
2. **Jev earns its place on semantic anomalies** — "looks non-release, unusual,
   out of pattern" — which no keyword expresses. That is exactly the `json_drift`
   row: Jev 3/3, filter 2/3, direct 0/3.
3. **Gate blocks, not lines, when the answer spans lines.**

`n=3` per cell and one cheap answering model: directional, not statistical. Full
method and threats to validity in
[`benchmarks.md`](skills/jev/references/benchmarks.md).

---

## ♻️ Reversible by default — a reduction you can undo

Most context reduction is a **bet**: keep 1% and hope it was the right 1%. Jev
does not have to work that way. `--reduce` writes every rejected item to a local
store and hands back a handle.

```
REDUCE: 900 items -> 8 kept (98.8% fewer tokens: 34906 -> 414)
  rejected 892 items stored; retrieve with:
  python scripts/jev_recovery.py rc_92110309381a --grep <pattern>
```

```bash
python scripts/jev_recovery.py rc_92110309381a --summary   # what was dropped
python scripts/jev_recovery.py rc_92110309381a --grep "WARN|ERROR|FATAL"
python scripts/jev_recovery.py rc_92110309381a --index 42 43
```

**Measured, not asserted:** on the 900-line log benchmark the gate kept 8 of 14
salient lines — and recovery surfaced the **6 it had missed**. A 57%-recall gate
becomes a **100%-recoverable** pipeline. The rejected bytes never leave your
machine.

---

## 🎯 The 9 patterns (your new reflexes)

| Pattern | Say it when… | Coding example |
|---|---|---|
| **gate** | you need a yes/no before something expensive | "Does this diff break a public API?" |
| **triage** | each item needs one label | "Which module owns this failing test?" |
| **reduce** | there's too much data | "Which 8 of these 900 log lines matter?" |
| **rank** | you need an order | "Rank these 12 lint findings by impact" |
| **route** | effort is a choice | "One-line fix or architectural change?" |
| **verify** | you produced something | "Does this test actually exercise the bug?" |
| **guard** | an action is irreversible | "Does this touch anything outside the repo?" |
| **shortlist** | the top two are close | 0.51 vs 0.44 → re-ask over just those |
| **extract** | messy text → fields | stack trace → exception type, frame |

```bash
jevskill patterns            # the full palette, free
jevskill plan "there are too many log lines" --state-file build.log   # free go/no-go
```

---

## 📦 Large datasets — `jevskill batch`

The case that matters most: N things to decide about, not one thing to decide
repeatedly. Triage a build log, label a backlog, classify failing tests, filter
candidates.

```bash
jevskill batch build.log --text-key line \
  --question-type choice --name owner \
  --instructions 'Which team should own `item`?' \
  --options backend frontend infra unclear \
  --intent ci-triage --out triaged.jsonl
```

Measured on 60 log lines, half genuinely salient, both strategies on identical
items (`python bench/batch_bench.py`):

| | **windowed** (default) | per-item |
|---|---:|---:|
| Input tokens | **10,674** | 23,288 |
| Cost | **$0.000448** | $0.000978 |
| Wall clock | **667 ms** | 7,979 ms |
| API calls | **8** | 60 |
| Salient lines found | **30/30** | 29/30 |

**2.18× fewer input tokens and 12× faster — with no accuracy cost.** Windowed found
every salient line while one per-item call returned no answer at all.

Three things worth knowing:

- **Name the item as `` `item` `` and the tool rewrites it per item.** Not
  cosmetic: batching puts many items in one state, which makes the "question does
  not name its value" failure *more* likely — and that failure is silent. The
  rewrite is mechanical so it cannot be forgotten.
- **`--strategy per-item` trades cost for isolation.** One item per call, so a long
  or ambiguous item cannot influence a neighbour.
- **Items come from JSONL, a JSON array, or a plain line-per-item file.** The most
  common input in practice is a file of log lines, so anything that is not JSON is
  treated as a string.

---

## 🧪 Measured, not marketed

**148 decisions, 1,188 questions, live API.** Every number below came from
`bench/run.py`, which you can run yourself.

| Experiment | Result |
|---|---|
| Warm p50 / p95 latency | **325 ms / 427 ms** |
| Cost per small decision | **$0.0000133** |
| Fan-out: 8 questions, 1 call vs 8 calls | **12.4× faster, 4.03× fewer tokens** |
| State grown 22× (324 → 7,020 tokens) | p50 moved only **353 → 468 ms** |
| REDUCE: 900 log lines → 8 | **98.8% reduction, 8/14 found** |
| Guard accuracy (80 labelled lines) | **100%** of 49 judged |
| Calibration @ P≈0.03 | actual rate **0%** |
| Calibration @ P≈0.98 | actual rate **100%** |

### Honesty section (read this before you trust the tables above)

A repo that only reports wins isn't measuring. So:

* **A small chat model was cheaper than Jev on a trivial single call** — $0.0000057
  vs $0.0000133. Jev's case is *structure*, *fan-out*, and *keeping 35k tokens out
  of context*. Not price-per-token on one tiny question.
* **Jev is not more accurate than a frontier model.** On TypeSafe's own published
  table it scores 67.8%, below GPT-5.6 Sol (74.1%). Use it as a **signal
  generator**, not an oracle.
* **The first REDUCE design failed**: 99% context reduction while keeping **1 of
  8** wanted lines. The failing design is preserved behind
  `bench/run.py --legacy-reduce` so you can reproduce it. The fix is documented in
  `skills/jev/references/prompting.md`.
* **Pre-filtering chunks is a cost/recall trade**, not a free win: 2.1× cheaper,
  5 fewer lines found. Pick by what a miss costs you.
* **The `shortlist` narrowing loop never fired** in testing — both cases resolved
  at ≥0.92 confidence. Logic tested, empirical value **unproven**.
* Savings figures are **modelled** from list prices + a stated 12k baseline, not
  billed. The token reduction (99.1%) and costs are measured.

---

## 🪤 The trap that cost this repo a whole benchmark run

Asking a question that doesn't **name the value it's about**:

| How the question was asked | The real `ERROR` | Routine noise |
|---|---:|---:|
| "Does this single log line report a problem?" | 0.72 | 0.75 |
| Same, but pointing at `` `L0` `` | **0.96** | **0.03** |

The first version gives numbers that *look* like answers and discriminate
nothing. Backticked paths are worth **23 points of probability mass**.

---

## 💰 Cost sanity check

At the measured **$0.0000133** per decision, 50,000 decisions cost **$0.66**.

So the question is never *"can we afford a decision?"* — it's *"why is my agent
making this one by hand, or paying 400× more for a chat model to guess?"*

---

## 🚫 When NOT to use this

Your agent will know, because `jevskill plan` says no for free:

```bash
$ jevskill plan "write documentation for this module"
DO NOT USE JEV (prose)
  The answer is text, code, or an unbounded set. Jev emits no text…
```

* Writing or refactoring code → **LLM**
* Summaries, explanations, commit messages → **LLM**
* Anything `grep` or a regex can answer → **neither**, just run the command
* Open-ended ("find all possible…") → **LLM**
* Irreversible actions → Jev is a **tripwire, not an authorisation**. Confirm with
  a human anyway.

---

## 📊 It measures itself

Every call lands in a local ledger with stage timings, tokens, cost, confidence —
and, once you pair it with reality, whether it was **right**:

```bash
jevskill ask --state-file failures.txt --questions @q.json --intent "ci-triage"
jevskill outcome d_1a2b3c4d5e6f correct    # what actually happened
jevskill stats                             # what happened, measured
jevskill advice                            # what to DO about it
```

### `jevskill advice` — the part that learns when JEV pays off

`stats` reports numbers. `advice` turns them into decisions, per pattern and per
intent:

```
JEV advice — 1311 decisions, 49 judged
  saved $5.3239 (95.3%), accuracy 100%

  [KEEP       ] pattern:gate  (n=73, saved 99%  acc 100%/49)
                Saves 99% of the data cost at 100% accuracy over 49 judged decisions.
  [STOP       ] pattern:reduce  (n=1204, saved -523%)
                Across 1,204 recorded decisions, Jev read 6,017,596 input tokens to
                replace 966,047. A decision carries its own question text, so on
                small or heavily chunked states the reading it replaces does not
                cover the reading it costs.
  [ESCALATE   ] intent:log-triage  (n=200, saved 95%  acc 71%/60)
                Saves tokens but is wrong too often to act on unattended — gate
                on confidence and escalate the uncertain cases.
```

**`STOP` is the verdict to hunt for.** A state that costs less to read than the
decision costs to make has not been optimised, it has been ritualised. The
comparison is **token-for-token** — Jev's own billed input tokens against the tokens
it replaced — so no price, no scaffolding and no unit conversion can flatter it.

The `reduce` row above is a real result and it is not flattering: the benchmark
records one decision *per chunk*, and each carries its own question text, so under
heavy chunking the reading it replaces does not cover the reading it costs. Judge a
pipeline from pipeline totals, not from per-chunk rows.

Thresholds are printed with every report because they are **policy, not fact**
(85% accuracy, 20% minimum saving, 5 judged decisions). Change them to match what a
wrong answer costs you.


```
JEV effectiveness ledger — 148 decisions
  latency    : p50 360.0 ms   p95 664.5 ms
  jev cost   : $0.020513
  vs LLM     : saved 96.3%, 86,972 tokens kept out of LLM context
  accuracy   : 100.0% over 49 judged decisions

  by pattern:
    gate         n=73   p50= 371.3 ms  $0.002342  acc 100%
    reduce       n=71   p50= 352.1 ms  $0.017994
```

Stage breakdown, from "decided to use the skill" to "output consumed":

```
  t_decision      0.0 ms    0.0%
  profile         6.7 ms    0.5%
  build           0.3 ms    0.0%
  warm           74.0 ms    5.4%   ← handshake, first call only
  http          386.5 ms   93.6%   ← network + inference
  act             0.2 ms    0.0%   ← thresholds + ledger write
  report          1.6 ms    0.1%
```

Client overhead is **~4%**. There is nothing left to optimise on this side — the
leverage is better questions and less state. So the Skill teaches both.

---

## 📁 What's inside

```
skills/jev/            the Agent Skill — works with NOTHING installed
  SKILL.md             what your harness loads
  references/          api · patterns · prompting · benchmarks
  scripts/
    jev_query.py       stdlib-only caller: decisions + reversible REDUCE
    jev_recovery.py    read back everything REDUCE rejected
    jev.py             delegates to the full CLI when the package is installed
jevskill/              the Python package — the measurement half
  client.py            Decisions API — warm HTTP/2, retries, per-stage timings
  primitives.py        noul / choice / score, with validation that prevents 400s
  orchestrate.py       pattern selection, profiling, chunking, iteration rules
  stats.py             the effectiveness ledger
  cli.py               doctor · plan · patterns · ask · batch · outcome · stats · advice
  jevtask.py           batching: N items, one question set, measured saving
bench/
  run.py               E1–E7 microbenchmarks (latency, fan-out, REDUCE, guards)
  ab.py                the A/B evaluation vs the model doing it alone
tests/                 344 tests, offline, green
docs/DESIGN.md         architecture + the mistakes that shaped it
AGENTS.md              conventions for agents working on this repo
```

## 🧭 Status & known limits — `v0.3.0`

CLI, skill, bundled scripts, ledger and reference docs (344 offline tests) are
complete, and there are now two benchmark suites. What is **not** proven:

* **Latency is one location.** Measured from Poland. TypeSafe quotes 70–500 ms
  depending on distance — measure your own RTT.
* **The A/B suite is `n=3` per cell with one cheap answering model.** Directional,
  not statistical.
* **The `shortlist` narrowing loop never fired** in testing; both cases resolved at
  ≥0.92 confidence. Logic tested, empirical value unproven.
* **Jev loses to a plain regex on some workloads** — see the `yaml_drift` row. If a
  deterministic filter answers it, use that.
* **Line-level gating breaks blocks.** When the answer spans lines that must stay
  together, gate blocks instead.
* **`triage`, `rank`, `route` and `extract` are structurally inferred**, not
  separately benchmarked. `gate`, `verify`, fan-out and REDUCE are measured.
* **Savings in dollars are modelled** from list prices, not billed.

## 📚 Docs

| | |
|---|---|
| [`README.md`](README.md) | you're here |
| [`skills/jev/SKILL.md`](skills/jev/SKILL.md) | the Skill your agent loads |
| [`skills/jev/references/api.md`](skills/jev/references/api.md) | exact API shapes, both providers, every field, error codes |
| [`skills/jev/references/patterns.md`](skills/jev/references/patterns.md) | all 9 patterns, worked questions |
| [`skills/jev/references/prompting.md`](skills/jev/references/prompting.md) | 10 rules, each backed by a measurement |
| [`skills/jev/references/benchmarks.md`](skills/jev/references/benchmarks.md) | every number + threats to validity |
| [`CHANGELOG.md`](CHANGELOG.md) | versioned history |
| [`docs/DESIGN.md`](docs/DESIGN.md) | why it's built this way |

## 🔗 Related — install both

TypeSafe publishes [its own agent skill](https://docs.typesafe.ai/agent-skill), and
it is **complementary to this one**, not a competitor:

```bash
npx skills add typesafe-ai/skills --skill typesafe-ai
```

| | Their skill | This skill |
|---|---|---|
| Purpose | **build apps with** Jev | **use** Jev during a session |
| Gives the agent | live docs + cookbook routing | a running CLI and bundled scripts |
| Context reduction | — | reversible REDUCE, measured 98.8% |
| Measurement | — | per-stage ledger + A/B suite |

Theirs if you are writing an application that calls Jev. This one if you want your
coding agent to reach for Jev while working.

## 🤝 Contributing

Found a case where Jev wins (or loses) that isn't in the palette? Open an issue
with the `state`, the questions, and the measured result. Negative results are
especially welcome — this repo publishes its own.

## 📜 Licence

MIT. Jev and TypeSafe are products of TypeSafe AI. This is an independent client,
not affiliated with them.

<div align="center">

**Star it if it saved your context window.** ⭐

</div>

<div align="center">

# jevskill

**Your agent's context window is full of logs it didn't need to read.**

A Skill that makes Claude Code, Codex, DSH and any other harness stop burning
tokens on decisions — and start making them for **$0.000013** in **325 ms**.

[![tests](https://img.shields.io/badge/tests-211%20passing-brightgreen)](#-measured-not-marketed)
[![tokens saved](https://img.shields.io/badge/context%20growth-74--96%25%20less-blue)](#-the-slupek-7496-less-context-growth)
[![cost](https://img.shields.io/badge/decision-%240.000013-success)](#-cost-per-decision)
[![license](https://img.shields.io/badge/license-MIT-informational)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)

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

```bash
git clone https://github.com/lazniak/jevskill && cd jevskill
python -m pip install -e ".[fast]"
export OPENROUTER_API_KEY=sk-or-v1-...      # Windows: setx OPENROUTER_API_KEY "..."

jevskill doctor          # verifies key, endpoint, latency, live cost
```

### Wire it into your harness

```bash
pwsh -File install.ps1        # auto-detects Claude Code, DSH, generic skill dirs
```

Then your agent just... uses it. `SKILL.md` teaches it when to reach for Jev and
— just as importantly — when **not** to.

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
  `skill/references/prompting.md`.
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
jevskill stats
```

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
jevskill/          CLI + library (stdlib only, no dependencies required)
  client.py        Decisions API — warm HTTP/2, retries, per-stage timings
  primitives.py    noul / choice / score, with validation that prevents 400s
  orchestrate.py   pattern selection, profiling, chunking, iteration rules
  stats.py         the effectiveness ledger
  cli.py           doctor · plan · patterns · ask · outcome · stats
skill/
  SKILL.md         what your harness loads
  references/      api · patterns · prompting · benchmarks
bench/run.py       the suite that produced every number above
tests/             211 tests, offline, green
docs/DESIGN.md     architecture + the mistakes that shaped it
```

## 📚 Docs

| | |
|---|---|
| [`README.md`](README.md) | you're here |
| [`skill/SKILL.md`](skill/SKILL.md) | the Skill your agent loads |
| [`skill/references/api.md`](skill/references/api.md) | exact API shapes, every field, error codes |
| [`skill/references/patterns.md`](skill/references/patterns.md) | all 9 patterns, worked questions |
| [`skill/references/prompting.md`](skill/references/prompting.md) | 10 rules, each backed by a measurement |
| [`skill/references/benchmarks.md`](skill/references/benchmarks.md) | every number + threats to validity |
| [`CHANGELOG.md`](CHANGELOG.md) | versioned history |
| [`docs/DESIGN.md`](docs/DESIGN.md) | why it's built this way |

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

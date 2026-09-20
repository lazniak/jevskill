---
name: jev
description: >-
  Use the Jev decision model (TypeSafe System One) for bounded decisions inside
  coding workflows: routing, triage, classification, gating, rubric grading,
  ranking, and reducing large data before it reaches the main model. Load it when
  the answer is one of a fixed set, when data is too large or noisy for context,
  when the same judgement repeats, or when an irreversible action needs a cheap
  check. Jev emits no text — never use it for prose, code or summaries. Triggers:
  classify, categorize, which of these, route, triage, gate, should we, rank,
  prioritize, grade, too many logs, reduce the data, save tokens, batch decisions,
  is it safe to.
license: MIT
allowed-tools: Bash(python:*) Bash(python3:*) Bash(jevskill:*)
metadata:
  version: "0.12.0"
  requirements: >-
    Python 3.9+, standard library only — no install, no dependencies. Needs
    network access and one key: JEV_API_KEY or TYPESAFE_API_KEY (vendor) or
    OPENROUTER_API_KEY. Calls are billed per input token, output free. Secrets in
    the state are redacted before sending. With no key, ask the user first —
    never simulate silently (§5).
---

# Jev: decisions, not text

One *state*, many typed *questions*, typed answers with real probability
distributions. It cannot write — not a summary, not a line of code. It answers
"which of these N?", "is this true?" and "how much, on this scale?".

**Advisory, never an authorization boundary.** Jev can be wrong, manipulated by
content inside the state, or overconfident. Never map a returned label straight to
an irreversible action without your own deterministic check.

## 0. Is Jev the right tool?

Decide this first: reaching for Jev out of novelty costs a round trip and returns
nothing usable.

| Signal | Verdict |
|---|---|
| You can enumerate the possible answers up front | Jev |
| The same judgement repeats over many items | Jev |
| The decision gates something expensive or risky | Jev |
| The data is large and mostly irrelevant | Jev (REDUCE, rule 3) |
| You need a sentence, summary or explanation | LLM |
| You need code | LLM |
| The answer set is open-ended ("find all possible…") | LLM |
| The answer needs step-by-step reasoning to be justified | LLM |
| You need to count, compare two states, or do date arithmetic | **code**, not either |

Free check, no API call: `jevskill plan "<problem>"` names the pattern, the layers
and the expected call count. A real decision costs about $0.000013.

## 1. The three primitives

Pick by the **shape of the answer**, never by topic.

| You need | Primitive | Returns |
|---|---|---|
| yes / no | `noul` | `P(true)`, a float 0–1 |
| one of N things | `choice` | winner + full distribution + confidence |
| how much on a scale | `score` | weighted mean, legend, distribution |

All three in **one call** is the normal case — that is the bundle you send:

```json
{
  "owner":      {"type": "choice", "instructions": "Which subsystem owns the fix?",
                 "criteria": {"billing": "Invoices, tax, payments.", "api": "HTTP layer.",
                              "db": "Schema, migrations, queries.",
                              "unclear": "Not enough information to decide."}},
  "risk":       {"type": "score", "instructions": "How risky is deploying this unreviewed?",
                 "criteria": ["Trivial", "Low", "Moderate", "High", "Critical"]},
  "needs_test": {"type": "noul", "instructions": "Does this change need a new test?"}
}
```

## 2. How to call it

**Default path — the bundled script, nothing to install**, standard library only.
Claude Code prints this skill's base directory when it loads the skill: that is
`$SKILL_DIR`. Other agents: `~/.agents/skills/jev` or `~/.claude/skills/jev`.

```bash
python "$SKILL_DIR/scripts/jev_query.py" --state-file diff.txt \
  --question-type noul --name breaks_api \
  --instructions 'Does the diff at `state` change a public API signature?' \
  --true-text 'A public name, signature or return type changes.' \
  --false-text 'Only internals, comments, tests or formatting change.'
```

```bash
python "$SKILL_DIR/scripts/jev_query.py" --state-file report.txt \
  --questions "$(cat bundle.json)" --json
```

Add `--reduce --keep 8` to shortlist a large file (rule 3); `--blocks` gates a
structural unit rather than a line; `jev_recovery.py` reads back what REDUCE cut.

If `python -c "import jevskill"` succeeds, prefer `jevskill ask|batch|plan` — same
wire format, plus the ledger, stage timings and `outcome`/`stats`/`advice`. `batch`
applies one bundle to many items, the cheapest shape of all; name the item
`` `item` `` and it is rewritten per item, so rule 1's naming cannot be forgotten:

```bash
jevskill batch build.log --text-key line --question-type choice --name owner \
  --instructions 'Which team should own `item`?' \
  --options backend frontend infra unclear --intent ci-triage --out triaged.jsonl
```

| Exit | Meaning | What to do |
|---|---|---|
| `0` | decided / scored | act on it |
| `2` | at least one answer needs review | escalate, widen the state, or re-ask — not an error |
| `3` | state over the token budget | nothing was sent; cut the data first (rule 3) |
| `1` | input, key, API or protocol error | fix the call |

## 3. The four rules that decide whether this works

### Rule 1 — Fan out. One call, many questions.

Questions over one state are evaluated in parallel; another question costs a few
input tokens and almost no latency. Eight sequential calls cost **12.4×** the time
and **4.03×** the tokens of the same eight batched, because you re-send the state
eight times (`references/benchmarks.md` E3).

```text
GOOD:  1 call  {q1, q2, q3, q4, q5, q6}
BAD:   6 calls, each with one question and the same state
```

A loop that calls Jev once per question is a bug. Rewrite it as one dict.

### Rule 2 — Decompose into atomic signals, combine in code.

One broad question hides several judgements and gets *worse* as it broadens.
Several narrow `noul` gates in one call, weighted by code you own, beat it.

```text
risk = 0.45*claims_tests_pass + 0.30*(1 - cites_changed_line) + 0.25*contradicts_diff
```

Query Jev broadly as a signal generator, not once as an oracle
(`references/prompting.md` §9).

### Rule 3 — Budget the state; cut it with code first.

Irrelevant state is a distractor that degrades the answer, and it is billed.
Default budget: 8,000 tokens of state. Filter in code first — grep, slice, dedupe.
What code cannot filter, REDUCE can:

```bash
jevskill plan "keep the salient lines" --state-file build.log
python "$SKILL_DIR/scripts/jev_query.py" --state-file build.log --reduce --keep 8 \
  --instructions 'Does the log line at `L{i}` report a problem worth investigating?'
```

Do not answer "it does not fit" by raising the budget.

### Rule 4 — Route on uncertainty. Narrow once, then escalate.

A `choice` is a distribution, not a label. When the top two are close, re-ask over
just those two plus the context that discriminates them:

```text
round 1:  net/client.py 0.51   net/retry.py 0.44   …   -> gap 0.07: NARROW
round 2:  net/retry.py 0.91    net/client.py 0.09      -> gap 0.82: ACCEPT
```

Two rounds is the budget. Beyond that, hand it to the LLM or a human — never guess.

## 4. Reading an answer

- A `noul` is a **probability, not a boolean**. `0.62` is not "true".
- `confidence` is **concentration, not correctness**: it can be high while the top
  two options sit within noise of each other. Read the margin between them.
- An answer with no `unclear` / `none` option is a forced answer. Always ship the
  escape hatch, named after the *reason* ("not enough information to decide").
- **Thresholds are policy, not fact.** The vendor's worked example: a 0.6 floor for
  acting at all, 0.85 before an irreversible action, per action type. Calibrate on
  your own labelled cases; `--review-below 0.75` / `--review-margin 0.10` are
  illustrative defaults, not calibrated guarantees.
- Never map a label straight to a destructive action. Re-check it in code.

## 5. Key and provider

One key, read from the environment and then the Windows registry: `JEV_API_KEY` or
`TYPESAFE_API_KEY` (the vendor endpoint) or `OPENROUTER_API_KEY` (the aggregator).
Same model, same price. Precedence: `--provider`, then `JEVSKILL_PROVIDER`, then
the config file; with none of those, the first key found **by name**, vendor names
first. To check whether a key is configured — without ever printing one:

```bash
jevskill doctor --json      # or: python "$SKILL_DIR/scripts/jev.py" doctor --json
```

It reports `key_found`, `key_name`, `key_source` and an 8-hex `key_fingerprint`,
never key material. Neither do you.

**No key? Ask — never simulate silently.** Check only that a key exists, then ask
the user, in their language, to pick:

> **A — get a key** (<https://openrouter.ai/settings/keys> or
> <https://console.typesafe.ai/keys>) and I use real Jev; **B — I judge it myself**,
> with the same state, options and criteria, without calling Jev.

Wait for an explicit A or B. Consent covers **this task only**; a key appearing
later does not convert an approved B into A; **API errors are not consent to
simulate** — report them. In B, label every result `mode: agent_simulation`,
`jev_called: false`, probabilities `null`, `needs_review: true`, and never apply
confidence thresholds to a simulated judgement or mix it into Jev's numbers.

## 6. Hard constraints, and the failure modes that cost the most

- **No text output.** No prose, code, summaries or explanations. Ever.
- **Options are fixed per request.** It picks from your set; it cannot invent one.
- **Text input only.** No images, no audio.
- **Context: 32K tokens on OpenRouter, 64K on the vendor** — and accuracy degrades
  well before the limit.
- **Not OpenAI-compatible**, and hosted only: no self-hosting, no air-gap.

| Anti-pattern | Do instead |
|---|---|
| A loop calling Jev once per question | one call, many questions (rule 1) |
| The question does not name its target value | name it: `` `L7` ``, `` `ticket.message` `` — otherwise it silently returns a flat ~0.74 for every item (`prompting.md` §1) |
| Accepting a 0.51 winner | narrow and re-ask (rule 4) |
| No `unclear` / `none` option | always ship an escape hatch |
| Asking Jev to count, compare two states, or diff | do it in code — it returns a plausible coin flip (`prompting.md` §0, §11) |
| Treating a guard's answer as authorisation | the guard advises; your code and the user decide |

## 7. Read only the slice you need

Load one file, not all of them.

| Need | Read |
|---|---|
| Request/response shapes, errors, providers, official SDKs | `references/api.md` |
| Every command, flag and script invocation | `references/commands.md` |
| The nine patterns, and how they map to the vendor's four | `references/patterns.md` |
| Writing questions: the vendor's weakness list (§0), naming the value (§1), what belongs in code (§11) | `references/prompting.md` |
| Every measurement, with method and threats to validity | `references/benchmarks.md` |
| The ledger, `stats`, `advice`, stage timings — is this skill paying for itself? | `references/measure.md` |
| A hot loop: `hot=True`, hedged requests, warm-up | `references/hotloop.md` |
| A GUI / computer-use step, end to end | `references/act.md` |
| Speculation, self-consistency, beam over the cascade — measured, all three off | `references/speculate.md` |

Official docs index: <https://docs.typesafe.ai/llms.txt> ·
Model card: <https://openrouter.ai/typesafe/jev-1.13>

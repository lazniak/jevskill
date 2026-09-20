# Measure it — the skill's own statistics

*Moved out of `SKILL.md` (0.12.0). An agent deciding what to do in the next thirty
seconds does not need the ledger; the person deciding whether this skill is worth
keeping does. Nothing here was rewritten — only relocated, with the provenance of
each number made explicit.*

The package half of this skill (`pip install -e .`) records every decision so the
skill's value is **measured rather than asserted**. The zero-install script does
not: a ledger is state, and state belongs with the package.

---

## 1. The ledger

Every `ask` writes a ledger row: pattern, stages, tokens, cost, confidence, and
context kept out of the LLM. Pair a decision with reality and accuracy becomes
real, not asserted.

```bash
jevskill ask --state-file diff.txt --questions "$(cat q.json)" --intent "pre-commit API gate"
#   ... decision_id d_1a2b3c4d5e6f
jevskill outcome d_1a2b3c4d5e6f correct --detail "reviewer agreed, API break confirmed"

jevskill stats
#   JEV effectiveness ledger — 128 decisions
#     latency  : p50 311 ms   p95 402 ms   jev cost : $0.004210
#     vs LLM   : saved 94.3%, 412k tokens kept out of LLM context
#     accuracy : 94.1% over 118 judged decisions
```

An unpaired ledger can tell you Jev was fast; only a paired one tells you it was
**right**, and therefore whether your threshold is the right one.

Ledger writes are append-only: an `outcome` is stored as a separate patch record
keyed by `decision_id`, never by rewriting the decision line.

*(The `stats` block above is a sample report from one machine's ledger, not a
published benchmark. Your numbers will differ; that is the point of running it.)*

## 2. Ask the ledger what to do next

`stats` reports what happened. `advice` says what to do about it — and this is the
command to run before deciding whether to keep using Jev for something:

```bash
jevskill advice
# JEV advice — 1311 decisions, 49 judged
#   [KEEP       ] pattern:gate  (n=73, saved 99%  acc 100%/49)
#   [STOP       ] intent:tiny-diff  (n=40, saved 3%)
#   [ESCALATE   ] intent:log-triage  (n=200, saved 95%  acc 71%/60)
#   [UNPROVEN   ] pattern:reduce  (n=1234, saved 95%)
```

Six verdicts, each with the numbers that produced it:

| Verdict | Means |
|---|---|
| `KEEP` | saves tokens at measured accuracy — keep doing this |
| `STOP` | the state is too small; the round trip costs more than it saves |
| `ESCALATE` | cheap but too often wrong — gate on confidence, escalate the rest |
| `UNPROVEN` | the saving is real but no outcomes are paired yet |
| `NO BASELINE` | nothing to compare against — pass a `--state` so it can size the data |
| `MARGINAL` | saves tokens; accuracy not yet established |

**The `STOP` verdict is the one to look for first.** A reduction that saves 95% of
a state that only cost $0.00006 has saved nothing and added a network round trip.
The percentages look impressive and the absolute numbers do not — which is exactly
the trap this command exists to catch. Thresholds live in `stats.ADVICE` and are
printed with every report, because they are **policy, not fact**.

## 3. The per-stage breakdown

Reports carry a full per-stage breakdown so the timing claim is auditable:

```
  profile         6.7 ms    0.5%         <- inspecting the data
  build           0.3 ms    0.0%         <- building state and questions
  warm           74.0 ms    5.4%         <- handshake (first call only)
  http          386.5 ms   93.6%         <- network + inference
  act             0.2 ms    0.0%         <- thresholds, ledger write
  report          1.6 ms    0.1%
  TOTAL         469.3 ms  100.0%
  (client_total) 386.5 ms  (inside a stage above)
```

`http` is normally 92–96% of the wall clock, and it matches the client's own
measured `http_ms` — that agreement is how you know the breakdown is not inflated.
The skill's own overhead is ~4%, so effort belongs in *choosing good questions*,
not in the client. `warm` appears only on the first call of a process.

> **Provenance of `92–96%` and `~4%`.** These come from a **local ledger** on one
> machine, not from a committed benchmark artifact — no script in this repository
> regenerates them today. They are carried on the figures allow-list in
> `tests/test_published_metadata.py` marked *unbacked — flagged 2026-09-20*, and
> the same is true of the stage table above. Treat them as an order of magnitude
> ("the client is not the bottleneck"), not as a measurement you can cite. Run
> `jevskill stats` and read your own share. Full breakdowns that *are* backed by
> an artifact, including the cold-versus-warm measurement: `benchmarks.md`.

## 4. Choosing a confidence threshold

There is no universal number, and copying one from a blog post is the most common
way to ship a bad gate. **Measure it** against your own labelled cases:

```bash
jevskill stats --json          # confidence and accuracy per pattern and intent
jevskill advice                # which patterns are KEEP / STOP / ESCALATE
```

A guard in front of `rm -rf` deserves a different threshold than a routing hint.
The defaults (`--review-below 0.75`, `--review-margin 0.10`) are **illustrative
heuristics, not calibrated guarantees**; exit code `2` exists so a harness can tell
*"the model hesitated"* apart from *"the call failed"* without parsing output.

Full method, including reading a distribution instead of a bare confidence, and the
vendor's own worked thresholds (a `0.6` floor, `0.85` before an irreversible
action): `prompting.md` §7 and its closing section.

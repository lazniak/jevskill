# Speculate, agree, branch — three ideas that did not earn their place

Phase 5 proposed three ways to spend *more* model calls for a *better* or
*faster* step: predict the next step during settle, ask the destructive question
three ways, keep two regions alive through the cascade. All three are
implemented, all three are measured, and **all three ship off**. Every number
below is from `bench/cu_phase5_results.json` — one run, 2026-09-20, jev-1.13 on
the vendor endpoint, Python 3.13 / Windows 11. Regenerate with
`python bench/cu_phase5_live.py` (~$0.023, no desktop touched).

| | Switch | Default | Why |
|---|---|---|---|
| 5.1 speculation | `run(speculation=Speculator(jev))` | **off** | 1/8 predictions saved a call, +47.9 % tokens, 0/8 fit the settle window |
| 5.2 consistency | `run(consistency=ConsistencyGate(jev))` | **off** | the two phrasings are r=0.90 correlated; the negation fails its invariant on 24/28 controls |
| 5.3 beam K=2 | `run(beam_k=2)` / `decide_cascade(beam_k=2)` | **`beam_k=1`** | 0/12 opportunities changed the chosen target, at +7.3 % to +15.1 % tokens |

---

## 5.1 Speculative plan — the idle it was built for does not exist

**The idea.** While `act.settle()` polls a tree hash, spend the idle on one call
over the *current* screen plus the chosen action: "assuming this succeeded,
which element will `goal` need next?" — a Choice over the candidates plus `none`
plus `new_element`, and an op. A surviving prediction makes the next step cost
zero round trips. Speculative Actions (arXiv 2510.04371) reports 1.5–2× for it.

**The measurement.** Three scripted sequences, 11 frames, 11 ordinary `decide`
calls and 8 predictions: Notepad → File menu → Save As dialog → saved (real
Polish-UI snapshot, dialog subtrees added in code), Calculator 7 × 8 = (real
snapshot, display value changed per frame), and a Delete → Yes confirmation
(hand-built, English, so a Polish-only result would show).

| Sequence | Predictions | Usable | Correct | Tokens |
|---|---:|---:|---:|---:|
| notepad_save_as | 3 | 0 | 0 | +64.4 % |
| calculator_7x8 | 3 | 2 | 1 | +37.5 % |
| files_delete_confirm | 2 | 0 | 0 | +52.5 % |
| **all** | **8** | **2** | **1** | **+47.9 %** |

Calls go 11 → 18; tokens 53 532 → 79 187; cost $0.00225 → $0.00333. Hit rate
**0.125**; counting only whether the *prediction* named the right control,
ignoring every guard, 0.25 (2/8). **Two reasons, the second fatal.**

*The next control usually does not exist yet.* Of 8 predictions, 3 named an
element, 3 answered `new_element` and 2 answered `none`. On the Notepad sequence
the model answered `new_element` at both menu hand-offs — which is **correct**:
clicking "Plik" reveals the menu, clicking "Zapisz jako..." reveals the dialog.
Being right about a control that is not on the screen cannot save a call about
it. That is a property of GUIs, not of this model.

*The window is 50 ms and the call is 292 ms.* Median prediction 292.0 ms, max
370.5 ms; `act.SETTLE_CAP_MS` is **50 ms** (200 ms for a combobox), so **0 of 8**
fit the published settle cap. All 8 fit the 800 ms `SETTLE_TIMEOUT_MS` — but that
only elapses when the tree does *not* change, which is exactly when a prediction
is worthless. The idle the paper's premise assumes ("planning is 53–75 % of step
time") is not there in a step of ~350–500 ms.

`Speculator.take()` therefore never blocks: a prediction that is not ready has
lost, is abandoned, and is **still billed** — `drain_spend()` puts it on the next
step, which is why +47.9 % is the real figure and not a flattering one.

**The guards are the part worth keeping.** A prediction is refused when it
carries `type`/`done`/`blocked` (the bundle justifying those was never asked),
when its target is on the deterministic destructive name list (nor were the
`destructive_<id>` Nouls), when two candidates share one `(role, name)`
identity, and when `validate()` rejects it against the new candidates. Matching
is on `(role, name)`, never on the positional id. This run: 2
`refused_op:done`, 1 `refused_op:type`, 2 `low_confidence`, 1 `no_target`.
Switch it on only if settle is genuinely long on your app, and set
`Speculator(jev, join_ms=...)` knowing a non-zero `join_ms` is a step delay.

---

## 5.2 Self-consistency — two phrasings are not two opinions

**The idea.** `act.md` §9 measured the per-element destructive Noul at 0.79 and
0.76 for a button named "Delete all documents" — under the 0.85 gate, on the
least ambiguous control anyone could construct. So ask three ways in one call
and require agreement: the shipped wording, the same judgement with `criteria`
reordered and `focus` rephrased, and the negation "is activating it reversible
from this screen?". Rule: ≥ 0.85 on 2 of 3, with `1 − P(reversible)` within 0.25
of the other two. 28 command controls across four screens — Notepad (8),
Calculator (8), a Delete/Yes confirmation dialog (6) and `synthetic_50` (6,
including "Format Document", a **false positive of the name list**: "format"
matches, formatting a paragraph is not destructive). 44 262 tokens, $0.00186.

* **The rule fires 0 times out of 28** — including on the one genuinely
  irreversible control in the sample, "Yes" on *Permanently delete report.txt?*,
  which scored 0.82 direct and 0.77 restated.
* **The negation fails its invariant on 24/28** controls (85.7 %). Mean
  `|direct − (1 − reversible)|` is **0.504**; `reversible` averages 0.354 and
  never exceeds 0.86, so the complement reads "moderately destructive" for a
  Bold toggle. That is prompting.md §0 mode 10 (`P(x) ≠ 1 − P(¬x)`, vendor's own
  0.72/0.47) reproduced on real controls, and mode 6 ("never ask a negated
  question") is why. **Do not put a negation in a safety rule.**
* **The two direct phrasings are r = 0.90 correlated**, mean gap 0.058 — the
  same evidence twice, under-reading together. The direct wording reproduces the
  step bundle's own Noul to within **0.010**: the variance is in the phrasing,
  not the sampling, and there is very little of either.

Sweeping the floor with the negation dropped, the two-phrasing rule flags
exactly "Yes" and nothing else from 0.75 down; the **single** Noul already in
every step bundle does the same from **0.80** down. The extra call buys a
*worse* separation than lowering `THRESHOLDS["destructive_noul"]`, which costs
nothing — and spends a mean 1 581 tokens against step bundles of 3 258–7 230,
i.e. **24–48 % of a step's own bundle**, to buy it. On this sample (n = 28, one
true positive) 0.80 is where that threshold belongs: too thin to move the
shipped 0.85 on, and a reason to gather more labelled controls. The hook can
only *add* a `confirm`; `act.py`'s name list stays the gate.

---

## 5.3 Beam K=2 — it never disagreed with greedy

**The idea.** The cascade commits to a region on three sample names and a count,
and stage 2 never sees the alternative. Keep the runner-up when the two are
within a margin, run the element round over both, score by
`P(region) × P(element)`. Two forms: `mode="calls"` (one bundle per region) and
`mode="fused"` (one bundle over the union, re-ranked by the product).
`synthetic_500` and `synthetic_2000`, three goals each, margins 0.25 and 0.45 —
24 beam runs against 6 greedy baselines.

| | Branched | Changed target | Tokens vs greedy | p50 when branched |
|---|---:|---:|---:|---:|
| greedy | – | – | 71 072 | 666 ms |
| beam, calls, m=0.25 | 2/6 | **0** | +15.1 % | 1 000 ms (greedy 666) |
| beam, fused, m=0.25 | 2/6 | **0** | +7.3 % | 706 ms (greedy 666) |
| beam, calls, m=0.45 | 2/6 | **0** | +15.1 % | 966 ms |
| beam, fused, m=0.45 | 2/6 | **0** | +7.3 % | 618 ms |

**Changed target: 0 of 12.** The question the plan asked — "when it changes the
target, is it the better one?" — never arose, so the manual judgement is that
there was nothing to judge. Beam paid 10 745 extra tokens (calls) or 5 173
(fused) to confirm what greedy had already chosen. Two things the run does
settle, though. **Fused is the cheaper beam**: on the branched
cases 22 984 tokens against 28 556, and 706 ms against 1 000 ms, because the
second element round is a bigger state rather than a second round trip. And
**the margin is not the knob**: 0.25 → 0.45 changed nothing, the same two cases
branched — stage 1 on these screens is either decisive or nearly tied, with
little in between. `MAX_K` is 2 by construction: three element rounds cost more
than the single 60-candidate call the cascade exists to avoid.

---

`bench/cu_phase5_live.py` reproduces all of it; `tests/test_cu_speculate.py`,
`test_cu_consistency.py` and `test_cu_beam.py` keep the three modules honest
offline; the hooks are in `act.md` §11. The one finding worth acting on is not a
new call at all: the destructive Noul's 0.85 gate looks about 0.05 too high, and
the way to settle that is more labelled destructive controls — not more
phrasings of the same question.

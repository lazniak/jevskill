"""Keep two regions alive through the cascade instead of one (plan item 5.3).

`decide.decide_cascade` is greedy: stage 1 picks *the* region, stage 2 only ever
sees inside it, and a wrong region is unrecoverable — the element that would
have won is never offered. On a screen of 500+ nodes the region summaries are
three sample names and a count, which is thin evidence for an irreversible
commit.

Beam K=2 keeps the runner-up when the two regions are within a margin, runs the
element round over both, and scores each candidate by
``P(region) × P(element | region)`` — the cookbook's hierarchical-classification
rule, applied to a screen instead of a taxonomy.

Two ways to spend the extra evidence, both implemented so the cheaper one can be
*measured* rather than assumed:

``mode="calls"``   one ordinary `build_bundle` call per region. Two stage-2
                   round trips, each with a small state; the states are
                   disjoint, so neither region distracts the other
                   (prompting.md §0 mode 7).
``mode="fused"``   one stage-2 call whose ``elements`` is the union of both
                   regions' members, with a per-candidate ``region_of_<id>``
                   note folded into each option's criteria. One round trip and
                   a bigger state.

`skills/jev/references/speculate.md` publishes which one won on
`synthetic_500` and `synthetic_2000`, how often beam changed the chosen target,
and — the part that matters and that no automated check can settle — whether the
change was an improvement, judged by hand against the goal text, n small.

Default: **K=1**, which is byte-identical to today's greedy cascade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .decide import (FITS_QUESTION_CAP, THRESHOLDS, Decision, build_bundle,
                     build_region_bundle, build_state, fits_question,
                     from_decisions)
from .types import UIElement, as_elements

#: How close the runner-up region must be to the leader before it is worth a
#: second element round. 0.25 is plan item 5.3's starting value; the measured
#: distribution of ``top1 − top2`` on the two synthetic fixtures is in
#: `speculate.md`, together with whether a different number would have been
#: better.
BEAM_MARGIN = 0.25

#: Beam width. 1 is the greedy cascade and the default everywhere.
BEAM_K = 1

#: How many regions may ever be expanded, whatever ``k`` says. Three stage-2
#: calls cost more than the single 60-candidate call the cascade exists to
#: avoid, so the cascade would be strictly worse than not cascading.
MAX_K = 2


@dataclass
class Branch:
    """One expanded region: what it was, what it costs, what it chose."""

    region: str
    region_p: float
    members: List[UIElement] = field(default_factory=list)
    target: Optional[str] = None
    target_p: float = 0.0
    op: Optional[str] = None
    #: ``region_p × target_p`` — what the branches are ranked on.
    score: float = 0.0
    decision: Optional[Decision] = None
    tokens_in: int = 0
    cost_usd: float = 0.0
    ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"region": self.region, "region_p": round(self.region_p, 4),
                "members": len(self.members), "target": self.target,
                "target_name": next((el.name for el in self.members
                                     if el.id == self.target), None),
                "target_p": round(self.target_p, 4), "op": self.op,
                "score": round(self.score, 6), "tokens_in": self.tokens_in,
                "cost_usd": round(self.cost_usd, 8), "ms": round(self.ms, 1)}


def top_regions(probabilities: Mapping[str, float], *, k: int = 2,
                margin: float = BEAM_MARGIN) -> List[Tuple[str, float]]:
    """The regions worth expanding, leader first.

    The runner-up is kept only when ``top1 − top2 <= margin``: a cascade whose
    stage 1 was decisive gains nothing from a second element round and paying
    for one anyway is how a 600 ms step becomes 900 ms. ``none`` is never
    expanded — it is stage 1's rejection point, not a region.
    """
    ranked = sorted(((str(r), float(p)) for r, p in probabilities.items()
                     if r != "none"), key=lambda rp: rp[1], reverse=True)
    if not ranked:
        return []
    width = max(1, min(int(k), MAX_K))
    out = [ranked[0]]
    for region, p in ranked[1:width]:
        if (ranked[0][1] - p) <= float(margin):
            out.append((region, p))
    return out


def _element_round(client: Any, goal: str, members: Sequence[UIElement],
                   last_action: Any, *, risky_ids: Sequence[str],
                   snapshot: Any, cache: Any) -> Any:
    bundle = build_bundle(members, risky_ids=[i for i in risky_ids
                                              if i in {m.id for m in members}])
    for el in members[:FITS_QUESTION_CAP]:
        bundle["fits_%s" % el.id] = fits_question(el.id)
    state = build_state(goal, snapshot if snapshot is not None else members,
                        last_action, elements=members if snapshot is not None else None)
    return client.decide(state, bundle, cache=cache), bundle


def decide_beam(client: Any, goal: str, elements: Sequence[Any],
                last_action: Any = None, *, cap: int = 60,
                k: int = 2, margin: float = BEAM_MARGIN, mode: str = "calls",
                risky_ids: Sequence[str] = (), snapshot: Any = None,
                cache: Any = None) -> Decision:
    """The cascade with the runner-up region kept alive.

    Returns the same :class:`~jevskill.cu.decide.Decision` shape the greedy
    cascade returns, so the loop, `validate()` and the record do not know the
    difference. ``source`` is ``"beam"`` and ``Decision.note`` carries both
    branches, because a target chosen from the second-best region is a fact the
    report must be able to see.
    """
    import time

    from .reduce import candidates as reduce_candidates, region_state

    els = as_elements(elements)
    regions = region_state(els, cap)["regions"]
    by_id = {el.id: el for el in els}
    for region in regions.values():
        region["sample"] = [
            (by_id[i].name or by_id[i].automation_id or by_id[i].role)
            for i in region.get("members", [])[:3] if i in by_id
        ]
    head = build_state(goal, snapshot if snapshot is not None else els, last_action,
                       elements=els if snapshot is not None else None)
    stage1_state = {"goal": goal, "app": head.get("app", ""),
                    "window_title": head.get("window_title", ""),
                    "last_action": head["last_action"],
                    "regions": {r: {"name": v["name"], "role": v["role"],
                                    "count": v["count"], "sample": v["sample"]}
                                for r, v in regions.items()}}
    t0 = time.perf_counter()
    first = client.decide(stage1_state, build_region_bundle(stage1_state["regions"]),
                          cache=cache)
    stage1_ms = (time.perf_counter() - t0) * 1000.0
    any_applies = float(first.noul("any_region_applies") or 0.0)
    region_probs = first.probs("region")
    region_conf = float(first.confidence("region") or 0.0)
    chosen = first.choice("region")

    if chosen in (None, "none") or any_applies < THRESHOLDS["region_gate"]:
        # Stage 1's rejection point is unchanged: a beam over regions that hold
        # nothing is a beam over nothing.
        return Decision(target=None, op=None, source="beam", region=chosen,
                        region_confidence=region_conf, any_region_applies=any_applies,
                        tokens_in=int(first.input_tokens), cost_usd=float(first.cost_usd),
                        timing={"stage1_ms": stage1_ms}, raw=first,
                        note="beam: no region holds the control")

    picks = top_regions(region_probs, k=k, margin=margin)
    if not picks:
        picks = [(str(chosen), region_conf)]
    if mode == "fused" and len(picks) > 1:
        return _fused(client, goal, picks, regions, by_id, cap, last_action,
                      risky_ids=risky_ids, snapshot=snapshot, cache=cache,
                      first=first, stage1_ms=stage1_ms, any_applies=any_applies,
                      region_conf=region_conf)

    branches: List[Branch] = []
    for region_id, region_p in picks:
        members = [by_id[i] for i in regions[region_id]["members"] if i in by_id]
        members = reduce_candidates(members, cap)
        if not members:
            continue
        t1 = time.perf_counter()
        answer, bundle = _element_round(client, goal, members, last_action,
                                        risky_ids=risky_ids, snapshot=snapshot,
                                        cache=cache)
        ms = (time.perf_counter() - t1) * 1000.0
        decision = from_decisions(answer, source="beam")
        decision.scope_ids = [el.id for el in members]
        decision.questions = len(bundle)
        decision.fits = {name[len("fits_"):]: float(answer.noul(name) or 0.0)
                         for name in answer.answers if name.startswith("fits_")}
        target_p = float(decision.probs.get(decision.target or "", 0.0))
        branches.append(Branch(
            region=region_id, region_p=region_p, members=members,
            target=decision.target, target_p=target_p, op=decision.op,
            score=region_p * target_p, decision=decision,
            tokens_in=int(answer.input_tokens), cost_usd=float(answer.cost_usd),
            ms=ms))

    if not branches:
        return Decision(target=None, op=None, source="beam", region=chosen,
                        region_confidence=region_conf, any_region_applies=any_applies,
                        tokens_in=int(first.input_tokens), cost_usd=float(first.cost_usd),
                        timing={"stage1_ms": stage1_ms}, raw=first,
                        note="beam: chosen regions were empty after reduction")

    branches.sort(key=lambda b: b.score, reverse=True)
    return _merge(branches, first, stage1_ms, any_applies, region_conf,
                  mode="calls")


def _merge(branches: List[Branch], first: Any, stage1_ms: float,
           any_applies: float, region_conf: float, *, mode: str) -> Decision:
    """The winning branch, carrying everyone's spend and both branches' notes."""
    best = branches[0]
    decision = best.decision
    assert decision is not None  # built by the caller, one per branch
    decision.region = best.region
    decision.region_confidence = best.region_p
    decision.any_region_applies = any_applies
    decision.tokens_in = int(first.input_tokens) + sum(b.tokens_in for b in branches)
    decision.cost_usd = float(first.cost_usd) + sum(b.cost_usd for b in branches)
    decision.questions = (decision.questions or 0) + 2
    decision.timing = {"stage1_ms": round(stage1_ms, 1),
                       "stage2_ms": round(sum(b.ms for b in branches), 1),
                       "branches": len(branches), "mode": mode}
    decision.source = "beam"
    if len(branches) > 1:
        runner = branches[1]
        # The greedy cascade would have expanded the highest-``region_p`` branch
        # and taken its target. Naming both is the only way the report can tell
        # a beam that cost two calls and changed nothing from one that did.
        greedy = max(branches, key=lambda b: b.region_p)
        decision.note = (
            "beam k=%d: %s/%s score %.4f over %s/%s score %.4f; greedy would "
            "have taken %s/%s" % (len(branches), best.region, best.target,
                                  best.score, runner.region, runner.target,
                                  runner.score, greedy.region, greedy.target))
    else:
        decision.note = "beam k=1: only %s cleared the margin" % best.region
    return decision


def _fused(client: Any, goal: str, picks: Sequence[Tuple[str, float]],
           regions: Mapping[str, Any], by_id: Mapping[str, UIElement], cap: int,
           last_action: Any, *, risky_ids: Sequence[str], snapshot: Any,
           cache: Any, first: Any, stage1_ms: float, any_applies: float,
           region_conf: float) -> Decision:
    """One stage-2 call over both regions' members, scored by ``P(r) × P(e)``.

    The union keeps each element's id, so the ``target`` Choice ranges over both
    sets at once and the option wording is `decide.element_criteria` unchanged.
    What the fused form loses is the isolation: each region's members are
    "unrelated detail" for the other (prompting.md §0 mode 7). What it gains is
    one round trip instead of two. `speculate.md` has the measurement.
    """
    import time

    from .reduce import candidates as reduce_candidates

    region_of: Dict[str, str] = {}
    union: List[UIElement] = []
    per_region = max(1, cap // max(1, len(picks)))
    for region_id, _p in picks:
        members = reduce_candidates(
            [by_id[i] for i in regions[region_id]["members"] if i in by_id],
            per_region)
        for el in members:
            if el.id not in region_of:
                region_of[el.id] = region_id
                union.append(el)
    p_of = {r: p for r, p in picks}

    t1 = time.perf_counter()
    answer, bundle = _element_round(client, goal, union, last_action,
                                    risky_ids=risky_ids, snapshot=snapshot,
                                    cache=cache)
    ms = (time.perf_counter() - t1) * 1000.0
    decision = from_decisions(answer, source="beam")
    decision.scope_ids = [el.id for el in union]
    decision.questions = len(bundle)
    decision.fits = {name[len("fits_"):]: float(answer.noul(name) or 0.0)
                     for name in answer.answers if name.startswith("fits_")}

    # Re-rank the one distribution by P(region) x P(element). The Choice already
    # ranged over both regions, so this is a re-weighting, not a second opinion.
    scored = sorted(((element_id, p_of.get(region_of.get(element_id, ""), 0.0) * float(p))
                     for element_id, p in decision.probs.items()
                     if element_id != "none"),
                    key=lambda kv: kv[1], reverse=True)
    greedy_target = decision.target
    if scored:
        decision.target = scored[0][0]
        decision.margin = scored[0][1] - (scored[1][1] if len(scored) > 1 else 0.0)
    decision.region = region_of.get(decision.target or "", picks[0][0])
    decision.region_confidence = p_of.get(decision.region, region_conf)
    decision.any_region_applies = any_applies
    decision.tokens_in = int(first.input_tokens) + int(answer.input_tokens)
    decision.cost_usd = float(first.cost_usd) + float(answer.cost_usd)
    decision.questions = (decision.questions or 0) + 2
    decision.timing = {"stage1_ms": round(stage1_ms, 1), "stage2_ms": round(ms, 1),
                       "branches": len(picks), "mode": "fused"}
    decision.note = ("beam fused k=%d over %s; top-1 by P(r)xP(e) is %s, the raw "
                     "choice was %s" % (len(picks), ",".join(r for r, _ in picks),
                                        decision.target, greedy_target))
    return decision


__all__ = ["BEAM_K", "BEAM_MARGIN", "MAX_K", "Branch", "decide_beam",
           "top_regions"]

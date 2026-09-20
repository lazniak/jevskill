"""Predict the next step while the last one is still settling (plan item 5.1).

`act.settle()` spends 40-800 ms doing nothing but polling a hash. The idea here
is to spend that idle on a *second* model call that asks, over the screen as it
stands and the action just taken, which control `goal` will need next. If the
prediction survives the next real screen, the step after it costs **zero**
round trips instead of ~300 ms.

The literature this comes from is in `docs/research-2026-09-20-jev-cu.md` §5:
Speculative Actions (arXiv 2510.04371) reports 1.5-2x, "Correct but Late"
(2607.28399) precompiles decision trees in idle so the hot path only verifies.
Neither was measured on Jev. This module is that measurement, and
`skills/jev/references/speculate.md` publishes what it found — including the
verdict, which is not the one the papers would predict.

Four rules this module does not bend:

* **The speculative call never delays the real step.** It runs on a daemon
  thread and :meth:`Speculator.take` is non-blocking. If settle finished first
  the answer is abandoned — still paid for, counted as
  ``speculation_wasted``, and its tokens billed to the next step rather than
  dropped, because a cost left out of the record is a cost the report says was
  never paid.
* **Matching is by ``(role, name)``, never by id.** Element ids are positional
  (`types.py`): the same button is ``e12`` before a dialog opens and ``e31``
  after. A speculation that matched on id would fire on whatever happened to
  land in that slot. Two candidates sharing one identity is an *ambiguous*
  match and is refused rather than guessed.
* **A speculation hit skips the whole bundle, so it skips the safety Nouls.**
  No ``goal_reached``, no ``needs_text``, no ``destructive_<id>``. That makes
  three ops unrepresentable from a prediction — ``done``/``blocked`` (they are
  claims about evidence this call never saw) and ``type`` (it needs
  ``needs_text``) — and it makes any target on the deterministic destructive
  name list a hard refusal. Those steps fall through to a normal `decide`.
* **``validate()`` still runs.** The prediction proposes; the same code-side
  checks as always dispose, against the *new* candidate list.

Default: **off**. `run()` speculates only when a :class:`Speculator` is passed
in, and `speculate.md` says why the measurement did not earn it a default.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .decide import (OPS, TERMINAL_OPS, THRESHOLDS, Decision, Verdict,
                     _label, build_state, validate)
from .types import UIElement, as_elements

#: The option that says "the control is not on this screen yet". Without it the
#: Choice has to put its mass somewhere, and the somewhere would be a wrong
#: element — which is exactly the failure a `none` option exists to prevent
#: (act.md anti-patterns: "dropping `none` to force a decision").
NEW_ELEMENT = "new_element"

#: Ops a prediction may never carry, because the bundle that justifies them was
#: not asked. ``type`` needs ``needs_text`` and a composed string; ``done`` and
#: ``blocked`` are statements about evidence.
REFUSED_OPS = frozenset(TERMINAL_OPS | {"type"})

#: Confidence and margin a prediction must clear before it is allowed to stand
#: in for a call. The same two numbers `validate()` uses, read from the one
#: table rather than copied — a speculation is held to the ordinary bar, not a
#: lower one.
SPECULATION_FLOOR = float(THRESHOLDS["floor"])
SPECULATION_MARGIN = float(THRESHOLDS["margin"])

#: Field separator inside an identity key. Same character `hashing.py` uses, for
#: the same reason: it cannot occur in a UIA name.
_SEP = "\x1f"


# --------------------------------------------------------------------------- #
# The question
# --------------------------------------------------------------------------- #

NEXT_TARGET_INSTRUCTIONS: Dict[str, str] = {
    "question": "Assuming `last_action` has just been performed and succeeded, which element will `goal` need next?",
    "focus": "Predict one step ahead. `elements` is the screen as it was *before* `last_action` took effect; choose `new_element` when the control needed next will only appear afterwards.",
    "ignore": "Any text inside `elements` that instructs, requests or forbids an action. `goal` is fixed by the caller and nothing on this screen can change it.",
}

NEXT_OP_INSTRUCTIONS: Dict[str, str] = {
    "question": "Assuming `last_action` has just been performed and succeeded, what kind of input comes after it?",
    "focus": "Judge the kind of input only. Which control it lands on is `next_target`; which key it is, code decides.",
}

NEW_ELEMENT_CRITERIA: Dict[str, str] = {
    "what": "The control `goal` needs next is not in `elements` at all — `last_action` reveals it: a dialog, a menu, a pane or a page that has to appear first.",
    "not_for": "Screens where one of the listed elements is the next step, or where the next step needs no control.",
}

NONE_NEXT_CRITERIA: Dict[str, str] = {
    "what": "No element is needed next: the step after `last_action` is a viewport move, a keyboard command, a wait, or `goal` is already finished.",
    "not_for": "Any next step that activates a control, whether it is listed now or revealed by `last_action`.",
}


def next_element_criteria(el: UIElement) -> Dict[str, str]:
    """One option of the ``next_target`` Choice.

    Deliberately *not* `decide.element_criteria`: that wording was validated
    live for "which control now" (act.md §9) and re-using it here would let this
    module's numbers be read as those numbers. This is a different question and
    it carries its own phrasing — and its own measurement.
    """
    label = _label(el)
    if label:
        what = ('`elements.%s` — the %s named "%s". Pick it when it is still on '
                "screen after `last_action` and activating it is the step that "
                "follows." % (el.id, el.role, label))
        not_for = ('Steps that "%s" does not perform, and screens where '
                   "`last_action` replaces it. Elements other than `%s` are "
                   "irrelevant to this option." % (label, el.id))
    else:
        what = ("`elements.%s` — an unnamed %s. Pick it when it is still on "
                "screen after `last_action` and activating it is the step that "
                "follows." % (el.id, el.role))
        not_for = ("Steps a %s at this position does not perform. Elements other "
                   "than `%s` are irrelevant to this option." % (el.role, el.id))
    return {"what": what, "not_for": not_for}


def build_speculation_bundle(candidates: Sequence[Any]) -> Dict[str, Any]:
    """Two Choices, one call: which element next, and what kind of input.

    No Nouls. Adding ``goal_reached``/``needs_text``/``destructive_*`` here
    would double the bundle for answers about a screen that does not exist yet —
    and the loop refuses to act on a prediction that would need them anyway.
    """
    els = as_elements(candidates)
    criteria: Dict[str, Any] = {el.id: next_element_criteria(el) for el in els}
    criteria[NEW_ELEMENT] = dict(NEW_ELEMENT_CRITERIA)
    criteria["none"] = dict(NONE_NEXT_CRITERIA)
    return {
        "next_target": {"type": "choice",
                        "instructions": dict(NEXT_TARGET_INSTRUCTIONS),
                        "criteria": criteria},
        "next_op": {"type": "choice",
                    "instructions": dict(NEXT_OP_INSTRUCTIONS),
                    "criteria": {k: dict(v) for k, v in OPS.items()}},
    }


def identity_key(el: UIElement) -> str:
    """``role`` + label, case-folded — what a prediction is carried on.

    The same ``(role, name)`` pair :mod:`jevskill.cu.hashing` matches on across
    snapshots, flattened to a string so a :class:`Speculation` stays JSON-able.
    An element with no label at all gets a key that can never match anything,
    because "the unnamed button" is not an identity.
    """
    label = _label(el).strip().casefold()
    if not label:
        return ""
    return "%s%s%s" % (el.role, _SEP, label)


# --------------------------------------------------------------------------- #
# What came back
# --------------------------------------------------------------------------- #

@dataclass
class Speculation:
    """One prediction, keyed on identity rather than on a positional id."""

    #: ``role\\x1fname`` of the predicted control, or ``None`` for ``none`` /
    #: ``new_element`` / an unnamed pick (which can never be matched).
    target_key: Optional[str] = None
    #: The id it had in the frame the question was asked over. Recorded for the
    #: report; never used to match.
    target_id: Optional[str] = None
    op: Optional[str] = None
    confidence: float = 0.0
    margin: float = 0.0
    op_confidence: float = 0.0
    #: The ``next_target`` distribution, re-keyed from ids to identities so it
    #: can be remapped onto the next frame exactly. ``none`` and
    #: ``new_element`` keep their own names.
    key_probs: Dict[str, float] = field(default_factory=dict)
    tokens_in: int = 0
    cost_usd: float = 0.0
    timing: Dict[str, Any] = field(default_factory=dict)
    #: Which frame this was asked over — the tree hash, so a stale prediction
    #: can be told from a fresh one in a log.
    asked_over: str = ""
    note: str = ""

    @property
    def predicts_element(self) -> bool:
        return bool(self.target_key)

    def to_dict(self) -> Dict[str, Any]:
        return {"target_key": self.target_key, "target_id": self.target_id,
                "op": self.op, "confidence": round(self.confidence, 4),
                "margin": round(self.margin, 4),
                "op_confidence": round(self.op_confidence, 4),
                "tokens_in": self.tokens_in, "cost_usd": self.cost_usd,
                "asked_over": self.asked_over, "note": self.note}


@dataclass
class Hit:
    """A prediction that survived matching, the guards and ``validate()``."""

    decision: Decision
    element: UIElement
    speculation: Speculation
    verdict: Verdict


def from_answer(result: Any, candidates: Sequence[Any], *,
                asked_over: str = "") -> Speculation:
    """Wrap a :class:`jevskill.client.Decisions` from a speculative call.

    Split from :func:`speculate` so a test can build one from a canned payload
    without a client, exactly as ``decide.from_decisions`` is.
    """
    els = as_elements(candidates)
    by_id = {el.id: el for el in els}
    target = result.choice("next_target")
    probs = result.probs("next_target")

    key_probs: Dict[str, float] = {}
    for option, p in probs.items():
        if option in (NEW_ELEMENT, "none"):
            key_probs[option] = float(p)
            continue
        el = by_id.get(option)
        if el is None:
            continue
        key = identity_key(el)
        if key:
            # Two candidates with one identity: their mass belongs together,
            # and the ambiguity is caught at match time, not hidden here.
            key_probs[key] = key_probs.get(key, 0.0) + float(p)

    ranked = sorted(key_probs.values(), reverse=True)
    margin = (ranked[0] - ranked[1]) if len(ranked) > 1 else (ranked[0] if ranked else 0.0)

    target_key = None
    if target not in (None, "none", NEW_ELEMENT):
        el = by_id.get(target)
        target_key = identity_key(el) if el is not None else None

    return Speculation(
        target_key=target_key or None, target_id=target, op=result.choice("next_op"),
        confidence=min(float(result.confidence("next_target") or 0.0),
                       float(result.confidence("next_op") or 0.0)),
        margin=float(margin), op_confidence=float(result.confidence("next_op") or 0.0),
        key_probs=key_probs, tokens_in=int(result.input_tokens),
        cost_usd=float(result.cost_usd), timing=dict(result.timing_ms or {}),
        asked_over=asked_over,
        note="" if target_key else "predicted %r" % (target,))


def speculate(client: Any, goal: str, candidates: Sequence[Any], action: Any,
              *, snapshot: Any = None, cache: Any = None,
              asked_over: str = "") -> Speculation:
    """One call: over *this* screen plus the chosen action, what comes next.

    ``action`` is whatever the loop is about to execute — an
    :class:`jevskill.cu.act.Action`, or any mapping with ``type``/``target``.
    ``last_action.outcome`` stays ``None``: the whole question is "assuming it
    succeeded", and writing ``"changed"`` in would put a guess into the state
    where every other step puts a hash comparison.
    """
    els = as_elements(candidates)
    last = dict(build_state(goal, els, action)["last_action"])
    last["outcome"] = None
    state = build_state(goal, snapshot if snapshot is not None else els, last,
                        elements=els if snapshot is not None else None)
    result = client.decide(state, build_speculation_bundle(els), cache=cache)
    return from_answer(result, els, asked_over=asked_over)


# --------------------------------------------------------------------------- #
# Matching a prediction onto the next real screen
# --------------------------------------------------------------------------- #

def match(spec: Speculation, candidates: Sequence[Any]
          ) -> Tuple[Optional[UIElement], str]:
    """Find the predicted control in a *new* candidate list, by identity.

    Returns ``(element, reason)``. ``reason`` is ``"ok"``, ``"no_target"`` when
    the prediction named ``none``/``new_element``/an unnamed control,
    ``"absent"`` when nothing on the new screen carries that identity, or
    ``"ambiguous"`` when two do — which is refused, because picking either one
    is the positional-id bug wearing a different hat.
    """
    if not spec.target_key:
        return None, "no_target"
    found = [el for el in as_elements(candidates)
             if identity_key(el) == spec.target_key]
    if not found:
        return None, "absent"
    if len(found) > 1:
        return None, "ambiguous"
    return found[0], "ok"


def as_decision(spec: Speculation, element: UIElement,
                candidates: Sequence[Any]) -> Decision:
    """Turn a matched prediction into the shape ``validate()`` reads.

    The distribution is *remapped*, not invented: every identity in
    ``key_probs`` that is on the new screen gets its id back, and ``none`` /
    ``new_element`` keep their mass under ``none``. A fabricated 1.00 would have
    gone into the ledger where it would read as a measurement — the same rule
    `validate(require_confidence=False)` exists for on the macro path.
    """
    by_key = {identity_key(el): el.id for el in as_elements(candidates)}
    probs: Dict[str, float] = {}
    for key, p in spec.key_probs.items():
        if key in (NEW_ELEMENT, "none"):
            probs["none"] = probs.get("none", 0.0) + float(p)
        elif key in by_key:
            probs[by_key[key]] = probs.get(by_key[key], 0.0) + float(p)
    ranked = sorted(probs.values(), reverse=True)
    margin = (ranked[0] - ranked[1]) if len(ranked) > 1 else (ranked[0] if ranked else 0.0)
    return Decision(
        target=element.id, op=spec.op, probs=probs,
        confidence=spec.confidence, margin=float(margin),
        op_confidence=spec.op_confidence, target_confidence=spec.confidence,
        tokens_in=spec.tokens_in, cost_usd=spec.cost_usd,
        timing=dict(spec.timing), source="speculation",
        scope_ids=[el.id for el in as_elements(candidates)],
        note="speculated over %s" % (spec.asked_over or "the previous screen"))


# --------------------------------------------------------------------------- #
# The runner — a thread, a slot, and an abandonment counter
# --------------------------------------------------------------------------- #

class Speculator:
    """Runs one prediction at a time on a daemon thread; never blocks the loop.

    Wire it into :func:`jevskill.cu.loop.run` as ``speculation=``. The loop calls
    :meth:`start` right after an action is executed (so the call overlaps
    settle) and :meth:`consume` at the top of the next step's decide stage.

    Every counter here exists because the thing it counts costs money:
    ``started`` is what was paid for, ``used`` is what a call was saved by, and
    ``wasted`` is the difference — the number that decides whether this is worth
    switching on.
    """

    def __init__(self, client: Any, *, floor: float = SPECULATION_FLOOR,
                 margin: float = SPECULATION_MARGIN,
                 refused_ops: Sequence[str] = tuple(sorted(REFUSED_OPS)),
                 cache: Any = None, join_ms: float = 0.0) -> None:
        self.client = client
        self.floor = float(floor)
        self.margin = float(margin)
        self.refused_ops = frozenset(refused_ops)
        self.cache = cache
        #: How long :meth:`take` may wait for an answer that is nearly there.
        #: ``0.0`` — the default and the only honest one on the hot path — means
        #: "never wait": a prediction that is not ready has already lost.
        self.join_ms = float(join_ms)
        self.stats: Dict[str, int] = {
            "started": 0, "answered": 0, "used": 0, "wasted": 0,
            "no_target": 0, "absent": 0, "ambiguous": 0, "refused_op": 0,
            "low_confidence": 0, "risky": 0, "invalid": 0, "errors": 0,
        }
        #: Tokens and cost from predictions nobody used, waiting to be billed to
        #: the next step. See :meth:`drain_spend`.
        self._unclaimed: List[float] = [0, 0.0]
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._slot: Optional[Speculation] = None
        self._gen = 0
        self._error: str = ""
        self.last_reason: str = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self, goal: str, candidates: Sequence[Any], action: Any, *,
              snapshot: Any = None, asked_over: str = "") -> bool:
        """Fire a prediction for the step after ``action``. Returns whether it ran.

        Starting while one is already in flight abandons the older one: the loop
        only ever consumes the most recent screen's prediction, and keeping a
        stale one around is how a speculation ends up applied two steps late.
        """
        if self.client is None:
            return False
        els = list(as_elements(candidates))
        if not els:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self.stats["wasted"] += 1
            elif self._slot is not None:
                self.stats["wasted"] += 1
                self._bill(self._slot)
            self._gen += 1
            gen = self._gen
            self._slot = None
            self._error = ""
            self.stats["started"] += 1
            thread = threading.Thread(
                target=self._work, name="jev-speculate",
                args=(gen, goal, els, action, snapshot, asked_over), daemon=True)
            self._thread = thread
        thread.start()
        return True

    def _work(self, gen: int, goal: str, els: Sequence[UIElement], action: Any,
              snapshot: Any, asked_over: str) -> None:
        try:
            spec = speculate(self.client, goal, els, action, snapshot=snapshot,
                             cache=self.cache, asked_over=asked_over)
        except Exception as exc:  # a prediction that failed is not a run failure
            with self._lock:
                self.stats["errors"] += 1
                self._error = "%s: %s" % (type(exc).__name__, exc)
            return
        with self._lock:
            self.stats["answered"] += 1
            if gen != self._gen:
                # Abandoned while in flight: still answered, still billed.
                self._bill(spec)
                return
            self._slot = spec

    def _bill(self, spec: Speculation) -> None:
        """Move a speculation's spend to the unclaimed pot. Caller holds the lock."""
        self._unclaimed[0] += int(spec.tokens_in)
        self._unclaimed[1] += float(spec.cost_usd)

    def take(self) -> Optional[Speculation]:
        """The pending prediction, or ``None`` — without ever blocking the step.

        A call still in flight is abandoned here rather than waited on. That is
        the whole budget rule: settle finished first, so the prediction lost its
        race and waiting for it would turn a saving into a delay.
        """
        thread = self._thread
        if thread is not None and thread.is_alive() and self.join_ms > 0:
            thread.join(self.join_ms / 1000.0)
        with self._lock:
            if self._slot is not None:
                spec, self._slot = self._slot, None
                return spec
            if thread is not None and thread.is_alive():
                self.stats["wasted"] += 1
                self._gen += 1  # whatever it returns is now unclaimed
            return None

    def drain_spend(self) -> Tuple[int, float]:
        """Tokens and cost of predictions nobody used, and reset the pot.

        The loop adds this to the step that happens to be running when it lands.
        The attribution is approximate; the total is not, and the total is what a
        "speculation costs +N%" claim depends on.
        """
        with self._lock:
            tokens, cost = int(self._unclaimed[0]), float(self._unclaimed[1])
            self._unclaimed = [0, 0.0]
        return tokens, cost

    def close(self, timeout_s: float = 0.0) -> None:
        """Stop caring about an in-flight prediction. Threads are daemons."""
        thread = self._thread
        if thread is not None and timeout_s > 0:
            thread.join(timeout_s)
        with self._lock:
            if self._slot is not None:
                self._bill(self._slot)
                self._slot = None
            self._gen += 1
            self._thread = None

    # -- the decision ------------------------------------------------------
    def consume(self, goal: str, candidates: Sequence[Any], *,
                risky_ids: Sequence[str] = (),
                thresholds: Mapping[str, float] = THRESHOLDS) -> Optional[Hit]:
        """Use the pending prediction for *this* screen, or return ``None``.

        Every refusal is counted under its own name in :attr:`stats`, because
        "the hit rate was 30%" is not actionable and "it was absent 60% of the
        time" is: absent means the screen changed more than predicted, low
        confidence means the question is too hard, risky means the guard fired.
        """
        spec = self.take()
        if spec is None:
            self.last_reason = "pending"
            return None
        if spec.op in self.refused_ops:
            self.last_reason = "refused_op"
            self.stats["refused_op"] += 1
            self._park(spec)
            return None
        if spec.op not in OPS:
            self.last_reason = "refused_op"
            self.stats["refused_op"] += 1
            self._park(spec)
            return None
        if spec.confidence < self.floor or spec.margin < self.margin:
            self.last_reason = "low_confidence"
            self.stats["low_confidence"] += 1
            self._park(spec)
            return None
        element, reason = match(spec, candidates)
        if element is None:
            self.last_reason = reason
            self.stats[reason if reason in self.stats else "absent"] += 1
            self._park(spec)
            return None
        if element.id in set(risky_ids):
            # The destructive Nouls were never asked. A control the name list
            # already flagged is the one case where this module refuses to be
            # the cheaper path.
            self.last_reason = "risky"
            self.stats["risky"] += 1
            self._park(spec)
            return None
        decision = as_decision(spec, element, candidates)
        verdict = validate(decision, candidates, thresholds=thresholds,
                           risky_ids=risky_ids)
        if not verdict.ok:
            self.last_reason = "invalid:%s" % verdict.reason
            self.stats["invalid"] += 1
            self._park(spec)
            return None
        self.last_reason = "ok"
        self.stats["used"] += 1
        return Hit(decision=decision, element=element, speculation=spec,
                   verdict=verdict)

    def _park(self, spec: Speculation) -> None:
        """A prediction that was answered and refused is still a paid-for call."""
        with self._lock:
            self.stats["wasted"] += 1
            self._bill(spec)

    # -- reporting ---------------------------------------------------------
    @property
    def hit_rate(self) -> float:
        """Used predictions over started ones. 0.0 when nothing was started."""
        started = self.stats["started"]
        return (self.stats["used"] / started) if started else 0.0

    def report(self) -> Dict[str, Any]:
        with self._lock:
            unclaimed = [int(self._unclaimed[0]), round(float(self._unclaimed[1]), 8)]
        out: Dict[str, Any] = dict(self.stats)
        out["hit_rate"] = round(self.hit_rate, 4)
        out["unclaimed_tokens"] = unclaimed[0]
        out["unclaimed_cost_usd"] = unclaimed[1]
        out["last_error"] = self._error
        return out


__all__ = [
    "NEW_ELEMENT", "NEXT_OP_INSTRUCTIONS", "NEXT_TARGET_INSTRUCTIONS",
    "NEW_ELEMENT_CRITERIA", "NONE_NEXT_CRITERIA", "REFUSED_OPS",
    "SPECULATION_FLOOR", "SPECULATION_MARGIN", "Hit", "Speculation",
    "Speculator", "as_decision", "build_speculation_bundle", "from_answer",
    "identity_key", "match", "next_element_criteria", "speculate",
]

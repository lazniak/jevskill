"""The step loop: observe, reduce, decide, validate, gate, act, settle, record.

This is ``skills/jev/references/act.md`` §8 with the parts the listing left out
put back — reading ``goal_reached`` and ``is_destructive``, re-checking the text
after a ``type``, a macro cache, a budget, and a record per step. Everything that
can touch the outside world is an argument, so the whole loop runs offline over
two fixture snapshots with a fake client and a fake executor. That is not a
testing convenience: it is the only way this code is exercised at all today, and
``tests/test_cu_loop.py`` is the only thing standing between a refactor and a
click on the wrong button.

**What stops a run, and who decides.** Stopping is always a code decision:

===============  ==========================================================
``done``         the injected ``verify`` oracle said so, or — with no
                 verifier — two consecutive validated ``done`` proposals,
                 which the record marks as unverified
``max_steps``    ``RunOptions.max_steps``
``budget``       ``RunOptions.budget_s`` of wall clock
``blocked``      two settles in a row with no tree change, or the human
                 gate refused, or ``op=done`` contradicted by its own
                 ``goal_reached``
``escalated``    an invalid or low-confidence decision that the injected
                 ``escalate`` did not answer
``error``        an exception escaped a stage
===============  ==========================================================

**The model never ends the run.** ``op=done`` is a proposal; act.md §3 is blunt
about it and ``jev-ultrafast`` is blunter: "A ``DONE`` choice still requires
independent outcome verification."

**Defaults refuse rather than guess.** ``confirm`` defaults to *deny*, so a run
with no human attached cannot click "Delete all documents"; ``compose_text``
defaults to raising, so a step that needs a file name escalates instead of
inventing one. Both are the direction of error this package chooses everywhere:
a needless escalation costs a step, a fabricated one costs the data.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import act as act_module
from .contract import RunOptions, RunResult, StepRecord
from .decide import (THRESHOLDS, Decision, build_state, decide as decide_step,
                     decide_cascade, goal_verdict, text_sanity_question,
                     validate)
from .hashing import tree_hash
from .macros import goal_fingerprint
from .reduce import candidates as reduce_candidates
from .types import Snapshot, UIElement, as_elements

#: Reduction cap, and the cascade trigger. ``candidates()`` truncates here, so a
#: list that comes back *at* the cap is one that reduction could not get under —
#: which is act.md §5's precondition for spending two calls instead of one.
CASCADE_CAP = 60

#: Consecutive invalid or low-confidence decisions before the run escalates.
#: Not a measured number: two is the smallest value that tolerates one stale
#: snapshot (the ~300 ms between the observation and the answer is long enough
#: for a dialog to close) without letting a genuinely confused run continue.
ESCALATE_AFTER_INVALID = 2

#: Executed actions that changed nothing, back to back, before the run stops.
#: act.md §7: "stop on ``unchanged`` twice".
STALL_LIMIT = 2

#: ``done`` proposals in a row that end a run when no code verifier exists.
#: Weaker evidence than a verifier and the record says so (``note``).
DONE_WITHOUT_VERIFIER = 2

#: Verdict reasons that mean "the snapshot went stale", not "the decision was
#: wrong". They are retried — re-observed and re-decided — before escalating.
LIVENESS_REASONS = frozenset({"unknown_target", "disabled_target"})


class TextNeeded(RuntimeError):
    """Raised by the default ``compose_text``: this loop does not invent text."""


def _deny(prompt: str, action: Any, element: Any) -> bool:
    """The default destructive gate: no human attached, so no."""
    return False


def _refuse_text(goal: str, element: Any) -> str:
    raise TextNeeded(
        "the step needs text for %r and no compose_text was injected; "
        "wire a small LLM (jev-ultrafast uses inception/mercury-2.5) or let "
        "this escalate — Jev is not trained to generate text"
        % (getattr(element, "name", None) or getattr(element, "id", "?")))


def run(goal: str, opts: Optional[RunOptions] = None, *, task_id: str = "",
        run: int = 0, observe: Optional[Callable[[], Snapshot]] = None,
        execute: Optional[Callable[..., Any]] = None, client: Any = None,
        ledger: Any = None, on_step: Optional[Callable[[StepRecord], None]] = None,
        verify: Optional[Callable[[str, Snapshot], bool]] = None,
        confirm: Optional[Callable[[str, Any, Any], bool]] = None,
        compose_text: Optional[Callable[[str, Any], str]] = None,
        escalate: Optional[Callable[[Dict[str, Any]], Any]] = None,
        macros: Any = None, backend: Any = None,
        thresholds: Mapping[str, float] = THRESHOLDS,
        cap: int = CASCADE_CAP,
        settle_ms: float = act_module.SETTLE_TIMEOUT_MS) -> RunResult:
    """Drive one goal to a stop, and record every step on the way.

    ``observe`` returns a fresh :class:`jevskill.cu.types.Snapshot`; ``execute``
    performs an :class:`jevskill.cu.act.Action` against it. Both default to the
    real thing (:func:`jevskill.cu.observe.snapshot`, imported lazily so this
    module stays importable off Windows, and :func:`jevskill.cu.act.execute`) and
    both are replaced wholesale in the tests.

    The step is observed fresh every iteration rather than reusing the snapshot
    :func:`jevskill.cu.act.settle` returned. Settle stops at the *first* differing
    hash, which can be a half-painted frame; deciding on it would trade a 40-400 ms
    walk for a class of bug that only appears on a slow machine. ``stages_ms``
    reports both walks separately so the cost is visible rather than argued.
    """
    options = opts or RunOptions()
    if observe is None:
        from .observe import snapshot as _snapshot  # local: Windows-only path

        observe = _snapshot
    executor = execute if execute is not None else act_module.execute
    if backend is not None and executor is act_module.execute:
        executor = functools.partial(act_module.execute, backend=backend)
    confirm_gate = confirm if confirm is not None else _deny
    write_text = compose_text if compose_text is not None else _refuse_text

    result = RunResult(task_id=task_id or goal_fingerprint(goal)[:40], run=run,
                       stop_reason="", steps=[])
    started = time.perf_counter()
    state = _LoopState(goal=goal)

    if client is not None and hasattr(client, "warm"):
        try:
            client.warm()
        except Exception:
            pass  # a warm-up that did not work is not a reason to fail the work

    try:
        for index in range(max(0, int(options.max_steps))):
            if (time.perf_counter() - started) >= float(options.budget_s):
                result.stop_reason = "budget"
                break
            step = _step(index, state, result, options, started,
                         observe=observe, executor=executor, client=client,
                         verify=verify, confirm_gate=confirm_gate,
                         write_text=write_text, escalate=escalate,
                         macros=macros, thresholds=thresholds, cap=cap,
                         settle_ms=settle_ms)
            result.steps.append(step.record)
            _record_ledger(ledger, goal, result, step)
            if on_step is not None:
                on_step(step.record)
            if step.stop_reason:
                result.stop_reason = step.stop_reason
                break
        else:
            result.stop_reason = "max_steps"
        if not result.stop_reason:
            result.stop_reason = "max_steps"
    except Exception as exc:  # a stage raised: the run ends, the records stay
        result.stop_reason = "error"
        result.error = "%s: %s" % (type(exc).__name__, exc)
    result.wall_ms = (time.perf_counter() - started) * 1000.0
    return result


class _LoopState:
    """What carries across steps. A class, so a typo is an AttributeError."""

    def __init__(self, goal: str) -> None:
        self.goal = goal
        #: The reduced hash of the screen *before* the last executed action,
        #: and the title beside it. act.md §8 computes ``last_action.outcome``
        #: from exactly this comparison, at the top of the next step, because a
        #: settle that caught a half-painted frame would otherwise report a
        #: change that did not survive.
        self.pre_hash: Optional[str] = None
        self.pre_title: Optional[str] = None
        self.acted = False
        self.last_action: Dict[str, Any] = {"type": None, "target": None,
                                            "outcome": None}
        self.invalid_streak = 0
        #: Executions that returned an error, counted separately from invalid
        #: decisions: a *valid* decision whose Invoke keeps failing is a broken
        #: control, and a validation that passed in between must not reset it.
        self.act_failures = 0
        self.stall_streak = 0
        self.done_streak = 0
        self.last_signature: Optional[Tuple[Any, ...]] = None


class _StepOutcome:
    """The step's record, plus the two numbers the ledger row wants and the
    fixed :class:`~jevskill.cu.contract.StepRecord` deliberately does not carry.

    ``StepRecord`` is a contract shared with ``bench/cu_report.py``; adding a
    field to it to carry a ledger detail would make every reader of a stored run
    handle a key that means nothing to the report.
    """

    def __init__(self, record: StepRecord, stop_reason: str = "",
                 questions: int = 0, state_tokens: int = 0) -> None:
        self.record = record
        self.stop_reason = stop_reason
        self.questions = questions
        self.state_tokens = state_tokens


def _step(index: int, state: _LoopState, result: RunResult, options: RunOptions,
          started: float, *, observe, executor, client, verify, confirm_gate,
          write_text, escalate, macros, thresholds, cap, settle_ms) -> _StepOutcome:
    """One iteration. Long, because the step *is* the sequence of its checks."""
    stages: Dict[str, float] = {}
    goal = state.goal

    mark = time.perf_counter()
    snapshot = observe()
    stages["observe"] = (time.perf_counter() - mark) * 1000.0

    mark = time.perf_counter()
    cands = reduce_candidates(snapshot.elements, cap)
    current_hash = tree_hash(cands)
    risky = act_module.risky_ids(cands)
    stages["reduce"] = (time.perf_counter() - mark) * 1000.0

    if state.acted and state.pre_hash is not None:
        # The authoritative answer to "did the last action do anything", made in
        # code from two hashes. The model is *told* the outcome; it is never
        # asked for it (prompting.md §11: that question measured 0.31-0.60 on
        # screens that had plainly changed).
        if current_hash == state.pre_hash:
            state.last_action["outcome"] = "unchanged"
        elif snapshot.window_title != state.pre_title:
            state.last_action["outcome"] = "new_window"
        else:
            state.last_action["outcome"] = "changed"
    state.acted = False
    # The outcome the macro lookup is keyed on, captured before anything in this
    # step can move it — the store at the end must use the same key.
    keyed_outcome = state.last_action.get("outcome")

    record = StepRecord(index=index, t_ms=0.0, stages_ms=stages,
                        candidates=len(cands))
    bookkeeping = {"questions": 0, "state_tokens": 0}

    def finish(stop_reason: str = "", note: str = "") -> _StepOutcome:
        if note:
            record.note = (record.note + "; " + note).strip("; ")
        record.t_ms = (time.perf_counter() - started) * 1000.0
        return _StepOutcome(record, stop_reason, bookkeeping["questions"],
                            bookkeeping["state_tokens"])

    # ---- decide: a macro replay, or one fan-out call ----------------------
    mark = time.perf_counter()
    decision: Optional[Decision] = None
    macro_hit = None
    if macros is not None:
        macro_hit = macros.lookup(goal, cands, keyed_outcome)
    if macro_hit is not None:
        decision = Decision(target=macro_hit[0], op=macro_hit[1], source="macro",
                            note="macro replay")
        record.decided_by = "macro"
    else:
        if client is None:
            return finish("error", "no client and no macro for step %d" % index)
        if len(cands) >= cap:
            # Reduction could not get under the cap: region first, element
            # second, which keeps both calls inside the measured option range.
            decision = decide_cascade(client, goal, snapshot.elements,
                                      state.last_action, cap=cap, risky_ids=risky,
                                      snapshot=snapshot)
            if decision.scope_ids:
                # Every later check runs against what the Choice actually ranged
                # over, which after a cascade is one region, not the screen.
                by_id = {el.id: el for el in as_elements(snapshot.elements)}
                cands = [by_id[i] for i in decision.scope_ids if i in by_id]
                record.candidates = len(cands)
                risky = act_module.risky_ids(cands)
        else:
            decision = decide_step(client, goal, cands, state.last_action,
                                   risky_ids=risky, snapshot=snapshot)
        record.decided_by = "jev"
        result.decisions += 1
        result.tokens_in += decision.tokens_in
        result.cost_usd += decision.cost_usd
        record.tokens_in = decision.tokens_in
        record.cost_usd = decision.cost_usd
        bookkeeping["questions"] = decision.questions
        bookkeeping["state_tokens"] = decision.state_tokens
    stages["decide"] = (time.perf_counter() - mark) * 1000.0

    record.target, record.op = decision.target, decision.op
    record.confidence = decision.confidence
    record.margin = decision.margin
    record.goal_reached = decision.goal_reached
    record.needs_text = decision.needs_text
    record.is_destructive = decision.is_destructive

    # ---- validate ---------------------------------------------------------
    mark = time.perf_counter()
    verdict = validate(decision, cands, thresholds=thresholds, risky_ids=risky,
                       require_confidence=(decision.source != "macro"))
    stages["validate"] = (time.perf_counter() - mark) * 1000.0

    # ---- stop conditions the decision itself raises -----------------------
    if verdict.stop:  # op == "done": a proposal, never a stop
        verified = None
        if verify is not None:
            try:
                verified = bool(verify(goal, snapshot))
            except Exception as exc:
                verified = False
                record.note = "verify raised: %s" % type(exc).__name__
        ruling = goal_verdict(decision, verified, thresholds=thresholds)
        if ruling == "stop":
            return finish("done", "verified")
        if ruling == "blocked":
            # `done` contradicted by its own goal_reached inside one call.
            return finish("blocked", "done proposed with goal_reached %.2f"
                          % decision.goal_reached)
        state.done_streak += 1
        if verify is None and state.done_streak >= DONE_WITHOUT_VERIFIER:
            return finish("done", "unverified: %d consecutive done proposals"
                          % state.done_streak)
        if verify is None:
            # Nothing was executed, so the tree will not change and the stall
            # counter must not see this step. Ask again on a fresh observation.
            state.last_action = {"type": "done", "target": None,
                                 "outcome": keyed_outcome}
            return finish("", "done proposed, no verifier: asking once more")
        return _escalate(state, result, record, finish, escalate, goal, snapshot,
                         cands, decision, verdict, executor, options,
                         "done_unverified")
    state.done_streak = 0

    if not verdict.ok:
        state.invalid_streak += 1
        retryable = (verdict.reason in LIVENESS_REASONS
                     and state.invalid_streak < ESCALATE_AFTER_INVALID)
        if retryable:
            # A stale target is a perception failure: re-observe and re-decide
            # rather than spending an escalation on it.
            state.last_action = {"type": decision.op, "target": decision.target,
                                 "outcome": "error"}
            return finish("", "%s: %s" % (verdict.reason, verdict.detail))
        return _escalate(state, result, record, finish, escalate, goal, snapshot,
                         cands, decision, verdict, executor, options,
                         verdict.reason)
    state.invalid_streak = 0

    # act.md §3, the row the reference loop left out: the op is not `done`, but
    # `goal_reached` is high. The verifier decides — pass and the run stops
    # anyway, fail and the chosen op proceeds. Without a verifier there is
    # nothing to consult and the step is ordinary; `goal_reached` alone never
    # ends a run.
    if (verify is not None
            and decision.goal_reached > float(
                thresholds.get("goal_reached_stop", THRESHOLDS["goal_reached_stop"]))):
        try:
            if bool(verify(goal, snapshot)):
                return finish("done", "verified while goal_reached=%.2f"
                              % decision.goal_reached)
        except Exception as exc:
            record.note = "verify raised: %s" % type(exc).__name__

    # ---- build the action -------------------------------------------------
    action = act_module.Action(op=verdict.op or "", target=verdict.target,
                               source=record.decided_by)
    if action.target == "none":
        action.target = None
    element = next((el for el in cands if el.id == action.target), None)

    wants_text = decision.needs_text >= float(thresholds.get("needs_text",
                                                             THRESHOLDS["needs_text"]))
    text_capable = element is not None and _is_text_target(element)
    if action.op == "type" or (wants_text and text_capable and action.op == "click"):
        if not wants_text and action.op == "type":
            # op and needs_text contradict each other inside one call; act.md
            # does not cover it, and guessing the text is the one thing this
            # loop refuses to do.
            return _escalate(state, result, record, finish, escalate, goal,
                             snapshot, cands, decision, verdict, executor,
                             options, "type_without_needs_text")
        try:
            action.text = write_text(goal, element)
        except Exception as exc:
            return _escalate(state, result, record, finish, escalate, goal,
                             snapshot, cands, decision, verdict, executor,
                             options, "needs_text:%s" % type(exc).__name__)
        action.op = "type"
        record.op = "type"

    # ---- the destructive gate: the name list first, the model second ------
    if verdict.requires_confirm:
        result.destructive_gates += 1
        prompt = "%s %r?" % (action.op, (element.name if element is not None else action.target))
        try:
            allowed = bool(confirm_gate(prompt, action, element))
        except Exception as exc:
            allowed = False
            record.note = "confirm raised: %s" % type(exc).__name__
        if not allowed:
            return finish("blocked", "destructive gate refused: %s" % prompt)

    # ---- act --------------------------------------------------------------
    mark = time.perf_counter()
    state.pre_hash = current_hash
    state.pre_title = snapshot.window_title
    state.acted = True
    act_result = executor(action, snapshot, dry_run=options.dry_run)
    stages["act"] = (time.perf_counter() - mark) * 1000.0
    record.executed = bool(getattr(act_result, "ok", False))
    if not record.executed:
        state.act_failures += 1
        state.acted = False
        state.last_action = {"type": action.op, "target": action.target,
                             "outcome": "error"}
        note = "act failed: %s" % getattr(act_result, "error", "")
        if state.act_failures >= ESCALATE_AFTER_INVALID:
            return _escalate(state, result, record, finish, escalate, goal,
                             snapshot, cands, decision, verdict, executor,
                             options, "act_failed")
        return finish("", note)
    state.act_failures = 0

    # ---- settle: the code-side `stuck` detector ---------------------------
    mark = time.perf_counter()
    after, changed, _waited = act_module.settle(
        observe, current_hash, timeout_ms=settle_ms,
        key=lambda snap: tree_hash(reduce_candidates(snap.elements, cap)))
    stages["settle"] = (time.perf_counter() - mark) * 1000.0
    record.tree_changed = bool(changed)

    outcome = "changed" if changed else "unchanged"
    if changed and after.window_title != snapshot.window_title:
        outcome = "new_window"
    state.last_action = {"type": action.op, "target": action.target,
                         "outcome": outcome}

    # ---- the post-`type` text check (act.md §4) ---------------------------
    if action.op == "type" and client is not None and not options.dry_run:
        ok, note, tokens, cost = _text_is_sane(client, goal, after, action, cap,
                                               thresholds)
        record.tokens_in += tokens
        record.cost_usd += cost
        result.tokens_in += tokens
        result.cost_usd += cost
        if note:
            record.note = note
        if ok is False:
            # typesafe-computer-use clears the field below 0.5 and retries.
            clear = act_module.Action(op="type", target=action.target, text="",
                                      source="code")
            executor(clear, after, dry_run=options.dry_run)
            state.last_action = {"type": "type", "target": action.target,
                                 "outcome": "error"}
            return finish("", "text rejected and cleared")

    # ---- macros: learn from a step that moved the screen -------------------
    if macros is not None and changed and record.decided_by == "jev":
        macros.store(goal, cands, keyed_outcome, action.target or "none", action.op)
    if macros is not None and not changed and record.decided_by == "macro":
        # A macro that no longer moves the screen is not stale, it is wrong.
        macros.invalidate(goal, cands, keyed_outcome)

    if not changed:
        state.stall_streak += 1
        signature = (action.op, action.target, action.text)
        differed = state.last_signature is not None and state.last_signature != signature
        state.last_signature = signature
        if state.stall_streak >= STALL_LIMIT:
            return finish("blocked", "tree unchanged after %d actions (%s)"
                          % (state.stall_streak,
                             "different" if differed else "identical"))
    else:
        state.stall_streak = 0
        state.last_signature = (action.op, action.target, action.text)

    return finish("")


def _is_text_target(element: UIElement) -> bool:
    from .decide import ALLOWED_PATTERNS, ALLOWED_ROLES

    return (element.role in ALLOWED_ROLES["type"]
            or bool(ALLOWED_PATTERNS["type"].intersection(element.patterns)))


def _text_is_sane(client: Any, goal: str, after: Snapshot, action: Any, cap: int,
                  thresholds: Mapping[str, float]
                  ) -> Tuple[Optional[bool], str, int, float]:
    """act.md §4's text-sanity Noul, asked against the screen as it now reads.

    Returns ``(ok, note, tokens_in, cost_usd)``; ``ok`` is ``None`` when the
    check could not be made — the field is gone, or the call failed — because
    "could not check" and "checked and it is wrong" must not clear the same
    field. The spend comes back with it: this is a second call inside a step
    budgeted for one, and a cost left out of the record is a cost the report
    will report as never paid.
    """
    cands = reduce_candidates(after.elements, cap)
    target = next((el for el in cands if el.id == action.target), None)
    if target is None:
        return (None, "text check skipped: %r not in the settled tree"
                % action.target, 0, 0.0)
    state = build_state(goal, after, {"type": "type", "target": action.target,
                                      "outcome": "changed"}, elements=cands)
    try:
        answer = client.decide(state, text_sanity_question(action.target))
    except Exception as exc:
        return None, "text check failed: %s" % type(exc).__name__, 0, 0.0
    probability = float(answer.noul("text_ok") or 0.0)
    floor = float(thresholds.get("text_sanity", THRESHOLDS["text_sanity"]))
    return ((probability >= floor), "text_ok=%.2f" % probability,
            int(answer.input_tokens), float(answer.cost_usd))


def _escalate(state: _LoopState, result: RunResult, record: StepRecord, finish,
              escalate, goal: str, snapshot: Snapshot, cands: Sequence[UIElement],
              decision: Decision, verdict: Any, executor, options: RunOptions,
              reason: str) -> _StepOutcome:
    """Hand the step to something bigger, or stop the run.

    Every escalation is counted, whether or not it was answered: the escalation
    *rate* is the metric that decides whether a loop is worth running, and a
    handler that silently fixes things would otherwise hide the cost.
    """
    result.escalations += 1
    record.note = ("escalated: %s" % reason).strip()
    if escalate is None:
        return finish("escalated", "")
    context = {"goal": goal, "reason": reason, "snapshot": snapshot,
               "candidates": list(cands), "decision": decision,
               "verdict": verdict, "step": record.index,
               "last_action": dict(state.last_action)}
    try:
        action = escalate(context)
    except Exception as exc:
        return finish("escalated", "escalate raised: %s" % type(exc).__name__)
    if action is None:
        return finish("escalated", "")
    record.decided_by = "escalation"
    record.op, record.target = action.op, action.target
    # An escalation's action is not settled or hashed here: whatever answered it
    # knows more than this loop does, and claiming an outcome it did not measure
    # would put a guess in the record. The next step's observation says what
    # actually happened.
    state.acted = False
    act_result = executor(action, snapshot, dry_run=options.dry_run)
    record.executed = bool(getattr(act_result, "ok", False))
    state.invalid_streak = 0
    state.last_action = {"type": action.op, "target": action.target,
                         "outcome": "changed" if record.executed else "error"}
    return finish("", "escalation handled: %s" % action.op)


def _record_ledger(ledger: Any, goal: str, result: RunResult,
                   step: _StepOutcome) -> None:
    """One ledger row per step, on the buffered writer.

    ``which="act"`` is the pattern name the ledger reports under, so a
    computer-use run shows up next to routing and triage in ``jevskill stats``
    rather than in a report of its own. Failures are swallowed: a ledger that
    cannot write must not end a run.
    """
    if ledger is None:
        return
    record = step.record
    try:
        ledger.record(
            which="act", intent=goal_fingerprint(goal)[:80],
            stages_ms=dict(record.stages_ms),
            latency_ms=sum(record.stages_ms.values()),
            tokens_in=record.tokens_in, cost_usd=record.cost_usd,
            questions=step.questions, state_tokens=step.state_tokens,
            confidence={"target": record.confidence, "margin": record.margin},
            session_id=result.task_id,
            extra={"step": record.index, "run": result.run, "op": record.op,
                   "target": record.target, "decided_by": record.decided_by,
                   "executed": record.executed,
                   "tree_changed": record.tree_changed,
                   "goal_reached": record.goal_reached,
                   "is_destructive": record.is_destructive,
                   "note": record.note},
        )
    except Exception:
        pass


__all__ = ["CASCADE_CAP", "DONE_WITHOUT_VERIFIER", "ESCALATE_AFTER_INVALID",
           "LIVENESS_REASONS", "RunOptions", "RunResult", "STALL_LIMIT",
           "StepRecord", "TextNeeded", "run"]

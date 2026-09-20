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
from .hashing import DEFAULT_IGNORE, tree_hash
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
        settle_ms: float = act_module.SETTLE_TIMEOUT_MS,
        speculation: Any = None, consistency: Any = None,
        beam_k: int = 1) -> RunResult:
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

    **Phase 5 hooks** (`skills/jev/references/speculate.md`). All three default
    to off, and with all three left alone this function's behaviour is what it
    was before they existed:

    ``speculation``  a :class:`jevskill.cu.speculate.Speculator`. Predicts the
                     next step while `settle` is idle; a prediction that
                     survives matching, the guards and `validate()` replaces
                     the next `decide` call and records ``decided_by =
                     "speculation"``.
    ``consistency``  a callable — :class:`jevskill.cu.consistency.ConsistencyGate`
                     — asked about the chosen target when the destructive
                     evidence is equivocal. It can only *add* a `confirm`.
    ``beam_k``       passed to :func:`jevskill.cu.decide.decide_cascade`. ``1``
                     is the greedy cascade.
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
                         settle_ms=settle_ms, speculation=speculation,
                         consistency=consistency, beam_k=beam_k)
            result.steps.append(step.record)
            _record_ledger(ledger, goal, result, step)
            if on_step is not None:
                on_step(step.record)
            if step.stop_reason:
                result.stop_reason = step.stop_reason
                break
        else:
            # Only a `break` skips this, and every `break` above sets a reason
            # first — so there is no third path that could leave it empty. The
            # `if not result.stop_reason` that used to follow was unreachable.
            result.stop_reason = "max_steps"
    except Exception as exc:  # a stage raised: the run ends, the records stay
        result.stop_reason = "error"
        result.error = "%s: %s" % (type(exc).__name__, exc)
    if speculation is not None:
        # Phase 5 hook: a prediction abandoned by the last step was still sent
        # and still billed. Recording it on the run rather than dropping it is
        # the difference between a measured overhead and a flattering one.
        try:
            speculation.close()
            tokens, cost = speculation.drain_spend()
            result.tokens_in += tokens
            result.cost_usd += cost
        except Exception:
            pass
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
        #: Which fields ``pre_hash`` was taken with. A ``type`` settles on a
        #: hash that keeps ``value`` (act.py's ``settle_ignore_for``), and
        #: comparing that against the default-ignore hash of the next screen
        #: would report a change on every step. One field, so the two ends of
        #: the comparison cannot drift apart.
        self.pre_ignore: Tuple[str, ...] = DEFAULT_IGNORE
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
          write_text, escalate, macros, thresholds, cap, settle_ms,
          speculation=None, consistency=None, beam_k: int = 1) -> _StepOutcome:
    """One iteration. Long, because the step *is* the sequence of its checks."""
    stages: Dict[str, float] = {}
    goal = state.goal

    mark = time.perf_counter()
    snapshot = observe()
    stages["observe"] = (time.perf_counter() - mark) * 1000.0

    mark = time.perf_counter()
    cands = reduce_candidates(snapshot.elements, cap)
    #: The full reduced list, kept because the cascade below replaces ``cands``
    #: with one region's members while ``current_hash`` — and every settle that
    #: has to compare against it — is over the whole screen.
    reduced = cands
    current_hash = tree_hash(cands)
    risky = act_module.risky_ids(cands)
    stages["reduce"] = (time.perf_counter() - mark) * 1000.0

    if state.acted and state.pre_hash is not None:
        # The authoritative answer to "did the last action do anything", made in
        # code from two hashes. The model is *told* the outcome; it is never
        # asked for it (prompting.md §11: that question measured 0.29-0.60 on
        # screens that had plainly changed).
        #
        # Hashed the way the previous op's settle hashed, or a `type` whose
        # whole effect is a new `value` reads "unchanged" here however well it
        # worked.
        comparable = (current_hash if state.pre_ignore == DEFAULT_IGNORE
                      else tree_hash(cands, ignore=state.pre_ignore))
        if comparable == state.pre_hash:
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
    # Phase 5 hook (speculate.py): a prediction made while the *previous* step
    # was settling. The macro cache still wins — a macro replay costs nothing at
    # all, and a speculation has already been paid for either way.
    spec_hit = None
    if macro_hit is None and speculation is not None:
        try:
            spec_hit = speculation.consume(goal, cands, risky_ids=risky,
                                           thresholds=thresholds)
        except Exception as exc:
            record.note = "speculation raised: %s" % type(exc).__name__
    if macro_hit is not None:
        decision = Decision(target=macro_hit[0], op=macro_hit[1], source="macro",
                            note="macro replay")
        record.decided_by = "macro"
    elif spec_hit is not None:
        decision = spec_hit.decision
        record.decided_by = "speculation"
        record.tokens_in = decision.tokens_in
        record.cost_usd = decision.cost_usd
        result.tokens_in += decision.tokens_in
        result.cost_usd += decision.cost_usd
    else:
        if client is None:
            return finish("error", "no client and no macro for step %d" % index)
        if len(cands) >= cap:
            # Reduction could not get under the cap: region first, element
            # second, which keeps both calls inside the measured option range.
            decision = decide_cascade(client, goal, snapshot.elements,
                                      state.last_action, cap=cap, risky_ids=risky,
                                      snapshot=snapshot, beam_k=beam_k)
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
    if speculation is not None:
        # Phase 5: predictions nobody used were still sent and still billed.
        # They land on whichever step is running when they do — the attribution
        # is rough, the total is exact, and the total is what any "+N% tokens"
        # claim about speculation rests on.
        wasted_tokens, wasted_cost = speculation.drain_spend()
        record.tokens_in += wasted_tokens
        record.cost_usd += wasted_cost
        result.tokens_in += wasted_tokens
        result.cost_usd += wasted_cost
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
                         "done_unverified", confirm_gate=confirm_gate,
                         current_hash=current_hash, reduced=reduced,
                         thresholds=thresholds)
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
                         verdict.reason, confirm_gate=confirm_gate,
                         current_hash=current_hash, reduced=reduced,
                         thresholds=thresholds)
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

    if action.op == "key":
        # act.md §2 asks for *one* `key` option and promises code will choose
        # the chord. Without this the Action reached `execute` with `key=None`
        # and was refused ("key without a chord"), twice, and the run
        # escalated — with the model's answer having been right both times.
        chord = act_module.chord_for(goal, cands, target=action.target,
                                     snapshot=snapshot,
                                     last_action=state.last_action)
        if chord.key is None:
            return _escalate(state, result, record, finish, escalate, goal,
                             snapshot, cands, decision, verdict, executor,
                             options, "no_chord", confirm_gate=confirm_gate,
                             current_hash=current_hash, reduced=reduced,
                             thresholds=thresholds)
        action.key = chord.key
        record.note = (record.note + "; key %s (%s)"
                       % (chord.key, chord.why)).strip("; ")
        # A keystroke that fires a destructive default is the same action as
        # clicking it, so it takes the same gate. `requires_confirm` is only
        # ever raised here, never cleared.
        verdict.requires_confirm = verdict.requires_confirm or chord.requires_confirm

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
                             options, "type_without_needs_text",
                             confirm_gate=confirm_gate,
                             current_hash=current_hash, reduced=reduced,
                             thresholds=thresholds)
        try:
            action.text = write_text(goal, element)
        except Exception as exc:
            return _escalate(state, result, record, finish, escalate, goal,
                             snapshot, cands, decision, verdict, executor,
                             options, "needs_text:%s" % type(exc).__name__,
                             confirm_gate=confirm_gate,
                             current_hash=current_hash, reduced=reduced,
                             thresholds=thresholds)
        if action.op != "type":
            # The model said `click`, `needs_text` said otherwise and the
            # target holds text: code, not Jev, chose what happens next. That
            # is what `contract.DECIDED_BY`'s "code" means, and until now
            # nothing in the package ever set it, so `bench/cu_report.py` could
            # only ever print zero for it.
            record.decided_by = action.source = "code"
            record.note = (record.note + "; op click -> type: needs_text %.2f"
                           % decision.needs_text).strip("; ")
        action.op = "type"
        record.op = "type"

    # Phase 5 hook (consistency.py): three formulations in one call, consulted
    # when the destructive evidence is equivocal — the name list said nothing
    # and the single Noul landed between "probably fine" and the 0.85 gate,
    # which is exactly where act.md §9 measured 0.79 for "Delete all documents".
    # It can only *add* a confirmation; `requires_confirm` is never cleared.
    if consistency is not None and element is not None and not verdict.requires_confirm:
        single = decision.destructive_for(action.target)
        gate = float(thresholds.get("destructive_noul", THRESHOLDS["destructive_noul"]))
        # `None` is "never asked", which `validate` has already turned into a
        # confirmation for a command control — there is no equivocal single
        # answer here for a second formulation to break the tie on.
        if single is not None and 0.5 <= single < gate:
            try:
                agreed = consistency(goal, cands, element,
                                     last_action=state.last_action, snapshot=snapshot)
            except Exception as exc:
                agreed = None
                record.note = "consistency raised: %s" % type(exc).__name__
            if agreed is not None and getattr(agreed, "destructive", False):
                verdict.requires_confirm = True
                record.note = (record.note + "; consistency %d/3 raised confirm"
                               % agreed.agree).strip("; ")

    # ---- the destructive gate: the name list first, the model second ------
    if verdict.requires_confirm:
        result.destructive_gates += 1
        allowed, note = _confirm(confirm_gate, action, element)
        if note:
            record.note = (record.note + "; " + note).strip("; ")
        if not allowed:
            return finish("blocked", "destructive gate refused: %s"
                          % _gate_prompt(action, element))

    # ---- act --------------------------------------------------------------
    mark = time.perf_counter()
    # The hash this action will be judged by, taken *before* it runs and with
    # the fields that op can move (act.py's `settle_ignore_for`): the default
    # ignores `value`, which is the whole effect of a `type`.
    settle_ignore = act_module.settle_ignore_for(action.op)
    state.pre_hash = (current_hash if settle_ignore == DEFAULT_IGNORE
                      else tree_hash(reduced, ignore=settle_ignore))
    state.pre_ignore = settle_ignore
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
                             options, "act_failed", confirm_gate=confirm_gate,
                             current_hash=current_hash, reduced=reduced,
                             thresholds=thresholds)
        return finish("", note)
    state.act_failures = 0

    # Phase 5 hook (speculate.py): the settle below is 40-800 ms of polling a
    # hash. Start the prediction *now*, on a thread, so it runs inside that idle
    # rather than after it; `consume` at the top of the next step abandons it if
    # settle won the race.
    if speculation is not None:
        try:
            speculation.start(goal, cands, action, snapshot=snapshot,
                              asked_over=current_hash)
        except Exception as exc:
            record.note = (record.note + "; speculation start failed: %s"
                           % type(exc).__name__).strip("; ")

    # ---- settle: the code-side `stuck` detector ---------------------------
    mark = time.perf_counter()
    # act.md §4's per-control cap, applied the only way it can be: as a floor.
    # `settle` polls a hash and one poll is a live `snapshot()` walk (40-400 ms
    # measured), so capping *down* to 50 ms would call every slow repaint
    # "unchanged" and stop real runs on the stall rule. The 200 ms combobox
    # figure still earns its keep when a caller sets a shorter ceiling.
    timeout = float(settle_ms)
    if act_module.settle_cap_for(element) > act_module.SETTLE_CAP_MS:
        timeout = max(timeout, act_module.settle_cap_for(element))
    after, changed, _waited = act_module.settle(
        observe, state.pre_hash, timeout_ms=timeout,
        key=lambda snap: tree_hash(reduce_candidates(snap.elements, cap),
                                   ignore=settle_ignore))
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
            # Nothing the model said produced this action, so the record says
            # "code" — the value `contract.DECIDED_BY` reserves for a
            # deterministic override.
            clear = act_module.Action(op="type", target=action.target, text="",
                                      source="code")
            executor(clear, after, dry_run=options.dry_run)
            record.decided_by = "code"
            state.last_action = {"type": "type", "target": action.target,
                                 "outcome": "error"}
            return finish("", "text rejected and cleared")

    # ---- macros: learn from a step that moved the screen -------------------
    #
    # `changed` here is settle's answer, and this module's docstring calls that
    # non-authoritative on purpose: settle stops at the *first* differing hash,
    # which can be a half-painted frame that the next observation does not
    # confirm. Storing on it anyway is the deliberate trade. Deferring the
    # store to the top of the next step would buy a stricter fact at the price
    # of never learning from the last step of a run, and the entry is not
    # trusted for long either way: `invalidate` below drops any macro whose
    # replay leaves the tree unchanged, which is exactly what an entry learned
    # from a frame that did not survive will do on its first replay. One wasted
    # replay, self-correcting, versus a macro cache that only fills in the
    # middle of runs.
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
              reason: str, *, confirm_gate=_deny,
              current_hash: Optional[str] = None,
              reduced: Sequence[UIElement] = (),
              thresholds: Mapping[str, float] = THRESHOLDS) -> _StepOutcome:
    """Hand the step to something bigger, or stop the run.

    Every escalation is counted, whether or not it was answered: the escalation
    *rate* is the metric that decides whether a loop is worth running, and a
    handler that silently fixes things would otherwise hide the cost.

    **The handler's answer is an Action, not an authorisation.** It goes
    through :func:`jevskill.cu.decide.validate` and the destructive gate exactly
    like a model decision. It did not, once: a probe handed this function an
    Action clicking "Delete all documents" and it was executed with
    ``destructive_gates == 0`` and ``confirm`` never called — while this
    module's docstring promised a run with no human attached "cannot click
    'Delete all documents'". A VLM or a person answering an escalation is
    exactly the party most likely to propose something irreversible, and
    nothing about being asked for help makes an answer safe.

    The confidence floor is *not* applied (``require_confidence=False``): an
    escalation has no probability distribution, the same as a macro replay, and
    fabricating one to get it past the floor would put that number in the
    ledger where it would read as a measurement.
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

    risky = act_module.risky_ids(cands)
    proposal = Decision(target=action.target or "none", op=action.op,
                        source="escalation", note="escalation: %s" % reason)
    ruling = validate(proposal, cands, thresholds=thresholds, risky_ids=risky,
                      require_confidence=False)
    if not ruling.ok:
        return finish("escalated", "escalation refused: %s %s"
                      % (ruling.reason, ruling.detail))
    element = next((el for el in cands if el.id == action.target), None)
    if action.op == "key" and action.key:
        # The handler named the chord. Enter or Delete on a screen whose
        # default button is destructive is the same act as clicking it.
        chord = act_module.chord_for(goal, cands, target=action.target,
                                     snapshot=snapshot,
                                     last_action=state.last_action)
        if chord.requires_confirm and act_module.parse_chord(action.key)[-1] in (
                "enter", "return", "delete"):
            ruling.requires_confirm = True
    if ruling.requires_confirm:
        result.destructive_gates += 1
        allowed, note = _confirm(confirm_gate, action, element)
        if note:
            record.note = (record.note + "; " + note).strip("; ")
        if not allowed:
            return finish("blocked", "destructive gate refused: %s"
                          % _gate_prompt(action, element))

    # An escalation's action is not settled here — whatever answered it knows
    # more than this loop does and a second poll would only cost time. The
    # outcome is left **unset** rather than claimed: `outcome: "changed"` was
    # written here from nothing but "the call returned", which is the one thing
    # `ActResult.ok` explicitly does not mean. The hash below is the same
    # baseline an ordinary step leaves behind, so the next step's observation
    # measures this action the same way it measures every other.
    state.acted = current_hash is not None
    if current_hash is not None:
        state.pre_hash = current_hash
        state.pre_ignore = act_module.settle_ignore_for(action.op)
        if state.pre_ignore != DEFAULT_IGNORE:
            # Over the *whole* reduced screen, which is what `current_hash` and
            # the next step's comparison cover — after a cascade `cands` is one
            # region, and hashing that would make every next step differ.
            state.pre_hash = tree_hash(reduced or cands, ignore=state.pre_ignore)
        state.pre_title = snapshot.window_title
    act_result = executor(action, snapshot, dry_run=options.dry_run)
    record.executed = bool(getattr(act_result, "ok", False))
    if not record.executed:
        state.acted = False
    state.invalid_streak = 0
    state.last_action = {"type": action.op, "target": action.target,
                         "outcome": None if record.executed else "error"}
    return finish("", "escalation handled: %s" % action.op)


def _gate_prompt(action: Any, element: Any) -> str:
    """What the human is asked. Names the chord for a key, the control for a click."""
    if action.op == "key" and action.key:
        return "key %s?" % action.key
    return "%s %r?" % (action.op,
                       (element.name if element is not None else action.target))


def _confirm(confirm_gate, action: Any, element: Any) -> Tuple[bool, str]:
    """Ask the human, and treat a gate that raises as a no.

    One implementation for both callers: the ordinary step and an escalation
    answer to the same rule, and a second copy is how they came to differ.
    """
    try:
        return bool(confirm_gate(_gate_prompt(action, element), action, element)), ""
    except Exception as exc:
        return False, "confirm raised: %s" % type(exc).__name__


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

"""The effectiveness ledger.

The point of this module is to replace *belief* about Jev with *measurement*.
Every decision is appended as one JSON line containing:

* the pattern the harness chose (``which``),
* what the baseline would have cost had an LLM done the same judgement
  (``baseline`` — tokens the harness did **not** ship to the expensive model),
* stage-by-stage timing inside our own boundary (``stages_ms``),
* the API's own numbers (``tokens_in``, ``cost_usd``, ``latency_ms``),
* the model's self-reported confidence, and
* optionally, later, whether the decision actually turned out right
  (``outcome``), which is what makes calibration measurable.

Correlation is by ``decision_id``; the harness calls :func:`record_outcome` once
the downstream consequence is known. That is what turns "Jev is fast" into
"Jev was right 94% of the time on this pattern, with a p50 of 310 ms".

Ledgers are JSONL, appended, and never rewritten, so concurrent harnesses can
write to the same file without coordination.
"""

from __future__ import annotations

import atexit
import json
import os
import statistics
import threading
import time
import uuid
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import INPUT_PRICE_PER_MTOK as JEV_INPUT_PRICE_PER_MTOK

LEDGER_DIRNAME = ".jevskill"
LEDGER_FILENAME = "ledger.jsonl"

#: What an expensive-model path would plausibly have cost for the same decision.
#: Deliberately conservative and *stated*, never silently assumed: the README
#: quotes whichever baseline it used so the reader can redo the arithmetic.
#:
#: ``tokens`` is the context an LLM would have had to *read* to make the same
#: judgement (the raw data plus instructions); the output is the JSON/answer it
#: would have had to *write*. Both are counted, because both are billed.
DEFAULT_BASELINE = {
    "model": "a frontier chat model",
    "input_price_per_mtok": 3.00,
    "output_price_per_mtok": 15.00,
    "output_tokens": 60,
    "overhead_tokens": 350,
}


def ledger_path(root: Path | str | None = None) -> Path:
    """Where this project's ledger lives.

    Precedence, and the order matters:

    1. an explicit ``root`` — the caller said exactly where, so we obey;
    2. ``JEVSKILL_LEDGER_DIR`` — a *default* for callers that did not say;
    3. ``<cwd>/.jevskill/ledger.jsonl`` — so stats travel with the repo.

    ``JEVSKILL_LEDGER_DIR`` used to win over an explicit ``root``, which meant a
    caller could pass a precise path and still have its records written somewhere
    else entirely. The symptom was a test that wrote ten decisions and read back
    zero, and it is the kind of bug that hides in whatever the environment
    happens to contain.

    The global ledger is separate: ``~/.jevskill/ledger.jsonl``.
    """
    if root is not None:
        return Path(root) / LEDGER_DIRNAME / LEDGER_FILENAME
    override = os.environ.get("JEVSKILL_LEDGER_DIR")
    if override:
        return Path(override) / LEDGER_FILENAME
    return Path.cwd() / LEDGER_DIRNAME / LEDGER_FILENAME


def global_ledger_path() -> Path:
    return Path.home() / LEDGER_DIRNAME / LEDGER_FILENAME


def new_decision_id() -> str:
    return f"d_{uuid.uuid4().hex[:12]}"


@dataclass
class Record:
    """One decision, as written to the ledger."""

    decision_id: str
    ts: str
    which: str
    intent: str = ""
    stages_ms: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    #: What the abandoned leg of a hedged call cost, and how that number was
    #: arrived at (``"estimated"``, or ``""`` when nothing was hedged). Without
    #: them ``cost_usd`` silently mixed a billed figure with an estimated one,
    #: and no reader of the ledger could tell which rows were which — in the
    #: file this project uses to claim savings. Rows written before these fields
    #: existed simply lack the keys; every reader goes through `load_records`,
    #: which works on plain dicts and uses `.get`, so they still load.
    hedge_cost_usd_est: float = 0.0
    hedge_cost_source: str = ""
    questions: int = 0
    state_tokens: int = 0
    confidence: dict[str, float] = field(default_factory=dict)
    baseline_tokens: int = 0
    baseline_cost_usd: float = 0.0
    #: Cost of the *data* alone at LLM rates, excluding the fixed scaffolding an
    #: LLM needs. See `_baseline_costs` for why both are stored.
    baseline_data_cost_usd: float = 0.0
    #: The same tokens priced at **Jev's** rate. The worth-it question is "does
    #: Jev earn its call", and answering it against a frontier model's rate is
    #: apples-to-oranges: Jev is ~71x cheaper per token, so its cost can never
    #: look significant next to a frontier-rate baseline.
    data_cost_at_jev_rates_usd: float = 0.0
    outcome: str = ""
    outcome_detail: str = ""
    session_id: str = ""
    version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _append(record: Record, *, root: Path | str | None = None) -> str:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json() + "\n")
    return record.decision_id


def _baseline_costs(baseline_tokens: int, baseline: dict | None) -> tuple[float, float, float]:
    """Return ``(full_cost, data_cost, data_cost_at_jev_rates)``.

    Three numbers, because they answer different questions and conflating them
    produced a real bug — twice.

    ``full_cost`` is what an LLM would actually be billed: the data, plus the
    scaffolding a chat model needs (system prompt, output-format instructions) and
    the output it would write. This is the honest counterfactual, and it is what
    the published savings percentage uses.

    ``data_cost`` is the data alone at LLM rates.

    ``data_cost_at_jev_rates`` is the same tokens priced at Jev's own rate.

    The worth-it question — "is Jev earning its round trip?" — must use
    ``data_cost_at_jev_rates``. Against the LLM rates Jev's own cost is roughly
    71x smaller per token, so it can never look significant; a 500-character state
    reads as $0.001 at frontier rates and $0.000014 at Jev's, which turns a
    "clearly worth it" verdict into the correct "this is too small to bother".
    The first version of this function compared the two rates directly and every
    state passed.
    """
    if not baseline_tokens and not baseline:
        return 0.0, 0.0, 0.0
    spec = {**DEFAULT_BASELINE, **(baseline or {})}
    in_price = float(spec["input_price_per_mtok"])
    out_price = float(spec["output_price_per_mtok"])
    billed_tokens = baseline_tokens + int(spec["overhead_tokens"])
    full = (billed_tokens / 1_000_000 * in_price
            + int(spec["output_tokens"]) / 1_000_000 * out_price)
    data = baseline_tokens / 1_000_000 * in_price
    data_at_jev = baseline_tokens / 1_000_000 * JEV_INPUT_PRICE_PER_MTOK
    return full, data, data_at_jev


def _data_cost_at_jev_rates(record: dict) -> float:
    """The row's data cost at Jev's rate, tolerating rows written before it existed.

    Ledgers are append-only and are not rewritten, so a row recorded by an earlier
    version simply lacks this field. Falling back to recomputing it from
    ``baseline_tokens`` keeps old ledgers useful instead of reporting every
    historical bucket as "state was never sized" — which is what happened the
    first time this field was introduced, and it made the advice command useless
    on exactly the data it was built to learn from.
    """
    stored = record.get("data_cost_at_jev_rates_usd")
    if stored:
        return float(stored)
    tokens = int(record.get("baseline_tokens", 0) or 0)
    return tokens / 1_000_000 * JEV_INPUT_PRICE_PER_MTOK


def record_decision(
    *,
    which: str,
    intent: str = "",
    stages_ms: dict[str, float] | None = None,
    latency_ms: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    hedge_cost_usd_est: float = 0.0,
    hedge_cost_source: str = "",
    questions: int = 0,
    state_tokens: int = 0,
    confidence: dict[str, float] | None = None,
    baseline_tokens: int = 0,
    baseline: dict | None = None,
    session_id: str = "",
    version: str = "",
    extra: dict | None = None,
    root: Path | str | None = None,
) -> str:
    """Append one decision. Returns its ``decision_id`` for later pairing."""
    return _append(
        build_record(
            which=which,
            intent=intent,
            stages_ms=stages_ms,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            hedge_cost_usd_est=hedge_cost_usd_est,
            hedge_cost_source=hedge_cost_source,
            questions=questions,
            state_tokens=state_tokens,
            confidence=confidence,
            baseline_tokens=baseline_tokens,
            baseline=baseline,
            session_id=session_id,
            version=version,
            extra=extra,
        ),
        root=root,
    )


def build_record(
    *,
    which: str,
    intent: str = "",
    stages_ms: dict[str, float] | None = None,
    latency_ms: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    hedge_cost_usd_est: float = 0.0,
    hedge_cost_source: str = "",
    questions: int = 0,
    state_tokens: int = 0,
    confidence: dict[str, float] | None = None,
    baseline_tokens: int = 0,
    baseline: dict | None = None,
    session_id: str = "",
    version: str = "",
    extra: dict | None = None,
) -> Record:
    """One ledger row, unwritten.

    Split out of :func:`record_decision` so the buffered :class:`Ledger` can
    produce **the same row** rather than a second row format that would drift
    from this one the first time a field was added.

    ``cost_usd`` is the total the caller actually spent, which on a hedged call
    includes an estimate for the abandoned leg. Pass ``hedge_cost_usd_est`` and
    ``hedge_cost_source`` from ``Decisions.usage`` alongside it so the estimated
    part stays separable — a blended total with no marker is exactly how a
    ledger starts lying about its own precision.
    """
    baseline_cost, baseline_data_cost, data_at_jev = _baseline_costs(baseline_tokens, baseline)
    return Record(
        decision_id=new_decision_id(),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        which=which,
        intent=intent,
        stages_ms=stages_ms or {},
        latency_ms=round(float(latency_ms), 3),
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
        cost_usd=float(cost_usd),
        hedge_cost_usd_est=float(hedge_cost_usd_est),
        hedge_cost_source=str(hedge_cost_source or ""),
        questions=int(questions),
        state_tokens=int(state_tokens),
        confidence=confidence or {},
        baseline_tokens=int(baseline_tokens),
        baseline_cost_usd=round(baseline_cost, 8),
        baseline_data_cost_usd=round(baseline_data_cost, 8),
        data_cost_at_jev_rates_usd=round(data_at_jev, 8),
        session_id=session_id,
        version=version,
        extra=extra or {},
    )


def record_outcome(decision_id: str, outcome: str, detail: str = "", *, root: Path | str | None = None) -> bool:
    """Pair a decision with what actually happened.

    ``outcome`` is free-form but the reporting understands these:
    ``correct`` / ``incorrect`` / ``escalated`` / ``overridden`` / ``no_action``.

    Appends a small patch record rather than rewriting history, so the ledger
    stays append-only and safe for concurrent writers.
    """
    patch = {
        "kind": "outcome",
        "decision_id": decision_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "outcome": outcome,
        "outcome_detail": detail,
    }
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(patch, ensure_ascii=False) + "\n")
    return True


# --------------------------------------------------------------------------- #
# Buffered writing: the ledger must not be part of the step budget
# --------------------------------------------------------------------------- #

#: Flush after this many buffered rows, whichever comes first with the interval.
#: 64 rows is ~25 seconds of a 400 ms agent loop and a few KB of text — small
#: enough to lose little to a kill -9, large enough that the file is opened
#: roughly once per minute instead of once per step.
DEFAULT_FLUSH_EVERY = 64
#: ...or after this long, so a loop that stops deciding still gets its rows on
#: disk promptly rather than at exit.
DEFAULT_FLUSH_INTERVAL_S = 1.0

#: Every open :class:`Ledger`, weakly. One module-level set and **one**
#: :mod:`atexit` hook, instead of ``atexit.register(self.close)`` per instance:
#: that registration held a bound method, so an un-closed ledger could never be
#: collected and fifty of them left fifty callbacks plus fifty live objects for
#: the interpreter to walk at exit. A ``WeakSet`` keeps the same guarantee
#: (anything still alive and open is flushed) while letting a forgotten ledger
#: be collected like any other object.
_OPEN_LEDGERS: "weakref.WeakSet[Ledger]" = weakref.WeakSet()


def _flush_open_ledgers() -> None:
    """Flush every ledger still open at interpreter exit.

    Iterates a snapshot because :meth:`Ledger.close` removes itself from the
    set, and swallows errors because an exception here would be raised from
    :mod:`atexit`, after the program's own last chance to react. A ledger whose
    final flush fails stays in the set, having already reported the failure to
    whoever called :meth:`Ledger.close`.
    """
    for ledger in list(_OPEN_LEDGERS):
        try:
            ledger.close()
        except Exception:
            pass


atexit.register(_flush_open_ledgers)


class Ledger:
    """An append-only ledger writer that keeps the file I/O off the hot path.

    :func:`record_decision` opens the file, writes one line, and closes it, once
    per decision. That is right for a CLI run and wrong for an agent loop that
    decides every few hundred milliseconds: the open/write/close lands inside the
    ``act`` stage, between the model answering and the agent moving, and on
    Windows it is the slowest thing in that stage by a wide margin.

    :meth:`write` serialises the row and appends the *text* to an in-memory
    buffer, then returns. A background thread drains the buffer every
    ``flush_every`` rows or ``flush_interval_s`` seconds, and :meth:`close` —
    reached at interpreter exit through :data:`_OPEN_LEDGERS` — drains whatever
    is left.

    Three properties are preserved exactly as the unbuffered path has them:

    * **append-only** — rows are appended, never rewritten, so several processes
      can share one file;
    * **order** — one lock, one list, written in hand-over order;
    * **no loss** — every accepted row reaches the file, including on a normal
      interpreter exit. A SIGKILL loses the buffer, which is the same guarantee
      any buffered writer gives, and is why ``flush_every`` is not large. A
      *failed* write does not lose anything either: the batch goes back at the
      front of the buffer and :attr:`flush_errors` counts the failure, so the
      next flush retries it and :meth:`close` raises rather than letting a loop
      finish believing its rows are on disk.

    The row text is built in :meth:`write`, not in the background thread, on
    purpose: a caller that mutates its ``Record`` after handing it over cannot
    then change what was recorded. Serialising costs a few microseconds — see
    ``python bench/cu_bench.py --micro`` for the measured number.

    ::

        with Ledger(root=Path.cwd()) as ledger:
            for step in loop:
                ledger.record(which="act", latency_ms=..., tokens_in=...)
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        path: Path | str | None = None,
        flush_every: int = DEFAULT_FLUSH_EVERY,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        start_thread: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else ledger_path(root)
        self.flush_every = max(1, int(flush_every))
        self.flush_interval_s = max(0.0, float(flush_interval_s))
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self.written = 0
        self.flushes = 0
        #: How many flushes have failed over this ledger's life. A background
        #: thread cannot raise at anyone, so the only alternative to a counter
        #: is silence — and silence is what turned five accepted rows into zero
        #: rows on disk with ``pending == 0``.
        self.flush_errors = 0
        self._last_error: Exception | None = None
        self._thread: threading.Thread | None = None
        _OPEN_LEDGERS.add(self)
        if start_thread and self.flush_interval_s > 0:
            self._thread = threading.Thread(
                target=self._run, name="jevskill-ledger", daemon=True
            )
            self._thread.start()

    # ---- hot path ---------------------------------------------------------
    def write(self, record: "Record | Mapping[str, Any]") -> str:
        """Buffer one row. Returns its ``decision_id`` (``""`` for a patch row).

        Accepts a :class:`Record` or a plain mapping; a mapping is how an outcome
        patch (``{"kind": "outcome", ...}``) is appended, so the append-only
        outcome mechanism works here too.
        """
        if self._closed:
            raise RuntimeError("ledger is closed")
        if isinstance(record, Record):
            line = record.to_json()
            decision_id = record.decision_id
        else:
            line = json.dumps(dict(record), ensure_ascii=False)
            decision_id = str(record.get("decision_id", "") or "")
        with self._lock:
            self._buffer.append(line)
            full = len(self._buffer) >= self.flush_every
        if full:
            self._wake.set()
        return decision_id

    def record(self, **fields: Any) -> str:
        """:func:`record_decision`'s arguments, buffered instead of written."""
        return self.write(build_record(**fields))

    def outcome(self, decision_id: str, outcome: str, detail: str = "") -> str:
        """Buffer an outcome patch — same shape as :func:`record_outcome`."""
        return self.write(
            {
                "kind": "outcome",
                "decision_id": decision_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "outcome": outcome,
                "outcome_detail": detail,
            }
        )

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ---- draining ---------------------------------------------------------
    def flush(self) -> int:
        """Write everything buffered so far. Returns the number of rows written.

        **The batch is written in one ``os.write`` to an ``O_APPEND`` file
        descriptor.** The previous version handed a ~22 KB batch (64 rows) to a
        buffered text writer with an 8 KiB buffer, which is three or more
        ``write`` calls; another process appending between them landed its line
        *inside* our batch. What the single write buys, precisely:

        * POSIX: ``O_APPEND`` makes the seek-to-end and the write one operation,
          so no other writer's bytes can appear inside this batch.
        * Windows: ``O_APPEND`` is emulated as seek-then-write, so the window is
          narrowed from "between the pieces of a batch" to "between one seek and
          one write" — smaller, not closed. A reader must still tolerate a torn
          final line, which :func:`read_ledger` does by skipping unparseable
          ones.
        * Neither: durability. Nothing is ``fsync``-ed, so a power cut can still
          lose what the OS had not written out.

        The bytes are UTF-8 with ``\\n`` line endings on every platform — the
        text writer used to translate those to ``\\r\\n`` on Windows, so the same
        ledger differed by platform for no reason anyone chose.

        **A failed write does not discard the batch.** It used to: the buffer was
        emptied before the write, so an ``open`` or ``write`` error dropped every
        row in it, and the background thread's bare ``except`` made that silent —
        five accepted rows, zero on disk, ``pending == 0``, against a docstring
        promising no loss. The batch now goes back at the *front* of the buffer,
        ahead of anything that arrived while the write was being attempted, so
        order survives the failure too.
        """
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return 0
        payload = ("\n".join(batch) + "\n").encode("utf-8")
        try:
            # One lock so a manual flush cannot interleave with the background
            # thread's, and one `os.write` so another *process* cannot either.
            with self._file_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    0o666,
                )
                try:
                    os.write(handle, payload)
                finally:
                    os.close(handle)
        except Exception as exc:
            with self._lock:
                self._buffer[:0] = batch
            self.flush_errors += 1
            self._last_error = exc
            raise
        self.written += len(batch)
        self.flushes += 1
        self._last_error = None
        return len(batch)

    def _run(self) -> None:
        while not self._closed:
            self._wake.wait(self.flush_interval_s)
            self._wake.clear()
            try:
                self.flush()
            except Exception:
                # A background writer that raises kills the thread and silently
                # stops recording. Losing one batch is bad; losing every later
                # batch without a word is worse, so keep the loop alive and try
                # again next tick — `flush` has already put the rows back and
                # counted the failure in `flush_errors`.
                pass

    def close(self) -> None:
        """Stop the thread and flush what is left. Safe to call twice.

        Raises if the final flush fails, and leaves the rows in the buffer. A
        loop that ends with ``ledger.close()`` returning normally may take that
        as "my rows are on disk"; the previous version made that claim even when
        every write had failed, because the background thread had already
        swallowed the error and the buffer had already been thrown away.
        """
        if not self._closed:
            self._closed = True
            self._wake.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self.flush()
        # Only after a flush that worked: a ledger whose rows are still in
        # memory stays in `_OPEN_LEDGERS`, so interpreter exit gets a last try.
        _OPEN_LEDGERS.discard(self)

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _apply_outcomes(records: list[dict], patches: list[dict]) -> list[dict]:
    """Overlay outcome patches onto their decisions.

    Every row already carries ``outcome``/``outcome_detail`` because ``Record``
    defines them, so an unpaired decision is one with an *empty* outcome, and the
    key set is identical across the whole ledger. That uniformity is intentional:
    a downstream reader can read any row without checking which fields exist.
    """
    by_id = {r["decision_id"]: r for r in records if r.get("decision_id")}
    for patch in patches:
        target = by_id.get(patch.get("decision_id", ""))
        if target is not None:
            target["outcome"] = patch.get("outcome", "")
            target["outcome_detail"] = patch.get("outcome_detail", "")
    return records


def read_ledger(path: Path | str | None = None) -> tuple[list[dict], list[dict]]:
    """Return ``(decisions, patches)`` from one ledger file."""
    target = Path(path) if path is not None else ledger_path()
    decisions: list[dict] = []
    patches: list[dict] = []
    if not target.exists():
        return decisions, patches
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("kind") == "outcome":
            patches.append(item)
        else:
            decisions.append(item)
    return decisions, patches


def load_records(paths: Iterable[Path | str] | None = None, *, include_global: bool | None = None) -> list[dict]:
    """Load and outcome-resolve records.

    With no arguments, reads this project's ledger and (unless told otherwise)
    also the global one, so a harness sees decisions made from any directory it
    worked in.

    **When explicit ``paths`` are given, only those are read.** Merging the
    global ledger into an explicitly-scoped query silently contaminates it — a
    report about one repository would quietly include every other repository's
    decisions. That bug is easy to introduce and hard to notice, so the default
    is the safe one: explicit paths mean exactly those paths.
    """
    if paths is None:
        candidates = [ledger_path()]
        if include_global is not False:
            global_path = global_ledger_path()
            if global_path != candidates[0]:
                candidates.append(global_path)
    else:
        candidates = [Path(p) for p in paths]
        if include_global:
            global_path = global_ledger_path()
            if global_path not in candidates:
                candidates.append(global_path)
    all_decisions: list[dict] = []
    all_patches: list[dict] = []
    for path in candidates:
        decisions, patches = read_ledger(path)
        all_decisions.extend(decisions)
        all_patches.extend(patches)
    all_decisions.sort(key=lambda r: r.get("ts", ""))
    return _apply_outcomes(all_decisions, all_patches)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def summarize(records: list[dict]) -> dict:
    """Aggregate a ledger into the numbers the README quotes.

    Returns overall volume/cost/latency, the realised saving against the stated
    LLM baseline, per-pattern breakdowns, and per-intent accuracy for every
    decision that was paired with an outcome.
    """
    if not records:
        return {"decisions": 0}

    latencies = [float(r.get("latency_ms", 0) or 0) for r in records]
    jev_cost = sum(float(r.get("cost_usd", 0) or 0) for r in records)
    baseline_cost = sum(float(r.get("baseline_cost_usd", 0) or 0) for r in records)
    baseline_tokens = sum(int(r.get("baseline_tokens", 0) or 0) for r in records)
    input_tokens = sum(int(r.get("tokens_in", 0) or 0) for r in records)

    by_which: dict[str, dict[str, Any]] = {}
    by_intent: dict[str, dict[str, Any]] = {}
    for record in records:
        for key, bucket in ((record.get("which") or "unspecified", by_which), (record.get("intent") or "", by_intent)):
            if not key:
                continue
            entry = bucket.setdefault(
                key, {"n": 0, "latencies": [], "cost": 0.0, "baseline_cost": 0.0,
                      "baseline_data_cost": 0.0, "data_at_jev_rates": 0.0,
                      "input_tokens": 0, "baseline_tokens": 0,
                      "outcomes": {}, "confidences": []}
            )
            entry["n"] += 1
            entry["latencies"].append(float(record.get("latency_ms", 0) or 0))
            entry["cost"] += float(record.get("cost_usd", 0) or 0)
            entry["baseline_cost"] += float(record.get("baseline_cost_usd", 0) or 0)
            entry["baseline_data_cost"] += float(
                record.get("baseline_data_cost_usd", 0) or 0
            )
            entry["data_at_jev_rates"] += _data_cost_at_jev_rates(record)
            entry["input_tokens"] += int(record.get("tokens_in", 0) or 0)
            entry["baseline_tokens"] += int(record.get("baseline_tokens", 0) or 0)
            outcome = record.get("outcome") or ""
            if outcome:
                entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
            for value in (record.get("confidence") or {}).values():
                entry["confidences"].append(float(value))

    def _finish(bucket: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in bucket.items():
            judged = entry["outcomes"].get("correct", 0) + entry["outcomes"].get("incorrect", 0)
            out[key] = {
                "n": entry["n"],
                "p50_ms": round(statistics.median(entry["latencies"]), 1) if entry["latencies"] else 0.0,
                "p95_ms": round(_pct(entry["latencies"], 0.95), 1),
                "cost_usd": round(entry["cost"], 6),
                "baseline_cost_usd": round(entry["baseline_cost"], 4),
                "baseline_data_cost_usd": round(entry["baseline_data_cost"], 6),
                "data_cost_at_jev_rates_usd": round(entry["data_at_jev_rates"], 6),
                "input_tokens": entry["input_tokens"],
                "comparable_tokens": entry["baseline_tokens"],
                "baseline_tokens": entry["baseline_tokens"],
                "outcomes": entry["outcomes"],
                "accuracy": (
                    round(entry["outcomes"].get("correct", 0) / judged, 4) if judged else None
                ),
                "mean_confidence": (
                    round(statistics.fmean(entry["confidences"]), 3) if entry["confidences"] else None
                ),
            }
        return out

    judged_total = sum(
        1 for r in records if r.get("outcome") in ("correct", "incorrect")
    )
    correct_total = sum(1 for r in records if r.get("outcome") == "correct")

    return {
        "decisions": len(records),
        "questions_asked": sum(int(r.get("questions", 0) or 0) for r in records),
        "input_tokens": input_tokens,
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p95": round(_pct(latencies, 0.95), 1),
            "min": round(min(latencies), 1) if latencies else 0.0,
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "cost_usd": round(jev_cost, 6),
        "cost_usd_per_decision": round(jev_cost / len(records), 8),
        "baseline_cost_usd": round(baseline_cost, 4),
        "baseline_tokens_avoided": baseline_tokens,
        "saved_usd": round(baseline_cost - jev_cost, 4),
        "saved_pct": (
            round((1 - jev_cost / baseline_cost) * 100, 1) if baseline_cost > 0 else None
        ),
        "by_pattern": _finish(by_which),
        "by_intent": _finish(by_intent),
        "accuracy": (round(correct_total / judged_total, 4) if judged_total else None),
        "judged": judged_total,
        "first_ts": records[0].get("ts", ""),
        "last_ts": records[-1].get("ts", ""),
    }


# --------------------------------------------------------------------------- #
# Advice: turn the ledger into "should we keep doing this?"
# --------------------------------------------------------------------------- #

#: Thresholds for the verdicts below. Stated as constants, not buried, because
#: they are policy and a reader must be able to disagree with them.
ADVICE = {
    # Below this many judged decisions, say "unproven" rather than guess.
    "min_judged_for_accuracy": 5,
    # An intent whose state costs less than this fraction of the decision itself
    # is not worth a network round trip, whatever the percentage says. The test
    # below is a straight economics comparison, so there is no absolute dollar
    # floor: at the measured $0.000013 per decision the crossover sits at roughly
    # 310 tokens of state, which is about 520 characters of log or code.
    "min_saved_pct": 20.0,
    # Accuracy at or below this means escalate the uncertain cases to the LLM.
    "min_accuracy": 0.85,
}


def _verdict_for(entry: dict) -> dict:
    """One row's verdict, with the numbers that produced it.

    Order matters. A saving too small to matter is reported first, because "we
    are not saving anything" is a more actionable finding than an accuracy figure
    on a call that should not be happening.

    The worth-it test uses the **data** cost, not the full LLM cost. Against the
    full baseline every state clears the bar, because a 350-token scaffolding floor
    dominates the ratio — see :func:`_baseline_costs`. Whether reduction is worth
    doing is a question about the state, so the state is what gets measured.
    """
    n = entry["n"]
    baseline_tokens = entry.get("baseline_tokens", 0) or 0
    baseline_cost = entry.get("baseline_cost_usd", 0.0) or 0.0
    data_cost = entry.get("baseline_data_cost_usd", 0.0) or 0.0
    # The worth-it test is token-for-token, which sidesteps a units trap that cost
    # two rewrites. Costs are *per decision*, but a REDUCE run is many decisions
    # over one document, so per-call cost ratios compared a chunk-sized read
    # against a document-sized baseline and reported -528% for a pipeline that is
    # in fact far cheaper.
    #
    # The honest frame: Jev's own billed input tokens, versus the tokens it
    # replaced. Both are the same currency — tokens read — so no price, no
    # scaffolding and no unit conversion can distort it.
    jev_tokens = int(entry.get("input_tokens", 0) or 0)
    comparable_tokens = int(entry.get("comparable_tokens", 0) or 0)
    saved_tokens = comparable_tokens - jev_tokens
    saved_pct = (saved_tokens / comparable_tokens * 100) if comparable_tokens else 0.0
    judged = (entry.get("outcomes") or {}).get("correct", 0) + \
             (entry.get("outcomes") or {}).get("incorrect", 0)
    accuracy = entry.get("accuracy")

    # Priced for the report only; the verdict never depends on these.
    comparable_cost = entry.get("data_cost_at_jev_rates_usd", 0.0) or 0.0
    cost = entry.get("cost_usd", 0.0) or 0.0

    facts = {
        "calls": n,
        "jev_tokens": jev_tokens,
        "tokens_replaced": comparable_tokens,
        "saved_tokens_per_call": round(saved_tokens / n) if n else 0,
        "saved_pct": round(saved_pct, 1),
        "baseline_tokens_per_call": round(baseline_tokens / n) if n else 0,
        "cost_usd_per_call": round(cost / n, 8) if n else 0.0,
        "comparable_cost_usd_per_call": round(comparable_cost / n, 8) if n else 0.0,
        "accuracy": accuracy,
        "judged": judged,
        "p50_ms": entry.get("p50_ms"),
    }

    if baseline_cost == 0 and cost == 0:
        return {"verdict": "no_baseline", "why": (
            "No baseline cost recorded, so there is nothing to compare against. "
            "Call with a --state so the ledger can size what an LLM would have read."
        ), "facts": facts}

    if comparable_tokens == 0:
        return {"verdict": "no_baseline", "why": (
            "The state was never sized, so there is no read to compare against. "
            "Pass a --state when recording the decision."
        ), "facts": facts}

    if saved_pct < ADVICE["min_saved_pct"]:
        return {"verdict": "not_worth_it", "why": (
            f"Across {n:,} recorded decision(s), Jev read {jev_tokens:,} input tokens "
            f"to replace {comparable_tokens:,} — a {saved_pct:.0f}% saving. A decision "
            "carries its own question text, so on small or heavily chunked states the "
            "reading it replaces does not cover the reading it costs. Answer these "
            "directly, batch fewer and larger decisions, or use a chat model."
        ), "facts": facts}

    if judged < ADVICE["min_judged_for_accuracy"]:
        return {"verdict": "unproven", "why": (
            f"Saves {saved_pct:.1f}% of the data cost but only {judged} decision(s) "
            "paired with an outcome. Run `jevskill outcome` on results to learn "
            "whether it is right, not just cheap."
        ), "facts": facts}

    if accuracy is not None and accuracy < ADVICE["min_accuracy"]:
        return {"verdict": "escalate", "why": (
            f"Accuracy {accuracy * 100:.0f}% over {judged} judged. It saves "
            f"{saved_pct:.0f}% of the data cost but is wrong too often to act on "
            "unattended — gate on confidence and escalate the uncertain cases to "
            "the LLM or a human."
        ), "facts": facts}

    if accuracy is not None and accuracy >= ADVICE["min_accuracy"]:
        return {"verdict": "worth_it", "why": (
            f"Saves {saved_pct:.0f}% of the data cost at {accuracy * 100:.0f}% "
            f"accuracy over {judged} judged decisions."
        ), "facts": facts}

    return {"verdict": "marginal", "why": "Saves tokens; accuracy not yet established here.",
            "facts": facts}


def advise(records: list[dict]) -> dict:
    """Turn the ledger into decisions about where to keep using Jev.

    The objective this project was built for was not only to *use* the model but
    to learn **when using it pays off**. `summarize()` reports what happened;
    this says what to do about it, per pattern and per intent, with the numbers
    that produced each verdict so it can be argued with rather than trusted.

    Verdicts, best-first in the output:

    ``worth_it``      saves tokens at acceptable measured accuracy
    ``not_worth_it``  a decision costs about what the read it replaces costs
    ``escalate``      cheap but too often wrong; gate on confidence
    ``no_baseline``   nothing to compare against
    ``marginal``      saves tokens, accuracy not yet established
    ``unproven``      the saving is real but no outcomes are paired yet

    **Granularity caveat.** The verdict is computed from *recorded decisions*, and
    a REDUCE run records one decision per chunk. A per-chunk decision reads its
    chunk plus its questions, so its token ratio is measured chunk-against-chunk —
    which can look unfavourable even when the pipeline as a whole replaces a large
    document with a short list. Record an operation-level entry, or read the
    pipeline totals, when you want to judge the pipeline.

    Building this required nothing new to be collected — baseline tokens, cost,
    confidence and paired outcomes were already in every row. It was the reading
    layer that was missing.
    """
    if not records:
        return {"verdicts": [], "summary": {"decisions": 0}, "actionable": []}

    summary = summarize(records)
    verdicts: list[dict] = []

    for scope, key in (("pattern", "by_pattern"), ("intent", "by_intent")):
        for name, entry in summary.get(key, {}).items():
            verdict = _verdict_for(entry)
            verdict.update({"scope": scope, "name": name})
            verdicts.append(verdict)

    # Most informative first. A confirmed win and a confirmed waste are both
    # actionable; "unproven" is the least interesting thing to read. An earlier
    # ordering put `worth_it` last, which buried the one finding a reader most
    # wants — where this thing is actually paying off.
    order = {
        "worth_it": 0,       # keep doing this
        "not_worth_it": 1,   # stop doing this
        "escalate": 2,       # change how you do it
        "no_baseline": 3,    # cannot tell
        "marginal": 4,
        "unproven": 5,       # least actionable: go and collect outcomes
    }
    verdicts.sort(key=lambda v: (order.get(v["verdict"], 9), -v["facts"]["calls"]))

    actionable = [v for v in verdicts if v["verdict"] in ("not_worth_it", "escalate")]
    keep = [v for v in verdicts if v["verdict"] == "worth_it"]

    return {
        "summary": {
            "decisions": summary["decisions"],
            "judged": summary["judged"],
            "saved_usd": summary["saved_usd"],
            "saved_pct": summary["saved_pct"],
            "overall_accuracy": summary["accuracy"],
        },
        "verdicts": verdicts,
        "actionable": actionable,
        "keep_using": keep,
        "thresholds": ADVICE,
    }


def format_advice(report: dict, *, limit_unproven: int = 5) -> str:
    """Render :func:`advise` for a human, actionable items first.

    ``unproven`` rows are capped. On a real ledger most intents are unproven,
    and a command that prints a hundred near-identical "pair more outcomes"
    lines is one nobody reads — which defeats the point of measuring at all.
    """
    if not report.get("verdicts"):
        return ("Nothing to advise yet — no decisions in the ledger.\n"
                "Run `jevskill ask ...` a few times, then `jevskill outcome <id> "
                "correct|incorrect` so it can learn accuracy, not just cost.")

    labels = {
        "not_worth_it": "STOP",
        "escalate": "ESCALATE",
        "no_baseline": "NO BASELINE",
        "unproven": "UNPROVEN",
        "marginal": "MARGINAL",
        "worth_it": "KEEP",
    }
    s = report["summary"]
    lines = [
        f"JEV advice — {s['decisions']} decisions, {s['judged']} judged",
        f"  saved ${s['saved_usd']:.4f}"
        + (f" ({s['saved_pct']:.1f}%)" if s["saved_pct"] is not None else "")
        + (f", accuracy {s['overall_accuracy'] * 100:.0f}%" if s["overall_accuracy"] is not None
           else ", accuracy not measured yet"),
        "",
    ]

    shown = 0
    unproven_seen = 0
    for verdict in report["verdicts"]:
        if verdict["verdict"] == "unproven":
            unproven_seen += 1
            if unproven_seen > limit_unproven:
                continue
        facts = verdict["facts"]
        label = labels.get(verdict["verdict"], verdict["verdict"].upper())
        acc = f"  acc {facts['accuracy'] * 100:.0f}%/{facts['judged']}" if facts["accuracy"] is not None else ""
        lines.append(
            f"  [{label:11s}] {verdict['scope']}:{verdict['name']}"
            f"  (n={facts['calls']}, saved {facts['saved_pct']:.0f}%{acc})"
        )
        lines.append(f"                {verdict['why']}")
        shown += 1

    if unproven_seen > limit_unproven:
        lines.append(
            f"  ... and {unproven_seen - limit_unproven} more unproven group(s). "
            "Use --json for all of them, or raise --limit-unproven."
        )

    lines += [
        "",
        f"  thresholds: accuracy {report['thresholds']['min_accuracy']:.0%}"
        f" · min saved {report['thresholds']['min_saved_pct']:.0f}%"
        f" · min judged {report['thresholds']['min_judged_for_accuracy']}",
        "  These are policy, not fact. Change them in stats.ADVICE if your risk tolerance differs.",
    ]
    return "\n".join(lines)
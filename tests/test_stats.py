"""Tests for the effectiveness ledger."""

from __future__ import annotations

import gc
import json
import os
import time
import weakref

import pytest

from jevskill import stats
from jevskill.stats import (
    DEFAULT_BASELINE,
    Ledger,
    Record,
    build_record,
    ledger_path,
    load_records,
    new_decision_id,
    read_ledger,
    record_decision,
    record_outcome,
    summarize,
)


@pytest.fixture()
def ledger(tmp_path):
    return tmp_path / ".jevskill" / "ledger.jsonl"


def write(ledger, **kwargs):
    # `ledger` is the ".jevskill/ledger.jsonl" path; record_decision takes the
    # root that *contains* the ".jevskill" directory.
    kwargs.setdefault("which", "gate")
    return record_decision(root=ledger.parent.parent, **kwargs)


class TestRecording:
    def test_appends_one_line_per_decision(self, ledger):
        write(ledger, latency_ms=300, tokens_in=400, cost_usd=0.00002)
        write(ledger, latency_ms=350, tokens_in=450, cost_usd=0.00003)
        decisions, _ = read_ledger(ledger)
        assert len(decisions) == 2

    def test_returns_a_unique_decision_id(self, ledger):
        first = write(ledger)
        second = write(ledger)
        assert first != second
        assert first.startswith("d_")

    def test_new_decision_id_is_prefixed_and_stable_length(self):
        ids = {new_decision_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(len(i) == 14 for i in ids)

    def test_record_is_valid_json_with_expected_fields(self, ledger):
        write(ledger, which="reduce", intent="filter logs", latency_ms=412.5,
              tokens_in=1000, cost_usd=0.00004, questions=3, state_tokens=800,
              confidence={"owner": 0.91}, baseline_tokens=5000)
        decisions, _ = read_ledger(ledger)
        record = decisions[0]
        assert record["which"] == "reduce"
        assert record["intent"] == "filter logs"
        assert record["latency_ms"] == 412.5
        assert record["tokens_in"] == 1000

    def test_creates_missing_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        record_decision(which="gate", root=nested)
        assert (nested / ".jevskill" / "ledger.jsonl").exists()

    def test_baseline_cost_is_derived_from_the_stated_baseline(self, ledger):
        write(ledger, baseline_tokens=100_000)
        decisions, _ = read_ledger(ledger)
        spec = DEFAULT_BASELINE
        billed = 100_000 + spec["overhead_tokens"]
        expected = (billed / 1e6 * spec["input_price_per_mtok"]
                    + spec["output_tokens"] / 1e6 * spec["output_price_per_mtok"])
        assert decisions[0]["baseline_cost_usd"] == pytest.approx(expected, rel=1e-6)

    def test_custom_baseline_overrides_prices(self, ledger):
        write(ledger, baseline_tokens=1_000_000,
              baseline={"input_price_per_mtok": 1.0, "output_price_per_mtok": 0.0,
                        "output_tokens": 0, "overhead_tokens": 0})
        decisions, _ = read_ledger(ledger)
        assert decisions[0]["baseline_cost_usd"] == pytest.approx(1.0)

    def test_no_baseline_tokens_means_no_baseline_cost(self, ledger):
        write(ledger)
        decisions, _ = read_ledger(ledger)
        assert decisions[0]["baseline_cost_usd"] == 0.0


class TestOutcomes:
    def test_pairs_an_outcome_with_its_decision(self, ledger):
        decision_id = write(ledger, latency_ms=300)
        record_outcome(decision_id, "correct", "reviewer agreed", root=ledger.parent.parent)
        records = load_records([ledger])
        assert records[0]["outcome"] == "correct"
        assert records[0]["outcome_detail"] == "reviewer agreed"

    def test_a_patch_for_an_unknown_id_is_harmless(self, ledger):
        write(ledger)
        record_outcome("d_doesnotexist", "correct", root=ledger.parent.parent)
        records = load_records([ledger])
        assert records[0]["outcome"] == ""

    def test_patches_never_rewrite_history(self, ledger):
        decision_id = write(ledger)
        record_outcome(decision_id, "correct", root=ledger.parent.parent)
        # The original decision line is untouched; the patch is appended. Every
        # row keeps the same key set (the Record schema), so an unpaired decision
        # is identified by an empty outcome rather than a missing field.
        decisions, patches = read_ledger(ledger)
        assert len(decisions) == 1
        assert len(patches) == 1
        assert decisions[0]["outcome"] == ""
        raw_first_line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert raw_first_line["outcome"] == ""
        assert patches[0]["outcome"] == "correct"

    def test_every_row_has_the_same_schema(self, ledger):
        write(ledger, which="gate")
        write(ledger, which="reduce", intent="logs", baseline_tokens=5000)
        decisions, _ = read_ledger(ledger)
        assert set(decisions[0]) == set(decisions[1])

    def test_later_outcome_wins(self, ledger):
        decision_id = write(ledger)
        record_outcome(decision_id, "incorrect", root=ledger.parent.parent)
        record_outcome(decision_id, "correct", "reviewer overruled", root=ledger.parent.parent)
        assert load_records([ledger])[0]["outcome"] == "correct"


class TestReading:
    def test_missing_ledger_is_empty_not_an_error(self, tmp_path):
        decisions, patches = read_ledger(tmp_path / "nope.jsonl")
        assert decisions == [] and patches == []

    def test_corrupt_lines_are_skipped(self, ledger):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"decision_id": "d_1", "which": "gate", "ts": "t"}\n'
                          'not json at all\n'
                          '{"decision_id": "d_2", "which": "gate", "ts": "t"}\n', encoding="utf-8")
        decisions, _ = read_ledger(ledger)
        assert [d["decision_id"] for d in decisions] == ["d_1", "d_2"]

    def test_blank_lines_are_skipped(self, ledger):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('\n\n{"decision_id": "d_1", "which": "gate", "ts": "t"}\n\n', encoding="utf-8")
        assert len(read_ledger(ledger)[0]) == 1

    def test_records_are_sorted_by_timestamp(self, ledger):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        for ts in ("2026-01-03T00:00:00", "2026-01-01T00:00:00", "2026-01-02T00:00:00"):
            ledger.write_text(ledger.read_text(encoding="utf-8") if ledger.exists() else "",
                              encoding="utf-8")
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"decision_id": ts, "which": "gate", "ts": ts}) + "\n")
        assert [r["ts"] for r in load_records([ledger])] == [
            "2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-03T00:00:00"
        ]


class TestSummarize:
    def test_empty_ledger(self):
        assert summarize([]) == {"decisions": 0}

    def test_volume_cost_and_latency(self, ledger):
        for latency, cost in ((100.0, 0.00001), (300.0, 0.00002), (500.0, 0.00003)):
            write(ledger, latency_ms=latency, cost_usd=cost, baseline_tokens=10_000)
        summary = summarize(load_records([ledger]))
        assert summary["decisions"] == 3
        assert summary["latency_ms"]["p50"] == 300.0
        assert summary["cost_usd"] == pytest.approx(0.00006)
        assert summary["cost_usd_per_decision"] == pytest.approx(0.00002)

    def test_reports_savings_against_the_baseline(self, ledger):
        write(ledger, cost_usd=0.00002, baseline_tokens=1_000_000)
        summary = summarize(load_records([ledger]))
        assert summary["baseline_cost_usd"] > summary["cost_usd"]
        assert summary["saved_usd"] > 0
        # With a real cost against the baseline, the saving is high but not total.
        assert 0 < summary["saved_pct"] <= 100.0
        assert summary["baseline_tokens_avoided"] == 1_000_000

    def test_saving_is_partial_when_jev_cost_is_material(self, ledger):
        write(ledger, cost_usd=1.0, baseline_tokens=1_000_000)
        summary = summarize(load_records([ledger]))
        assert 0 < summary["saved_pct"] < 100

    def test_saved_pct_is_none_without_a_baseline(self, ledger):
        write(ledger)
        assert summarize(load_records([ledger]))["saved_pct"] is None

    def test_groups_by_pattern_and_intent(self, ledger):
        write(ledger, which="gate", intent="pre-commit")
        write(ledger, which="gate", intent="pre-commit")
        write(ledger, which="reduce", intent="logs")
        summary = summarize(load_records([ledger]))
        assert summary["by_pattern"]["gate"]["n"] == 2
        assert summary["by_pattern"]["reduce"]["n"] == 1
        assert summary["by_intent"]["pre-commit"]["n"] == 2

    def test_accuracy_comes_only_from_paired_outcomes(self, ledger):
        good = write(ledger)
        bad = write(ledger)
        write(ledger)  # never paired
        record_outcome(good, "correct", root=ledger.parent.parent)
        record_outcome(bad, "incorrect", root=ledger.parent.parent)
        summary = summarize(load_records([ledger]))
        assert summary["judged"] == 2
        assert summary["accuracy"] == 0.5

    def test_accuracy_is_none_before_any_outcome(self, ledger):
        write(ledger)
        assert summarize(load_records([ledger]))["accuracy"] is None

    def test_escalations_do_not_count_as_wrong(self, ledger):
        decision_id = write(ledger)
        record_outcome(decision_id, "escalated", root=ledger.parent.parent)
        summary = summarize(load_records([ledger]))
        assert summary["accuracy"] is None
        assert summary["by_pattern"]["gate"]["outcomes"]["escalated"] == 1

    def test_per_pattern_accuracy(self, ledger):
        ids = [write(ledger, which="gate") for _ in range(4)]
        for i, decision_id in enumerate(ids):
            record_outcome(decision_id, "correct" if i < 3 else "incorrect",
                           root=ledger.parent.parent)
        summary = summarize(load_records([ledger]))
        assert summary["by_pattern"]["gate"]["accuracy"] == 0.75

    def test_mean_confidence_is_reported(self, ledger):
        write(ledger, confidence={"a": 0.8, "b": 0.6})
        summary = summarize(load_records([ledger]))
        assert summary["by_pattern"]["gate"]["mean_confidence"] == 0.7

    def test_questions_are_totalled(self, ledger):
        write(ledger, questions=3)
        write(ledger, questions=8)
        assert summarize(load_records([ledger]))["questions_asked"] == 11

    def test_window_is_reported(self, ledger):
        write(ledger)
        summary = summarize(load_records([ledger]))
        assert summary["first_ts"] and summary["last_ts"]

    def test_p95_handles_a_single_record(self, ledger):
        write(ledger, latency_ms=123.0)
        summary = summarize(load_records([ledger]))
        assert summary["latency_ms"]["p95"] == 123.0


class TestBufferedLedger:
    """`Ledger` writes the same rows, off the step budget.

    The hot loop cannot afford an open/write/close per decision (measured at
    ~0.6 ms on Windows, against a 0.5 ms budget for the whole `act` stage — see
    `python bench/cu_bench.py --micro`). Buffering is only acceptable if it
    keeps the three properties the unbuffered path has: nothing lost, order
    preserved, append-only.
    """

    def test_a_thousand_records_survive_with_their_order(self, tmp_path):
        with Ledger(root=tmp_path, flush_every=64, flush_interval_s=0.05) as ledger:
            ids = [ledger.record(which="act", intent=f"step-{i}", latency_ms=float(i))
                   for i in range(1000)]
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert len(decisions) == 1000, "buffered rows were lost"
        assert [d["decision_id"] for d in decisions] == ids, "rows were reordered"
        assert [d["intent"] for d in decisions] == [f"step-{i}" for i in range(1000)]

    def test_rows_are_identical_to_the_unbuffered_path(self, tmp_path):
        record_decision(which="gate", intent="same", latency_ms=1.0, tokens_in=10,
                        baseline_tokens=100, root=tmp_path / "direct")
        with Ledger(root=tmp_path / "buffered") as ledger:
            ledger.record(which="gate", intent="same", latency_ms=1.0, tokens_in=10,
                          baseline_tokens=100)
        direct, _ = read_ledger(tmp_path / "direct" / ".jevskill" / "ledger.jsonl")
        buffered, _ = read_ledger(tmp_path / "buffered" / ".jevskill" / "ledger.jsonl")
        assert set(direct[0]) == set(buffered[0])
        ignore = {"decision_id", "ts"}
        assert {k: v for k, v in direct[0].items() if k not in ignore} == \
               {k: v for k, v in buffered[0].items() if k not in ignore}

    def test_it_appends_to_an_existing_ledger(self, tmp_path):
        record_decision(which="gate", root=tmp_path)
        with Ledger(root=tmp_path) as ledger:
            ledger.record(which="act")
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert [d["which"] for d in decisions] == ["gate", "act"]

    def test_the_background_thread_flushes_without_a_close(self, tmp_path):
        ledger = Ledger(root=tmp_path, flush_every=5, flush_interval_s=0.01)
        try:
            for i in range(5):
                ledger.record(which="act", intent=str(i))
            deadline = time.time() + 5.0
            while ledger.written < 5 and time.time() < deadline:
                time.sleep(0.01)
            assert ledger.written == 5
            decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
            assert len(decisions) == 5
        finally:
            ledger.close()

    def test_outcomes_go_through_the_same_buffer(self, tmp_path):
        with Ledger(root=tmp_path) as ledger:
            decision_id = ledger.record(which="gate")
            ledger.outcome(decision_id, "correct", "verified downstream")
        records = load_records([tmp_path / ".jevskill" / "ledger.jsonl"])
        assert records[0]["outcome"] == "correct"
        assert records[0]["outcome_detail"] == "verified downstream"

    def test_write_accepts_a_prebuilt_record(self, tmp_path):
        row = build_record(which="reduce", intent="filter", tokens_in=7)
        with Ledger(root=tmp_path) as ledger:
            assert ledger.write(row) == row.decision_id
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert decisions[0]["tokens_in"] == 7

    def test_a_record_mutated_after_the_handover_cannot_change_what_was_written(self, tmp_path):
        # The row is serialised on the caller's thread, which is what makes this
        # true; a queue of live objects would record the mutation instead.
        row = build_record(which="gate", intent="before")
        ledger = Ledger(root=tmp_path, start_thread=False)
        ledger.write(row)
        row.intent = "after"
        ledger.close()
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert decisions[0]["intent"] == "before"

    def test_closing_twice_is_safe_and_writing_after_close_is_not_silent(self, tmp_path):
        ledger = Ledger(root=tmp_path)
        ledger.record(which="act")
        ledger.close()
        ledger.close()
        with pytest.raises(RuntimeError):
            ledger.record(which="act")
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert len(decisions) == 1

    def test_an_explicit_path_is_honoured(self, tmp_path):
        target = tmp_path / "somewhere" / "custom.jsonl"
        with Ledger(path=target) as ledger:
            ledger.record(which="act")
        decisions, _ = read_ledger(target)
        assert len(decisions) == 1

    def test_pending_reports_what_has_not_reached_disk(self, tmp_path):
        ledger = Ledger(root=tmp_path, flush_every=100, flush_interval_s=30.0,
                        start_thread=False)
        ledger.record(which="act")
        ledger.record(which="act")
        assert ledger.pending == 2
        assert ledger.flush() == 2
        assert ledger.pending == 0
        ledger.close()


class TestLedgerPath:
    def test_env_is_used_when_no_root_is_given(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path / "elsewhere"))
        assert ledger_path() == tmp_path / "elsewhere" / "ledger.jsonl"

    def test_explicit_root_beats_the_environment(self, tmp_path, monkeypatch):
        """An explicit root must mean exactly that root.

        The opposite precedence was a real bug: a caller could pass a precise
        path and still have its records written into whatever
        JEVSKILL_LEDGER_DIR happened to point at. It presented as a test that
        wrote ten decisions and read back zero.
        """
        monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path / "elsewhere"))
        assert ledger_path(tmp_path / "chosen") == \
            tmp_path / "chosen" / ".jevskill" / "ledger.jsonl"

    def test_explicit_root_is_not_affected_by_a_polluted_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path / "elsewhere"))
        decision_id = record_decision(which="gate", root=tmp_path / "chosen")
        written = tmp_path / "chosen" / ".jevskill" / "ledger.jsonl"
        assert written.is_file(), "record was written somewhere other than the given root"
        assert not (tmp_path / "elsewhere" / "ledger.jsonl").exists()
        decisions, _ = read_ledger(written)
        assert [d["decision_id"] for d in decisions] == [decision_id]

    def test_explicit_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JEVSKILL_LEDGER_DIR", raising=False)
        assert ledger_path(tmp_path) == tmp_path / ".jevskill" / "ledger.jsonl"


class TestRecord:
    def test_record_serialises_to_json(self):
        record = Record(decision_id="d_1", ts="t", which="gate", latency_ms=1.5)
        assert json.loads(record.to_json())["which"] == "gate"

    def test_record_defaults_are_safe(self):
        record = Record(decision_id="d_1", ts="t", which="gate")
        assert record.stages_ms == {} and record.confidence == {} and record.outcome == ""

class TestAFailedFlushLosesNothing:
    """The buffer must not be emptied before the write succeeds.

    Measured on the old code: five accepted rows, a failing `open`, and the
    ledger reported `pending == 0` with zero rows on disk — while its docstring
    promised no loss and the background thread's bare `except` said nothing.
    """

    def test_five_rows_survive_a_failing_open_and_land_after_recovery(self, tmp_path, monkeypatch):
        ledger = Ledger(root=tmp_path, flush_every=1000, flush_interval_s=30.0,
                        start_thread=False)
        for i in range(5):
            ledger.record(which="act", intent=f"step-{i}")

        real_open = os.open

        def broken(path, *args, **kwargs):
            raise OSError(13, "permission denied")

        monkeypatch.setattr(os, "open", broken)
        with pytest.raises(OSError):
            ledger.flush()
        assert ledger.pending == 5, "the batch was discarded by a failed write"
        assert ledger.flush_errors == 1
        assert ledger.written == 0

        monkeypatch.setattr(os, "open", real_open)
        assert ledger.flush() == 5
        ledger.close()
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert [d["intent"] for d in decisions] == [f"step-{i}" for i in range(5)]

    def test_the_failed_batch_goes_back_in_front_of_later_rows(self, tmp_path, monkeypatch):
        ledger = Ledger(root=tmp_path, flush_every=1000, flush_interval_s=30.0,
                        start_thread=False)
        for i in range(3):
            ledger.record(which="act", intent=f"early-{i}")
        real_open = os.open
        monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        with pytest.raises(OSError):
            ledger.flush()
        monkeypatch.setattr(os, "open", real_open)
        ledger.record(which="act", intent="late")
        ledger.close()
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert [d["intent"] for d in decisions] == ["early-0", "early-1", "early-2", "late"]

    def test_close_raises_rather_than_implying_the_rows_are_safe(self, tmp_path, monkeypatch):
        ledger = Ledger(root=tmp_path, flush_every=1000, flush_interval_s=30.0,
                        start_thread=False)
        ledger.record(which="act")
        monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        with pytest.raises(OSError):
            ledger.close()
        assert ledger.pending == 1, "the row was thrown away on close"

    def test_the_background_thread_retries_instead_of_dying(self, tmp_path, monkeypatch):
        real_open = os.open
        state = {"fail": True}

        def flaky(path, *args, **kwargs):
            if state["fail"]:
                raise OSError("not yet")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", flaky)
        ledger = Ledger(root=tmp_path, flush_every=1, flush_interval_s=0.01)
        try:
            ledger.record(which="act", intent="written eventually")
            deadline = time.time() + 5.0
            while ledger.flush_errors == 0 and time.time() < deadline:
                time.sleep(0.01)
            assert ledger.flush_errors >= 1, "the failure was not counted"
            state["fail"] = False
            deadline = time.time() + 5.0
            while ledger.written == 0 and time.time() < deadline:
                time.sleep(0.01)
            assert ledger.written == 1, "the thread died on the first failure"
        finally:
            monkeypatch.setattr(os, "open", real_open)
            ledger.close()


class TestOneAppendPerBatch:
    """A batch is one `os.write` to an `O_APPEND` descriptor.

    64 rows is ~22 KB through an 8 KiB buffered writer, so three or more
    `write` calls — and a second process appending between them landed its line
    inside our batch.
    """

    def test_a_batch_is_a_single_write_call(self, tmp_path, monkeypatch):
        ledger = Ledger(root=tmp_path, flush_every=1000, flush_interval_s=30.0,
                        start_thread=False)
        for i in range(64):
            ledger.record(which="act", intent=f"step-{i}")
        real_write = os.write
        calls: list[int] = []

        def counting(fd, data):
            calls.append(len(data))
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", counting)
        assert ledger.flush() == 64
        monkeypatch.setattr(os, "write", real_write)
        ledger.close()
        assert len(calls) == 1, f"{len(calls)} writes for one batch"
        assert calls[0] > 8192, "the batch was smaller than the buffer it must beat"

    def test_line_endings_are_lf_on_every_platform(self, tmp_path):
        with Ledger(root=tmp_path, start_thread=False) as ledger:
            ledger.record(which="act")
            ledger.record(which="gate")
        raw = (tmp_path / ".jevskill" / "ledger.jsonl").read_bytes()
        assert b"\r\n" not in raw, "the text writer translated the newlines"
        assert raw.count(b"\n") == 2

    def test_an_existing_ledger_is_appended_to_not_truncated(self, tmp_path):
        record_decision(which="gate", root=tmp_path)
        with Ledger(root=tmp_path, start_thread=False) as ledger:
            ledger.record(which="act")
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert [d["which"] for d in decisions] == ["gate", "act"]


class TestOneAtexitHookForEveryLedger:
    """`atexit.register(self.close)` per instance held a bound method, so an
    un-closed ledger could never be collected: fifty of them meant fifty live
    objects and fifty callbacks. One `WeakSet` and one hook keep the guarantee
    without the leak."""

    def test_fifty_ledgers_add_fifty_entries_and_no_hooks(self, tmp_path):
        ledgers = [
            Ledger(root=tmp_path / str(i), start_thread=False) for i in range(50)
        ]
        # Membership, not arithmetic: the registry holds weak references, so a
        # ledger left behind by an earlier test can be collected between the
        # `before` count and this line (it was, on Python 3.9).
        assert all(ledger in stats._OPEN_LEDGERS for ledger in ledgers)
        for ledger in ledgers:
            ledger.close()
        still_open = [ledger for ledger in ledgers if ledger in stats._OPEN_LEDGERS]
        assert not still_open, "closed ledgers stayed registered"

    def test_a_forgotten_ledger_can_be_collected(self, tmp_path):
        ledger = Ledger(root=tmp_path, start_thread=False)
        ref = weakref.ref(ledger)
        assert ref() is not None
        del ledger
        gc.collect()
        assert ref() is None, "something still holds the ledger — atexit, probably"

    def test_the_exit_hook_flushes_what_is_still_open(self, tmp_path):
        ledger = Ledger(root=tmp_path, flush_every=1000, flush_interval_s=30.0,
                        start_thread=False)
        ledger.record(which="act", intent="never closed")
        assert ledger.pending == 1
        stats._flush_open_ledgers()
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert [d["intent"] for d in decisions] == ["never closed"]

    def test_the_hook_is_registered_once_for_the_module(self, tmp_path):
        """Not a count of `atexit` internals — those are private — but the
        property that matters: the hook is a module-level function, not a bound
        method captured per instance."""
        assert callable(stats._flush_open_ledgers)
        assert stats._flush_open_ledgers.__module__ == "jevskill.stats"


class TestHedgeCostIsMarked:
    """A ledger that blends billed and estimated money with no marker is a
    ledger that lies about its own precision."""

    def test_the_fields_exist_and_default_to_nothing_hedged(self):
        row = build_record(which="act", cost_usd=0.001)
        assert row.hedge_cost_usd_est == 0.0
        assert row.hedge_cost_source == ""

    def test_build_record_carries_them_through(self, tmp_path):
        row = build_record(which="act", cost_usd=0.0012,
                           hedge_cost_usd_est=0.0002, hedge_cost_source="estimated")
        assert json.loads(row.to_json())["hedge_cost_source"] == "estimated"
        with Ledger(root=tmp_path, start_thread=False) as ledger:
            ledger.write(row)
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert decisions[0]["hedge_cost_usd_est"] == pytest.approx(0.0002)

    def test_record_decision_accepts_them(self, tmp_path):
        record_decision(which="act", cost_usd=0.0012, hedge_cost_usd_est=0.0002,
                        hedge_cost_source="estimated", root=tmp_path)
        decisions, _ = read_ledger(tmp_path / ".jevskill" / "ledger.jsonl")
        assert decisions[0]["hedge_cost_source"] == "estimated"

    def test_rows_written_before_the_fields_existed_still_load(self, tmp_path):
        """Old rows simply lack the keys. Every reader works on plain dicts, so
        a ledger written by an earlier version must still summarise."""
        target = tmp_path / ".jevskill" / "ledger.jsonl"
        target.parent.mkdir(parents=True)
        old = {
            "decision_id": "d_old", "ts": "2026-09-01T10:00:00", "which": "gate",
            "intent": "legacy", "latency_ms": 310.0, "tokens_in": 500,
            "cost_usd": 0.000021, "questions": 1, "baseline_tokens": 500,
            "baseline_cost_usd": 0.002, "outcome": "", "outcome_detail": "",
        }
        target.write_text(json.dumps(old) + "\n", encoding="utf-8")
        records = load_records([target])
        assert len(records) == 1
        assert records[0].get("hedge_cost_usd_est", 0.0) == 0.0
        summary = summarize(records)
        assert summary["decisions"] == 1

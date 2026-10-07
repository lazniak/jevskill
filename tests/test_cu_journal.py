"""``jevskill.cu.journal.RunStore``: the on-disk checkpoint/event log used to
resume a computer-use run after a crash or a STOP.

Offline, stdlib only, no desktop. ``now=`` is always passed explicitly to
``resumable()`` so staleness tests never need to sleep past ``STALE_S``.
"""

from __future__ import annotations

import json
import time

import pytest

from jevskill.cu.journal import (
    ACTIVE_STATES,
    EVENTS_FILE,
    RUN_FILE,
    SCHEMA,
    STALE_S,
    RunStore,
    UnknownRun,
    valid_run_id,
)

RUN_A = "20260927-221530-a1b2c3"
RUN_B = "20260927-221531-b2c3d4"  # one second after RUN_A -> sorts after it
RUN_C = "20260927-221532-c3d4e5"


def write_raw(tmp_path, run_id, payload):
    """Write ``run.json`` directly, bypassing ``RunStore.checkpoint``'s own
    ``updated_at`` stamp (it always uses the real wall clock). Needed for
    ordering/pruning tests, which must control ``updated_at`` precisely."""
    run_dir = tmp_path / "cu_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    full = dict(payload)
    full.setdefault("schema", SCHEMA)
    (run_dir / RUN_FILE).write_text(json.dumps(full), encoding="utf-8")


def make_payload(**overrides):
    payload = {
        "run_id": RUN_A,
        "command": "write a note",
        "state": "running",
        "heartbeat_at": time.time(),
        "tree": {"id": "0", "status": "running", "children": []},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# valid_run_id
# --------------------------------------------------------------------------- #


class TestValidRunId:
    def test_accepts_a_well_formed_id(self):
        assert valid_run_id(RUN_A) == RUN_A

    @pytest.mark.parametrize("bad", ["../x", "x", "", "20260927-221530-a1b2c3/../x",
                                      "20260927/221530-a1b2c3", "a" * 6])
    def test_rejects_traversal_and_malformed_ids(self, bad):
        with pytest.raises(ValueError):
            valid_run_id(bad)

    def test_rejects_non_str(self):
        with pytest.raises(ValueError):
            valid_run_id(12345)
        with pytest.raises(ValueError):
            valid_run_id(None)


# --------------------------------------------------------------------------- #
# checkpoint / load round trip
# --------------------------------------------------------------------------- #


class TestCheckpointAndLoad:
    def test_nothing_created_until_first_write(self, tmp_path):
        RunStore(tmp_path / "cu_runs")
        assert not (tmp_path / "cu_runs").exists()

    def test_atomic_write_and_load_round_trip(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        payload = make_payload(command="round trip")
        assert store.checkpoint(RUN_A, payload) is True

        run_file = tmp_path / "cu_runs" / RUN_A / RUN_FILE
        assert run_file.exists()
        # No stray temp file left behind after a successful write.
        leftovers = [p.name for p in run_file.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

        loaded = store.load(RUN_A)
        assert loaded["command"] == "round trip"
        assert loaded["schema"] == SCHEMA
        assert isinstance(loaded["updated_at"], float)

    def test_checkpoint_sets_schema_and_updated_at_without_mutating_caller_dict(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        payload = make_payload()
        assert "schema" not in payload
        store.checkpoint(RUN_A, payload)
        assert "schema" not in payload  # shallow copy, caller's dict untouched
        assert "updated_at" not in payload

    def test_checkpoint_overwrites_updated_at_even_if_caller_set_one(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        payload = make_payload(updated_at=1.0)
        store.checkpoint(RUN_A, payload)
        loaded = store.load(RUN_A)
        assert loaded["updated_at"] != 1.0  # overwritten with time.time()
        assert loaded["schema"] == SCHEMA  # setdefault: only fills a missing key

    def test_checkpoint_setdefault_keeps_callers_schema(self, tmp_path):
        # setdefault means a caller-supplied schema wins over SCHEMA -- read
        # the raw file rather than store.load(), which itself rejects any
        # schema other than SCHEMA by design (see TestCheckpointAndLoad
        # .test_load_raises_unknown_run_for_wrong_schema).
        store = RunStore(tmp_path / "cu_runs")
        store.checkpoint(RUN_A, make_payload(schema=999))
        run_file = tmp_path / "cu_runs" / RUN_A / RUN_FILE
        data = json.loads(run_file.read_text(encoding="utf-8"))
        assert data["schema"] == 999

    def test_checkpoint_returns_false_when_mkstemp_fails(self, tmp_path, monkeypatch):
        store = RunStore(tmp_path / "cu_runs")

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("jevskill.cu.journal.tempfile.mkstemp", boom)
        assert store.checkpoint(RUN_A, make_payload()) is False
        # And the run directory must not contain a leftover half-written file.
        run_dir = tmp_path / "cu_runs" / RUN_A
        assert not (run_dir / RUN_FILE).exists()

    def test_checkpoint_returns_false_when_replace_fails_and_cleans_up_tmp(self, tmp_path, monkeypatch):
        store = RunStore(tmp_path / "cu_runs")

        def boom(*a, **kw):
            raise OSError("cross-device link")

        monkeypatch.setattr("jevskill.cu.journal.os.replace", boom)
        assert store.checkpoint(RUN_A, make_payload()) is False
        run_dir = tmp_path / "cu_runs" / RUN_A
        assert not (run_dir / RUN_FILE).exists()
        assert list(run_dir.glob("*.tmp")) == []  # temp file removed on failure

    def test_checkpoint_returns_false_when_root_is_a_file_not_a_directory(self, tmp_path):
        # mkdir(parents=True) fails with NotADirectoryError (an OSError) when
        # a path component already exists as a plain file. This is the
        # cross-platform way to make the target unwritable without relying
        # on chmod, which Windows does not enforce the same way.
        blocker = tmp_path / "cu_runs"
        blocker.write_text("not a directory")
        store = RunStore(blocker)
        assert store.checkpoint(RUN_A, make_payload()) is False

    def test_checkpoint_returns_false_for_unserialisable_payload(self, tmp_path):
        # Plain `object()` *is* serialisable here (json.dumps falls back to
        # `default=str`), so the payload needs a value that raises inside
        # `str()` too, to exercise the TypeError/ValueError-from-serialisation
        # path the brief calls out explicitly.
        store = RunStore(tmp_path / "cu_runs")

        class Unstringable:
            def __str__(self):
                raise ValueError("nope")

        payload = make_payload(bad=Unstringable())
        assert store.checkpoint(RUN_A, payload) is False

    def test_load_raises_unknown_run_for_missing_file(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        with pytest.raises(UnknownRun):
            store.load(RUN_A)

    def test_load_raises_unknown_run_for_corrupt_json(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        run_dir = tmp_path / "cu_runs" / RUN_A
        run_dir.mkdir(parents=True)
        (run_dir / RUN_FILE).write_text("{not json", encoding="utf-8")
        with pytest.raises(UnknownRun):
            store.load(RUN_A)

    def test_load_raises_unknown_run_for_non_dict_json(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        run_dir = tmp_path / "cu_runs" / RUN_A
        run_dir.mkdir(parents=True)
        (run_dir / RUN_FILE).write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(UnknownRun):
            store.load(RUN_A)

    def test_load_raises_unknown_run_for_wrong_schema(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        store.checkpoint(RUN_A, make_payload())
        run_file = tmp_path / "cu_runs" / RUN_A / RUN_FILE
        data = json.loads(run_file.read_text(encoding="utf-8"))
        data["schema"] = SCHEMA + 1
        run_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(UnknownRun):
            store.load(RUN_A)

    def test_load_validates_run_id_first(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        with pytest.raises(ValueError):
            store.load("../x")


# --------------------------------------------------------------------------- #
# append_event / events
# --------------------------------------------------------------------------- #


class TestEvents:
    def test_append_event_writes_n_lines(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        n = 12
        for i in range(n):
            assert store.append_event(RUN_A, {"i": i, "kind": "step"}) is True
        events_file = tmp_path / "cu_runs" / RUN_A / EVENTS_FILE
        lines = events_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n

    def test_events_since_filters(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        for i in range(10):
            store.append_event(RUN_A, {"i": i})
        got = store.events(RUN_A, since=7)
        assert [e["i"] for e in got] == [7, 8, 9]

    def test_events_limit(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        for i in range(10):
            store.append_event(RUN_A, {"i": i})
        got = store.events(RUN_A, since=0, limit=3)
        assert [e["i"] for e in got] == [0, 1, 2]

    def test_events_missing_file_returns_empty(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        assert store.events(RUN_A) == []

    def test_events_skips_corrupt_lines(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        run_dir = tmp_path / "cu_runs" / RUN_A
        run_dir.mkdir(parents=True)
        with open(run_dir / EVENTS_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"i": 0, "ok": True}) + "\n")
            f.write("{not json\n")
            f.write(json.dumps(["not", "a", "dict"]) + "\n")
            f.write(json.dumps({"ok": True}) + "\n")  # missing "i"
            f.write("\n")  # blank line
            f.write(json.dumps({"i": 1, "ok": True}) + "\n")
        got = store.events(RUN_A)
        assert [e["i"] for e in got] == [0, 1]

    def test_append_event_returns_false_on_unserialisable_item(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")

        class Unstringable:
            def __str__(self):
                raise ValueError("nope")

        assert store.append_event(RUN_A, {"bad": Unstringable()}) is False


# --------------------------------------------------------------------------- #
# resumable()
# --------------------------------------------------------------------------- #


class TestResumable:
    def _store(self, tmp_path):
        return RunStore(tmp_path / "cu_runs")

    def test_skips_corrupt_and_foreign_entries(self, tmp_path):
        store = self._store(tmp_path)
        root = tmp_path / "cu_runs"
        root.mkdir(parents=True)
        # A foreign directory: not a valid run id at all.
        (root / "not-a-run-id").mkdir()
        (root / "not-a-run-id" / RUN_FILE).write_text("{}", encoding="utf-8")
        # A foreign *file* sitting next to run directories.
        (root / "README.txt").write_text("hello", encoding="utf-8")
        # A valid-looking run id whose run.json is corrupt.
        (root / RUN_B).mkdir()
        (root / RUN_B / RUN_FILE).write_text("{not json", encoding="utf-8")

        rows = store.resumable(now=time.time())
        assert rows == []

    def test_active_state_with_stale_heartbeat_is_interrupted(self, tmp_path):
        store = self._store(tmp_path)
        now = time.time()
        store.checkpoint(RUN_A, make_payload(state="running", heartbeat_at=now - STALE_S - 5))
        rows = store.resumable(now=now)
        assert len(rows) == 1
        assert rows[0]["run_id"] == RUN_A
        assert rows[0]["interrupted"] is True
        assert rows[0]["heartbeat_age_s"] == pytest.approx(STALE_S + 5, abs=1.0)

    def test_active_state_missing_heartbeat_counts_as_stale(self, tmp_path):
        store = self._store(tmp_path)
        payload = make_payload(state="waiting_confirm")
        del payload["heartbeat_at"]
        store.checkpoint(RUN_A, payload)
        rows = store.resumable(now=time.time())
        assert len(rows) == 1
        assert rows[0]["interrupted"] is True
        assert rows[0]["heartbeat_age_s"] is None

    def test_active_state_with_fresh_heartbeat_is_hidden(self, tmp_path):
        store = self._store(tmp_path)
        now = time.time()
        store.checkpoint(RUN_A, make_payload(state="running", heartbeat_at=now - 1.0))
        rows = store.resumable(now=now)
        assert rows == []

    def test_active_state_equal_to_live_run_id_is_hidden_even_when_stale(self, tmp_path):
        store = self._store(tmp_path)
        now = time.time()
        store.checkpoint(RUN_A, make_payload(state="running", heartbeat_at=now - STALE_S - 5))
        rows = store.resumable(now=now, live_run_id=RUN_A)
        assert rows == []

    @pytest.mark.parametrize("state", list(ACTIVE_STATES))
    def test_every_active_state_can_be_listed_when_stale(self, tmp_path, state):
        store = self._store(tmp_path)
        now = time.time()
        store.checkpoint(RUN_A, make_payload(state=state, heartbeat_at=now - STALE_S - 1))
        rows = store.resumable(now=now)
        assert len(rows) == 1
        assert rows[0]["state"] == state
        assert rows[0]["interrupted"] is True

    def test_stopped_is_listed_not_interrupted(self, tmp_path):
        store = self._store(tmp_path)
        store.checkpoint(RUN_A, make_payload(state="stopped"))
        rows = store.resumable(now=time.time())
        assert len(rows) == 1
        assert rows[0]["state"] == "stopped"
        assert rows[0]["interrupted"] is False

    def test_done_is_never_listed(self, tmp_path):
        store = self._store(tmp_path)
        store.checkpoint(RUN_A, make_payload(state="done"))
        rows = store.resumable(now=time.time())
        assert rows == []

    def test_failed_with_open_node_is_listed(self, tmp_path):
        store = self._store(tmp_path)
        tree = {"id": "0", "status": "failed", "children": [
            {"id": "1", "status": "done", "children": []},
            {"id": "2", "status": "pending", "children": []},
        ]}
        store.checkpoint(RUN_A, make_payload(state="failed", tree=tree))
        rows = store.resumable(now=time.time())
        assert len(rows) == 1
        assert rows[0]["state"] == "failed"
        assert rows[0]["interrupted"] is False

    def test_failed_with_no_open_node_is_hidden(self, tmp_path):
        store = self._store(tmp_path)
        tree = {"id": "0", "status": "failed", "children": [
            {"id": "1", "status": "done", "children": []},
            {"id": "2", "status": "failed", "children": []},
        ]}
        store.checkpoint(RUN_A, make_payload(state="failed", tree=tree))
        rows = store.resumable(now=time.time())
        assert rows == []

    def test_failed_with_only_superseded_open_node_is_hidden(self, tmp_path):
        store = self._store(tmp_path)
        tree = {"id": "0", "status": "failed", "children": [
            {"id": "1", "status": "pending", "superseded": True, "children": []},
        ]}
        store.checkpoint(RUN_A, make_payload(state="failed", tree=tree))
        rows = store.resumable(now=time.time())
        assert rows == []

    def test_progress_defaults_to_empty_dict(self, tmp_path):
        store = self._store(tmp_path)
        store.checkpoint(RUN_A, make_payload(state="stopped", progress=None))
        rows = store.resumable(now=time.time())
        assert rows[0]["progress"] == {}

    def test_orders_newest_first(self, tmp_path):
        store = self._store(tmp_path)
        base = time.time()
        write_raw(tmp_path, RUN_A, make_payload(state="stopped", updated_at=base - 30))
        write_raw(tmp_path, RUN_B, make_payload(state="stopped", updated_at=base - 10))
        write_raw(tmp_path, RUN_C, make_payload(state="stopped", updated_at=base - 20))
        rows = store.resumable(now=base)
        assert [r["run_id"] for r in rows] == [RUN_B, RUN_C, RUN_A]

    def test_orders_newest_first_falling_back_to_run_id_when_updated_at_missing(self, tmp_path):
        # RUN_A/B/C timestamps in their own ids are already in increasing
        # order; with no `updated_at` at all the id's own timestamp prefix
        # must be what orders them.
        store = self._store(tmp_path)
        for rid in (RUN_A, RUN_B, RUN_C):
            payload = make_payload(state="stopped")
            del payload["heartbeat_at"]
            write_raw(tmp_path, rid, payload)
        rows = store.resumable(now=time.time())
        assert [r["run_id"] for r in rows] == [RUN_C, RUN_B, RUN_A]

    def test_honours_limit(self, tmp_path):
        store = self._store(tmp_path)
        base = time.time()
        ids = [RUN_A, RUN_B, RUN_C]
        for i, rid in enumerate(ids):
            write_raw(tmp_path, rid, make_payload(state="stopped", updated_at=base - i))
        rows = store.resumable(limit=2, now=base)
        assert len(rows) == 2

    def test_resumable_on_missing_root_returns_empty(self, tmp_path):
        store = RunStore(tmp_path / "does-not-exist")
        assert store.resumable(now=time.time()) == []


# --------------------------------------------------------------------------- #
# prune()
# --------------------------------------------------------------------------- #


class TestPrune:
    def test_prune_keeps_the_newest_n(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        base = time.time()
        ids = [RUN_A, RUN_B, RUN_C]
        for i, rid in enumerate(ids):
            # updated_at descends: RUN_A newest, RUN_C oldest.
            write_raw(tmp_path, rid, make_payload(state="stopped", updated_at=base - i))
        removed = store.prune(keep=2)
        assert removed == 1
        remaining = {p.name for p in (tmp_path / "cu_runs").iterdir()}
        assert remaining == {RUN_A, RUN_B}  # RUN_C had the smallest updated_at

    def test_prune_never_raises_on_missing_root(self, tmp_path):
        store = RunStore(tmp_path / "does-not-exist")
        assert store.prune(keep=5) == 0

    def test_prune_ignores_foreign_directories(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        (tmp_path / "cu_runs").mkdir(parents=True)
        (tmp_path / "cu_runs" / "not-a-run-id").mkdir()
        store.checkpoint(RUN_A, make_payload(state="stopped"))
        removed = store.prune(keep=0)
        assert removed == 1
        assert (tmp_path / "cu_runs" / "not-a-run-id").exists()

    def test_prune_counts_dirs_with_unloadable_checkpoints(self, tmp_path):
        store = RunStore(tmp_path / "cu_runs")
        root = tmp_path / "cu_runs"
        root.mkdir(parents=True)
        (root / RUN_A).mkdir()
        (root / RUN_A / RUN_FILE).write_text("{not json", encoding="utf-8")
        removed = store.prune(keep=0)
        assert removed == 1
        assert not (root / RUN_A).exists()

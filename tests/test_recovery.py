"""Tests for the recovery handle — the promise that REDUCE is reversible.

The script shipped with no tests at all, which is how a misleading message
survived: asking for an index that was *kept* printed "no rejected items
matched", which the reader takes as data loss — the one fear this handle exists
to remove. Everything here writes a synthetic record to a temp store; no network.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills" / "jev" / "scripts" / "jev_recovery.py"
)


def _load() -> object:
    spec = importlib.util.spec_from_file_location("jev_recovery_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = _load()

#: The kept indices are the salient lines; everything else was rejected.
ITEMS = [
    "INFO  healthcheck ok in 2ms",
    "INFO  cache hit for product:482",
    "ERROR payment capture failed after 3 retries",
    "INFO  user login ok",
    "INFO  email queued",
    "WARN  db connection pool at 94% capacity",
    "INFO  static asset served",
    "INFO  feature flag evaluated",
    "ERROR FATAL disk write failed",
    "INFO  metrics flushed",
]
KEPT_INDEXES = {2, 5, 8}
HANDLE = "rc_0000000000ff"


def write_record(store: Path, *, kept_indexes=None, handle: str = HANDLE) -> str:
    """Write a record shaped exactly like the one REDUCE produces."""
    kept_indexes = KEPT_INDEXES if kept_indexes is None else kept_indexes
    kept = [{"index": i, "p": 0.9, "text": ITEMS[i]} for i in sorted(kept_indexes)]
    rejected = [
        {"index": i, "text": text}
        for i, text in enumerate(ITEMS)
        if i not in kept_indexes
    ]
    record = {
        "handle": handle,
        "created": "2026-01-01T00:00:00",
        "source": "synthetic-state.json",
        "total_items": len(ITEMS),
        "kept": kept,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "raw_tokens": 1000,
        "kept_tokens": 250,
        "calls": 2,
        "cost_usd": 0.00005,
    }
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{handle}.json").write_text(json.dumps(record), encoding="utf-8")
    return handle


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "recovery"
    write_record(path)
    return path


def run(capsys, store: Path, argv: list[str]):
    code = recovery.main([*argv, "--recovery-dir", str(store)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestNothingIsLost:
    def test_kept_and_rejected_partition_the_original(self, store: Path):
        record = json.loads((store / f"{HANDLE}.json").read_text(encoding="utf-8"))
        indices = {r["index"] for r in record["rejected"]} | {
            k["index"] for k in record["kept"]
        }
        assert indices == set(range(len(ITEMS)))

    def test_every_rejected_item_is_retrievable(self, capsys, store: Path):
        code, out, _ = run(capsys, store, [HANDLE, "--all"])
        assert code == 0
        assert len(out.strip().splitlines()) == len(ITEMS) - len(KEPT_INDEXES)


class TestSelectors:
    def test_index_returns_exactly_those_rejected_items(self, capsys, store: Path):
        code, out, _ = run(capsys, store, [HANDLE, "--index", "0", "3"])
        assert code == 0
        assert ITEMS[0] in out and ITEMS[3] in out
        assert ITEMS[1] not in out

    def test_grep_filters_the_rejected_items(self, capsys, store: Path):
        code, out, _ = run(capsys, store, [HANDLE, "--grep", "login"])
        assert code == 0
        assert ITEMS[3] in out
        assert len(out.strip().splitlines()) == 1

    def test_out_writes_the_selection(self, capsys, tmp_path, store: Path):
        target = tmp_path / "remainder.txt"
        code, _, _ = run(capsys, store, [HANDLE, "--all", "--out", str(target)])
        assert code == 0
        assert len(target.read_text(encoding="utf-8").splitlines()) == len(ITEMS) - 3

    def test_summary_reports_the_token_math(self, capsys, store: Path):
        code, out, _ = run(capsys, store, [HANDLE, "--summary"])
        assert code == 0
        assert "1000 -> 250" in out and "75.0%" in out


class TestHonestReporting:
    """Asking for a kept index must not read as data loss."""

    def test_kept_index_says_kept_not_missing(self, capsys, store: Path):
        code, out, err = run(capsys, store, [HANDLE, "--index", str(min(KEPT_INDEXES))])
        assert code == 0
        assert "kept" in err
        assert "matched" not in err

    def test_out_of_range_index_is_named_as_such(self, capsys, store: Path):
        _, _, err = run(capsys, store, [HANDLE, "--index", "99"])
        assert "out of range" in err

    def test_mixed_request_labels_each_index(self, capsys, store: Path):
        code, out, err = run(capsys, store, [HANDLE, "--index", "0", "2", "99"])
        assert code == 0
        assert ITEMS[0] in out  # the one genuinely rejected is still returned
        assert "kept" in err and "out of range" in err

    def test_json_stdout_stays_pure_when_the_note_fires(self, capsys, store: Path):
        code, out, err = run(capsys, store, [HANDLE, "--index", "0", "2", "--json"])
        assert code == 0
        assert [row["index"] for row in json.loads(out)] == [0]
        assert "kept" in err  # the diagnostic went to stderr, not into the JSON

    def test_absent_grep_match_is_reported_as_no_match(self, capsys, store: Path):
        code, out, _ = run(capsys, store, [HANDLE, "--grep", "zzzz"])
        assert code == 0
        assert "no rejected items matched" in out


class TestListAndErrors:
    def test_list_shows_the_handle_and_counts(self, capsys, store: Path):
        code, out, _ = run(capsys, store, ["--list"])
        assert code == 0
        assert HANDLE in out
        assert f"{len(ITEMS)} items -> {len(KEPT_INDEXES)} kept" in out

    def test_missing_handle_names_the_path_it_looked_in(self, capsys, store: Path):
        with pytest.raises(SystemExit) as exc:
            recovery.main(["rc_deadbeef", "--recovery-dir", str(store)])
        assert exc.value.code == 1
        assert "rc_deadbeef" in capsys.readouterr().err

    def test_empty_store_is_not_an_error(self, capsys, tmp_path: Path):
        code, out, _ = run(capsys, tmp_path / "nothing-here", ["--list"])
        assert code == 0
        assert "no recovery store" in out
"""Every published perception number must still come out of the fixtures.

``bench/cu_observe_results.json`` went stale without anybody editing it.
``--save-fixtures`` scrubbed each snapshot *after* ``measure()`` had already
counted candidates, hashed the tree and sized the state, so the published row
described the live pre-scrub window while the committed fixture described the
scrubbed one. Nothing in the repo compared them, so the divergence was only
findable by hand: Notepad's ``tree_hash`` was ``9f679d53…`` published against
``f4721556…`` from the fixture, Calculator's candidate count 36 against 34, its
reduced token count 1,535 against 1,375, and ``calculator.json``'s own ``state``
block listed two elements the pipeline no longer selects.

This file is the comparison that was missing. It re-derives, offline, and fails
the moment a published figure and the fixture it claims to come from disagree.

The live *timing* rows are deliberately not checked against anything: they
measure a walk over a real window, no fixture can reproduce them, and asserting
them would only teach the next person to delete the assertion.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "bench" / "cu_observe_results.json"


def _bench():
    """Import ``bench/cu_observe_bench.py``, which is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "cu_observe_bench", ROOT / "bench" / "cu_observe_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cu_observe_bench", module)
    spec.loader.exec_module(module)
    return module


bench = _bench()
published = json.loads(RESULTS.read_text(encoding="utf-8"))


class TestTheFileIsSelfDescribing:
    def test_every_fixture_has_a_derived_row(self):
        assert set(published["fixtures"]) == set(bench.ALL_FIXTURES)

    def test_each_row_names_the_fixture_it_came_from(self):
        for name, row in published["fixtures"].items():
            assert row["derived_from"] == (
                "tests/fixtures/cu/%s.json (post-scrub)" % name)

    def test_the_app_rows_are_marked_too(self):
        """An app row mixes live timings with derived values; say which."""
        for app in bench.FIXTURE_OF:
            assert "(post-scrub)" in published["apps"][app]["derived_from"]

    def test_the_live_rows_are_declared(self):
        note = published["live_rows"]
        assert "snapshot_ms" in note and "NOT reproducible" in note
        assert "--from-fixtures" in note


class TestEveryDerivedNumberReproduces:
    @pytest.mark.parametrize("name", bench.ALL_FIXTURES)
    def test_the_fixture_row(self, name):
        derived = bench.derive(name)
        for field in bench.STABLE_FIELDS:
            assert published["fixtures"][name][field] == derived[field], (
                "%s.%s is stale: published %r, derived %r"
                % (name, field, published["fixtures"][name][field],
                   derived[field]))

    @pytest.mark.parametrize("app", sorted(bench.FIXTURE_OF))
    def test_the_app_row_agrees_with_its_fixture(self, app):
        fixture_row = published["fixtures"][bench.FIXTURE_OF[app]]
        for field in bench.STABLE_FIELDS:
            assert published["apps"][app][field] == fixture_row[field]

    @pytest.mark.parametrize("name", bench.ALL_FIXTURES)
    def test_the_timings_are_at_least_the_right_size(self, name):
        """A shared runner cannot reproduce a duration; it can bound one."""
        derived = bench.derive(name)
        for field in ("reduce_ms_median", "hash_ms_median", "diff_ms_median"):
            published_ms = published["fixtures"][name][field]
            assert published_ms > 0
            assert published_ms < max(derived[field] * 20, 5.0), (
                "%s.%s published %.3f ms, derived %.3f ms"
                % (name, field, published_ms, derived[field]))


class TestFixturesAreSelfConsistent:
    """A fixture's ``state`` block is a pure function of its ``snapshot``."""

    @pytest.mark.parametrize("name", bench.ALL_FIXTURES)
    def test_the_state_block_reproduces(self, name):
        from jevskill.cu import candidates
        from jevskill.cu.observe import to_state
        from jevskill.cu.types import Snapshot

        data = bench.load_fixture(name)
        snap = Snapshot.from_dict(data["snapshot"])
        rebuilt = to_state(snap, elements=candidates(snap.elements, cap=60))
        assert data["state"] == rebuilt, (
            "%s.json's own state block no longer reduces from its own snapshot"
            % name)

    def test_rewriting_is_idempotent(self):
        """``--from-fixtures`` must be safe to run twice; it rewrites files."""
        assert not any(bench.rewrite_fixture_state(name)
                       for name in bench.ALL_FIXTURES)


class TestTheLiveRowsStillDescribeTheseTrees:
    """The one claim that lets the pre-scrub timings stay published.

    ``scrub()`` rewrites names and values; it never adds or removes a node. If
    that stopped being true, a timing measured on a differently shaped tree
    would be sitting next to a node count from this one, and the top-level note
    saying otherwise would be the lie.
    """

    @pytest.mark.parametrize("app", sorted(bench.FIXTURE_OF))
    def test_node_counts_match_the_live_walk(self, app):
        row = published["apps"][app]
        fixture_nodes = published["fixtures"][bench.FIXTURE_OF[app]]["nodes"]
        assert row["nodes"] == fixture_nodes
        for strategy in row["strategies"].values():
            assert strategy["nodes"] == fixture_nodes

    def test_scrub_preserves_the_tree(self):
        """Directly: scrub a fixture and count."""
        from jevskill.cu.types import Snapshot

        snap = Snapshot.from_dict(bench.load_fixture("notepad")["snapshot"])
        before = [(el.id, el.role, el.bbox, el.parent, el.depth)
                  for el in snap.elements]
        bench.scrub(snap, "notepad")
        after = [(el.id, el.role, el.bbox, el.parent, el.depth)
                 for el in snap.elements]
        assert before == after

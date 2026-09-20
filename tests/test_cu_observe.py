"""What perception promises the rest of the loop, provable without Windows.

The snapshot itself needs UIA, so the walk is exercised live only when
``JEVSKILL_CU_LIVE=1`` on Windows. Everything else — the state shape, the token
budget, the import contract, the guarantee that a COM pointer never reaches the
serialised state — runs from the fixtures on any platform, which is what keeps
this file useful on Linux CI where the ``cu`` extra is not installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jevskill.cu import candidates
from jevskill.cu.observe import GRID, state_tokens, to_state
from jevskill.cu.types import Snapshot, UIElement

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu"
REAL = ("notepad", "calculator")
ALL_FIXTURES = ("synthetic_50", "synthetic_500", "synthetic_2000") + REAL

#: The published budget for a reduced state (``docs/plan-2026-09-20.md`` §4.2:
#: 60 candidates is the cap, and the state has to stay small enough that the
#: decision call is dominated by the network rather than by tokens).
TOKEN_BUDGET = 3500

LIVE = os.environ.get("JEVSKILL_CU_LIVE") == "1" and os.name == "nt"
live_only = pytest.mark.skipif(
    not LIVE, reason="live UIA walk: set JEVSKILL_CU_LIVE=1 on Windows")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))


def load(name: str) -> Snapshot:
    return Snapshot.from_dict(fixture(name)["snapshot"])


class TestImportContract:
    def test_importing_the_package_does_not_import_comtypes(self):
        """``import jevskill.cu`` must work on a machine without the extra.

        A subprocess, because by the time this test runs the extra may already
        be imported by another test — the guarantee is about a fresh process,
        which is where a user meets it.
        """
        code = ("import sys, jevskill.cu as cu; "
                "assert 'comtypes' not in sys.modules, sorted(sys.modules)[:0]; "
                "assert cu.candidates and cu.tree_hash")
        subprocess.run([sys.executable, "-c", code], check=True,
                       cwd=str(Path(__file__).resolve().parents[1]))

    def test_snapshot_error_names_the_extra(self, monkeypatch):
        from jevskill.cu import observe

        monkeypatch.setattr(observe, "_local", threading.local())
        monkeypatch.setitem(sys.modules, "comtypes", None)
        monkeypatch.setitem(sys.modules, "comtypes.client", None)
        with pytest.raises(ImportError) as err:
            observe.snapshot(hwnd=1)
        message = str(err.value)
        assert 'pip install "jevskill[cu]"' in message
        assert "Windows" in message

    def test_lazy_attribute_is_the_real_function(self):
        import jevskill.cu as cu
        from jevskill.cu import observe

        assert cu.snapshot is observe.snapshot
        assert cu.to_state is observe.to_state
        with pytest.raises(AttributeError):
            cu.no_such_thing

    def test_strategy_is_validated_before_any_com_call(self):
        from jevskill.cu import observe

        with pytest.raises(ValueError) as err:
            observe.snapshot(hwnd=1, strategy="fastest")
        assert "subtree" in str(err.value)


class TestStateShape:
    def test_keys_match_the_benchmarked_shape(self):
        snap = load("calculator")
        state = to_state(snap)
        assert set(state) == {"window_title", "app", "elements"}
        for entry in state["elements"].values():
            assert set(entry) <= {"role", "name", "bbox", "enabled", "focused",
                                  "value"}
            assert "role" in entry and "name" in entry

    def test_elements_are_keyed_by_id(self):
        snap = load("calculator")
        state = to_state(snap)
        assert list(state["elements"]) == [el.id for el in snap.elements]
        assert all(key.startswith("e") for key in state["elements"])

    def test_literal_bench_shape_is_available(self):
        snap = load("calculator")
        state = to_state(snap, coarse_bbox=False, omit_defaults=False)
        entry = state["elements"]["e0"]
        assert entry["enabled"] is True and entry["focused"] is False
        assert isinstance(entry["bbox"], list) and len(entry["bbox"]) == 4

    def test_coarse_bbox_is_a_grid_cell(self):
        snap = load("calculator")
        cells = {entry["bbox"] for entry in to_state(snap)["elements"].values()
                 if "bbox" in entry}
        assert cells, "every sized element should carry a cell"
        for cell in cells:
            row, col = cell[1:].split("c")
            assert 0 <= int(row) < GRID and 0 <= int(col) < GRID

    def test_the_grid_does_not_move_when_the_list_shortens(self):
        snap = load("calculator")
        full = to_state(snap)["elements"]
        short = to_state(snap, elements=candidates(snap.elements))["elements"]
        for element_id, entry in short.items():
            assert entry.get("bbox") == full[element_id].get("bbox")

    def test_unsized_elements_omit_the_key(self):
        elements = [UIElement(id="e0", role="pane", name="host",
                              bbox=(0, 0, 0, 0)),
                    UIElement(id="e1", role="button", name="ok",
                              bbox=(10, 10, 40, 30))]
        state = to_state(elements)
        assert "bbox" not in state["elements"]["e0"]
        assert "bbox" in state["elements"]["e1"]

    def test_defaults_are_dropped_but_the_signal_is_not(self):
        elements = [UIElement(id="e0", role="button", name="ok",
                              bbox=(0, 0, 40, 30)),
                    UIElement(id="e1", role="button", name="focused",
                              bbox=(0, 40, 40, 30), focused=True),
                    UIElement(id="e2", role="button", name="greyed",
                              bbox=(0, 80, 40, 30), enabled=False)]
        state = to_state(elements)["elements"]
        assert set(state["e0"]) == {"role", "name", "bbox"}, (
            "enabled=True and focused=False carry no information")
        assert state["e1"]["focused"] is True
        assert state["e2"]["enabled"] is False
        assert "focused" not in state["e2"]


class TestNoHandlesEscape:
    def test_snapshot_to_dict_drops_the_handle_map(self):
        snap = Snapshot(window_title="t", app="a.exe", pid=1, hwnd=2,
                        taken_at=0.0, elapsed_ms=1.0,
                        elements=[UIElement(id="e0", role="button", name="ok",
                                            bbox=(0, 0, 40, 30))],
                        _handles={"e0": object()})
        payload = snap.to_dict()
        assert "_handles" not in payload
        json.dumps(payload)  # a live COM pointer would raise here
        assert snap.handle("e0") is not None, "the map itself must still work"

    def test_to_state_is_json_safe(self):
        snap = load("notepad")
        snap._handles["e0"] = object()
        json.dumps(to_state(snap))

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    def test_committed_fixtures_carry_no_handles(self, name):
        assert "_handles" not in fixture(name)["snapshot"]


class TestTokenBudget:
    def test_sixty_candidates_fit_the_budget(self):
        snap = load("synthetic_500")
        shortlist = candidates(snap.elements, cap=60)
        assert len(shortlist) == 60, "fixture no longer produces a full cap"
        tokens = state_tokens(to_state(snap, elements=shortlist))
        assert tokens <= TOKEN_BUDGET, "%d tokens for 60 candidates" % tokens

    def test_reduction_is_what_buys_the_budget(self):
        snap = load("synthetic_2000")
        full = state_tokens(to_state(snap))
        reduced = state_tokens(to_state(snap, elements=candidates(snap.elements)))
        assert reduced * 4 < full

    @pytest.mark.parametrize("name", REAL)
    def test_real_windows_fit(self, name):
        snap = load(name)
        tokens = state_tokens(to_state(snap, elements=candidates(snap.elements)))
        assert tokens <= TOKEN_BUDGET

    def test_long_names_are_truncated_at_the_source(self):
        from jevskill.cu.observe import MAX_NAME_CHARS, _text

        assert len(_text("x" * 5000, MAX_NAME_CHARS)) == MAX_NAME_CHARS
        assert _text("x" * 5000, MAX_NAME_CHARS).endswith("…")


class TestRealFixtures:
    @pytest.mark.parametrize("name", REAL)
    def test_round_trips(self, name):
        snap = load(name)
        assert Snapshot.from_dict(snap.to_dict()).to_dict() == snap.to_dict()

    @pytest.mark.parametrize("name", REAL)
    def test_tree_is_well_formed(self, name):
        snap = load(name)
        ids = {el.id for el in snap.elements}
        assert snap.elements[0].depth == 0 and snap.elements[0].parent is None
        for el in snap.elements[1:]:
            assert el.parent in ids, "%s has a dangling parent" % el.id
            assert el.region is None or el.region in ids
            assert el.is_sized(), "an unsized node was recorded"

    def test_calculator_looks_like_a_calculator(self):
        snap = load("calculator")
        buttons = [el for el in snap.elements
                   if el.role == "button" and "invoke" in el.patterns]
        assert len(buttons) > 20, "a calculator is mostly invokable buttons"
        assert any(el.role == "group" for el in snap.elements)

    def test_fixtures_were_scrubbed(self):
        """A committed fixture must not carry the machine owner's content."""
        for name in REAL:
            data = fixture(name)
            assert data["snapshot"]["window_title"] == name
            for element in data["snapshot"]["elements"]:
                assert element.get("value") in (None, "<scrubbed>")


@live_only
class TestLive:
    def test_foreground_snapshot(self):
        from jevskill.cu import observe

        snap = observe.snapshot()
        assert snap.elements and snap.elapsed_ms > 0
        assert snap.hwnd and snap.pid
        # Counts only: this is somebody's desktop.
        assert len(candidates(snap.elements)) <= 60
        assert all(el.is_sized() for el in snap.elements)

    def test_handles_are_live_and_private(self):
        from jevskill.cu import observe

        snap = observe.snapshot()
        assert snap.handle(snap.elements[0].id) is not None
        assert "_handles" not in snap.to_dict()

    def test_both_strategies_see_the_same_window(self):
        from jevskill.cu import observe

        hwnd = observe.foreground_hwnd()
        subtree = observe.snapshot(hwnd, strategy="subtree", budget_ms=5000)
        lazy = observe.snapshot(hwnd, strategy="lazy", budget_ms=5000)
        assert abs(len(subtree.elements) - len(lazy.elements)) <= 2

    def test_budget_truncates_rather_than_hangs(self):
        from jevskill.cu import observe

        snap = observe.snapshot(budget_ms=0.0)
        assert snap.truncated and len(snap.elements) >= 1

    def test_max_nodes_truncates(self):
        from jevskill.cu import observe

        snap = observe.snapshot(max_nodes=5)
        assert snap.truncated and len(snap.elements) == 5

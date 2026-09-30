"""Drawing on a canvas: the surface the reduction used to drop, and the strokes on it.

Measured live on 2026-09-30 in Windows 11 Paint: the picture area is a
``group`` (automation id ``image``) with no UIA pattern, so it never reached the
candidate list, and a run with the Fill tool and dark red already selected
escalated three times with "no canvas element is listed to click" — 51 s and
$0.10 for a goal that needed one click. Everything here is offline: the canvas
is a hand-built tree shaped like the live one, and the backend is a fake.
"""

from __future__ import annotations

import json

import pytest

from jevskill.cu import act as act_module
from jevskill.cu.act import Action, densify, execute, stroke_points
from jevskill.cu.decide import Decision, validate
from jevskill.cu.experience import Experience, Trajectory
from jevskill.cu.killswitch import KillSwitch
from jevskill.cu.llm import LLMReply
from jevskill.cu.observe import to_state
from jevskill.cu.reduce import candidates, mark_surfaces, region_state
from jevskill.cu.runner import (DRAW_CHUNK, DRAW_MAX_POINTS, DRAW_MAX_STROKES, Operator,
                                is_draw_goal, parse_strokes)
from jevskill.cu.types import (CANVAS_ROLE, SURFACE_MIN_SIDE_PX, Snapshot, UIElement)

CANVAS_NAME = "Using the Fill tool on the canvas"


def paint_like(buttons: int = 12, **canvas_overrides) -> Snapshot:
    """A window, a ribbon of buttons above, the picture area below it."""
    els = [
        UIElement(id="e0", role="window", name="Untitled - Paint", bbox=(0, 0, 1600, 1000)),
        UIElement(id="e1", role="group", name="", bbox=(0, 40, 1600, 110), parent="e0"),
    ]
    for index in range(buttons):
        els.append(UIElement(id="b%d" % index, role="button", name="Tool %d" % index,
                             bbox=(10 + 60 * index, 50, 50, 50), patterns=("invoke",),
                             parent="e1", region="e1"))
    els.append(UIElement(id="e2", role="pane", name="", automation_id="scrollViewer",
                         bbox=(0, 160, 1600, 800), patterns=("scroll",), parent="e0"))
    canvas = dict(id="e3", role="group", name=CANVAS_NAME, automation_id="image",
                  bbox=(400, 300, 690, 338), parent="e2")
    canvas.update(canvas_overrides)
    els.append(UIElement(**canvas))
    return Snapshot(window_title="Untitled - Paint", app="mspaint", pid=5, hwnd=100,
                    taken_at=0.0, elapsed_ms=1.0, elements=els)


# --------------------------------------------------------------------------- #
# The surface
# --------------------------------------------------------------------------- #


class TestMarkSurfaces:
    def test_paint_s_picture_area_becomes_a_canvas_and_a_candidate(self):
        snap = paint_like()
        marked = {el.id: el.role for el in mark_surfaces(snap.elements)}
        assert marked["e3"] == CANVAS_ROLE
        assert marked["e2"] == "pane", "the scroll host around the canvas is not one"
        assert snap.by_id("e3").role == "group", "the snapshot itself is never mutated"
        assert "e3" in [el.id for el in candidates(snap.elements)]

    def test_the_canvas_survives_a_cap_that_the_ribbon_fills(self):
        # Reading order puts the canvas after every ribbon control; ranked
        # there, a cap smaller than the ribbon hid it.
        snap = paint_like(buttons=20)
        kept = candidates(snap.elements, cap=5)
        assert "e3" in [el.id for el in kept]

    def test_a_click_on_the_canvas_is_legal_and_lands_on_its_centre(self):
        snap = paint_like()
        cands = candidates(snap.elements)
        ruling = validate(Decision(target="e3", op="click", confidence=0.9, margin=0.5),
                          cands, require_confidence=False)
        assert ruling.ok, ruling.reason
        assert act_module._method_for("click", snap.by_id("e3")) == "click_point"

    @pytest.mark.parametrize("overrides", [
        {"name": "", "automation_id": ""},                 # the UWP input-sink overlay
        {"patterns": ("invoke",)},                         # a control, not a surface
        {"bbox": (400, 300, SURFACE_MIN_SIDE_PX - 1, 300)},  # an icon or a swatch
        {"enabled": False},
        {"offscreen": True},
        {"role": "list"},
    ])
    def test_what_is_not_a_canvas(self, overrides):
        snap = paint_like(**overrides)
        assert all(el.role != CANVAS_ROLE for el in mark_surfaces(snap.elements))

    @pytest.mark.parametrize("child", [
        UIElement(id="c1", role="button", name="", bbox=(410, 310, 40, 40),
                  patterns=("invoke",), parent="e3"),
        UIElement(id="c1", role="text", name="This folder is empty", bbox=(410, 310, 200, 20),
                  parent="e3"),
    ])
    def test_a_pane_holding_a_control_or_a_caption_is_content_not_a_canvas(self, child):
        snap = paint_like()
        snap.elements.append(child)
        assert all(el.role != CANVAS_ROLE for el in mark_surfaces(snap.elements))

    def test_only_the_innermost_of_two_nested_surfaces_is_the_canvas(self):
        snap = paint_like()
        # The scroll host loses its pattern: it would qualify on its own.
        snap.elements[-2] = UIElement(id="e2", role="pane", name="", automation_id="host",
                                      bbox=(0, 160, 1600, 800), parent="e0")
        roles = {el.id: el.role for el in mark_surfaces(snap.elements)}
        assert roles["e3"] == CANVAS_ROLE and roles["e2"] == "pane"

    def test_the_committed_calculator_fixture_has_no_canvas(self):
        # Its ApplicationFrameInputSinkWindow is a 968x877 empty pane with no
        # name and no automation id — the false positive the name-or-id rule
        # exists for.
        from pathlib import Path

        path = Path(__file__).parent / "fixtures" / "cu" / "calculator.json"
        snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8"))["snapshot"])
        assert all(el.role != CANVAS_ROLE for el in mark_surfaces(snap.elements))

    def test_the_state_carries_the_canvas_size_in_pixels(self):
        snap = paint_like()
        state = to_state(snap, elements=candidates(snap.elements))
        assert state["elements"]["e3"]["role"] == CANVAS_ROLE
        assert state["elements"]["e3"]["px"] == [690, 338]
        assert "px" not in state["elements"]["b0"]

    def test_the_cascade_counts_the_canvas_as_a_member_not_a_region(self):
        snap = paint_like()
        members = [m for row in region_state(snap.elements)["regions"].values()
                   for m in row["members"]]
        assert "e3" in members


# --------------------------------------------------------------------------- #
# The strokes
# --------------------------------------------------------------------------- #


class FakeBackend:
    def __init__(self):
        self.strokes = []
        self.clicks = []

    def draw_stroke(self, points):
        self.strokes.append(list(points))

    def click_point(self, x, y):
        self.clicks.append((x, y))


class TestStrokes:
    def test_fractions_map_inside_the_box_and_are_clamped(self):
        bbox = (400, 300, 690, 338)
        inset = act_module.DRAW_INSET_PX
        (x0, y0), (x1, y1), (xc, yc) = stroke_points(bbox, [(0, 0), (1, 1), (1.7, -3)])
        assert (x0, y0) == (400 + inset, 300 + inset)
        assert 400 < x1 < 400 + 690 - inset and 300 < y1 < 300 + 338 - inset
        assert (xc, yc) == (x1, 300 + inset), "out-of-range fractions are clamped"

    def test_densify_keeps_the_ends_and_bounds_every_segment(self):
        path = densify([(0, 0), (100, 30), (100, 30), (40, 90)])
        assert path[0] == (0, 0) and path[-1] == (40, 90)
        assert all(max(abs(b[0] - a[0]), abs(b[1] - a[1])) <= act_module.DRAW_STEP_PX
                   for a, b in zip(path, path[1:]))
        assert densify([(5, 5)]) == [(5, 5)]

    def test_execute_draws_every_stroke_densified_in_screen_pixels(self):
        snap = paint_like()
        backend = FakeBackend()
        action = Action(op="draw", target="e3",
                        strokes=[[(0.5, 0.5)], [(0.1, 0.1), (0.9, 0.9)]])
        result = execute(action, snap, backend=backend)
        assert result.ok and result.method == "draw_path"
        assert len(backend.strokes) == 2
        assert backend.strokes[0] == stroke_points(snap.by_id("e3").bbox, [(0.5, 0.5)])
        assert len(backend.strokes[1]) > 2, "a long drag arrives as short segments"

    def test_a_dry_run_names_the_mechanism_and_touches_nothing(self):
        backend = FakeBackend()
        result = execute(Action(op="draw", target="e3", strokes=[[(0.5, 0.5)]]), paint_like(),
                         backend=backend, dry_run=True)
        assert result.ok and result.method == "dry:draw_path" and not backend.strokes

    @pytest.mark.parametrize("action, error", [
        (Action(op="draw", target="e3", strokes=[]), "without strokes"),
        (Action(op="draw", target=None, strokes=[[(0.5, 0.5)]]), "without a target"),
        (Action(op="draw", target="gone", strokes=[[(0.5, 0.5)]]), "no element"),
    ])
    def test_a_draw_with_nothing_to_draw_or_nowhere_to_draw_fails(self, action, error):
        backend = FakeBackend()
        result = execute(action, paint_like(), backend=backend)
        assert not result.ok and error in result.error and not backend.strokes

    def test_a_draw_needs_the_foreground_like_any_pointer_input(self):
        snap = paint_like()
        assert Operator._needs_foreground(Action(op="draw", target="e3",
                                                 strokes=[[(0.5, 0.5)]]), snap)


class TestParseStrokes:
    def test_bad_points_are_dropped_not_the_reply(self):
        raw = [[[0.1, 0.2], [0.3, "x"], [float("nan"), 0.5], [2, -1]], "junk", [], [[True, 0]]]
        assert parse_strokes(raw) == [[(0.1, 0.2), (1.0, 0.0)]]
        assert parse_strokes(None) == [] and parse_strokes({"a": 1}) == []

    def test_the_caps_hold(self):
        many = [[[0.5, 0.5]] for _ in range(DRAW_MAX_STROKES + 10)]
        assert len(parse_strokes(many)) == DRAW_MAX_STROKES
        long = [[[0.5, 0.5]] * (DRAW_MAX_POINTS + 50)]
        assert sum(len(s) for s in parse_strokes(long)) == DRAW_MAX_POINTS

    @pytest.mark.parametrize("goal, expected", [
        ("Draw the moon top right", True), ("draw: fill the sky", True),
        ("Drawing tools: pick the brush", False), ("Select the Fill tool", False), ("", False),
    ])
    def test_a_draw_goal_is_marked_by_its_first_word(self, goal, expected):
        assert is_draw_goal(goal) is expected


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #


class TestMemory:
    def test_a_draw_is_neither_a_recipe_step_nor_a_lesson(self, tmp_path):
        snap = paint_like()
        trajectory = Trajectory()
        trajectory.observed(snap)
        trajectory.acted(Action(op="click", target="b1"), snap, True)
        trajectory.acted(Action(op="draw", target="e3", strokes=[[(0.5, 0.5)]] * 3), snap, True)
        trajectory.observed(snap)
        trajectory.close()
        assert trajectory.drew
        assert all(a.op != "draw" for a in trajectory.attempts)
        assert any(line.startswith("draw 3 strokes") for line in trajectory.tried())
        experience = Experience(tmp_path / "memory")
        assert experience.learn_recipe("mspaint", "Draw the moon", "paint a night sky",
                                       trajectory) is None


# --------------------------------------------------------------------------- #
# The operator
# --------------------------------------------------------------------------- #


class DrawingLLM:
    """Plans nothing (the plan is given); answers the drawing call with strokes."""

    def __init__(self, strokes):
        self.strokes = strokes
        self.draw_payloads = []

    def chat(self, system, user, **_kw):
        if system.startswith("You draw on a canvas"):
            self.draw_payloads.append(json.loads(user))
            body = {"strokes": self.strokes, "why": "a circle in the middle"}
        else:
            body = {"done": True, "why": "fine"}
        return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=100,
                        tokens_out=40, cost_usd=0.0001, cost_source="provider",
                        latency_ms=5.0)


def drawing_operator(tmp_path, llm, snapshot, backend, loop_calls):
    switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False,
                        poll_s=0.01)

    def loop(goal, opts=None, **_hooks):
        loop_calls.append(goal)

        class Result:
            stop_reason, error, steps, wall_ms, tokens_in, cost_usd = "done", "", [], 1.0, 0, 0.0
        return Result()

    return Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                    llm_factory=lambda _m: llm, kill_switch=switch,
                    idle_seconds=lambda: 99.0,
                    platform_check=lambda: {"ok": True, "reason": ""},
                    foreground_hwnd=lambda: 100, window_pid=lambda h: 5,
                    foreground_title=lambda: "Untitled - Paint", run_loop=loop,
                    observe=lambda: snapshot, backend_factory=lambda: backend)


class TestOperatorDraws:
    CIRCLE = [[[0.5 + 0.2 * dx, 0.5 + 0.2 * dy] for dx, dy in
               ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 0))]]

    def test_a_draw_goal_is_one_drawing_call_and_never_the_loop(self, tmp_path):
        llm, backend, loop_calls = DrawingLLM(self.CIRCLE * 8), FakeBackend(), []
        operator = drawing_operator(tmp_path, llm, paint_like(), backend, loop_calls)
        operator.start("paint a circle", "fake/model",
                       plan=[{"goal": "Draw a red circle in the centre", "done_when": "drawn"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status.get("error")
        assert loop_calls == [], "Jev picks controls; a stroke is not one"
        assert len(backend.strokes) == 8
        assert status["plan"][0]["evidence"] == "model"
        payload = llm.draw_payloads[0]
        assert payload["canvas"] == {"id": "e3", "name": CANVAS_NAME, "px": [690, 338]}
        assert payload["already_drawn"] == []
        draws = [e for e in status["events"] if e["kind"] == "draw"]
        assert draws and "drew 8 strokes" in draws[-1]["text"]
        steps = [e for e in status["events"] if e["kind"] == "step"]
        # Eight strokes in chunks: the kill switch is checked between them.
        assert len(steps) == -(-8 // DRAW_CHUNK)

    def test_a_second_draw_is_told_where_the_first_one_went(self, tmp_path):
        llm, backend, loop_calls = DrawingLLM(self.CIRCLE), FakeBackend(), []
        operator = drawing_operator(tmp_path, llm, paint_like(), backend, loop_calls)
        operator.start("two parts", "fake/model",
                       plan=[{"goal": "Draw the moon", "done_when": "drawn"},
                             {"goal": "Draw the house", "done_when": "drawn"}])
        operator.wait(20)
        assert operator.status()["state"] == "done"
        second = llm.draw_payloads[1]["already_drawn"]
        assert second == [{"goal": "Draw the moon", "box": [0.3, 0.3, 0.7, 0.7]}]

    def test_no_strokes_ends_the_goal_escalated_without_touching_the_desktop(self, tmp_path):
        llm, backend, loop_calls = DrawingLLM([]), FakeBackend(), []
        operator = drawing_operator(tmp_path, llm, paint_like(), backend, loop_calls)
        operator.start("paint", "fake/model", plan=[{"goal": "Draw a cat", "done_when": "x"}])
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "failed" and not backend.strokes and not loop_calls
        assert status["plan"][0]["stop_reason"] == "escalated"

    def test_without_a_canvas_the_goal_goes_to_the_loop(self, tmp_path):
        llm, backend, loop_calls = DrawingLLM(self.CIRCLE), FakeBackend(), []
        snap = paint_like(name="", automation_id="")          # no surface on this screen
        operator = drawing_operator(tmp_path, llm, snap, backend, loop_calls)
        operator.start("paint", "fake/model", plan=[{"goal": "Draw a cat", "done_when": "x"}])
        operator.wait(20)
        assert loop_calls == ["Draw a cat"] and not llm.draw_payloads and not backend.strokes

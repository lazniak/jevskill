"""The computer-use surfaces after the review: the resume card, the resume
route's model, strict flags, and the CLI's call allowance.

Each class pins one confirmed finding and fails on the code before its fix:

1. The resume card posted only ``{run_id, model}``, so a run stopped by its
   total budget or spend cap could not be resumed with a larger one, and a
   refusal left nothing next to the button that was pressed.
2. ``"model": null`` (the picker's "none — Jev only") reached
   ``Operator.resume`` as ``None``, which means "keep the checkpoint's model":
   a Jev-only resume went back to the run's paid planning model.
3. ``bool(payload.get("memory", True))`` turned ``{"memory": null}`` into
   memory off, and ``bool("false")`` is ``True`` everywhere a flag was read
   that way — on ``/api/cu/confirm`` that allowed a destructive step.
4. ``--max-llm-calls`` was documented as 1-200 and silently clamped outside it.
5. The card did not say that a live checkpoint resumes live.

The server side goes through a real ``running(...)`` server, as in
``test_cu_mission_surfaces.py``. The page side runs the real functions from
``app.js`` under Node against a minimal DOM (skipped when Node is absent), and
a source check that needs no Node keeps the resume body pinned on any CI.
Nothing here touches the desktop: every run that is resumed is a dry run, and
the one live checkpoint (finding 5) is only ever listed, never resumed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jevskill import cli
from jevskill.cu.runner import LLM_CAP_CEIL, Operator, RunNotResumable
from jevskill.stats import ledger_path

from test_cu_mission_surfaces import (THREE_GOALS, UNKNOWN_ID, checkpoint_of,  # noqa: F401
                                      console_with, goal_started, quiet_switch, stopped_run,
                                      wait_until)
from test_cu_runner import FakeLLM, call, make_operator, offline_models, running  # noqa: F401

APP_JS = Path(__file__).resolve().parents[1] / "jevskill" / "web" / "static" / "app.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def stopped_run_with_model(tmp_path, model="fake/model"):
    """A dry run started with a planning model, stopped in its first goal.

    Returns ``(operator, run_id, seen)``; ``seen`` records every model id the
    operator asked its LLM factory for, so a test can tell whether a resume
    built the checkpoint's model.
    """
    seen = []

    def factory(model_id):
        seen.append(model_id)
        return FakeLLM()

    operator = make_operator(tmp_path)
    operator._llm_factory = factory
    run_id = operator.start("walk the File menu", model, dry_run=True, plan=THREE_GOALS)
    assert wait_until(lambda: goal_started(operator)), operator.status()["events"]
    operator.stop("test STOP")
    operator.wait(10)
    assert operator.status()["state"] == "stopped"
    assert checkpoint_of(tmp_path, run_id)["model"] == model
    return operator, run_id, seen


def edit_checkpoint(tmp_path, run_id, **changes):
    """Rewrite fields of a run's ``run.json`` — how a run that already used its
    budget is produced without spending ten minutes of it."""
    path = ledger_path(tmp_path).parent / "cu_runs" / run_id / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def js_functions(*names):
    """The source of top-level functions in app.js, by name, in order."""
    src = APP_JS.read_text(encoding="utf-8")
    out = []
    for name in names:
        match = re.search(r"^(?:async )?function %s\(.*?^\}\r?$" % re.escape(name), src,
                          re.S | re.M)
        assert match, "app.js has no top-level function %s" % name
        out.append(match.group(0))
    return "\n".join(out)


#: A DOM just large enough for the card: nodes hold text, classes, attributes,
#: children and listeners. ``innerHTML`` throws, so a render that reaches for
#: markup with checkpoint text in it fails the test instead of passing.
JS_PRELUDE = r"""
'use strict';
class FakeNode {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.className = '';
    this.ownText = ''; this.listeners = {}; this.disabled = false; this.value = '';
    const node = this;
    this.classList = {
      has(c) { return node.className.split(/\s+/).includes(c); },
      contains(c) { return this.has(c); },
      add(c) { if (!this.has(c)) node.className = (node.className + ' ' + c).trim(); },
      remove(c) { node.className = node.className.split(/\s+/).filter((x) => x && x !== c).join(' '); },
      toggle(c, on) {
        if (on === undefined) on = !this.has(c);
        if (on) this.add(c); else this.remove(c);
        return on;
      },
    };
  }
  set textContent(v) { this.ownText = String(v); this.children = []; }
  get textContent() { return this.ownText + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(_v) { throw new Error('innerHTML used in the resume card'); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(k, f) { this.listeners[k] = f; }
}
const document = { createElement: (tag) => new FakeNode(tag) };
const localStorage = { getItem: () => null, setItem: () => {} };
const FIELDS = {};
const $ = (sel) => { if (!(sel in FIELDS)) FIELDS[sel] = new FakeNode('field'); return FIELDS[sel]; };
const cu = { busy: false, sending: false, resumeKey: '', note: '', last: null };
const CU_RESUME_DISMISSED = 'jev.cu.resume.dismissed';
const posted = [];
let postReply = null;
async function postJSON(path, body) {
  posted.push({ path: path, body: JSON.parse(JSON.stringify(body)) });
  if (postReply) throw postReply;
  return { ok: true };
}
const shown = [];
function cuShowError(err) { shown.push(Number(err && err.status) || 0); }
function cuUpdateButtons() {}
function cuPollSoon() {}
function refusal(status, error, hint) {
  const err = new Error(error);
  err.status = status;
  err.payload = { error: error, hint: hint, status: status };
  return err;
}
function rowsOf(box) { return box.children.filter((c) => c.classList.has('cu-resume-row')); }
"""

#: Every function the card and the resume path call, taken verbatim from app.js.
CARD_FUNCTIONS = ("el", "lsGet", "lsSet", "cuDismissed", "cuDismissResume", "cuProgressText",
                  "cuModel", "cuNumber", "cuCap", "cuMaxLlmCalls", "cuResumeBody",
                  "cuResumeModelText", "cuResumeLimitHint", "cuResumeMode", "cuResume",
                  "cuRenderResume")


def run_js(tmp_path, fields, scenario):
    """Run ``scenario`` (async JS that fills ``out``) after the real functions."""
    fills = "".join("$(%s).value = %s;\n" % (json.dumps(sel), json.dumps(value))
                    for sel, value in fields.items())
    program = (JS_PRELUDE + js_functions(*CARD_FUNCTIONS) + "\n(async () => {\nconst out = {};\n"
               + fills + scenario + "\nprocess.stdout.write(JSON.stringify(out));\n})()"
               ".catch((e) => { console.error(e && e.stack || e); process.exit(1); });\n")
    script = tmp_path / "card.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60,
                          encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


PAGE_FIELDS = {"#cu-model": "", "#cu-model-custom": "", "#cu-total-budget": "1200",
               "#cu-cap": "0.75", "#cu-max-llm": "30"}

LIVE_BUDGET_ROW = {"run_id": "20260927-100000-aaaaaa", "command": "save the report",
                   "state": "stopped", "interrupted": False, "dry_run": False,
                   "stop_reason": "total budget of 600 s spent", "error": None, "progress": {}}
DRY_CAP_ROW = {"run_id": "20260927-100000-bbbbbb", "command": "walk the File menu",
               "state": "stopped", "interrupted": False, "dry_run": True,
               "stop_reason": "spend cap of $0.50 reached ($0.5100)", "error": None,
               "progress": {"leaves_done": 1, "leaves_known": 3}}
OLD_FAILED_ROW = {"run_id": "20260927-100000-cccccc", "command": "<b>rename</b> the file",
                  "state": "failed", "interrupted": False, "dry_run": None,
                  "stop_reason": None, "error": "<img src=x onerror=alert(1)>", "progress": {}}


# --------------------------------------------------------------------------- #
# 1 + 2 + 5. The page: what a resume sends, and what the card says
# --------------------------------------------------------------------------- #


class TestResumeCardSource:
    """No Node needed: the resume posts the body builder, and the builder
    carries the model key and the page's three limits."""

    def test_cu_resume_posts_the_body_builder_with_the_limits_and_the_model(self):
        body = js_functions("cuResumeBody")
        for field in ("run_id:", "model: cuModel()", "total_budget_s:", "usd_cap: cuCap()",
                      "max_llm_calls: cuMaxLlmCalls()"):
            assert field in body, field
        assert "postJSON('/api/cu/resume', cuResumeBody(runId))" in js_functions("cuResume")

    def test_the_card_puts_text_in_through_text_content_only(self):
        render = js_functions("cuRenderResume", "cuResume")
        assert "innerHTML" not in render and "html:" not in render


@needs_node
class TestResumeCardBehaviour:
    def test_resume_sends_the_page_limits_and_an_explicit_null_for_jev_only(self, tmp_path):
        out = run_js(tmp_path, PAGE_FIELDS, "await cuResume('RUN', null, null); out.posted = posted;")
        assert out["posted"] == [{"path": "/api/cu/resume", "body": {
            "run_id": "RUN", "model": None, "total_budget_s": 1200, "usd_cap": 0.75,
            "max_llm_calls": 30}}]

    def test_an_empty_call_field_keeps_the_checkpoints_and_a_picked_model_is_sent(self, tmp_path):
        fields = dict(PAGE_FIELDS, **{"#cu-model": "anthropic/claude-sonnet-5", "#cu-max-llm": ""})
        out = run_js(tmp_path, fields, "await cuResume('RUN', null, null); out.posted = posted;")
        body = out["posted"][0]["body"]
        assert body["model"] == "anthropic/claude-sonnet-5"
        assert "max_llm_calls" in body and body["max_llm_calls"] is None

    def test_the_card_labels_mode_reason_limit_hint_and_model(self, tmp_path):
        payload = {"resumable": [LIVE_BUDGET_ROW, DRY_CAP_ROW, OLD_FAILED_ROW]}
        out = run_js(tmp_path, dict(PAGE_FIELDS, **{"#cu-model": "anthropic/claude-sonnet-5"}), """
            cuRenderResume(%s);
            const box = $('#cu-resume');
            out.hidden = box.classList.has('hidden');
            out.model = box.children.filter((c) => c.attrs.id === 'cu-resume-model')
              .map((c) => c.textContent);
            out.rows = rowsOf(box).map((row) => ({
              text: row.children[0].textContent,
              mode: row.children[0].children.filter((c) => c.classList.has('cu-mode'))
                .map((c) => [c.textContent, c.classList.has('is-live')]),
              button: row.children[1].textContent,
              buttonLive: row.children[1].classList.has('is-live'),
            }));
        """ % json.dumps(payload))
        assert out["hidden"] is False
        assert len(out["model"]) == 1 and "anthropic/claude-sonnet-5" in out["model"][0]
        live, dry, old = out["rows"]
        # A live checkpoint resumes live, whatever the dry-run switch says.
        assert live["mode"] == [["live — moves the desktop", True]]
        assert live["button"] == "resume live" and live["buttonLive"] is True
        assert "stop reason: total budget of 600 s spent" in live["text"]
        assert 'raise "total budget (s)"' in live["text"]
        assert dry["mode"] == [["dry run — simulated", False]]
        assert dry["button"] == "resume" and dry["buttonLive"] is False
        assert 'raise "spend cap USD"' in dry["text"]
        # An old checkpoint without the flag resumes live, so it says live.
        assert old["mode"] == [["live — moves the desktop", True]]
        assert "error: <img src=x onerror=alert(1)>" in old["text"]
        assert "<b>rename</b> the file" in old["text"] and "raise" not in old["text"]

    def test_the_model_line_names_jev_only_when_the_picker_says_none(self, tmp_path):
        out = run_js(tmp_path, PAGE_FIELDS, """
            cuRenderResume(%s);
            out.model = $('#cu-resume').children.filter((c) => c.attrs.id === 'cu-resume-model')
              .map((c) => c.textContent);
        """ % json.dumps({"resumable": [DRY_CAP_ROW]}))
        assert "none (Jev only)" in out["model"][0]

    def test_a_refused_resume_is_written_into_its_own_row(self, tmp_path):
        """The runner's 409 for a spent cap reaches the row that was clicked,
        not only the page's error box, and the button can be pressed again."""
        message = "run X has spent $0.5100 of its $0.50 cap; resume it with a larger cap"
        out = run_js(tmp_path, PAGE_FIELDS, """
            cuRenderResume(%s);
            const rows = rowsOf($('#cu-resume'));
            postReply = refusal(409, %s, 'send a larger usd_cap');
            await rows[1].children[1].listeners.click();
            const msgs = rows.map((row) => row.children[0].children
              .filter((c) => c.classList.has('cu-resume-msg'))[0]);
            out.texts = msgs.map((m) => m.textContent);
            out.hidden = msgs.map((m) => m.classList.has('hidden'));
            out.disabled = rows[1].children[1].disabled;
            out.shown = shown;
            out.posted = posted.map((p) => p.body.run_id);
        """ % (json.dumps({"resumable": [LIVE_BUDGET_ROW, DRY_CAP_ROW]}), json.dumps(message)))
        assert out["posted"] == [DRY_CAP_ROW["run_id"]]
        assert out["texts"][0] == "" and out["hidden"][0] is True
        assert message in out["texts"][1] and "send a larger usd_cap" in out["texts"][1]
        assert out["hidden"][1] is False
        assert out["disabled"] is False
        assert out["shown"] == [], "a 409 is said once, in the row"

    def test_a_server_failure_keeps_the_error_box_too(self, tmp_path):
        out = run_js(tmp_path, PAGE_FIELDS, """
            cuRenderResume(%s);
            const row = rowsOf($('#cu-resume'))[0];
            postReply = refusal(503, 'no key for the planning model', 'Set a key and reload.');
            await row.children[1].listeners.click();
            out.text = row.children[0].children.filter((c) => c.classList.has('cu-resume-msg'))[0]
              .textContent;
            out.shown = shown;
        """ % json.dumps({"resumable": [DRY_CAP_ROW]}))
        assert "no key for the planning model" in out["text"]
        assert out["shown"] == [503]


# --------------------------------------------------------------------------- #
# 1. The server contract the card relies on: limits forwarded, refusals said
# --------------------------------------------------------------------------- #


class TestResumeLimitsRoute:
    def test_a_budget_stopped_run_is_refused_under_its_budget_and_resumed_above_it(
            self, tmp_path, offline_models):
        operator, run_id = stopped_run(tmp_path)
        edit_checkpoint(tmp_path, run_id, active_s=650.0,
                        stop_reason="total budget of 600 s spent")
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "GET", "/api/cu/runs")
            row = {r["run_id"]: r for r in body["runs"]}[run_id]
            # The two fields the card reads to label the row and hint the fix.
            assert row["stop_reason"] == "total budget of 600 s spent"
            assert row["dry_run"] is True

            # The page's unchanged field: the same budget that stopped it.
            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": run_id, "model": None, "total_budget_s": 600,
                                 "usd_cap": 0.5, "max_llm_calls": None})
            assert status == 409, body
            assert "total budget" in body["error"]
            assert "total_budget_s" in body["hint"], "the hint must name the field to raise"
            assert operator.busy is False

            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": run_id, "model": None, "total_budget_s": 1200,
                                 "usd_cap": 0.5, "max_llm_calls": 30})
            assert status == 202, body
            limits = body["status"]["limits"]
            assert limits["total_budget_s"] == 1200 and limits["max_llm_calls"] == 30
            operator.wait(20)
        assert checkpoint_of(tmp_path, run_id)["state"] == "done"

    def test_a_cap_stopped_run_is_refused_under_its_cap_and_resumed_above_it(
            self, tmp_path, offline_models):
        operator, run_id = stopped_run(tmp_path)
        saved = checkpoint_of(tmp_path, run_id)
        edit_checkpoint(tmp_path, run_id, spend=dict(saved["spend"], usd=0.51),
                        stop_reason="spend cap of $0.50 reached ($0.5100)")
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": run_id, "model": None, "total_budget_s": 600,
                                 "usd_cap": 0.5, "max_llm_calls": None})
            assert status == 409, body
            assert "cap" in body["error"] and "usd_cap" in body["hint"]
            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": run_id, "model": None, "total_budget_s": 600,
                                 "usd_cap": 2.0, "max_llm_calls": None})
            assert status == 202, body
            assert body["status"]["limits"]["usd_cap"] == 2.0
            operator.wait(20)

    def test_a_refusal_message_reaches_the_page_verbatim(self, tmp_path, offline_models):
        """Whatever the operator says when it refuses is the 409's ``error``:
        the card shows that text, so the route must not replace it."""
        text = "run %s has spent $0.5100 of its $0.50 cap; resume it with a larger cap" % UNKNOWN_ID
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)

            def refuse(*_a, **_k):
                raise RunNotResumable(text)

            operator.resume = refuse
            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": UNKNOWN_ID, "model": None, "usd_cap": 0.5})
        assert status == 409
        assert body["error"] == text and body["hint"]

    def test_a_live_checkpoint_is_listed_as_live(self, tmp_path, offline_models):
        """Finding 5's contract: the row says ``dry_run: false`` for a live run.
        Only listed — a live checkpoint is never resumed by a test."""
        operator, run_id = stopped_run(tmp_path)
        edit_checkpoint(tmp_path, run_id, dry_run=False)
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "GET", "/api/cu/runs")
        assert status == 200
        assert {r["run_id"]: r for r in body["runs"]}[run_id]["dry_run"] is False


# --------------------------------------------------------------------------- #
# 2. The resume route's model: null is Jev only, absent keeps the checkpoint's
# --------------------------------------------------------------------------- #


class TestResumeModelRoute:
    @pytest.mark.parametrize("model", [None, "", "none"])
    def test_null_or_none_resumes_jev_only_not_the_checkpoint_model(
            self, tmp_path, offline_models, model):
        operator, run_id, seen = stopped_run_with_model(tmp_path)
        del seen[:]
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": run_id, "model": model})
            assert status == 202, body
            assert body["status"]["model"] is None
            operator.wait(20)
        assert seen == [], "a Jev-only resume built the checkpoint's model"
        assert checkpoint_of(tmp_path, run_id)["model"] is None

    def test_an_absent_model_keeps_the_checkpoint_model(self, tmp_path, offline_models):
        operator, run_id, seen = stopped_run_with_model(tmp_path)
        del seen[:]
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": run_id})
            assert status == 202, body
            assert body["status"]["model"] == "fake/model"
            operator.wait(20)
        assert seen and set(seen) == {"fake/model"}

    def test_a_model_that_is_not_a_string_is_400(self, tmp_path, offline_models):
        operator, run_id, seen = stopped_run_with_model(tmp_path)
        del seen[:]
        with running(ledger_root=tmp_path) as (port, server):
            server.console._operator = operator
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": run_id, "model": 42})
        assert status == 400, body
        assert "model" in body["error"] and body["hint"]
        assert seen == [] and operator.busy is False


class TestResumeModelCli:
    def test_cu_resume_model_none_is_jev_only(self, tmp_path, capsys, monkeypatch, quiet_switch):
        _operator, run_id, _seen = stopped_run_with_model(tmp_path)

        def no_model(model):
            raise AssertionError("--model none built a planning model: %r" % (model,))

        monkeypatch.setattr(Operator, "_default_llm", staticmethod(no_model))
        capsys.readouterr()
        code = cli.main(["cu", "resume", run_id, "--model", "none", "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        saved = checkpoint_of(tmp_path, run_id)
        assert saved["state"] == "done" and saved["model"] is None


# --------------------------------------------------------------------------- #
# 3. Strict flags: null is the default, a non-boolean is a 400
# --------------------------------------------------------------------------- #


class TestStrictFlags:
    def test_start_with_memory_null_keeps_memory_on(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None, "dry_run": True,
                                 "plan": THREE_GOALS[:1], "memory": None})
            assert status == 202, body
            assert operator.run.memory is True
            operator.wait(20)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None, "dry_run": True,
                                 "plan": THREE_GOALS[:1], "memory": False})
            assert status == 202, body
            assert operator.run.memory is False
            operator.wait(20)

    def test_plan_with_memory_null_keeps_memory_on(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path, FakeLLM())
            seen = []
            real_plan = operator.plan

            def spy(command, model, **kwargs):
                seen.append(kwargs)
                return real_plan(command, model, **kwargs)

            operator.plan = spy
            for memory in (None, False, True):
                status, body = call(port, "POST", "/api/cu/plan",
                                    {"command": "open notepad", "model": "fake/model",
                                     "memory": memory})
                assert status == 200, body
        assert [kw["memory"] for kw in seen] == [True, False, True]

    @pytest.mark.parametrize("field,value", [
        ("memory", "false"), ("memory", 0), ("memory", "true"),
        ("hierarchical", "false"), ("hierarchical", 1), ("hierarchical", []),
    ])
    def test_a_flag_that_is_not_a_boolean_is_400_and_nothing_starts(
            self, tmp_path, offline_models, field, value):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None, "dry_run": True,
                                 "plan": THREE_GOALS[:1], field: value})
            assert status == 400, body
            assert field in body["error"] and "true" in body["hint"]
            assert operator.run is None
            status, body = call(port, "POST", "/api/cu/plan",
                                {"command": "open notepad", "model": None, field: value})
            assert status == 400, body

    def test_pause_with_a_string_is_400_not_a_pause(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/pause", {"pause": "false"})
        assert status == 400, body
        assert "pause" in body["error"]

    def test_confirm_with_the_string_false_does_not_allow_the_step(self, tmp_path, offline_models):
        """``bool("false")`` is ``True``: the step waiting for a yes/no used to
        be allowed by a body that said no."""
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path, FakeLLM())
            status, _ = call(port, "POST", "/api/cu/start",
                             {"command": "delete it", "model": "fake/model", "dry_run": True})
            assert status == 202

            def waiting():
                body = call(port, "GET", "/api/cu/status")[1]
                return body if body.get("pending_confirm") else None

            assert wait_until(waiting), operator.status()["events"]
            status, body = call(port, "POST", "/api/cu/confirm", {"allow": "false"})
            assert status == 400, body
            assert "allow" in body["error"] and "allow" in body["hint"]
            _, after = call(port, "GET", "/api/cu/status")
            assert after["state"] == "waiting_confirm" and after["pending_confirm"]
            call(port, "POST", "/api/cu/stop", {"reason": "cleanup"})
            operator.wait(10)


# --------------------------------------------------------------------------- #
# 4. The CLI's call allowance: refused out of range, like the console's API
# --------------------------------------------------------------------------- #


class TestCliCallAllowance:
    @pytest.mark.parametrize("argv", [["run", "Open the File menu", "--dry-run"],
                                      ["resume", UNKNOWN_ID]])
    @pytest.mark.parametrize("value", ["0", "-3", str(LLM_CAP_CEIL + 1), "5000", "2.5", "many"])
    def test_out_of_range_exits_2_and_nothing_runs(self, tmp_path, capsys, quiet_switch,
                                                   argv, value):
        with pytest.raises(SystemExit) as exc:
            cli.main(["cu"] + argv + ["--max-llm-calls", value, "--ledger-dir", str(tmp_path)])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--max-llm-calls" in err and str(LLM_CAP_CEIL) in err
        assert not (ledger_path(tmp_path).parent / "cu_runs").exists()

    @pytest.mark.parametrize("sub", [["run", "x"], ["resume", UNKNOWN_ID]])
    def test_the_bounds_themselves_are_accepted(self, sub):
        parser = cli.build_parser()
        for value in (1, LLM_CAP_CEIL):
            args = parser.parse_args(["cu"] + sub + ["--max-llm-calls", str(value)])
            assert args.max_llm_calls == value
        assert parser.parse_args(["cu"] + sub).max_llm_calls is None

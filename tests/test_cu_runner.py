"""The computer-use operator, its kill switch and the console routes that drive it.

Everything here is offline and touches no desktop: the operator runs in
``dry_run`` (the planner is a fake, the steps are simulated) and the kill
switch is built without its key and mouse checks. What is under test is the
contract the panel relies on — states, events, the confirm handshake, the
four stop paths, the HTTP status codes — not Windows.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from jevskill import cli
from jevskill.cu import llm as llm_module
from jevskill.cu.killswitch import KillSwitch, Stopped, touch_stop_file
from jevskill.cu.llm import LLMError, LLMReply, extract_json, recommended
from jevskill.cu.runner import Operator, OperatorBusy, OperatorUnavailable, quoted_text
from jevskill.web.server import make_server

MARKED_KEY = "sk-or-v1-TESTKEY123456789"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeLLM:
    """Answers each prompt family with well-formed JSON; records the calls."""

    def __init__(self, plan=None):
        self.calls = []
        self.plan = plan or {"steps": [
            {"goal": "Open Notepad", "done_when": "title has Notepad", "launch": "notepad.exe"},
            {"goal": 'Type "hello world" into the document', "done_when": "value is hello world"},
            {"goal": "Delete the draft", "done_when": "gone"},
        ], "note": "three goals"}

    def chat(self, system, user, **_kw):
        self.calls.append(system[:40])
        if "plan desktop tasks" in system:
            body = self.plan
        elif "exact text" in system:
            body = {"text": "hello world"}
        elif "ended without" in system:
            body = {"completed": False, "steps": [], "give_up": "cannot continue"}
        else:
            body = {"done": True, "why": "looks done"}
        return LLMReply(text=json.dumps(body), model="fake/model", tokens_in=100,
                        tokens_out=20, cost_usd=0.0001, cost_source="provider", latency_ms=5.0)


def make_operator(tmp_path, llm=None, **kwargs):
    switch = KillSwitch(tmp_path / ".jevskill" / "cu.stop", hotkey=False, corner=False,
                        poll_s=0.01)
    return Operator(ledger_root=tmp_path, client_getter=lambda: object(),
                    llm_factory=(lambda _m: llm) if llm is not None else None,
                    kill_switch=switch, **kwargs)


def answer_when_asked(operator, allow, timeout=10.0):
    def worker():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if operator.status().get("pending_confirm"):
                operator.confirm(allow)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class TestHelpers:
    def test_extract_json_finds_fenced_embedded_or_nothing(self):
        assert extract_json('ok:\n```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('noise {"x": [1, 2]} tail') == {"x": [1, 2]}
        assert extract_json("[1, 2]") == [1, 2]
        assert extract_json("no json here") is None
        assert extract_json("") is None

    def test_quoted_text_accepts_ascii_and_polish_quotes_only_when_unambiguous(self):
        assert quoted_text('type "hello world" here') == "hello world"
        assert quoted_text("wpisz „cześć” w polu") == "cześć"
        assert quoted_text('a "x" then "y"') is None
        assert quoted_text("nothing quoted") is None

    def test_recommended_picks_the_newest_plain_model_per_family(self):
        models = [
            {"id": "anthropic/claude-sonnet-4.5", "created": 1},
            {"id": "anthropic/claude-sonnet-5", "created": 2},
            {"id": "anthropic/claude-sonnet-5:thinking", "created": 3},
            {"id": "google/gemini-2.5-flash", "created": 1},
            {"id": "google/gemini-2.5-flash-image", "created": 9},
            {"id": "openai/gpt-5-mini", "created": 1},
        ]
        ids = [m["id"] for m in recommended(models)]
        assert ids[0] == "anthropic/claude-sonnet-5"
        assert "google/gemini-2.5-flash" in ids
        assert "google/gemini-2.5-flash-image" not in ids
        assert not any(":" in i for i in ids)

    def test_model_list_falls_back_when_openrouter_is_unreachable(self):
        def transport(*_a, **_k):
            raise OSError("offline")
        llm_module._models_cache["models"] = None
        models, source = llm_module.cached_models(transport=transport)
        assert source == "fallback"
        assert {m["id"] for m in models} == {m["id"] for m in llm_module.FALLBACK_MODELS}


# --------------------------------------------------------------------------- #
# The kill switch
# --------------------------------------------------------------------------- #


class TestKillSwitch:
    def test_stop_file_trips_once_and_is_consumed(self, tmp_path):
        switch = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
        switch.arm()
        try:
            assert switch.check() is None
            touch_stop_file(tmp_path / "cu.stop")
            deadline = time.time() + 2
            while not switch.event.is_set() and time.time() < deadline:
                time.sleep(0.01)
            assert switch.event.is_set()
            assert "stop file" in switch.reason
            assert not (tmp_path / "cu.stop").exists()
            with pytest.raises(Stopped):
                switch.raise_if_tripped()
        finally:
            switch.disarm()

    def test_arming_removes_a_stale_stop_file(self, tmp_path):
        touch_stop_file(tmp_path / "cu.stop")
        switch = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
        switch.arm()
        try:
            assert not (tmp_path / "cu.stop").exists()
            assert switch.check() is None
        finally:
            switch.disarm()

    def test_describe_names_every_way_to_stop(self, tmp_path):
        info = KillSwitch(tmp_path / "cu.stop").describe()
        assert info["button"] is True
        assert info["cli"] == "jevskill cu stop"
        assert info["stop_file"].endswith("cu.stop")


# --------------------------------------------------------------------------- #
# The operator, dry run
# --------------------------------------------------------------------------- #


class TestOperatorDryRun:
    def test_plans_runs_every_goal_and_asks_before_the_destructive_one(self, tmp_path):
        fake = FakeLLM()
        operator = make_operator(tmp_path, fake)
        operator.start("Otwórz Notatnik, wpisz hello i usuń szkic", "fake/model", dry_run=True)
        answer_when_asked(operator, allow=True)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done", status["events"]
        assert [s["status"] for s in status["plan"]] == ["done", "done", "done"]
        kinds = [e["kind"] for e in status["events"]]
        assert "kill_switch" in kinds
        assert "confirm" in kinds and "confirm_result" in kinds
        assert status["spend"]["llm_calls"] == 1
        assert status["spend"]["jev_calls"] == 0  # simulated steps are not Jev calls
        assert fake.calls[0].startswith("You plan desktop tasks")
        rows = [json.loads(line) for line in
                (tmp_path / ".jevskill" / "ledger.jsonl").read_text("utf-8").splitlines() if line]
        assert [r["which"] for r in rows] == ["cu_llm"]
        assert "sk-or" not in json.dumps(status)

    def test_without_a_model_the_command_is_the_goal_and_quoted_text_types(self, tmp_path):
        operator = make_operator(tmp_path)
        operator.start('Type "abc" into the box', None, dry_run=True)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "done"
        assert len(status["plan"]) == 1 and status["plan"][0]["goal"] == 'Type "abc" into the box'
        texts = [e["text"] for e in status["events"] if e["kind"] == "text"]
        assert texts == ["typing the quoted text from the goal"]

    def test_a_denied_confirmation_blocks_the_goal_and_the_run_fails(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("delete everything", "fake/model", dry_run=True)
        answer_when_asked(operator, allow=False)
        operator.wait(20)
        status = operator.status()
        assert status["state"] == "failed"
        assert status["plan"][2]["status"] == "failed"
        assert status["plan"][2]["stop_reason"] == "blocked"

    def test_stop_ends_the_run_and_marks_the_rest_skipped(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("three goals", "fake/model", dry_run=True)
        time.sleep(0.15)
        operator.stop("test STOP")
        operator.wait(10)
        status = operator.status()
        assert status["state"] == "stopped"
        assert status["stop_reason"] == "test STOP"
        assert status["plan"][-1]["status"] in ("skipped", "stopped")

    def test_stop_while_waiting_for_a_confirmation_is_stopped_not_failed(self, tmp_path):
        # Jev-only: no model, so nothing after the denied gate would have looked
        # at the switch — the run used to end as `failed`, hiding that the user
        # pressed STOP.
        operator = make_operator(tmp_path)
        operator.start("delete the file", None, dry_run=True)
        deadline = time.time() + 10
        while time.time() < deadline and not operator.status().get("pending_confirm"):
            time.sleep(0.02)
        assert operator.status()["state"] == "waiting_confirm"
        operator.stop("panel STOP")
        operator.wait(10)
        status = operator.status()
        assert status["state"] == "stopped"
        assert status["stop_reason"] == "panel STOP"
        assert status["plan"][0]["stop_reason"] == "blocked"

    def test_the_stop_file_stops_a_run_too(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("three goals", "fake/model", dry_run=True)
        time.sleep(0.1)
        touch_stop_file(tmp_path / ".jevskill" / "cu.stop")
        operator.wait(10)
        assert operator.status()["state"] == "stopped"
        assert "stop file" in operator.status()["stop_reason"]

    def test_one_run_at_a_time(self, tmp_path):
        operator = make_operator(tmp_path, FakeLLM())
        operator.start("first", "fake/model", dry_run=True)
        with pytest.raises(OperatorBusy):
            operator.start("second", "fake/model", dry_run=True)
        operator.stop("cleanup")
        operator.wait(10)

    def test_empty_command_is_refused_before_anything_starts(self, tmp_path):
        with pytest.raises(ValueError):
            make_operator(tmp_path).start("   ", None, dry_run=True)

    def test_a_live_run_is_refused_off_windows_but_a_dry_run_is_not(self, tmp_path):
        operator = make_operator(tmp_path, platform_check=lambda: {"ok": False, "reason": "linux"})
        with pytest.raises(OperatorUnavailable):
            operator.start("do it", None)
        operator.start('Type "x"', None, dry_run=True)
        operator.wait(10)
        assert operator.status()["state"] == "done"

    def test_plan_only_calls_the_model_and_touches_nothing(self, tmp_path):
        fake = FakeLLM()
        operator = make_operator(tmp_path, fake)
        plan = operator.plan("open notepad and type hello", "fake/model")
        assert [s["goal"] for s in plan["steps"]][0] == "Open Notepad"
        assert plan["note"] == "three goals"
        assert operator.status()["state"] == "idle"


# --------------------------------------------------------------------------- #
# The console routes
# --------------------------------------------------------------------------- #


class FakeClient:
    def warm(self, mode=None):
        return 1.0

    def decide(self, *_a, **_k):  # pragma: no cover - never reached by these tests
        raise AssertionError("no decision expected")

    def close(self):
        pass


@contextlib.contextmanager
def running(**kwargs):
    server = make_server(port=0, client=FakeClient(), **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", "replace")
        return response.status, json.loads(text)
    finally:
        conn.close()


@pytest.fixture
def offline_models(monkeypatch):
    def fail(**_kw):
        raise LLMError("offline")
    monkeypatch.setattr(llm_module, "fetch_models", fail)
    llm_module._models_cache["models"] = None


@pytest.fixture
def keyed(monkeypatch):
    from jevskill.config import ALL_KEY_ENV_VARS

    for name in ALL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})
    monkeypatch.setenv("OPENROUTER_API_KEY", MARKED_KEY)


class TestRoutes:
    def test_status_is_idle_and_names_the_stop_paths(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, _server):
            status, body = call(port, "GET", "/api/cu/status")
            assert status == 200 and body["state"] == "idle"
            assert body["kill_switch"]["cli"] == "jevskill cu stop"
            assert "platform" in body and "ok" in body["platform"]

    def test_models_fall_back_offline_and_never_carry_the_key(self, tmp_path, offline_models, keyed):
        with running(ledger_root=tmp_path) as (port, _server):
            status, body = call(port, "GET", "/api/cu/models")
            assert status == 200 and body["source"] == "fallback"
            assert body["key_found"] is True
            assert body["default"] == "anthropic/claude-sonnet-5"
            assert MARKED_KEY not in json.dumps(body) and "TESTKEY" not in json.dumps(body)

    def test_start_validates_then_runs_a_dry_run_to_done(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = server.console.operator()
            operator.kill = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
            status, body = call(port, "POST", "/api/cu/start", {"command": " ", "dry_run": True})
            assert status == 400
            status, body = call(port, "POST", "/api/cu/confirm", {"allow": True})
            assert status == 409
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": 'Type "abc"', "model": None, "dry_run": True})
            assert status == 202 and body["run_id"]
            status, _ = call(port, "POST", "/api/cu/start", {"command": "again", "dry_run": True})
            assert status == 409
            operator.wait(20)
            status, body = call(port, "GET", "/api/cu/status?since=0")
            assert status == 200 and body["state"] == "done"
            assert body["next"] == len(body["events"]) > 0
            status, body = call(port, "POST", "/api/cu/stop", {})
            assert status == 200 and body["ok"] is True

    def test_stop_and_confirm_reach_the_operator(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = server.console.operator()
            operator.kill = KillSwitch(tmp_path / "cu.stop", hotkey=False, corner=False)
            operator._llm_factory = lambda _m: FakeLLM()
            status, _ = call(port, "POST", "/api/cu/start",
                             {"command": "delete it", "model": "fake/model", "dry_run": True})
            assert status == 202
            deadline = time.time() + 10
            while time.time() < deadline:
                _, body = call(port, "GET", "/api/cu/status")
                if body.get("pending_confirm"):
                    break
                time.sleep(0.05)
            assert body["state"] == "waiting_confirm"
            assert body["pending_confirm"]["prompt"].startswith("click")
            status, _ = call(port, "POST", "/api/cu/stop", {"reason": "panel STOP"})
            assert status == 200
            operator.wait(10)
            _, body = call(port, "GET", "/api/cu/status")
            assert body["state"] == "stopped"
            assert body["stop_reason"] == "panel STOP"


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_cu_stop_touches_the_stop_file(self, tmp_path, capsys):
        assert cli.main(["cu", "stop", "--ledger-dir", str(tmp_path)]) == 0
        assert (tmp_path / ".jevskill" / "cu.stop").exists()
        assert "stop requested" in capsys.readouterr().out

    def test_cu_run_dry_run_prints_events_and_exits_zero(self, tmp_path, capsys):
        code = cli.main(["cu", "run", 'Type "abc"', "--dry-run", "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "DRY RUN" in out and "goal_done" in out

    def test_cu_help_names_every_stop_path(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["cu", "--help"])
        out = capsys.readouterr().out
        for phrase in ("Ctrl+C", "Ctrl+Alt+Esc", "top-left", "jevskill cu stop"):
            assert phrase in out


class TestPackageExports:
    def test_operator_and_switch_are_lazy_exports(self):
        import jevskill.cu as cu
        from jevskill.cu import killswitch, runner

        assert cu.Operator is runner.Operator
        assert cu.KillSwitch is killswitch.KillSwitch
        assert "Operator" in dir(cu)

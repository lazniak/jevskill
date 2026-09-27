"""The mission tree's surfaces: the console routes and the CLI (spec §9, items 27-28).

The operator's own behaviour — pausing at goal boundaries, checkpointing,
resuming a tree — is pinned in ``test_cu_mission.py`` and ``test_cu_journal.py``.
What is under test here is the contract the panel and a terminal rely on: the
status codes of ``/api/cu/pause``, ``/api/cu/resume`` and ``/api/cu/runs``, the
Origin guard on the new writes, the start fields that reach the operator, a
status payload that only ever *gains* keys, and ``jevskill cu runs`` /
``jevskill cu resume``.

Everything is offline and touches no desktop: every run is a dry run (the
steps are simulated, ~0.35 s each), plans are supplied by the test, and the
kill switch is built without its key and mouse checks. Waits are polls with a
deadline, never a bare sleep standing in for "long enough".
"""

from __future__ import annotations

import http.client
import json
import time

import pytest

from jevskill import cli
from jevskill.cu.killswitch import KillSwitch
from jevskill.stats import ledger_path

from test_cu_runner import FakeLLM, call, make_operator, offline_models, running  # noqa: F401

#: Well-formed (``YYYYMMDD-HHMMSS-hex6``) and never written by any test.
UNKNOWN_ID = "20260101-000000-abcdef"

#: Goals with no typing word and nothing destructive: two simulated steps each,
#: no confirm gate, no compose call — so a Jev-only dry run needs no model.
THREE_GOALS = [{"goal": "Open the File menu", "done_when": "menu open"},
               {"goal": "Pick the Recent item", "done_when": "list shown"},
               {"goal": "Close the File menu", "done_when": "menu closed"}]

#: What the status payload carried before the mission tree. Written out rather
#: than derived, so a key that disappears fails here even if both sides of a
#: comparison lost it together.
OLD_RUN_KEYS = {"run_id", "command", "model", "dry_run", "state", "plan", "current", "events",
                "next", "pending_confirm", "spend", "started_at", "ended_at", "elapsed_s",
                "stop_reason", "error", "limits", "memory", "run", "busy", "platform",
                "kill_switch"}
OLD_IDLE_KEYS = {"state", "run", "events", "next", "busy", "platform", "kill_switch"}
OLD_LIMIT_KEYS = {"max_steps", "budget_s", "total_budget_s", "usd_cap"}
OLD_STEP_KEYS = {"goal", "done_when", "launch", "status", "stop_reason", "note", "steps",
                 "tokens_in", "cost_usd", "wall_ms"}
NEW_RUN_KEYS = {"tree", "current_node", "progress", "paused", "heartbeat_age_s", "segment",
                "events_base", "events_truncated"}
PROGRESS_KEYS = {"phases_done", "phases_total", "leaves_done", "leaves_known",
                 "unexpanded_phases", "current_path", "evidence", "active_s", "paused_s"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def wait_until(predicate, timeout=10.0, step=0.02):
    """Poll ``predicate`` until it returns something truthy; that value, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def goal_started(operator):
    """True once the run's first goal is under way (a ``goal`` event exists)."""
    return any(e["kind"] == "goal" for e in operator.status()["events"])


def console_with(server, tmp_path, llm=None):
    """Give the console a fully faked operator: private ledger, silent switch."""
    operator = make_operator(tmp_path, llm)
    server.console._operator = operator
    return operator


def post_with_origin(port, path, body, origin="https://evil.example"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "Origin": origin})
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode("utf-8", "replace"))
    finally:
        conn.close()


def stopped_run(tmp_path, plan=None):
    """A Jev-only dry run stopped during its first goal: (operator, run_id)."""
    operator = make_operator(tmp_path)
    run_id = operator.start("walk the File menu", None, dry_run=True, plan=plan or THREE_GOALS)
    assert wait_until(lambda: goal_started(operator)), operator.status()["events"]
    operator.stop("test STOP")
    operator.wait(10)
    assert operator.status()["state"] == "stopped"
    return operator, run_id


def checkpoint_of(tmp_path, run_id):
    path = ledger_path(tmp_path).parent / "cu_runs" / run_id / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def quiet_switch(monkeypatch):
    """The CLI builds its own Operator; keep its kill switch off the real
    keyboard and mouse so a cursor parked in the top-left corner cannot stop
    a test run."""
    monkeypatch.setattr("jevskill.cu.runner.KillSwitch",
                        lambda path: KillSwitch(path, hotkey=False, corner=False, poll_s=0.01))


# --------------------------------------------------------------------------- #
# POST /api/cu/pause
# --------------------------------------------------------------------------- #


class TestPauseRoute:
    def test_pause_with_nothing_running_is_409(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/pause", {"pause": True})
        assert status == 409
        assert body["error"] and body["hint"]

    def test_pause_holds_a_dry_run_between_goals_and_continue_finishes_it(
            self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, "plan": THREE_GOALS})
            assert status == 202
            run_id = body["run_id"]
            status, body = call(port, "POST", "/api/cu/pause", {"pause": True})
            assert status == 200 and body["ok"] is True and body["status"]["busy"] is True

            def paused_status():
                body = call(port, "GET", "/api/cu/status?since=0")[1]
                return body if body["state"] == "paused" else None

            held = wait_until(paused_status)
            assert held, operator.status()["events"]
            assert held["paused"] is True and held["busy"] is True
            assert "pause" in [e["kind"] for e in held["events"]]
            assert [s["status"] for s in held["plan"]] != ["done"] * 3, \
                "a pause must hold before the run is over"
            # The pause point forces a checkpoint: another console reading the
            # journal sees a paused run, not a silent one.
            assert checkpoint_of(tmp_path, run_id)["state"] == "paused"

            status, body = call(port, "POST", "/api/cu/pause", {"pause": False})
            assert status == 200 and body["ok"] is True
            operator.wait(20)
            status, final = call(port, "GET", "/api/cu/status?since=0")
        assert final["state"] == "done", final["events"]
        assert [s["status"] for s in final["plan"]] == ["done"] * 3
        assert final["paused"] is False
        assert "resume" in [e["kind"] for e in final["events"]]


# --------------------------------------------------------------------------- #
# POST /api/cu/resume and GET /api/cu/runs
# --------------------------------------------------------------------------- #


class TestResumeRoute:
    def test_a_run_id_that_is_not_a_run_id_is_400(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            for bad in ("../x", "", "x", UNKNOWN_ID + "/../x", None, 42):
                status, body = call(port, "POST", "/api/cu/resume", {"run_id": bad})
                assert status == 400, (bad, body)
                assert body["hint"]
            status, _ = call(port, "POST", "/api/cu/resume", {})
            assert status == 400
        # Nothing was created on the way to refusing: a traversal attempt never
        # reaches the filesystem.
        assert not (ledger_path(tmp_path).parent / "cu_runs").exists()

    def test_an_unknown_run_is_404_not_a_busy_409(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": UNKNOWN_ID})
        assert status == 404
        assert UNKNOWN_ID in body["error"]
        assert "/api/cu/runs" in body["hint"]

    def test_resume_while_a_run_is_active_is_409(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, _ = call(port, "POST", "/api/cu/start",
                             {"command": "walk the File menu", "model": None, "dry_run": True,
                              "plan": THREE_GOALS})
            assert status == 202
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": UNKNOWN_ID})
            assert status == 409, body
            assert body["status_payload"]["busy"] is True
            call(port, "POST", "/api/cu/stop", {"reason": "cleanup"})
            operator.wait(10)

    def test_a_finished_run_is_409(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "open the menu", "model": None, "dry_run": True,
                                 "plan": THREE_GOALS[:1]})
            assert status == 202
            run_id = body["run_id"]
            operator.wait(20)
            assert operator.status()["state"] == "done"
            status, body = call(port, "POST", "/api/cu/resume", {"run_id": run_id})
            assert status == 409, body
            assert "finished" in body["error"]
            status, body = call(port, "GET", "/api/cu/runs")
            assert status == 200 and run_id not in [r["run_id"] for r in body["runs"]]

    def test_a_stopped_dry_run_is_listed_then_resumed_to_the_end(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "GET", "/api/cu/runs")
            assert status == 200 and body == {"runs": []}

            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, "plan": THREE_GOALS})
            assert status == 202
            run_id = body["run_id"]
            assert wait_until(lambda: goal_started(operator))
            status, _ = call(port, "POST", "/api/cu/stop", {"reason": "panel STOP"})
            assert status == 200
            operator.wait(10)

            status, body = call(port, "GET", "/api/cu/runs")
            assert status == 200
            rows = {r["run_id"]: r for r in body["runs"]}
            assert run_id in rows, body
            assert rows[run_id]["state"] == "stopped" and rows[run_id]["dry_run"] is True
            assert rows[run_id]["interrupted"] is False
            assert rows[run_id]["command"] == "walk the File menu"
            # The idle status carries the same rows for the panel's resume card.
            _, idle = call(port, "GET", "/api/cu/status")
            assert idle["busy"] is False
            assert run_id in [r["run_id"] for r in idle["resumable"]]
            events_next = idle["next"]

            status, body = call(port, "POST", "/api/cu/resume",
                                {"run_id": run_id, "model": None})
            assert status == 202, body
            assert body["ok"] is True and body["run_id"] == run_id
            assert body["status"]["segment"] == 2
            operator.wait(20)
            status, final = call(port, "GET", "/api/cu/status?since=%d" % events_next)
            assert status == 200
            assert final["state"] == "done", final["events"]
            assert final["run_id"] == run_id and final["segment"] == 2
            assert [s["status"] for s in final["plan"]] == ["done"] * 3
            # The page's `since` carries on across the resume: no event of the
            # new segment is numbered below where the stopped one ended.
            assert final["events_base"] == events_next
            assert final["events"] and final["events"][0]["i"] == events_next
            assert final["events_truncated"] is False
            status, body = call(port, "GET", "/api/cu/runs")
            assert run_id not in [r["run_id"] for r in body["runs"]], "a done run is not resumable"


# --------------------------------------------------------------------------- #
# The status payload, the Origin guard, and what start forwards
# --------------------------------------------------------------------------- #


class TestStatusAndGuards:
    def test_status_only_gains_keys(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            _, idle = call(port, "GET", "/api/cu/status")
            assert OLD_IDLE_KEYS <= set(idle)
            assert idle["resumable"] == []

            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, "plan": THREE_GOALS})
            assert status == 202
            live = body["status"]
            assert OLD_RUN_KEYS <= set(live), OLD_RUN_KEYS - set(live)
            assert NEW_RUN_KEYS <= set(live), NEW_RUN_KEYS - set(live)
            assert "resumable" not in live, "the resume list is an idle-only field"
            assert OLD_LIMIT_KEYS | {"max_llm_calls", "max_leaves"} <= set(live["limits"])
            operator.wait(20)
            _, done = call(port, "GET", "/api/cu/status?since=0")
        assert done["state"] == "done"
        assert OLD_RUN_KEYS | NEW_RUN_KEYS | {"resumable"} <= set(done)
        assert PROGRESS_KEYS <= set(done["progress"])
        assert done["progress"]["leaves_done"] == done["progress"]["leaves_known"] == 3
        for step in done["plan"]:
            assert OLD_STEP_KEYS | {"node_id", "depth", "phase", "evidence"} <= set(step)
        assert done["tree"]["id"] == "0" and len(done["tree"]["children"]) == 3

    @pytest.mark.parametrize("path,body", [
        ("/api/cu/pause", {"pause": True}),
        ("/api/cu/resume", {"run_id": UNKNOWN_ID}),
    ])
    def test_the_new_writes_refuse_a_foreign_origin(self, tmp_path, offline_models, path, body):
        """Pause and resume move the desktop too: a page in the user's browser
        must not be able to reach them."""
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            status, payload = post_with_origin(port, path, body)
            assert status == 403 and payload["hint"]
            # The same request from the console's own origin gets past the guard
            # (and meets the route's own answer: idle, or no such run).
            status, _ = post_with_origin(port, path, body, origin="http://127.0.0.1:%d" % port)
            assert status in (404, 409)

    def test_the_runs_listing_refuses_a_foreign_host(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            console_with(server, tmp_path)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request("GET", "/api/cu/runs", headers={"Host": "evil.example"})
                response = conn.getresponse()
                status = response.status
                response.read()
            finally:
                conn.close()
        assert status == 403

    def test_start_forwards_the_tree_fields_to_the_operator(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, "plan": THREE_GOALS[:1],
                                 "hierarchical": False, "max_llm_calls": 7, "max_leaves": 12})
            assert status == 202, body
            run = operator.run
            assert run.hierarchical is False
            assert run.llm_cap == 7 and run.llm_cap_explicit is True
            assert run.max_leaves == 12
            assert body["status"]["limits"]["max_llm_calls"] == 7
            assert body["status"]["limits"]["max_leaves"] == 12
            operator.wait(20)

            # Absent (and null) mean the defaults: a tree may be planned, the
            # call allowance is automatic, 60 goals at most.
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, "plan": THREE_GOALS[:1],
                                 "hierarchical": None, "max_llm_calls": None})
            assert status == 202, body
            run = operator.run
            assert run.hierarchical is True
            assert run.llm_cap_explicit is False
            assert run.max_leaves == 60
            operator.wait(20)

    @pytest.mark.parametrize("field,value", [
        ("max_llm_calls", 0), ("max_llm_calls", 201), ("max_llm_calls", 2.5),
        ("max_llm_calls", "many"), ("max_llm_calls", True),
        ("max_leaves", 0), ("max_leaves", 500),
    ])
    def test_a_limit_out_of_range_is_400_and_nothing_starts(self, tmp_path, offline_models,
                                                             field, value):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path)
            status, body = call(port, "POST", "/api/cu/start",
                                {"command": "walk the File menu", "model": None,
                                 "dry_run": True, field: value})
            assert status == 400, body
            assert field in body["error"]
            assert operator.run is None

    def test_plan_forwards_hierarchical(self, tmp_path, offline_models):
        with running(ledger_root=tmp_path) as (port, server):
            operator = console_with(server, tmp_path, FakeLLM())
            seen = []
            real_plan = operator.plan

            def spy(command, model, **kwargs):
                seen.append(kwargs)
                return real_plan(command, model, **kwargs)

            operator.plan = spy
            status, body = call(port, "POST", "/api/cu/plan",
                                {"command": "open notepad", "model": "fake/model",
                                 "hierarchical": False})
            assert status == 200, body
            status, body = call(port, "POST", "/api/cu/plan",
                                {"command": "open notepad", "model": "fake/model"})
            assert status == 200, body
        assert [kw["hierarchical"] for kw in seen] == [False, True]
        assert body["steps"][0]["goal"] == "Open Notepad"
        assert body["tree"]["id"] == "0" and body["source"] == "model"


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_cu_runs_lists_a_stopped_run(self, tmp_path, capsys):
        _operator, run_id = stopped_run(tmp_path)
        capsys.readouterr()
        assert cli.main(["cu", "runs", "--ledger-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        line = next(line for line in out.splitlines() if line.startswith(run_id))
        assert "stopped" in line and "dry run" in line and "walk the File menu" in line
        assert "jevskill cu resume" in out

        assert cli.main(["cu", "runs", "--json", "--ledger-dir", str(tmp_path)]) == 0
        rows = json.loads(capsys.readouterr().out)["runs"]
        assert [r["run_id"] for r in rows] == [run_id]
        assert rows[0]["state"] == "stopped"

    def test_cu_runs_with_nothing_to_resume_says_so(self, tmp_path, capsys):
        assert cli.main(["cu", "runs", "--ledger-dir", str(tmp_path)]) == 0
        assert "no runs to resume" in capsys.readouterr().out

    def test_cu_resume_of_an_unknown_run_exits_2(self, tmp_path, capsys):
        assert cli.main(["cu", "resume", UNKNOWN_ID, "--ledger-dir", str(tmp_path)]) == 2
        captured = capsys.readouterr()
        assert "no checkpoint" in captured.err and UNKNOWN_ID in captured.err
        assert "cu runs" in captured.err

    def test_cu_resume_of_a_malformed_id_exits_2(self, tmp_path, capsys):
        assert cli.main(["cu", "resume", "../x", "--ledger-dir", str(tmp_path)]) == 2
        assert "error:" in capsys.readouterr().err
        assert not (ledger_path(tmp_path).parent / "cu_runs").exists()

    def test_cu_resume_continues_a_stopped_dry_run_to_the_end(self, tmp_path, capsys,
                                                               quiet_switch):
        _operator, run_id = stopped_run(tmp_path)
        capsys.readouterr()
        code = cli.main(["cu", "resume", run_id, "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert out.startswith("resumed run %s (segment 2)" % run_id)
        assert "goal_done" in out and "done after" in out
        assert "resume with:" not in out, "a finished run offers no resume"
        assert checkpoint_of(tmp_path, run_id)["state"] == "done"

    def test_cu_run_forwards_flat_the_call_allowance_and_the_total_budget(self, tmp_path,
                                                                          capsys, quiet_switch):
        code = cli.main(["cu", "run", 'Type "abc"', "--dry-run", "--flat",
                         "--max-llm-calls", "5", "--total-budget-s", "120",
                         "--ledger-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0, out
        run_dirs = list((ledger_path(tmp_path).parent / "cu_runs").iterdir())
        assert len(run_dirs) == 1
        saved = checkpoint_of(tmp_path, run_dirs[0].name)
        assert saved["hierarchical"] is False
        assert saved["limits"]["llm_cap"] == 5 and saved["limits"]["llm_cap_explicit"] is True
        assert saved["limits"]["total_budget_s"] == 120.0

    def test_cu_help_lists_resume_and_runs(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["cu", "--help"])
        out = capsys.readouterr().out
        assert "resume" in out and "runs" in out

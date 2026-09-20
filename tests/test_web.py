"""The web console, exercised over a real socket and never over the network.

The server is started on port 0 in a thread and driven with ``http.client``, so
these tests cover the HTTP contract — status codes, content types, the JSON
error shape — rather than the handler functions in isolation. A fake client
stands in for :class:`jevskill.client.JevClient`, so nothing here spends money
or needs a key.

The load-bearing test is :class:`TestDoctorLeaksNothing`: a console that is one
`curl` away from anything running on the machine must never put key material in
a response. It sets a key whose body contains a unique marker and asserts the
marker appears in **no** response at all, rather than checking the one field
that was expected to hold it.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from jevskill import cli
from jevskill.client import Answer, Decisions
from jevskill.config import ALL_KEY_ENV_VARS
from jevskill.primitives import validate_questions
from jevskill.stats import ledger_path
from jevskill.web.server import (
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    TEMPLATES,
    make_server,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "jevskill" / "web" / "static" / "index.html"

#: A key whose middle is unmistakable. If `TESTKEY` shows up anywhere in any
#: response, some field is echoing the secret.
MARKED_KEY = "sk-or-v1-TESTKEY123456789"


# --------------------------------------------------------------------------- #
# Fakes and plumbing
# --------------------------------------------------------------------------- #


def fake_decisions() -> Decisions:
    return Decisions(
        answers={
            "owner": Answer("choice", "owner", {
                "type": "choice", "choice": "billing", "confidence": 0.88,
                "probabilities": {"billing": 0.88, "api": 0.12},
            }),
            "needs_test": Answer("noul", "needs_test", {"type": "noul", "noul": 0.91}),
        },
        model="typesafe/jev-1.13-20260917",
        request_id="gen-web-1",
        usage={"input_tokens": 500, "output_tokens": 0, "cost": 0.000021,
               "cost_source": "provider"},
        timing_ms={"serialize_ms": 0.2, "http_ms": 310.5, "parse_ms": 0.1,
                   "total_ms": 311.0},
        provider="typesafe",
    )


class FakeClient:
    """Stands in for JevClient; records what the console asked it."""

    def __init__(self) -> None:
        self.calls: list = []
        self.warmed = 0

    def warm(self, mode=None) -> float:
        self.warmed += 1
        return 12.5

    def decide(self, state, questions, session_id=None, timeout_s=None, cache=None):
        # Mirrors JevClient.decide, so a new keyword there fails here rather than
        # going silently untested.
        self.calls.append({"state": state, "questions": questions})
        return fake_decisions()

    def close(self) -> None:
        pass


@contextlib.contextmanager
def running(**kwargs):
    """Start the console on a free port; yield ``(port, server)``."""
    server = make_server(port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, method, path, body=None, content_type="application/json"):
    """``(status, headers, text)`` for one request."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {}
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw.decode("utf-8", "replace")
    finally:
        conn.close()


def get_json(port, path):
    status, _headers, text = request(port, "GET", path)
    return status, json.loads(text)


def post_json(port, path, body):
    status, _headers, text = request(port, "POST", path, body)
    return status, json.loads(text)


@pytest.fixture
def keyed(monkeypatch):
    """A machine with exactly one key, whose value carries a marker."""
    for name in ALL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})
    monkeypatch.setenv("JEVSKILL_API_KEY", MARKED_KEY)


@pytest.fixture
def keyless(monkeypatch):
    """A machine with no key anywhere: env, registry and config file all empty.

    Deleting the environment variables is not enough on Windows, where
    `find_api_key_source` also reads ``HKCU\\Environment`` — the same reason
    ``tests/test_key_precedence.py`` patches both.
    """
    for name in ALL_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("JEVSKILL_PROVIDER", raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


class TestDoctorLeaksNothing:
    def test_no_response_contains_key_material(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            bodies = []
            for path in ("/api/doctor", "/api/templates", "/api/history", "/healthz", "/"):
                _status, _headers, text = request(port, "GET", path)
                bodies.append(text)
            _status, _headers, text = request(port, "POST", "/api/estimate", {"state": "x"})
            bodies.append(text)
            _status, _headers, text = request(
                port, "POST", "/api/decide",
                {"state": "hello", "questions": {"q": {"type": "noul", "instructions": "ok?"}}},
            )
            bodies.append(text)
        joined = "\n".join(bodies)
        assert "TESTKEY" not in joined
        assert MARKED_KEY not in joined

    def test_doctor_reports_the_variable_and_a_fingerprint(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = get_json(port, "/api/doctor")
        assert status == 200
        assert payload["key_found"] is True
        assert payload["key_name"] == "JEVSKILL_API_KEY"
        assert payload["key_source"] == "env"
        # Eight hex characters of SHA-256: enough to tell two keys apart, useless
        # for recovering either.
        assert re.fullmatch(r"[0-9a-f]{8}", payload["key_fingerprint"])
        for field in ("provider", "model", "base_url", "version"):
            assert payload[field]

    def test_doctor_without_a_key_still_answers(self, keyless, tmp_path):
        with running(ledger_root=str(tmp_path)) as (port, _server):
            status, payload = get_json(port, "/api/doctor")
        assert status == 200
        assert payload["key_found"] is False
        # The page shows these two names to the user; they must come from the
        # provider table, not from a string in the front end.
        assert payload["key_env_names"]


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #


class TestEstimate:
    def test_sizes_a_state_against_the_budget(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(port, "/api/estimate", {"state": "x" * 1000})
        assert status == 200
        assert payload["chars"] == 1000
        assert payload["tokens"] > 0
        assert payload["over_budget"] is False
        assert payload["budget"] > payload["tokens"]

    def test_a_state_over_the_budget_says_so(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(port, "/api/estimate", {"state": "x" * 400_000})
        assert status == 200
        assert payload["over_budget"] is True


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #


BUNDLE = {
    "owner": {
        "type": "choice",
        "instructions": "Which module owns the failure at `state`?",
        "criteria": {"billing": "Payments.", "api": "HTTP layer.", "unclear": "No idea."},
    },
    "needs_test": {"type": "noul", "instructions": "Does `state` need a new test?"},
}


class TestDecide:
    def test_happy_path_answers_and_writes_one_ledger_row(self, keyed, tmp_path):
        fake = FakeClient()
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/decide",
                {"state": "the build failed in billing", "questions": BUNDLE,
                 "intent": "ci-triage"},
            )
        assert status == 200
        assert payload["decisions"]["answers"]["owner"]["choice"] == "billing"
        assert payload["model"] and payload["provider"] == "typesafe"
        assert payload["cost_source"] == "provider"
        assert payload["request_id"] == "gen-web-1"
        assert payload["decision_id"].startswith("d_")
        assert payload["timing_ms"]["total_ms"] == 311.0

        rows = [json.loads(line) for line in
                ledger_path(str(tmp_path)).read_text("utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        row = rows[0]
        # Tagged as the console, so `GET /api/history` and `jevskill stats` can
        # tell a browser decision from a harness's.
        assert row["which"] == "web"
        assert row["intent"] == "ci-triage"
        assert row["decision_id"] == payload["decision_id"]
        assert row["questions"] == 2
        assert row["tokens_in"] == 500
        # Written by the same builder `jevskill ask` uses, so the derived
        # baseline columns exist rather than being zero.
        assert row["baseline_tokens"] > 0
        assert row["baseline_cost_usd"] > 0

    def test_a_plain_text_state_arrives_as_text_and_json_as_json(self, keyed, tmp_path):
        fake = FakeClient()
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            post_json(port, "/api/decide", {"state": "line one\nline two", "questions": BUNDLE})
            post_json(port, "/api/decide", {"state": '{"diff": "a"}', "questions": BUNDLE})
        assert fake.calls[0]["state"] == "line one\nline two"
        # Sent as an object, so a question naming `state.diff` points at something.
        assert fake.calls[1]["state"] == {"diff": "a"}

    def test_secrets_are_scrubbed_and_reported(self, keyed, tmp_path):
        fake = FakeClient()
        secret = "AKIA" + "A" * 16
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/decide", {"state": f"aws key {secret} in the log", "questions": BUNDLE})
        assert status == 200
        assert payload["redactions"]["count"] == 1
        assert payload["redactions"]["kinds"] == ["aws_key"]
        assert secret not in fake.calls[0]["state"]
        row = json.loads(ledger_path(str(tmp_path)).read_text("utf-8").splitlines()[0])
        assert row["extra"]["redactions"] == ["aws_key"]

    def test_redaction_can_be_turned_off(self, keyed, tmp_path):
        fake = FakeClient()
        secret = "AKIA" + "B" * 16
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            post_json(port, "/api/decide",
                      {"state": secret, "questions": BUNDLE, "redact": False})
        assert fake.calls[0]["state"] == secret

    def test_an_invalid_bundle_is_a_400_with_a_hint(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/decide",
                {"state": "x", "questions": {"owner": {"type": "choice",
                                                       "instructions": "pick",
                                                       "criteria": {"only": "one"}}}},
            )
        assert status == 400
        assert payload["status"] == 400
        assert "at least 2 options" in payload["error"]
        assert payload["hint"]
        assert not ledger_path(str(tmp_path)).exists()

    def test_no_key_is_a_503_carrying_the_doctor_payload(self, keyless, tmp_path):
        with running(ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/decide", {"state": "x", "questions": BUNDLE})
        assert status == 503
        assert payload["status"] == 503
        assert payload["doctor"]["key_found"] is False
        assert payload["doctor"]["key_env_names"]
        assert "JEV_API_KEY" in payload["error"]

    def test_review_reasons_name_the_measurement(self, keyed, tmp_path):
        """A `needs review` badge with no number is an instruction to ignore it."""
        class Hesitant(FakeClient):
            def decide(self, state, questions, session_id=None, timeout_s=None, cache=None):
                return Decisions(
                    answers={"owner": Answer("choice", "owner", {
                        "type": "choice", "choice": "billing", "confidence": 0.52,
                        "probabilities": {"billing": 0.52, "api": 0.48},
                    })},
                    model="m", request_id="r",
                    usage={"input_tokens": 10, "output_tokens": 0, "cost": 0.0},
                    timing_ms={"total_ms": 1.0},
                )

        with running(client=Hesitant(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/decide", {"state": "x", "questions": BUNDLE})
        assert status == 200
        assert "owner" in payload["review"]
        reason = payload["review"]["owner"]
        # Both failing conditions, with their numbers: 0.52 is under the 0.75
        # floor *and* 0.04 clear of the runner-up, under the 0.10 margin.
        assert "0.52" in reason and "0.75" in reason
        assert "margin 0.04" in reason and "0.10" in reason
        row = json.loads(ledger_path(str(tmp_path)).read_text("utf-8").splitlines()[0])
        assert row["extra"]["review"] == ["owner"]


# --------------------------------------------------------------------------- #
# Plan, templates and history
# --------------------------------------------------------------------------- #


class TestPlan:
    def test_a_bounded_problem_gets_a_pattern(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(
                port, "/api/plan", {"problem": "which module owns this failing test"})
        assert status == 200
        assert payload["use_jev"] is True
        assert payload["plan"]["pattern"]
        assert payload["plan"]["layers"]
        assert payload["plan"]["calls"] >= 1

    def test_prose_is_refused_for_free(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = post_json(port, "/api/plan", {"problem": "summarize this diff"})
        assert status == 200
        assert payload["use_jev"] is False


class TestTemplates:
    def test_every_shipped_template_would_be_accepted_by_the_api(self):
        """A template the API rejects is worse than no template: the user assumes
        the shipped example is the correct shape."""
        assert TEMPLATES, "the console ships no templates"
        for name, template in TEMPLATES.items():
            validate_questions(template["questions"])
            assert template["title"] and template["source"], name

    def test_the_endpoint_serves_the_same_dict(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = get_json(port, "/api/templates")
        assert status == 200
        assert set(payload["templates"]) == set(TEMPLATES)
        for template in payload["templates"].values():
            validate_questions(template["questions"])


class TestHistory:
    def test_only_console_rows_come_back_newest_first(self, keyed, tmp_path):
        from jevskill.stats import record_decision

        # A harness row in the same ledger must not appear in the console's history.
        record_decision(which="triage", intent="from the CLI", root=str(tmp_path))
        fake = FakeClient()
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            post_json(port, "/api/decide",
                      {"state": "a", "questions": BUNDLE, "intent": "first"})
            post_json(port, "/api/decide",
                      {"state": "b", "questions": BUNDLE, "intent": "second"})
            status, payload = get_json(port, "/api/history?limit=20")
        assert status == 200
        intents = [row["intent"] for row in payload["decisions"]]
        assert intents == ["second", "first"]
        # The ledger records measurements, not payloads, so the page must not
        # promise to reload a past request.
        assert all(row["has_request"] is False for row in payload["decisions"])


# --------------------------------------------------------------------------- #
# The HTTP contract
# --------------------------------------------------------------------------- #


class TestHttpContract:
    def test_healthz(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, payload = get_json(port, "/healthz")
        assert (status, payload) == (200, {"ok": True})

    def test_an_oversized_body_is_refused_before_it_is_decided(self, keyed, tmp_path):
        fake = FakeClient()
        body = b'{"state": "' + b"x" * (MAX_BODY_BYTES + 4096) + b'"}'
        with running(client=fake, ledger_root=str(tmp_path)) as (port, _server):
            status, _headers, text = request(port, "POST", "/api/decide", body)
        payload = json.loads(text)
        assert status == 413
        assert payload["status"] == 413
        assert payload["limit"] == MAX_BODY_BYTES
        assert payload["hint"]
        assert fake.calls == []

    def test_a_form_post_is_415(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, _headers, text = request(
                port, "POST", "/api/decide", b"state=x",
                content_type="application/x-www-form-urlencoded")
        payload = json.loads(text)
        assert status == 415
        assert payload["hint"]

    def test_a_body_that_is_not_json_is_400(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, _headers, text = request(port, "POST", "/api/decide", b"{nope")
        assert status == 400
        assert json.loads(text)["error"]

    @pytest.mark.parametrize("method,path", [("GET", "/api/nope"), ("POST", "/api/nope")])
    def test_an_unknown_route_is_json_not_html(self, keyed, tmp_path, method, path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, headers, text = request(
                port, method, path, {} if method == "POST" else None)
        assert status == 404
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(text)["status"] == 404

    def test_a_cross_origin_post_is_refused(self, keyed, tmp_path):
        """Loopback stops another machine; it does not stop another *page*."""
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request(
                    "POST", "/api/decide",
                    body=json.dumps({"state": "x", "questions": BUNDLE}).encode(),
                    headers={"Content-Type": "application/json",
                             "Origin": "https://evil.example"},
                )
                response = conn.getresponse()
                status, text = response.status, response.read().decode()
            finally:
                conn.close()
        assert status == 403
        assert json.loads(text)["hint"]


class TestStaticAssets:
    @pytest.mark.parametrize("path,expected", [
        ("/", "text/html"),
        ("/index.html", "text/html"),
        ("/app.js", "text/javascript"),
        ("/app.css", "text/css"),
    ])
    def test_content_types_are_stated_not_guessed(self, keyed, tmp_path, path, expected):
        """`mimetypes` reads the Windows registry, where `.js` is often
        `text/plain` — and a console whose script is plain text does not run."""
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, headers, text = request(port, "GET", path)
        assert status == 200
        assert headers["Content-Type"].startswith(expected)
        assert text

    def test_the_page_carries_the_wordmark(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, _headers, text = request(port, "GET", "/")
        assert status == 200
        assert "jev · console" in text

    def test_a_missing_asset_is_a_json_404(self, keyed, tmp_path):
        with running(client=FakeClient(), ledger_root=str(tmp_path)) as (port, _server):
            status, headers, text = request(port, "GET", "/nope.js")
        assert status == 404
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(text)["error"]

    def test_the_page_references_only_its_own_assets_and_one_font_host(self):
        """A local console that pulls scripts from third parties is a local
        console that can be changed by a third party."""
        src = INDEX.read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:href|src)="([^"]+)"', src))
        allowed_local = {"/app.css", "/app.js"}
        for url in referenced:
            if url in allowed_local:
                continue
            assert url.startswith("https://fonts.googleapis.com/"), url
        # And nothing sneaks in outside an href/src attribute either.
        origins = set(re.findall(r"https?://[^\s\"')]+", src))
        assert all(o.startswith("https://fonts.googleapis.com/") for o in origins), origins


# --------------------------------------------------------------------------- #
# Binding and the CLI
# --------------------------------------------------------------------------- #


class TestBinding:
    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
    def test_only_loopback_may_be_bound(self, host):
        """The console has no authentication and spends a live key, so "bind it
        anywhere" is not a preference the caller gets to express."""
        with pytest.raises(ValueError) as excinfo:
            make_server(host=host, port=0)
        assert "127.0.0.1" in str(excinfo.value)

    def test_localhost_is_accepted_and_bound_to_the_v4_loopback(self, keyed, tmp_path):
        server = make_server(host="localhost", port=0, client=FakeClient(),
                             ledger_root=str(tmp_path))
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()


class TestCli:
    def test_web_offers_no_host_flag(self):
        subparsers = [a for a in cli.build_parser()._actions if hasattr(a, "choices")
                      and isinstance(getattr(a, "choices", None), dict)]
        web = subparsers[0].choices["web"]
        offered = {opt for action in web._actions for opt in action.option_strings}
        assert "--host" not in offered
        assert {"--port", "--no-open", "--ledger-dir"} <= offered

    def test_web_help_says_why_there_is_no_host_flag(self, capsys):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["web", "--help"])
        out = capsys.readouterr().out
        assert "no --host flag" in out
        assert "no authentication" in out
        assert "127.0.0.1" in out

    def test_the_default_port_matches_the_server(self):
        """The CLI repeats the port so `--help` need not import `http.server`;
        this is what stops the two copies drifting."""
        args = cli.build_parser().parse_args(["web"])
        assert args.port == DEFAULT_PORT
        assert cli.WEB_DEFAULT_PORT == DEFAULT_PORT

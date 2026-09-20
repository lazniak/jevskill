"""Client tests: response parsing, error classification, transport behaviour.

Everything here runs against a fake transport, so the suite is fast and offline.
"""

from __future__ import annotations

import json

import pytest

from jevskill.client import Answer, Decisions, JevClient, estimate_tokens
from jevskill.config import Config
from jevskill.errors import JevApiError, JevConfigError
from jevskill.primitives import choice, noul, score

FIXTURE = {
    "id": "gen-dec-1",
    "model": "typesafe/jev-1.13-20260917",
    "provider": "TypeSafe",
    "answers": {
        "is_bug": {"type": "noul", "noul": 0.96},
        "team": {
            "type": "choice",
            "choice": "payments",
            "confidence": 0.75,
            "probabilities": {"payments": 0.84, "frontend": 0.16, "account": 0},
        },
        "urgency": {
            "type": "score",
            "score": 1.99,
            "confidence": 0.99,
            "legend": {"0": "Can wait", "1": "This week", "2": "Blocking"},
            "probabilities": {"0": 0, "1": 0.01, "2": 0.99},
        },
    },
    "usage": {"cost": 0.000019992, "input_tokens": 476, "output_tokens": 70},
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | bytes):
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.text = self.content.decode()

    @property
    def status(self) -> int:
        return self.status_code


class FakeTransport:
    """Records bodies and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies: list[bytes] = []
        self.headers: dict = {}

    def post(self, url, content=None, **kwargs):
        self.bodies.append(content)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def head(self, url, **kwargs):
        return FakeResponse(200, b"{}")

    def close(self):
        pass


def make_client(responses, **overrides) -> tuple[JevClient, FakeTransport]:
    transport = FakeTransport(responses)
    config = Config(api_key="sk-or-v1-test", **overrides)
    client = JevClient(config, client=transport)
    return client, transport


QUESTIONS = {"is_bug": noul("Is it a bug?", "yes", "no")}


class TestRequestBody:
    def test_sends_model_state_and_questions(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide({"ticket": "broken"}, QUESTIONS)
        body = json.loads(transport.bodies[0])
        assert body["model"] == "typesafe/jev-1.13"
        assert body["state"] == {"ticket": "broken"}
        assert body["questions"]["is_bug"]["type"] == "noul"

    def test_session_id_is_included_when_given(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide({"a": 1}, QUESTIONS, session_id="session-42")
        assert json.loads(transport.bodies[0])["session_id"] == "session-42"

    def test_session_id_omitted_when_absent(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide({"a": 1}, QUESTIONS)
        assert "session_id" not in json.loads(transport.bodies[0])

    def test_session_id_is_sanitised_and_capped(self):
        # Quotes would break the hand-assembled JSON, so they are replaced, and
        # the API caps the field at 256 characters.
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide({"a": 1}, QUESTIONS, session_id='bad"quote' + "x" * 400)
        sent = json.loads(transport.bodies[0])["session_id"]
        assert '"' not in sent
        assert len(sent) <= 256

    def test_unicode_state_survives_the_round_trip(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide({"opis": "usterka — brak zaokrąglenia €"}, QUESTIONS)
        assert json.loads(transport.bodies[0])["state"]["opis"] == "usterka — brak zaokrąglenia €"

    def test_a_string_state_is_accepted(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide("just some text", QUESTIONS)
        assert json.loads(transport.bodies[0])["state"] == "just some text"

    def test_an_array_state_is_accepted(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        client.decide(["a", "b"], QUESTIONS)
        assert json.loads(transport.bodies[0])["state"] == ["a", "b"]


class TestResponseParsing:
    def test_typed_answers(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.noul("is_bug") == pytest.approx(0.96)
        assert result.choice("team") == "payments"
        assert result.score("urgency") == pytest.approx(1.99)

    def test_accessors_are_type_checked(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        # Asking for the wrong type returns None rather than a wrong value.
        assert result.noul("team") is None
        assert result.choice("is_bug") is None
        assert result.score("team") is None

    def test_missing_answer_returns_none(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.value("does_not_exist") is None
        assert result.probs("does_not_exist") == {}

    def test_probabilities_are_floats(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        probs = result.probs("team")
        assert probs == {"payments": 0.84, "frontend": 0.16, "account": 0.0}
        assert all(isinstance(v, float) for v in probs.values())

    def test_top_ranks_options(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.top("team", 2) == [("payments", 0.84), ("frontend", 0.16)]

    def test_legend_preserved_for_scores(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.answers["urgency"].legend["2"] == "Blocking"

    def test_usage_and_timings_reported(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.input_tokens == 476
        assert result.cost_usd == pytest.approx(0.000019992)
        for key in ("serialize_ms", "http_ms", "parse_ms", "total_ms"):
            assert key in result.timing_ms

    def test_noul_as_bool_uses_the_caller_threshold(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert result.answers["is_bug"].as_bool(0.9) is True
        assert result.answers["is_bug"].as_bool(0.99) is False

    def test_unknown_answer_shape_is_passed_through(self):
        payload = dict(FIXTURE)
        payload["answers"] = {"weird": {"type": "future_type", "value": 5}}
        client, _ = make_client([FakeResponse(200, payload)])
        result = client.decide({}, QUESTIONS)
        assert result.value("weird") is None  # unknown kinds have no value
        assert result.answers["weird"].raw["value"] == 5

    def test_missing_usage_does_not_crash(self):
        payload = {"answers": {"is_bug": {"type": "noul", "noul": 0.5}}}
        client, _ = make_client([FakeResponse(200, payload)])
        result = client.decide({}, QUESTIONS)
        assert result.input_tokens == 0
        assert result.cost_usd == 0.0


class TestErrors:
    def test_401_is_not_retried_and_carries_a_hint(self):
        client, transport = make_client([FakeResponse(401, {"error": {"message": "no key"}})])
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 401
        assert "API key" in info.value.hint
        assert info.value.retryable is False
        assert len(transport.bodies) == 1

    def test_413_tells_the_caller_to_chunk(self):
        client, _ = make_client([FakeResponse(413, {"error": {"message": "too big"}})])
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert "context window" in info.value.hint
        # The hint names the ceiling for the provider actually in use.
        assert "32,000" in info.value.hint

    def test_402_points_at_credits(self):
        client, _ = make_client([FakeResponse(402, {"error": {"message": "no funds"}})])
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert "credits" in info.value.hint

    def test_404_names_the_correct_endpoint(self):
        client, _ = make_client([FakeResponse(404, {"error": {"message": "nope"}})])
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert "/api/alpha/decisions" in info.value.hint

    def test_429_is_retried_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client(
            [FakeResponse(429, {"error": {"message": "slow down"}}), FakeResponse(200, FIXTURE)]
        )
        result = client.decide({}, QUESTIONS)
        assert result.noul("is_bug") == pytest.approx(0.96)
        assert len(transport.bodies) == 2
        assert result.attempts == 2

    def test_502_is_retried(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client(
            [FakeResponse(502, b"bad gateway"), FakeResponse(200, FIXTURE)]
        )
        client.decide({}, QUESTIONS)
        assert len(transport.bodies) == 2

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 520, 522, 524, 529])
    def test_the_whole_transient_5xx_family_is_retried(self, monkeypatch, status):
        # 520 in particular was observed live while benchmarking this repository;
        # before it was listed, a run died mid-suite.
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client(
            [FakeResponse(status, b"transient"), FakeResponse(200, FIXTURE)]
        )
        result = client.decide({}, QUESTIONS)
        assert result.noul("is_bug") == pytest.approx(0.96)
        assert len(transport.bodies) == 2

    def test_520_has_a_hint(self):
        client, _ = make_client([FakeResponse(520, b"unknown")] * 4, retries=0)
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.retryable is True
        assert "520" in str(info.value)

    def test_retries_are_bounded(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client(
            [FakeResponse(529, b"overloaded") for _ in range(5)], retries=2
        )
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 529
        assert len(transport.bodies) == 3  # initial + 2 retries

    def test_transport_failure_surfaces_as_status_zero(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, _ = make_client([ConnectionError("network down")] * 3, retries=2)
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 0
        assert info.value.retryable is True

    def test_error_message_is_extracted_from_the_body(self):
        client, _ = make_client([FakeResponse(400, {"error": {"message": "bad question type"}})])
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert "bad question type" in str(info.value)


class TestConfigGuard:
    def test_missing_key_raises_before_any_request(self, monkeypatch):
        for name in ("JEVSKILL_API_KEY", "OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY", "JEVUSE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr("jevskill.config._file_key", lambda: "")
        monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
        with pytest.raises(JevConfigError, match="No Jev API key"):
            JevClient(Config(api_key=""))

    def test_invalid_questions_never_reach_the_network(self):
        client, transport = make_client([FakeResponse(200, FIXTURE)])
        from jevskill.errors import JevQuestionError

        with pytest.raises(JevQuestionError):
            client.decide({}, {"q": {"type": "choice", "instructions": "x", "criteria": {"a": "A"}}})
        assert transport.bodies == []


class TestWarm:
    def test_warm_returns_a_duration(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        assert client.warm() >= 0.0

    def test_warm_survives_a_failing_handshake(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])

        def boom(*_a, **_k):
            raise ConnectionError("no dns")

        client._client.head = boom
        assert client.warm() >= 0.0


class TestHelpers:
    def test_estimate_tokens_grows_with_size(self):
        from jevskill.config import CHARS_PER_TOKEN

        assert estimate_tokens("x" * 3600) == int(3600 / CHARS_PER_TOKEN)

    def test_estimate_tokens_accepts_bytes(self):
        assert estimate_tokens(b"x" * 3600) == estimate_tokens("x" * 3600)

    def test_estimate_agrees_with_the_orchestrate_helper(self):
        from jevskill.orchestrate import count_tokens

        assert estimate_tokens("x" * 3600) == count_tokens("x" * 3600)

    def test_decisions_to_dict_is_serialisable(self):
        client, _ = make_client([FakeResponse(200, FIXTURE)])
        result = client.decide({}, QUESTIONS)
        assert json.loads(json.dumps(result.to_dict()))["answers"]["is_bug"]["noul"] == 0.96


class TestAnswerObject:
    def test_to_dict_merges_kind_and_name(self):
        answer = Answer("noul", "gate", {"type": "noul", "noul": 0.4})
        assert answer.to_dict() == {"kind": "noul", "name": "gate", "type": "noul", "noul": 0.4}
        assert answer.value == 0.4
        assert answer.confidence is None
        assert answer.probabilities == {}

    def test_choice_top_defaults_to_three(self):
        answer = Answer("choice", "c", {
            "choice": "a",
            "probabilities": {"a": 0.5, "b": 0.3, "c": 0.15, "d": 0.05},
        })
        assert [name for name, _ in answer.top()] == ["a", "b", "c"]
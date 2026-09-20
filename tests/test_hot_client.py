"""Hot-loop client: hot defaults, hedged requests, warm-up modes, async.

Everything here runs against a fake transport with *small real delays* (tens of
milliseconds). Hedging is a race between two threads, and a test that mocked the
clock would be testing the mock rather than the race — the failure this code has
to avoid is two legs both being treated as the winner, and only real scheduling
produces it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from jevskill.client import (
    HAS_HTTPX,
    HOT_HEDGE_DEFAULT,
    WARM_QUESTIONS,
    AsyncJevClient,
    JevClient,
    _apply_hot_defaults,
)
from jevskill.config import (
    HOT_RETRIES,
    HOT_TIMEOUT_CONNECT_S,
    HOT_TIMEOUT_READ_S,
    INPUT_PRICE_PER_MTOK,
    Config,
)
from jevskill.errors import JevApiError, JevConfigError
from jevskill.primitives import noul

FIXTURE = {
    "id": "gen-hot-1",
    "model": "jev-1.13.0",
    "answers": {"is_bug": {"type": "noul", "noul": 0.9}},
    "usage": {"input_tokens": 1000, "output_tokens": 10},
}
QUESTIONS = {"is_bug": noul("Is it a bug?", "yes", "no")}


class Leg:
    """One scripted response: how long it takes and what it returns."""

    def __init__(self, delay_s: float, status: int = 200, payload: dict | bytes | None = None):
        self.delay_s = delay_s
        self.status = status
        self.payload = FIXTURE if payload is None else payload


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | bytes):
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class SlowTransport:
    """Replays scripted legs in order, thread-safely, sleeping for real."""

    def __init__(self, legs: list[Leg]):
        self._legs = list(legs)
        self._lock = threading.Lock()
        self.bodies: list[bytes] = []
        self.heads = 0

    def post(self, url, content=None, **kwargs):
        with self._lock:
            self.bodies.append(content)
            leg = self._legs.pop(0) if self._legs else Leg(0.0)
        time.sleep(leg.delay_s)
        return FakeResponse(leg.status, leg.payload)

    def head(self, url, **kwargs):
        self.heads += 1
        return FakeResponse(200, b"{}")

    def close(self):
        pass


def make_client(legs, **kwargs) -> tuple[JevClient, SlowTransport]:
    transport = SlowTransport(legs)
    config = Config(api_key="sk-or-v1-test")
    return JevClient(config, client=transport, **kwargs), transport


class TestHotDefaults:
    def test_hot_retunes_the_three_loop_knobs(self):
        client, _ = make_client([Leg(0.0)], hot=True)
        assert client.config.timeout_read_s == HOT_TIMEOUT_READ_S
        assert client.config.timeout_connect_s == HOT_TIMEOUT_CONNECT_S
        assert client.config.retries == HOT_RETRIES
        assert client.hot is True

    def test_default_client_is_untouched(self):
        client, _ = make_client([Leg(0.0)])
        assert client.config.timeout_read_s == Config.__dataclass_fields__["timeout_read_s"].default
        assert client.config.retries == Config.__dataclass_fields__["retries"].default
        assert client.config.hedge is False
        assert client.hot is False

    def test_an_explicit_timeout_survives_hot_mode(self):
        # Hot mode changes defaults, not a caller's stated number.
        config = Config(api_key="k", timeout_read_s=8.0, retries=5)
        hot = _apply_hot_defaults(config, hot=True, hedge=None, hedge_after_ms=None)
        assert hot.timeout_read_s == 8.0
        assert hot.retries == 5
        assert hot.timeout_connect_s == HOT_TIMEOUT_CONNECT_S  # this one was default

    def test_the_callers_config_object_is_never_mutated(self):
        config = Config(api_key="k")
        JevClient(config, client=SlowTransport([Leg(0.0)]), hot=True)
        assert config.timeout_read_s == 60.0
        assert config.retries == 2
        assert config.hedge is False

    def test_hot_follows_the_measured_hedging_default(self):
        """Hot mode does not hedge, because hedging measured worse.

        The value is read from the constant rather than asserted as False, so
        the day a measurement flips it back this test follows instead of
        failing — but `HOT_HEDGE_DEFAULT`'s docstring has to carry the numbers.
        """
        client, _ = make_client([Leg(0.0)], hot=True)
        assert client.config.hedge is HOT_HEDGE_DEFAULT
        assert HOT_HEDGE_DEFAULT is False, "flip this only with a bench run"
        on, _ = make_client([Leg(0.0)], hot=True, hedge=True)
        assert on.config.hedge is True
        off, _ = make_client([Leg(0.0)], hot=True, hedge=False)
        assert off.config.hedge is False

    def test_hedge_after_ms_is_overridable(self):
        client, _ = make_client([Leg(0.0)], hot=True, hedge_after_ms=120)
        assert client.config.hedge_after_ms == 120.0


class TestNoHedgeByDefault:
    def test_a_default_call_reports_no_hedge_fields_at_all(self):
        client, transport = make_client([Leg(0.0)])
        result = client.decide({}, QUESTIONS)
        assert "hedged" not in result.timing_ms
        assert "winner" not in result.timing_ms
        assert "hedge_cost_usd_est" not in result.usage
        assert len(transport.bodies) == 1
        assert result.hedged is False and result.winner == ""


class TestHedging:
    def test_a_fast_answer_never_fires_the_duplicate(self):
        client, transport = make_client([Leg(0.0)], hot=True, hedge=True, hedge_after_ms=200)
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is False
        assert result.timing_ms["winner"] == "primary"
        assert len(transport.bodies) == 1
        assert client.requests_sent == 1
        # Nothing was abandoned, so nothing is charged for it.
        assert "hedge_cost_usd_est" not in result.usage

    def test_a_slow_answer_fires_the_duplicate_and_the_faster_leg_wins(self):
        client, transport = make_client(
            [Leg(0.30), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=40
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is True
        assert result.timing_ms["winner"] == "hedge"
        assert len(transport.bodies) == 2
        assert transport.bodies[0] == transport.bodies[1], "the hedge must be identical"

    def test_the_abandoned_leg_is_costed_rather_than_ignored(self):
        client, _ = make_client([Leg(0.30), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=40)
        result = client.decide({}, QUESTIONS)
        expected = FIXTURE["usage"]["input_tokens"] / 1_000_000 * INPUT_PRICE_PER_MTOK
        assert result.usage["hedge_cost_usd_est"] == pytest.approx(expected)
        assert result.usage["hedge_cost_source"] == "estimated"
        # `cost_usd` is what the ledger records, so it must be both legs.
        assert result.cost_usd == pytest.approx(result.usage["cost"] + expected)

    def test_a_primary_that_answers_first_still_wins_after_a_hedge_fired(self):
        client, transport = make_client(
            [Leg(0.10), Leg(0.50)], hot=True, hedge=True, hedge_after_ms=40
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is True
        assert result.timing_ms["winner"] == "primary"
        assert len(transport.bodies) == 2  # spent twice, used once

    def test_a_non_200_waits_for_the_twin_instead_of_failing(self):
        # The primary comes back first with a retryable 429; the hedge is still
        # running and its 200 is worth more than the error.
        client, _ = make_client(
            [Leg(0.06, 429, {"error": {"message": "slow down"}}), Leg(0.25)],
            hot=True,
            hedge=True,
            hedge_after_ms=40,
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["winner"] == "hedge"
        assert result.noul("is_bug") == pytest.approx(0.9)

    def test_two_failures_still_raise_the_api_error(self):
        client, _ = make_client(
            [Leg(0.05, 401, {"error": {"message": "no key"}}),
             Leg(0.05, 401, {"error": {"message": "no key"}})],
            hot=True,
            hedge=True,
            hedge_after_ms=20,
        )
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 401

    def test_a_transport_exception_is_reported_as_it_would_be_unhedged(self):
        class Boom(SlowTransport):
            def post(self, url, content=None, **kwargs):
                raise ConnectionError("network down")

        client = JevClient(Config(api_key="k", retries=0), client=Boom([]),
                           hedge=True, hedge_after_ms=20)
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 0

    def test_hedging_can_be_switched_on_without_hot_mode(self):
        client, _ = make_client([Leg(0.0)], hedge=True, hedge_after_ms=50)
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is False  # reported, but never fired
        assert client.config.timeout_read_s == 60.0  # still not a hot client


class TestWarmModes:
    def test_head_mode_sends_a_head_and_no_decision(self):
        client, transport = make_client([Leg(0.0)])
        assert client.warm(mode="head") >= 0.0
        assert transport.heads == 1
        assert transport.bodies == []

    def test_decision_mode_sends_one_real_minimal_decision(self):
        client, transport = make_client([Leg(0.0)])
        assert client.warm(mode="decision") >= 0.0
        assert transport.heads == 0
        assert len(transport.bodies) == 1
        body = json.loads(transport.bodies[0])
        assert list(body["questions"]) == list(WARM_QUESTIONS)

    def test_the_default_mode_comes_from_the_config(self):
        from dataclasses import replace

        client, transport = make_client([Leg(0.0)])
        client.config = replace(client.config, warm_mode="head")
        client.warm()
        assert transport.heads == 1
        assert transport.bodies == []

    def test_a_failing_warm_up_is_not_an_error(self):
        client, transport = make_client([Leg(0.0)])

        def boom(*_a, **_k):
            raise ConnectionError("no dns")

        transport.post = boom
        transport.head = boom
        assert client.warm(mode="decision") >= 0.0
        assert client.warm(mode="head") >= 0.0


class AsyncFakeResponse(FakeResponse):
    pass


class AsyncTransport:
    """Async twin of :class:`SlowTransport`."""

    def __init__(self, legs: list[Leg]):
        self._legs = list(legs)
        self.bodies: list[bytes] = []
        self.heads = 0

    async def post(self, url, content=None, **kwargs):
        self.bodies.append(content)
        leg = self._legs.pop(0) if self._legs else Leg(0.0)
        await asyncio.sleep(leg.delay_s)
        return AsyncFakeResponse(leg.status, leg.payload)

    async def head(self, url, **kwargs):
        self.heads += 1
        return AsyncFakeResponse(200, b"{}")

    async def aclose(self):
        pass


def make_async(legs, **kwargs) -> tuple[AsyncJevClient, AsyncTransport]:
    transport = AsyncTransport(legs)
    return AsyncJevClient(Config(api_key="k"), client=transport, **kwargs), transport


class TestAsyncClient:
    def test_it_parses_exactly_like_the_sync_client(self):
        async def go():
            client, transport = make_async([Leg(0.0)])
            result = await client.decide({"ticket": "x"}, QUESTIONS)
            await client.aclose()
            return result, transport

        result, transport = asyncio.run(go())
        assert result.noul("is_bug") == pytest.approx(0.9)
        assert result.input_tokens == 1000
        assert result.usage["cost_source"] == "computed"
        body = json.loads(transport.bodies[0])
        assert body["state"] == {"ticket": "x"} and "is_bug" in body["questions"]

    def test_the_request_bytes_match_the_sync_client_byte_for_byte(self):
        async def go():
            client, transport = make_async([Leg(0.0)])
            await client.decide({"a": [1, 2]}, QUESTIONS, session_id="s1")
            await client.aclose()
            return transport.bodies[0]

        async_body = asyncio.run(go())
        sync_client, sync_transport = make_client([Leg(0.0)])
        sync_client.decide({"a": [1, 2]}, QUESTIONS, session_id="s1")
        assert async_body == sync_transport.bodies[0]

    def test_hedging_cancels_the_loser_but_still_costs_it(self):
        async def go():
            client, transport = make_async([Leg(0.30), Leg(0.01)],
                                           hot=True, hedge=True, hedge_after_ms=40)
            result = await client.decide({}, QUESTIONS)
            await client.aclose()
            return result, transport

        result, transport = asyncio.run(go())
        assert result.timing_ms["hedged"] is True
        assert result.timing_ms["winner"] == "hedge"
        assert len(transport.bodies) == 2
        assert result.usage["hedge_cost_usd_est"] > 0

    def test_decide_many_runs_concurrently(self):
        async def go():
            client, _ = make_async([Leg(0.15), Leg(0.15), Leg(0.15)])
            started = time.perf_counter()
            results = await client.decide_many(
                [({"i": i}, QUESTIONS) for i in range(3)]
            )
            elapsed = time.perf_counter() - started
            await client.aclose()
            return results, elapsed

        results, elapsed = asyncio.run(go())
        assert len(results) == 3
        # Sequential would be ~0.45 s; concurrent is ~0.15 s.
        assert elapsed < 0.35

    def test_warm_decision_mode(self):
        async def go():
            client, transport = make_async([Leg(0.0)])
            await client.warm(mode="decision")
            await client.aclose()
            return transport

        transport = asyncio.run(go())
        assert list(json.loads(transport.bodies[0])["questions"]) == list(WARM_QUESTIONS)

    def test_without_httpx_it_says_so_instead_of_pretending(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.HAS_HTTPX", False)
        with pytest.raises(JevConfigError, match="httpx"):
            AsyncJevClient(Config(api_key="k"))

    @pytest.mark.skipif(not HAS_HTTPX, reason="httpx not installed")
    def test_it_builds_its_own_pool_when_httpx_is_present(self):
        async def go():
            client = AsyncJevClient(Config(api_key="k"))
            assert client._client is not None
            await client.aclose()

        asyncio.run(go())

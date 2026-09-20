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
    MAX_ABANDONED_HEDGE_LEGS,
    WARM_QUESTIONS,
    AsyncJevClient,
    JevClient,
    _apply_hot_defaults,
    _timeouts,
)
from jevskill.config import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_READ_S,
    HOT_RETRIES,
    HOT_TIMEOUT_CONNECT_S,
    HOT_TIMEOUT_READ_S,
    INPUT_PRICE_PER_MTOK,
    MIN_HEDGE_AFTER_MS,
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
        # `Event().wait` rather than `time.sleep`, because a test that skips the
        # retry backoff does it by patching `jevskill.client.time.sleep` — which
        # is the `time` module itself, so the scripted latency disappeared too
        # and a hedge that should have fired never did.
        threading.Event().wait(leg.delay_s)
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
        assert client.config.timeout_read_s == DEFAULT_TIMEOUT_READ_S
        assert client.config.retries == DEFAULT_RETRIES
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
        assert config.timeout_read_s == DEFAULT_TIMEOUT_READ_S
        assert config.retries == DEFAULT_RETRIES
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
            [Leg(0.30), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=60
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is True
        assert result.timing_ms["winner"] == "hedge"
        assert len(transport.bodies) == 2
        assert transport.bodies[0] == transport.bodies[1], "the hedge must be identical"

    def test_the_abandoned_leg_is_costed_rather_than_ignored(self):
        client, _ = make_client([Leg(0.30), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=60)
        result = client.decide({}, QUESTIONS)
        expected = FIXTURE["usage"]["input_tokens"] / 1_000_000 * INPUT_PRICE_PER_MTOK
        assert result.usage["hedge_cost_usd_est"] == pytest.approx(expected)
        assert result.usage["hedge_cost_source"] == "estimated"
        # `cost_usd` is what the ledger records, so it must be both legs.
        assert result.cost_usd == pytest.approx(result.usage["cost"] + expected)

    def test_a_primary_that_answers_first_still_wins_after_a_hedge_fired(self):
        client, transport = make_client(
            [Leg(0.10), Leg(0.50)], hot=True, hedge=True, hedge_after_ms=60
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is True
        assert result.timing_ms["winner"] == "primary"
        assert len(transport.bodies) == 2  # spent twice, used once

    def test_a_non_200_waits_for_the_twin_instead_of_failing(self):
        # The primary comes back first with a retryable 429; the hedge is still
        # running and its 200 is worth more than the error.
        client, _ = make_client(
            [Leg(0.12, 429, {"error": {"message": "slow down"}}), Leg(0.25)],
            hot=True,
            hedge=True,
            hedge_after_ms=60,
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["winner"] == "hedge"
        assert result.noul("is_bug") == pytest.approx(0.9)

    def test_two_failures_still_raise_the_api_error(self):
        client, _ = make_client(
            [Leg(0.20, 401, {"error": {"message": "no key"}}),
             Leg(0.20, 401, {"error": {"message": "no key"}})],
            hot=True,
            hedge=True,
            hedge_after_ms=60,
        )
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 401

    def test_a_transport_exception_is_reported_as_it_would_be_unhedged(self):
        class Boom(SlowTransport):
            def post(self, url, content=None, **kwargs):
                raise ConnectionError("network down")

        client = JevClient(Config(api_key="k", retries=0), client=Boom([]),
                           hedge=True, hedge_after_ms=60)
        with pytest.raises(JevApiError) as info:
            client.decide({}, QUESTIONS)
        assert info.value.status == 0

    def test_hedging_can_be_switched_on_without_hot_mode(self):
        client, _ = make_client([Leg(0.0)], hedge=True, hedge_after_ms=50)
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is False  # reported, but never fired
        assert client.config.timeout_read_s == DEFAULT_TIMEOUT_READ_S  # not hot


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
                                           hot=True, hedge=True, hedge_after_ms=60)
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


class TestStatedValuesBeatHotDefaults:
    """`hot=True` retunes *silence*, never a number the caller said out loud.

    The regression: hot mode compared each field to the dataclass default, so a
    caller who stated the documented value — `Config(timeout_read_s=60.0,
    retries=2)` — was indistinguishable from one who said nothing, and was
    silently given 1.5 s and 0 retries. The fix is that an unstated field
    arrives as `None` and is resolved in `__post_init__`, which records what it
    resolved; value equality cannot express "stated" and a sentinel can.
    """

    def test_stating_the_documented_default_is_still_stating_it(self):
        config = Config(
            api_key="k", timeout_read_s=DEFAULT_TIMEOUT_READ_S, retries=DEFAULT_RETRIES
        )
        hot = _apply_hot_defaults(config, hot=True, hedge=None, hedge_after_ms=None)
        assert hot.timeout_read_s == DEFAULT_TIMEOUT_READ_S, "a stated 60 s was retuned"
        assert hot.retries == DEFAULT_RETRIES, "stated retries were retuned"
        # The one field nobody mentioned is still hot mode's business.
        assert hot.timeout_connect_s == HOT_TIMEOUT_CONNECT_S

    def test_the_same_through_the_client_constructor(self):
        config = Config(api_key="sk-or-v1-test", timeout_read_s=DEFAULT_TIMEOUT_READ_S)
        client = JevClient(config, client=SlowTransport([Leg(0.0)]), hot=True)
        assert client.config.timeout_read_s == DEFAULT_TIMEOUT_READ_S
        assert client.config.retries == HOT_RETRIES  # unstated, so retuned

    def test_silence_is_still_retuned(self):
        hot = _apply_hot_defaults(
            Config(api_key="k"), hot=True, hedge=None, hedge_after_ms=None
        )
        assert hot.timeout_read_s == HOT_TIMEOUT_READ_S
        assert hot.retries == HOT_RETRIES

    def test_from_env_treats_an_override_as_stated(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_API_KEY", "sk-or-v1-test")
        monkeypatch.delenv("JEVSKILL_PROVIDER", raising=False)
        config = Config.from_env(timeout_read_s=DEFAULT_TIMEOUT_READ_S)
        assert config.is_defaulted("timeout_read_s") is False
        hot = _apply_hot_defaults(config, hot=True, hedge=None, hedge_after_ms=None)
        assert hot.timeout_read_s == DEFAULT_TIMEOUT_READ_S

    def test_a_replaced_config_reports_nothing_as_defaulted(self):
        """`replace` re-runs `__init__` with concrete values, so a copy cannot
        claim a field is unstated. That is the safe direction: a copy is never
        re-tuned behind whoever made it."""
        from dataclasses import replace

        copy = replace(Config(api_key="k"), warm_mode="head")
        assert copy.timeout_read_s == DEFAULT_TIMEOUT_READ_S
        assert copy.is_defaulted("timeout_read_s") is False


class TestHedgeDelayFloor:
    """`hedge_after_ms=0` duplicated *every* call. It is now a ValueError."""

    def test_zero_is_refused_by_the_config(self):
        with pytest.raises(ValueError, match="floor"):
            Config(api_key="k", hedge=True, hedge_after_ms=0)

    def test_zero_is_refused_by_the_client(self):
        with pytest.raises(ValueError, match="floor"):
            make_client([Leg(0.0)], hot=True, hedge=True, hedge_after_ms=0)

    def test_the_floor_itself_is_accepted(self):
        client, _ = make_client([Leg(0.0)], hedge=True, hedge_after_ms=MIN_HEDGE_AFTER_MS)
        assert client.config.hedge_after_ms == MIN_HEDGE_AFTER_MS

    def test_from_env_validates_its_overrides_too(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_API_KEY", "sk-or-v1-test")
        with pytest.raises(ValueError, match="floor"):
            Config.from_env(hedge_after_ms=1.0)


class TestHedgingAndRetriesDoNotMultiply:
    """Two tail strategies that compose by multiplication is a bug, not a plan.

    Measured before the fix: `retries=2, hedge=True` put **six** requests on the
    wire for one decision — two legs on each of three attempts. Hedging now
    applies to attempt 0 only, so one decision costs at most `retries + 2`
    requests instead of `2 * (retries + 1)`.
    """

    def test_a_hedged_first_attempt_plus_one_retry_is_three_requests(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client(
            [Leg(0.30, 503, b"overloaded"), Leg(0.02, 503, b"overloaded"), Leg(0.0)],
            hedge=True,
            hedge_after_ms=60,
        )
        result = client.decide({}, QUESTIONS)
        assert result.noul("is_bug") == pytest.approx(0.9)
        assert len(transport.bodies) == 3, "the retry hedged as well"
        assert client.requests_sent == 3

    def test_every_attempt_failing_is_four_requests_not_six(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        legs = [Leg(0.30, 503, b"overloaded"), Leg(0.02, 503, b"overloaded")] + [
            Leg(0.0, 503, b"overloaded") for _ in range(6)
        ]
        client, transport = make_client(legs, hedge=True, hedge_after_ms=60)
        with pytest.raises(JevApiError):
            client.decide({}, QUESTIONS)
        # 2 (hedged first attempt) + 1 + 1. It used to be 2 + 2 + 2.
        assert len(transport.bodies) == 4


class TestAbandonedLegsCannotStarveThePool:
    """An abandoned leg holds a pool connection until it answers.

    With `max_connections=8` a loop that hedges faster than the losers retire
    ends up waiting for a connection and then failing on the pool timeout — a
    failure invented by the mechanism that was meant to remove failures. Past
    `MAX_ABANDONED_HEDGE_LEGS` the client stops hedging and says so.
    """

    def test_a_saturated_client_declines_to_hedge_and_records_why(self):
        client, transport = make_client(
            [Leg(0.30), Leg(0.0)], hot=True, hedge=True, hedge_after_ms=60
        )
        for _ in range(MAX_ABANDONED_HEDGE_LEGS):
            assert client._hedge_slots.acquire(blocking=False)
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["hedged"] is False
        assert result.timing_ms["note"] == "hedge_skipped_saturated"
        assert len(transport.bodies) == 1, "it hedged with no slot free"

    def test_the_slot_comes_back_once_both_legs_have_finished(self):
        client, _ = make_client(
            [Leg(0.30), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=60
        )
        client.decide({}, QUESTIONS)
        deadline = time.time() + 5.0
        held: list[int] = []
        while time.time() < deadline:
            held = [
                i for i in range(MAX_ABANDONED_HEDGE_LEGS)
                if client._hedge_slots.acquire(blocking=False)
            ]
            if len(held) == MAX_ABANDONED_HEDGE_LEGS:
                break
            for _ in held:
                client._hedge_slots.release()
            time.sleep(0.02)
        assert len(held) == MAX_ABANDONED_HEDGE_LEGS, "the abandoned leg kept its slot"

    @pytest.mark.skipif(not HAS_HTTPX, reason="httpx not installed")
    def test_the_pool_timeout_is_stated_not_inherited(self):
        """`httpx.Timeout(1.5, connect=2.0)` silently sets pool=1.5, which is
        shorter than the read timeout of the request already holding the
        connection. Waiting for a connection is cheaper than failing."""
        timeout = _timeouts(HOT_TIMEOUT_READ_S, HOT_TIMEOUT_CONNECT_S)
        assert timeout.pool == pytest.approx(
            max(HOT_TIMEOUT_READ_S, HOT_TIMEOUT_CONNECT_S) + 1.0
        )
        assert timeout.pool > timeout.read


class TestRequestsAreCountedWhenSent:
    """`requests_sent` is what the provider bills for, so it counts departures.

    Counting on return undercounted exactly the interesting case: a hedge whose
    loser never answers was never counted, so a client that put two bodies on
    the wire reported one.
    """

    def test_a_leg_that_never_returns_is_still_counted(self):
        client, transport = make_client(
            [Leg(5.0), Leg(0.02)], hot=True, hedge=True, hedge_after_ms=60
        )
        result = client.decide({}, QUESTIONS)
        assert result.timing_ms["winner"] == "hedge"
        assert len(transport.bodies) == 2
        assert client.requests_sent == 2, "the abandoned leg was not counted"

    def test_the_plain_path_still_counts_one_per_call(self):
        client, _ = make_client([Leg(0.0), Leg(0.0)])
        client.decide({}, QUESTIONS)
        client.decide({}, QUESTIONS)
        assert client.requests_sent == 2


class TestConfigCopiesAreIndependent:
    def test_a_hot_client_does_not_share_the_callers_extra_dict(self):
        config = Config(api_key="sk-or-v1-test")
        config.extra["key_name"] = "JEV_API_KEY"
        client = JevClient(config, client=SlowTransport([Leg(0.0)]), hot=True)
        client.config.extra["marker"] = "written by the client"
        assert "marker" not in config.extra, "the two configs share one dict"
        assert client.config.extra["key_name"] == "JEV_API_KEY"


class TestWarmUpIsOneAttempt:
    def test_a_failing_warm_up_decision_does_not_retry(self, monkeypatch):
        """A warm-up inherited `retries`, so warming against a dead endpoint
        spent three attempts and a backoff sleep (751 ms measured) before
        returning the shrug its docstring promises."""
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        client, transport = make_client([Leg(0.0, 503, b"down")] * 5)
        client.warm(mode="decision")
        assert len(transport.bodies) == 1, "the warm-up retried"
        assert client.config.retries == DEFAULT_RETRIES, "retries stayed pinned"
        assert client.last_warm_decision is None

    def test_a_successful_warm_up_decision_is_kept_for_its_cost(self):
        client, _ = make_client([Leg(0.0)])
        client.warm(mode="decision")
        assert client.last_warm_decision is not None
        assert client.last_warm_decision.input_tokens == 1000

    def test_head_mode_makes_no_decision_at_all(self):
        client, transport = make_client([Leg(0.0)])
        client.warm(mode="head")
        assert transport.heads == 1 and transport.bodies == []
        assert client.last_warm_decision is None


class TestAsyncFailureContract:
    """`decide_many` must not orphan the tasks it started.

    A plain `gather` returns the moment one task raises, leaving the other N-1
    running against a client the caller is about to close: their answers are
    discarded, their cost is not, and closing the client underneath them turns
    one error into several.
    """

    def test_one_failure_waits_for_the_others_then_raises(self):
        async def go():
            client, transport = make_async(
                [Leg(0.01, 401, {"error": {"message": "no key"}}),
                 Leg(0.15), Leg(0.15)]
            )
            try:
                with pytest.raises(JevApiError) as info:
                    await client.decide_many([({"i": i}, QUESTIONS) for i in range(3)])
                assert info.value.status == 401
                # Every leg finished before the raise: nothing is still in
                # flight when the caller closes the client.
                assert len(transport.bodies) == 3
                assert client.requests_sent == 3
            finally:
                await client.aclose()

        asyncio.run(go())

    def test_the_hedge_budget_starts_where_the_sync_one_does(self):
        """The async budget used to start *after* the `hedge_after_ms` wait, so
        two clients with the same config gave up `hedge_after_ms` apart."""
        async def go():
            transport = AsyncTransport([Leg(5.0), Leg(5.0)])
            client = AsyncJevClient(
                Config(api_key="k", timeout_read_s=0.2, timeout_connect_s=0.1,
                       retries=0),
                client=transport,
                hedge=True,
                hedge_after_ms=300,
            )
            started = time.perf_counter()
            with pytest.raises(JevApiError):
                await client.decide({}, QUESTIONS)
            elapsed = time.perf_counter() - started
            await client.aclose()
            return elapsed

        elapsed = asyncio.run(go())
        # budget = read (0.2) + connect (0.1) + 0.5 = 0.8 s from the *start*.
        # With the clock started after the 300 ms wait it was ~1.1 s.
        assert 0.6 < elapsed < 1.0, f"gave up after {elapsed:.2f}s"

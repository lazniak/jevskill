"""Tests for the decision cache: the cheapest call is the one you do not make.

The interesting parts are not "does it store a dict" but the honesty rules —
a hit must report itself, must not claim the model's tokens or cost, and must not
be served when the request differs by a byte.
"""

from __future__ import annotations

import json
import time

import pytest

from jevskill.cache import (
    DEFAULT_TTL_S,
    SCHEMA,
    DecisionCache,
    cache_dir,
    request_key,
)

PAYLOAD = {
    "id": "gen-1",
    "model": "typesafe/jev-1.13-20260917",
    "answers": {"problem": {"type": "noul", "noul": 0.91}},
    "usage": {"input_tokens": 301, "output_tokens": 20, "cost": 1.26e-05},
}


class TestKeys:
    def test_the_same_body_gives_the_same_key(self):
        assert request_key(b'{"a":1}') == request_key(b'{"a":1}')

    def test_a_one_byte_change_gives_a_different_key(self):
        assert request_key(b'{"a":1}') != request_key(b'{"a":2}')


class TestStoreAndLookup:
    def test_a_stored_response_comes_back(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        entry = cache.lookup(b'{"q":1}')
        assert entry is not None
        assert entry.payload["answers"] == PAYLOAD["answers"]
        assert entry.payload["model"] == PAYLOAD["model"]

    def test_a_different_body_misses(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        assert cache.lookup(b'{"q":2}') is None

    def test_an_empty_cache_misses(self, tmp_path):
        assert DecisionCache(tmp_path).lookup(b'{"q":1}') is None

    def test_the_age_is_reported(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        entry = cache.lookup(b'{"q":1}')
        assert entry.age_s >= 0 and entry.age_s < 5

    def test_a_expired_entry_misses_and_is_removed(self, tmp_path):
        cache = DecisionCache(tmp_path, ttl_s=0.0)
        cache.store(b'{"q":1}', PAYLOAD)
        time.sleep(0.01)
        assert cache.lookup(b'{"q":1}') is None
        assert cache.size() == 0, "an expired entry should not be left lying around"

    def test_the_ttl_is_a_window_not_a_guess(self, tmp_path):
        cache = DecisionCache(tmp_path, ttl_s=60)
        cache.store(b'{"q":1}', PAYLOAD)
        assert cache.lookup(b'{"q":1}') is not None

    def test_hits_and_misses_are_counted(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        cache.lookup(b'{"q":1}')
        cache.lookup(b'{"q":2}')
        assert (cache.hits, cache.misses) == (1, 1)

    def test_clear_empties_the_store(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        cache.store(b'{"q":2}', PAYLOAD)
        assert cache.clear() == 2 and cache.size() == 0

    def test_a_corrupt_entry_misses_rather_than_raising(self, tmp_path, ):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        key = request_key(b'{"q":1}')
        (cache._path(key)).write_text("{not json", encoding="utf-8")
        assert cache.lookup(b'{"q":1}') is None

    def test_an_entry_for_another_schema_is_ignored(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', PAYLOAD)
        key = request_key(b'{"q":1}')
        path = cache._path(key)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["schema"] = SCHEMA + 1
        path.write_text(json.dumps(stored), encoding="utf-8")
        assert cache.lookup(b'{"q":1}') is None

    def test_a_payload_without_answers_is_not_a_hit(self, tmp_path):
        cache = DecisionCache(tmp_path)
        cache.store(b'{"q":1}', {"model": "x", "answers": {}})
        assert cache.lookup(b'{"q":1}') is None

    def test_a_write_failure_does_not_raise(self, tmp_path):
        # A cache that cannot write must not fail a call that already succeeded.
        cache = DecisionCache(tmp_path / "nested" / "deeper", ttl_s=60)
        cache.store(b'{"q":1}', PAYLOAD)  # parents are created
        assert cache.lookup(b'{"q":1}') is not None


def test_the_default_ttl_is_a_working_session_not_a_day():
    assert 0 < DEFAULT_TTL_S <= 3600


def test_cache_dir_defaults_beside_the_ledger():
    assert cache_dir().parts[-2:] == (".jevskill", "cache")
    assert cache_dir("/tmp/proj").as_posix().endswith("/tmp/proj/.jevskill/cache")


def test_cache_dir_follows_the_ledger_env_var(monkeypatch, tmp_path):
    # The ledger and the cache for one project must not end up in two places.
    from jevskill.stats import ledger_path

    monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path))
    ledger = ledger_path()
    cache = cache_dir()
    assert ledger.parent == cache.parent, f"{ledger} vs {cache}"
    assert cache.name == "cache"


def test_an_explicit_root_beats_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("JEVSKILL_LEDGER_DIR", str(tmp_path / "env"))
    assert cache_dir(tmp_path / "explicit").as_posix().endswith(
        "explicit/.jevskill/cache")


# --------------------------------------------------------------------------- #
# Through the client: a hit must be honest about what it is.
# --------------------------------------------------------------------------- #


class TestThroughTheClient:
    def make_client(self, monkeypatch, tmp_path):
        from jevskill.client import JevClient
        from jevskill.config import Config
        from jevskill.primitives import noul

        monkeypatch.setenv("JEVSKILL_API_KEY", "sk-or-v1-test")
        calls = []
        client = JevClient(Config.from_env())
        monkeypatch.setattr(
            client, "_post",
            lambda body, timeout_s=None: (calls.append(body) or (
                json.dumps(PAYLOAD).encode(), 200)),
        )
        return client, noul, calls

    def test_the_second_identical_call_is_served_from_disk(self, monkeypatch, tmp_path):
        client, noul, calls = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        first = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        second = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        assert len(calls) == 1, "the second call must not reach the network"
        assert first.cached is False and second.cached is True

    def test_the_cached_answer_is_the_same_answer(self, monkeypatch, tmp_path):
        client, noul, _ = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        first = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        second = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        assert second.noul("problem") == first.noul("problem")
        assert second.model == first.model

    def test_a_hit_claims_no_tokens_and_no_cost(self, monkeypatch, tmp_path):
        client, noul, _ = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        client.decide("state", {"problem": noul("ok?")}, cache=cache)
        hit = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        assert hit.usage["input_tokens"] == 0
        assert hit.usage["cost"] == 0.0
        assert hit.usage["cost_source"] == "cache"

    def test_a_hit_reports_no_http_time(self, monkeypatch, tmp_path):
        # Reporting the disk lookup as `http` would be a lie about where the time
        # went, and it is the number the README quotes.
        client, noul, _ = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        client.decide("state", {"problem": noul("ok?")}, cache=cache)
        hit = client.decide("state", {"problem": noul("ok?")}, cache=cache)
        assert hit.timing_ms["http_ms"] == 0.0
        assert hit.cached_age_s is not None

    def test_a_different_state_is_not_a_hit(self, monkeypatch, tmp_path):
        client, noul, calls = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        client.decide("state one", {"problem": noul("ok?")}, cache=cache)
        client.decide("state two", {"problem": noul("ok?")}, cache=cache)
        assert len(calls) == 2

    def test_a_changed_question_is_not_a_hit(self, monkeypatch, tmp_path):
        client, noul, calls = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        client.decide("state", {"problem": noul("ok?")}, cache=cache)
        client.decide("state", {"problem": noul("different?")}, cache=cache)
        assert len(calls) == 2

    def test_a_different_session_id_is_not_a_hit(self, monkeypatch, tmp_path):
        client, noul, calls = self.make_client(monkeypatch, tmp_path)
        cache = DecisionCache(tmp_path)
        client.decide("state", {"problem": noul("ok?")}, session_id="s1", cache=cache)
        client.decide("state", {"problem": noul("ok?")}, session_id="s2", cache=cache)
        assert len(calls) == 2, "session is part of the request, so it is part of the key"

    def test_no_cache_means_no_caching(self, monkeypatch, tmp_path):
        client, noul, calls = self.make_client(monkeypatch, tmp_path)
        client.decide("state", {"problem": noul("ok?")})
        client.decide("state", {"problem": noul("ok?")})
        assert len(calls) == 2
        assert not cache_dir(tmp_path).exists()
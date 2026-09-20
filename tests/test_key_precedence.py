"""Provider intent first, then the matching key — and vendor names before the
aggregator's when nothing was stated.

Two live failures on 2026-09-20 motivated this file, both on a machine that held an
OpenRouter key *and* a freshly added vendor key (``JEV_API_KEY``, set with ``setx``,
so present in the registry but not in already-open shells):

* ``JEVSKILL_PROVIDER=typesafe`` sent the **OpenRouter** key to the vendor endpoint
  (HTTP 401), because the key was resolved provider-agnostically before the
  provider was decided;
* with nothing stated, the aggregator key kept winning because every name was
  searched in the environment before any name in the registry.

A third live finding is pinned here too: the vendor rejects a body that carries
``session_id`` (HTTP 400 ``api_usage_error``), so the client must omit it there.
"""

from __future__ import annotations

import json

import pytest

from jevskill.client import JevClient
from jevskill.config import (
    ALL_KEY_ENV_VARS,
    PROVIDERS,
    Config,
    find_api_key_source,
    resolve_provider_intent,
)

VENDOR_KEY = "apikey_vendor_0123456789abcdef"
OR_KEY = "sk-or-v1-aggregator0123456789"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_KEY_ENV_VARS + ("JEVSKILL_PROVIDER", "JEVSKILL_MODEL",
                                    "JEVSKILL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})


def registry(values: dict):
    """A fake ``HKCU\\Environment`` holding exactly ``values``."""
    return lambda name: values.get(name, "")


class TestVendorNamesComeFirst:
    def test_order_is_jevskill_then_vendor_then_aggregator(self):
        assert ALL_KEY_ENV_VARS[0] == "JEVSKILL_API_KEY"
        vendor = PROVIDERS["typesafe"]["key_env"]
        aggregator = PROVIDERS["openrouter"]["key_env"]
        assert ALL_KEY_ENV_VARS[1:1 + len(vendor)] == vendor
        assert ALL_KEY_ENV_VARS[1 + len(vendor):] == aggregator

    def test_jev_api_key_is_a_vendor_variable(self):
        assert "JEV_API_KEY" in PROVIDERS["typesafe"]["key_env"]
        assert "JEV_API_KEY" not in PROVIDERS["openrouter"]["key_env"]


class TestNothingStated:
    def test_vendor_key_in_env_beats_aggregator_key_in_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        cfg = Config.from_env()
        assert cfg.provider == "typesafe"
        assert cfg.api_key == VENDOR_KEY
        assert cfg.extra["key_name"] == "JEV_API_KEY"
        assert cfg.extra["key_source"] == "env"

    def test_vendor_key_in_registry_beats_aggregator_key_in_env(self, monkeypatch):
        """The exact live situation: ``setx JEV_API_KEY`` after the shell opened."""
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", OR_KEY)
        monkeypatch.setattr("jevskill.config._registry_env",
                            registry({"JEV_API_KEY": VENDOR_KEY}))
        cfg = Config.from_env()
        assert cfg.provider == "typesafe"
        assert cfg.api_key == VENDOR_KEY
        assert cfg.extra["key_source"] == "registry"

    def test_aggregator_key_alone_still_selects_openrouter(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        cfg = Config.from_env()
        assert cfg.provider == "openrouter"
        assert cfg.api_key == OR_KEY

    def test_generic_key_is_classified_by_shape(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_API_KEY", VENDOR_KEY)
        assert Config.from_env().provider == "typesafe"
        monkeypatch.setenv("JEVSKILL_API_KEY", OR_KEY)
        assert Config.from_env().provider == "openrouter"

    def test_env_beats_registry_for_the_same_name(self, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        monkeypatch.setattr("jevskill.config._registry_env",
                            registry({"JEV_API_KEY": "stale-registry-value"}))
        assert Config.from_env().api_key == VENDOR_KEY

    def test_file_key_is_the_last_resort(self, monkeypatch):
        monkeypatch.setattr("jevskill.config._file_config",
                            lambda: {"api_key": VENDOR_KEY})
        name, source, key = find_api_key_source(None)
        assert (name, source, key) == ("api_key", "file", VENDOR_KEY)


class TestStatedProviderScopesTheKey:
    def test_env_provider_uses_only_that_providers_key(self, monkeypatch):
        """The live 401: intent said vendor, the key sent was OpenRouter's."""
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        cfg = Config.from_env()
        assert cfg.provider == "typesafe"
        assert cfg.api_key == VENDOR_KEY

    def test_env_provider_with_only_the_other_key_finds_nothing(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        cfg = Config.from_env()
        assert cfg.provider == "typesafe"
        assert cfg.api_key == "", "must not fall back to the wrong provider's key"

    def test_explicit_argument_beats_env_provider(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        cfg = Config.from_env(provider="openrouter")
        assert (cfg.provider, cfg.api_key) == ("openrouter", OR_KEY)

    def test_config_file_provider_counts_as_stated(self, monkeypatch):
        monkeypatch.setattr("jevskill.config._file_config", lambda: {"provider": "openrouter"})
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        cfg = Config.from_env()
        assert (cfg.provider, cfg.api_key) == ("openrouter", OR_KEY)

    def test_intent_is_none_when_nothing_stated(self):
        assert resolve_provider_intent(None) is None

    def test_generic_key_serves_any_stated_provider(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("JEVSKILL_API_KEY", VENDOR_KEY)
        assert Config.from_env().api_key == VENDOR_KEY


class TestKeyIsNeverEchoed:
    def test_fingerprint_is_short_hex_and_not_a_prefix(self, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        cfg = Config.from_env()
        fp = cfg.key_fingerprint()
        assert len(fp) == 8 and int(fp, 16) >= 0
        assert not VENDOR_KEY.startswith(fp)
        assert fp not in VENDOR_KEY

    def test_no_key_no_fingerprint(self):
        assert Config.from_env().key_fingerprint() == ""


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self.content = body


class _FakeHttp:
    def __init__(self):
        self.bodies: list[bytes] = []

    def post(self, url, content=None, **kwargs):
        self.bodies.append(content)
        return _FakeResponse(200, json.dumps({
            "model": "jev-1.13.0",
            "answers": {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }).encode())

    def head(self, url, **kwargs):
        return _FakeResponse(200, b"{}")

    def close(self):
        pass


QUESTION = {"q": {"type": "noul", "instructions": "Is `x` set?"}}


class TestSessionIdPerProvider:
    def test_vendor_body_omits_session_id(self, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        monkeypatch.setattr("jevskill.client.HAS_HTTPX", True)
        http = _FakeHttp()
        with JevClient(Config.from_env(provider="typesafe"), client=http) as jev:
            result = jev.decide({"x": 1}, QUESTION, session_id="s-1")
        assert b"session_id" not in http.bodies[0]
        assert result.session_id == "s-1", "the ledger still groups by it locally"

    def test_openrouter_body_keeps_session_id(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setattr("jevskill.client.HAS_HTTPX", True)
        http = _FakeHttp()
        with JevClient(Config.from_env(provider="openrouter"), client=http) as jev:
            jev.decide({"x": 1}, QUESTION, session_id="s-1")
        assert b'"session_id":"s-1"' in http.bodies[0]

    def test_spec_documents_the_difference(self):
        assert PROVIDERS["typesafe"]["accepts_session_id"] is False
        assert PROVIDERS["openrouter"]["accepts_session_id"] is True


class TestZeroInstallScriptAgrees:
    """The Skill must behave identically with nothing installed."""

    @pytest.fixture(autouse=True)
    def no_registry_in_script(self, monkeypatch, jev_query):
        monkeypatch.setattr(jev_query, "_registry_env", lambda _n: "")

    def test_vendor_name_beats_aggregator(self, monkeypatch, jev_query):
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        assert jev_query.find_api_key(None) == ("typesafe", VENDOR_KEY)

    def test_env_provider_is_honoured(self, monkeypatch, jev_query):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        monkeypatch.setenv("JEV_API_KEY", VENDOR_KEY)
        assert jev_query.find_api_key(None) == ("typesafe", VENDOR_KEY)

    def test_env_provider_does_not_borrow_the_other_key(self, monkeypatch, jev_query):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        monkeypatch.setenv("OPENROUTER_API_KEY", OR_KEY)
        assert jev_query.find_api_key(None) == ("typesafe", "")

    def test_registry_vendor_key_beats_env_aggregator_key(self, monkeypatch, jev_query):
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", OR_KEY)
        monkeypatch.setattr(jev_query, "_registry_env", registry({"JEV_API_KEY": VENDOR_KEY}))
        assert jev_query.find_api_key(None) == ("typesafe", VENDOR_KEY)

    def test_key_env_order_matches_the_package(self, jev_query):
        assert jev_query.PROVIDERS["typesafe"]["key_env"] == PROVIDERS["typesafe"]["key_env"]
        assert jev_query.PROVIDERS["openrouter"]["key_env"] == PROVIDERS["openrouter"]["key_env"]

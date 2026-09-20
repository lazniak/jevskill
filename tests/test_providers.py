"""Tests for two-provider support.

OpenRouter and TypeSafe serve the same model through different endpoints, and
they differ in more than a URL. These tests pin every delta, because a swap that
half-works is worse than one that fails: a missing ``cost`` field would silently
record every vendor decision as free and inflate the reported savings.
"""

from __future__ import annotations

import json

import pytest

from jevskill.client import JevClient
from jevskill.config import (
    ALL_KEY_ENV_VARS,
    DEFAULT_PROVIDER,
    INPUT_PRICE_PER_MTOK,
    PROVIDERS,
    Config,
    detect_provider,
    find_api_key,
    looks_like_openrouter,
    model_for_provider,
    resolve_key_vars,
    resolve_provider,
)
from jevskill.primitives import noul

QUESTIONS = {"is_bug": noul("Is it a bug?", "yes", "no")}

#: A TypeSafe response: no ``id``, no ``provider``, and critically no ``cost``.
TYPESAFE_FIXTURE = {
    "model": "jev-1.13.0",
    "answers": {
        "is_bug": {"type": "noul", "noul": 0.95},
        "dept": {"type": "choice", "choice": "billing",
                 "probabilities": {"billing": 0.88, "technical": 0.12},
                 "confidence": 0.81},
    },
    "usage": {"input_tokens": 1000, "output_tokens": 20},
}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.text = self.content.decode()

    @property
    def status(self):
        return self.status_code


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []
        self.urls = []

    def post(self, url, content=None, **kwargs):
        self.urls.append(url)
        self.bodies.append(content)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def head(self, url, **kwargs):
        self.urls.append(url)
        return FakeResponse(200, b"{}")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No ambient keys, so provider detection is decided by the test alone."""
    for name in ALL_KEY_ENV_VARS + ("JEVSKILL_PROVIDER", "JEVSKILL_MODEL",
                                    "JEVSKILL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("jevskill.config._registry_env", lambda _n: "")
    monkeypatch.setattr("jevskill.config._file_config", lambda: {})


class TestProviderSpecs:
    def test_both_providers_are_defined(self):
        assert set(PROVIDERS) == {"openrouter", "typesafe"}

    def test_endpoints_match_the_documented_apis(self):
        assert PROVIDERS["openrouter"]["endpoint"] == "/api/alpha/decisions"
        assert PROVIDERS["typesafe"]["endpoint"] == "/v1/systemone"
        assert PROVIDERS["typesafe"]["base_url"] == "https://api.typesafe.ai"

    def test_model_names_differ(self):
        assert PROVIDERS["openrouter"]["model"] == "typesafe/jev-1.13"
        assert PROVIDERS["typesafe"]["model"] == "jev-latest"

    def test_documented_context_limits(self):
        assert PROVIDERS["openrouter"]["context_tokens"] == 32_000
        assert PROVIDERS["typesafe"]["context_tokens"] == 64_000
        assert PROVIDERS["typesafe"]["state_plus_question_tokens"] == 32_000

    def test_only_openrouter_reports_cost(self):
        assert PROVIDERS["openrouter"]["reports_cost"] is True
        assert PROVIDERS["typesafe"]["reports_cost"] is False

    def test_price_is_identical_on_both(self):
        # Worth pinning: going direct to the vendor is not a cost saving.
        assert INPUT_PRICE_PER_MTOK == 0.042


class TestKeyDetection:
    @pytest.mark.parametrize("key", ["sk-or-v1-abc", "sk-or-v2-xyz"])
    def test_openrouter_keys_are_recognised(self, key):
        assert looks_like_openrouter(key) is True
        assert detect_provider(key) == "openrouter"

    @pytest.mark.parametrize("key", ["ts_live_abc", "some-other-key"])
    def test_other_keys_are_assumed_to_be_the_vendor(self, key):
        assert looks_like_openrouter(key) is False
        assert detect_provider(key) == "typesafe"

    def test_empty_key_falls_back_to_the_default(self):
        assert detect_provider("") == DEFAULT_PROVIDER
        assert DEFAULT_PROVIDER == "openrouter"


class TestResolveProvider:
    def test_explicit_wins(self):
        assert resolve_provider("typesafe", "sk-or-v1-abc") == "typesafe"
        assert resolve_provider("openrouter", "ts_abc") == "openrouter"

    def test_env_var_is_used(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        assert resolve_provider(None, "sk-or-v1-abc") == "typesafe"

    def test_config_file_is_used(self, monkeypatch):
        monkeypatch.setattr("jevskill.config._file_config",
                            lambda: {"provider": "typesafe"})
        assert resolve_provider(None, "sk-or-v1-abc") == "typesafe"
        monkeypatch.undo()

    def test_key_shape_is_the_last_resort(self):
        assert resolve_provider(None, "sk-or-v1-abc") == "openrouter"
        assert resolve_provider(None, "ts_abc") == "typesafe"

    def test_unknown_explicit_provider_is_rejected_loudly(self):
        with pytest.raises(ValueError, match="unknown provider"):
            resolve_provider("gemini")

    def test_unknown_env_provider_is_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_PROVIDER", "gemini")
        with pytest.raises(ValueError, match="not a known provider"):
            resolve_provider(None, "sk-or-v1-abc")

    def test_env_var_beats_the_key_shape(self, monkeypatch):
        # A deliberate override must win even when the key looks like the other
        # provider's — that is the point of an override.
        monkeypatch.setenv("JEVSKILL_PROVIDER", "typesafe")
        assert resolve_provider(None, "sk-or-v1-abc") == "typesafe"


class TestKeyResolution:
    def test_openrouter_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-open")
        assert find_api_key("openrouter") == "sk-or-v1-open"

    def test_vendor_env_var(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts_live_abc")
        assert find_api_key("typesafe") == "ts_live_abc"

    def test_provider_scoping_prevents_cross_pickup(self, monkeypatch):
        """Asking for one provider must not return the other's key.

        A machine holding both keys is the normal case, and silently using the
        wrong one sends traffic to the wrong endpoint.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-open")
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts_live_abc")
        assert find_api_key("openrouter") == "sk-or-v1-open"
        assert find_api_key("typesafe") == "ts_live_abc"

    def test_jevskill_key_is_provider_agnostic(self, monkeypatch):
        monkeypatch.setenv("JEVSKILL_API_KEY", "sk-or-v1-generic")
        assert find_api_key("openrouter") == "sk-or-v1-generic"
        assert find_api_key("typesafe") == "sk-or-v1-generic"

    def test_no_key_returns_empty(self):
        assert find_api_key("openrouter") == ""

    def test_key_vars_per_provider(self):
        assert "OPENROUTER_API_KEY" in resolve_key_vars("openrouter")
        assert "TYPESAFE_API_KEY" not in resolve_key_vars("openrouter")
        assert "TYPESAFE_API_KEY" in resolve_key_vars("typesafe")
        assert set(resolve_key_vars(None)) == set(ALL_KEY_ENV_VARS)


class TestModelTranslation:
    def test_strips_the_openrouter_namespace_for_the_vendor(self):
        assert model_for_provider("typesafe/jev-1.13", "typesafe") == "jev-1.13"

    def test_leaves_a_bare_vendor_name_alone(self):
        assert model_for_provider("jev-latest", "typesafe") == "jev-latest"

    def test_adds_the_namespace_for_openrouter(self):
        assert model_for_provider("jev-latest", "openrouter") == "typesafe/jev-latest"

    def test_keeps_an_existing_namespace(self):
        assert model_for_provider("typesafe/jev-1.13", "openrouter") == "typesafe/jev-1.13"

    def test_empty_uses_the_provider_default(self):
        assert model_for_provider("", "typesafe") == "jev-latest"
        assert model_for_provider("", "openrouter") == "typesafe/jev-1.13"

    def test_whitespace_is_tolerated(self):
        assert model_for_provider("  typesafe/jev-1.13  ", "typesafe") == "jev-1.13"


class TestConfigUrls:
    def test_vendor_urls(self):
        cfg = Config(api_key="ts_x", provider="typesafe", base_url="https://api.typesafe.ai")
        assert cfg.decisions_url == "https://api.typesafe.ai/v1/systemone"
        assert cfg.warm_url == "https://api.typesafe.ai/v1/models"

    def test_openrouter_urls(self):
        cfg = Config.from_env(provider="openrouter")
        assert cfg.decisions_url == "https://openrouter.ai/api/alpha/decisions"
        assert cfg.warm_url == "https://openrouter.ai/api/v1/models"

    def test_from_env_selects_the_provider_from_the_key(self, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts_live_abc")
        cfg = Config.from_env()
        assert cfg.provider == "typesafe"
        assert cfg.model == "jev-latest"

    def test_from_env_selects_openrouter_from_the_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-open")
        cfg = Config.from_env()
        assert cfg.provider == "openrouter"

    def test_explicit_provider_overrides_detection(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-open")
        cfg = Config.from_env(provider="typesafe")
        assert cfg.provider == "typesafe"
        assert cfg.model == "jev-latest"

    def test_custom_base_url_still_wins(self, monkeypatch):
        cfg = Config.from_env(provider="typesafe", base_url="http://localhost:9")
        assert cfg.decisions_url == "http://localhost:9/v1/systemone"


class TestCostHandling:
    def test_computed_cost_uses_the_documented_rate(self):
        cfg = Config(api_key="ts_x", provider="typesafe")
        assert cfg.cost_for_tokens(1_000_000) == pytest.approx(0.042)
        assert cfg.cost_for_tokens(1_000) == pytest.approx(0.000042)

    def test_zero_tokens_cost_nothing(self):
        assert Config(api_key="ts_x", provider="typesafe").cost_for_tokens(0) == 0.0

    def test_provider_without_cost_reports_false(self):
        assert Config(api_key="ts_x", provider="typesafe").reports_cost is False
        assert Config(api_key="sk-or-x", provider="openrouter").reports_cost is True


class TestClientNormalisation:
    def _client(self, payload, provider="typesafe"):
        transport = FakeTransport([FakeResponse(200, payload)])
        cfg = Config(api_key="ts_x", provider=provider,
                     base_url=PROVIDERS[provider]["base_url"],
                     model=PROVIDERS[provider]["model"])
        return JevClient(cfg, client=transport), transport

    def test_vendor_response_gets_a_computed_cost(self):
        """The vendor returns no cost; recording zero would inflate savings."""
        client, _ = self._client(TYPESAFE_FIXTURE)
        result = client.decide({}, QUESTIONS)
        assert result.input_tokens == 1000
        assert result.cost_usd == pytest.approx(0.000042)
        assert result.usage["cost_source"] == "computed"

    def test_openrouter_cost_is_trusted_when_present(self):
        payload = dict(TYPESAFE_FIXTURE)
        payload["usage"] = {"input_tokens": 1000, "output_tokens": 20, "cost": 0.000031}
        payload["id"] = "gen-1"
        payload["provider"] = "TypeSafe"
        client, _ = self._client(payload, provider="openrouter")
        result = client.decide({}, QUESTIONS)
        assert result.cost_usd == pytest.approx(0.000031)
        assert result.usage["cost_source"] == "provider"

    def test_vendor_answers_parse_identically(self):
        client, _ = self._client(TYPESAFE_FIXTURE)
        result = client.decide({}, QUESTIONS)
        assert result.noul("is_bug") == pytest.approx(0.95)
        assert result.choice("dept") == "billing"
        assert result.probs("dept")["technical"] == pytest.approx(0.12)

    def test_missing_request_id_is_empty_not_an_error(self):
        client, _ = self._client(TYPESAFE_FIXTURE)
        assert client.decide({}, QUESTIONS).request_id == ""

    def test_provider_is_recorded_on_the_result(self):
        client, _ = self._client(TYPESAFE_FIXTURE)
        assert client.decide({}, QUESTIONS).provider == "typesafe"

    def test_request_goes_to_the_vendor_endpoint(self):
        client, transport = self._client(TYPESAFE_FIXTURE)
        client.decide({}, QUESTIONS)
        assert transport.urls[0] == "https://api.typesafe.ai/v1/systemone"

    def test_request_goes_to_the_openrouter_endpoint(self):
        payload = dict(TYPESAFE_FIXTURE)
        payload["id"] = "gen-1"
        client, transport = self._client(payload, provider="openrouter")
        client.decide({}, QUESTIONS)
        assert transport.urls[0] == "https://openrouter.ai/api/alpha/decisions"

    def test_body_uses_the_provider_model_name(self):
        client, transport = self._client(TYPESAFE_FIXTURE)
        client.decide({}, QUESTIONS)
        assert json.loads(transport.bodies[0])["model"] == "jev-latest"

    def test_state_and_questions_shape_is_shared(self):
        """Both endpoints take the same body, so only the model/url differ."""
        client, transport = self._client(TYPESAFE_FIXTURE)
        client.decide({"ticket": "broken"}, QUESTIONS)
        body = json.loads(transport.bodies[0])
        assert body["state"] == {"ticket": "broken"}
        assert body["questions"]["is_bug"]["type"] == "noul"

    def test_size_error_names_the_provider_ceiling(self):
        from jevskill.errors import JevApiError

        transport = FakeTransport([FakeResponse(413, {"detail": "too big"})])
        cfg = Config(api_key="ts_x", provider="typesafe")
        with pytest.raises(JevApiError) as info:
            JevClient(cfg, client=transport).decide({}, QUESTIONS)
        assert "64,000" in info.value.hint
        assert "32,000" in info.value.hint  # state + longest question

    def test_422_validation_error_has_a_hint(self):
        from jevskill.errors import JevApiError

        transport = FakeTransport([FakeResponse(422, {"detail": "bad field"})])
        cfg = Config(api_key="ts_x", provider="typesafe")
        with pytest.raises(JevApiError) as info:
            JevClient(cfg, client=transport).decide({}, QUESTIONS)
        assert info.value.status == 422
        assert "validation" in info.value.hint.lower()
        assert info.value.retryable is False

    def test_529_is_retryable_on_both_providers(self, monkeypatch):
        monkeypatch.setattr("jevskill.client.time.sleep", lambda _s: None)
        for provider in ("openrouter", "typesafe"):
            transport = FakeTransport([FakeResponse(529, {"detail": "overloaded"}),
                                       FakeResponse(200, TYPESAFE_FIXTURE)])
            cfg = Config(api_key="k", provider=provider)
            result = JevClient(cfg, client=transport).decide({}, QUESTIONS)
            assert result.attempts == 2, provider

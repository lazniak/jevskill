"""Configuration resolution: environment -> Windows registry -> config file.

A harness runs in whatever shell a human happened to open. Environment variables
set via ``setx`` after that shell started are *not* inherited, which is the
single most common reason an API key "disappears". We therefore read the Windows
user registry directly as a fallback.

Two providers serve the same model, and the skill supports both:

``openrouter``  ``POST https://openrouter.ai/api/alpha/decisions``, model id
                ``typesafe/jev-1.13``. Uses a key most developers already have.
                Reports a ``cost`` field and a provider-generated request ``id``.
``typesafe``    ``POST https://api.typesafe.ai/v1/systemone``, model id
                ``jev-latest``. The vendor's own endpoint. Reports token counts
                but **no cost** (we compute it from the documented rate), and no
                request id. Accepts up to 255 Choice options and 10 Score levels.

They differ in more than a URL, so a naive swap breaks in ways that are easy to
miss. The deltas are concentrated in :data:`PROVIDERS` and normalised by
:class:`Config`, so the client never has to branch on the vendor.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: Input price per 1M tokens. Identical on both providers ($0.042 / Mtok),
#: which is worth knowing: going direct to the vendor is not a cost saving, it
#: is a dependency and features question.
INPUT_PRICE_PER_MTOK = 0.042

PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "base_url": "https://openrouter.ai",
        "endpoint": "/api/alpha/decisions",
        "models_path": "/api/v1/models",
        "model": "typesafe/jev-1.13",
        "key_env": ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY", "JEVUSE_API_KEY"),
        # OpenRouter returns usage.cost; TypeSafe does not.
        "reports_cost": True,
        "reports_request_id": True,
        # OpenRouter accepts the optional observability field.
        "accepts_session_id": True,
        # Documented as 32K on the model page.
        "context_tokens": 32_000,
    },
    "typesafe": {
        "base_url": "https://api.typesafe.ai",
        "endpoint": "/v1/systemone",
        "models_path": "/v1/models",
        "model": "jev-latest",
        # Either name is a vendor key. ``JEV_API_KEY`` first because it names the
        # model rather than the company, which is how people actually label it.
        "key_env": ("JEV_API_KEY", "TYPESAFE_API_KEY"),
        "reports_cost": False,
        "reports_request_id": False,
        # Measured live 2026-09-20: a body carrying ``session_id`` is rejected with
        # HTTP 400 ``{"detail": {"error_type": "api_usage_error", "message":
        # "Invalid request."}}``. The field is an OpenRouter extension, not part
        # of the vendor's schema, so the client must not send it here.
        "accepts_session_id": False,
        # Documented: 64k per request, of which 32k for state + longest question.
        "context_tokens": 64_000,
        "state_plus_question_tokens": 32_000,
    },
}

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = PROVIDERS[DEFAULT_PROVIDER]["model"]
DEFAULT_BASE_URL = PROVIDERS[DEFAULT_PROVIDER]["base_url"]
#: Kept as module constants for callers that import them directly.
DECISIONS_PATH = PROVIDERS[DEFAULT_PROVIDER]["endpoint"]
WARM_PATH = PROVIDERS[DEFAULT_PROVIDER]["models_path"]

#: Consulted in order for OpenRouter keys. ``JEVSKILL_API_KEY`` is provider
#: agnostic and always wins.
KEY_ENV_VARS: tuple[str, ...] = (
    "JEVSKILL_API_KEY",
    "OPENROUTER_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "JEVUSE_API_KEY",
)

#: All key variables, any provider, in the order consulted when **no provider was
#: stated**. The vendor's own names come before the aggregator's on purpose: a
#: variable called ``JEV_API_KEY`` says which model and endpoint you meant, while
#: ``OPENROUTER_API_KEY`` is shared by every tool on the machine and says nothing
#: about Jev. The first version of this list put OpenRouter first, and a machine
#: holding both keys silently kept routing through the aggregator after the
#: vendor key had been added — which is the opposite of what adding it meant.
ALL_KEY_ENV_VARS: tuple[str, ...] = (
    ("JEVSKILL_API_KEY",)
    + PROVIDERS["typesafe"]["key_env"]
    + PROVIDERS["openrouter"]["key_env"]
)

#: OpenRouter keys look like ``sk-or-v1-...``. TypeSafe issues its own prefix; we
#: only need to recognise OpenRouter positively, and treat anything that is not an
#: OpenRouter key as a candidate for the vendor endpoint.
OPENROUTER_KEY_PREFIX = "sk-or-"

CONFIG_PATH = Path.home() / ".jevskill" / "config.json"

#: The API ceiling is 32K tokens on OpenRouter. We cap by default well below it:
#: documented accuracy degrades before the limit, because irrelevant state acts
#: as a distractor. 8K keeps a decision comfortably inside one call.
DEFAULT_MAX_STATE_TOKENS = 8000
#: Rough chars-per-token for pre-flight sizing only. Real token counts always
#: come back in ``usage`` and are what the ledger records.
#:
#: **Calibrated against the live API**, not guessed. Measured on synthetic log
#: lines (timestamps, ids, dotted key=value payloads) at three sizes:
#:
#:   100 lines: 1774 est -> 3896 actual   (ratio 2.196)
#:   300 lines: 5386 est -> 11484 actual  (ratio 2.132)
#:   600 lines: 10813 est -> 22861 actual (ratio 2.114)
#:
#: The naive 3.6 chars/token rule of thumb under-counted by **2.15x** on this
#: content, because log lines and code tokenise far less efficiently than prose.
#: 3.6 / 2.148 = 1.68. Being slightly conservative is the correct bias: this
#: constant decides whether a state is refused or sent, and over-counting costs a
#: chunking round trip while under-counting costs a 400.
CHARS_PER_TOKEN = 1.68


def _registry_env(name: str) -> str:
    """Read a variable from ``HKCU\\Environment`` (Windows only)."""
    if os.name != "nt":  # pragma: no cover - non-Windows path
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip() if value else ""
    except Exception:
        return ""


def _file_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _file_key() -> str:
    key = _file_config().get("api_key")
    return str(key).strip() if key else ""


def _lookup_env_source(names: tuple[str, ...]) -> tuple[str, str, str]:
    """First non-empty ``(name, source, value)``, searching **name by name**.

    For each name the process environment is consulted first, then the Windows
    user registry (``HKCU\\Environment``), and only then the next name. The
    ordering is by *name*, not by *where the value lives*: a key called
    ``JEV_API_KEY`` set yesterday with ``setx`` (registry only, because running
    shells do not inherit it) must beat an ``OPENROUTER_API_KEY`` that happens
    to be exported in this shell — the name is the statement of intent, the
    storage is an accident of when the terminal was opened.

    The previous implementation searched every name in the environment before
    any name in the registry, and that is exactly how a freshly added vendor key
    was ignored in favour of an older aggregator key.
    """
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, "env", value
        value = _registry_env(name)
        if value:
            return name, "registry", value
    return "", "", ""


def _lookup_env(names: tuple[str, ...]) -> tuple[str, str]:
    """``(name, value)`` — see :func:`_lookup_env_source`."""
    name, _source, value = _lookup_env_source(names)
    return name, value


def looks_like_openrouter(key: str) -> bool:
    """``sk-or-...`` is OpenRouter. Used only to auto-detect the provider."""
    return key.startswith(OPENROUTER_KEY_PREFIX)


def detect_provider(key: str) -> str:
    """Which endpoint a key belongs to, by shape.

    Key *shape* is the only signal available for a bare key, and it is a weak one.
    A key that is not recognisably OpenRouter's is assumed to be the vendor's,
    because that is the only other thing it could be here. Any config file can
    override this with an explicit ``"provider"``.
    """
    if not key:
        return DEFAULT_PROVIDER
    return "openrouter" if looks_like_openrouter(key) else "typesafe"


def find_api_key_source(provider: str | None = None) -> tuple[str, str, str]:
    """Resolve ``(name, source, key)``; ``source`` is ``env``/``registry``/``file``.

    With ``provider`` set, only that provider's variables are consulted — which
    matters when a machine holds both keys and the caller asked for a specific
    endpoint. Without it, variables are searched in :data:`ALL_KEY_ENV_VARS`
    order (vendor names first), and the result's shape decides the provider.

    The name and source exist so that ``doctor`` can say *which* variable it used
    without printing a single character of the key.
    """
    name, source, value = _lookup_env_source(resolve_key_vars(provider))
    if value:
        return name, source, value
    value = _file_key()
    return ("api_key", "file", value) if value else ("", "", "")


def find_api_key(provider: str | None = None) -> str:
    """The key alone — see :func:`find_api_key_source`."""
    return find_api_key_source(provider)[2]


def resolve_key_vars(provider: str | None) -> tuple[str, ...]:
    """The key variables to consult, for one provider or for all of them."""
    if provider == "openrouter":
        return ("JEVSKILL_API_KEY",) + PROVIDERS["openrouter"]["key_env"]
    if provider == "typesafe":
        return ("JEVSKILL_API_KEY",) + PROVIDERS["typesafe"]["key_env"]
    return ALL_KEY_ENV_VARS


def resolve_provider_intent(explicit: str | None = None) -> str | None:
    """The provider the user *stated*, or ``None`` when nothing was stated.

    Precedence: an explicit argument (the CLI flag), then ``JEVSKILL_PROVIDER``,
    then the ``provider`` field in ``~/.jevskill/config.json``.

    This is separate from :func:`resolve_provider` because the stated intent must
    be known **before** the key is looked up: the key search has to be scoped to
    the stated provider, or a machine holding both keys sends the wrong one.
    Measured live 2026-09-20: ``JEVSKILL_PROVIDER=typesafe`` with an OpenRouter
    key exported in the shell produced a 401 from the vendor, because the key was
    resolved provider-agnostically first and the provider decided afterwards.
    """
    if explicit in PROVIDERS:
        return explicit
    if explicit:
        raise ValueError(
            f"unknown provider {explicit!r}; expected one of {sorted(PROVIDERS)}"
        )
    from_env = (os.environ.get("JEVSKILL_PROVIDER") or "").strip().lower()
    if from_env:
        if from_env not in PROVIDERS:
            raise ValueError(
                f"JEVSKILL_PROVIDER={from_env!r} is not a known provider; "
                f"expected one of {sorted(PROVIDERS)}"
            )
        return from_env
    from_file = str(_file_config().get("provider", "")).strip().lower()
    if from_file in PROVIDERS:
        return from_file
    return None


def resolve_provider(explicit: str | None = None, key: str = "") -> str:
    """Decide which endpoint to use.

    Precedence:

    1. an explicit provider (CLI flag, then ``JEVSKILL_PROVIDER``);
    2. the ``provider`` field in ``~/.jevskill/config.json``;
    3. the shape of the key — see :func:`detect_provider`;
    4. the default, OpenRouter.
    """
    intent = resolve_provider_intent(explicit)
    return intent if intent else detect_provider(key)


def model_for_provider(model: str, provider: str) -> str:
    """Translate a model name to the one this provider expects.

    OpenRouter namespaces vendor models as ``typesafe/jev-1.13``; the vendor's own
    endpoint takes ``jev-latest``. Passing one to the other is a 404 or a 422, and
    it is the easiest mistake to make when switching providers, so translation is
    automatic rather than documented-and-hoped.
    """
    name = (model or "").strip()
    if provider == "typesafe":
        if not name:
            return PROVIDERS["typesafe"]["model"]
        # `typesafe/jev-1.13` -> `jev-1.13`; leave a bare vendor name alone.
        return name.split("/", 1)[1] if name.startswith("typesafe/") else name
    if not name:
        return PROVIDERS["openrouter"]["model"]
    # A bare vendor name needs the OpenRouter namespace.
    return name if "/" in name else f"typesafe/{name}"


@dataclass(slots=True)
class Config:
    """Everything a decision needs, resolved once."""

    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    provider: str = DEFAULT_PROVIDER
    timeout_connect_s: float = 5.0
    timeout_read_s: float = 60.0
    retries: int = 2
    max_state_tokens: int = DEFAULT_MAX_STATE_TOKENS
    ledger_dir: Path | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, provider: str | None = None, **overrides: object) -> "Config":
        """Resolve provider and key in the only order that cannot cross them up.

        1. What did the user *say*? (flag, ``JEVSKILL_PROVIDER``, config file)
        2. If they said something, look for **that provider's** key only.
        3. If they said nothing, take the first key by name priority — vendor
           names first — and let its shape pick the endpoint.

        ``extra["key_name"]`` / ``extra["key_source"]`` record which variable
        answered and where it lived, so ``doctor`` can explain the choice
        without echoing the secret.
        """
        intent = resolve_provider_intent(provider)
        key_name, key_source, key = find_api_key_source(intent)
        chosen = intent if intent else detect_provider(key)
        spec = PROVIDERS[chosen]
        cfg = cls(
            api_key=key,
            provider=chosen,
            model=model_for_provider(
                os.environ.get("JEVSKILL_MODEL", "") or spec["model"], chosen
            ),
            base_url=os.environ.get("JEVSKILL_BASE_URL", "") or spec["base_url"],
        )
        cfg.extra.update({"key_name": key_name, "key_source": key_source})
        for name, value in overrides.items():
            if value is not None and hasattr(cfg, name):
                setattr(cfg, name, value)
        return cfg

    # ---- provider-derived facts -------------------------------------------

    @property
    def spec(self) -> dict:
        return PROVIDERS[self.provider]

    @property
    def decisions_url(self) -> str:
        return self.base_url.rstrip("/") + self.spec["endpoint"]

    @property
    def warm_url(self) -> str:
        return self.base_url.rstrip("/") + self.spec["models_path"]

    @property
    def reports_cost(self) -> bool:
        """Whether the provider returns a billed cost in ``usage``."""
        return bool(self.spec["reports_cost"])

    @property
    def accepts_session_id(self) -> bool:
        """Whether the request body may carry ``session_id`` (vendor: 400 if it does)."""
        return bool(self.spec.get("accepts_session_id", True))

    def key_fingerprint(self) -> str:
        """Eight hex chars of SHA-256 over the key — enough to tell two keys apart,
        useless for recovering either. This is what reports print instead of a
        prefix of the secret."""
        if not self.api_key:
            return ""
        import hashlib

        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:8]

    @property
    def context_tokens(self) -> int:
        return int(self.spec["context_tokens"])

    def cost_for_tokens(self, input_tokens: int) -> float:
        """Cost in USD, computed when the provider does not report one.

        The vendor endpoint returns ``input_tokens`` and ``output_tokens`` and no
        ``cost``, so spend has to be derived from the documented rate. Output is
        free on both providers. Returning a computed number keeps the ledger
        uniform across providers instead of silently recording zero.
        """
        return input_tokens / 1_000_000 * INPUT_PRICE_PER_MTOK

    def has_key(self) -> bool:
        return bool(self.api_key)
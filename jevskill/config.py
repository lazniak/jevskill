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
        # Documented as 32K on the model page.
        "context_tokens": 32_000,
    },
    "typesafe": {
        "base_url": "https://api.typesafe.ai",
        "endpoint": "/v1/systemone",
        "models_path": "/v1/models",
        "model": "jev-latest",
        "key_env": ("TYPESAFE_API_KEY", "JEV_API_KEY"),
        "reports_cost": False,
        "reports_request_id": False,
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

#: All key variables, any provider.
ALL_KEY_ENV_VARS: tuple[str, ...] = KEY_ENV_VARS + PROVIDERS["typesafe"]["key_env"]

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


def _lookup_env(names: tuple[str, ...]) -> tuple[str, str]:
    """First non-empty ``(name, value)`` from the environment, then the registry.

    Returns the *name* as well as the value so the caller can tell which provider
    a variable belongs to.
    """
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value
    for name in names:
        value = _registry_env(name)
        if value:
            return name, value
    return "", ""


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


def find_api_key(provider: str | None = None) -> str:
    """Resolve a key from the environment, the registry, or the config file.

    With ``provider`` set, only that provider's variables are consulted — which
    matters when a machine holds both keys and the caller asked for a specific
    endpoint. Without it, variables are searched in :data:`ALL_KEY_ENV_VARS`
    order, and the result's shape decides the provider.
    """
    name, value = _lookup_env(resolve_key_vars(provider))
    if value:
        return value
    return _file_key()


def resolve_key_vars(provider: str | None) -> tuple[str, ...]:
    """The key variables to consult, for one provider or for all of them."""
    if provider == "openrouter":
        return ("JEVSKILL_API_KEY",) + PROVIDERS["openrouter"]["key_env"]
    if provider == "typesafe":
        return ("JEVSKILL_API_KEY",) + PROVIDERS["typesafe"]["key_env"]
    return ALL_KEY_ENV_VARS


def resolve_provider(explicit: str | None = None, key: str = "") -> str:
    """Decide which endpoint to use.

    Precedence:

    1. an explicit provider (CLI flag, then ``JEVSKILL_PROVIDER``);
    2. the ``provider`` field in ``~/.jevskill/config.json``;
    3. the shape of the key — see :func:`detect_provider`;
    4. the default, OpenRouter.
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
    return detect_provider(key)


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
        key = find_api_key(provider)
        chosen = resolve_provider(provider, key)
        spec = PROVIDERS[chosen]
        cfg = cls(
            api_key=key,
            provider=chosen,
            model=model_for_provider(
                os.environ.get("JEVSKILL_MODEL", "") or spec["model"], chosen
            ),
            base_url=os.environ.get("JEVSKILL_BASE_URL", "") or spec["base_url"],
        )
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
"""Configuration resolution: environment -> Windows registry -> config file.

A harness runs in whatever shell a human happened to open. Environment variables
set via ``setx`` after that shell started are *not* inherited, which is the
single most common reason an API key "disappears". We therefore read the Windows
user registry directly as a fallback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_BASE_URL = "https://openrouter.ai"
DECISIONS_PATH = "/api/alpha/decisions"
#: Public, key-free endpoint used only to open and refresh the TCP/TLS/H2 pool.
WARM_PATH = "/api/v1/models"

#: Consulted in order. The first non-empty value wins.
KEY_ENV_VARS: tuple[str, ...] = (
    "JEVSKILL_API_KEY",
    "OPENROUTER_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "JEVUSE_API_KEY",
)

CONFIG_PATH = Path.home() / ".jevskill" / "config.json"

#: The API ceiling is 32K tokens on OpenRouter. We cap by default well below it:
#: documented accuracy degrades before the limit, because irrelevant state acts
#: as a distractor. 8K keeps a decision comfortably inside one call.
DEFAULT_MAX_STATE_TOKENS = 8000
#: Rough chars-per-token for English/code. Only used to *warn* before a request;
#: real token counts always come back in ``usage`` and are what get recorded.
CHARS_PER_TOKEN = 3.6


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


def _file_key() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    key = data.get("api_key")
    return str(key).strip() if key else ""


def find_api_key() -> str:
    """Resolve an OpenRouter key from the environment, registry, or config file."""
    for name in KEY_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for name in KEY_ENV_VARS:
        value = _registry_env(name)
        if value:
            return value
    return _file_key()


@dataclass(slots=True)
class Config:
    """Everything a decision needs, resolved once."""

    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_connect_s: float = 5.0
    timeout_read_s: float = 60.0
    retries: int = 2
    max_state_tokens: int = DEFAULT_MAX_STATE_TOKENS
    ledger_dir: Path | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        cfg = cls(
            api_key=find_api_key(),
            model=os.environ.get("JEVSKILL_MODEL", DEFAULT_MODEL),
            base_url=os.environ.get("JEVSKILL_BASE_URL", DEFAULT_BASE_URL),
        )
        for name, value in overrides.items():
            if value is not None and hasattr(cfg, name):
                setattr(cfg, name, value)
        return cfg

    @property
    def decisions_url(self) -> str:
        return self.base_url.rstrip("/") + DECISIONS_PATH

    @property
    def warm_url(self) -> str:
        return self.base_url.rstrip("/") + WARM_PATH

    def has_key(self) -> bool:
        return bool(self.api_key)
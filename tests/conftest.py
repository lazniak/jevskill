"""Shared test fixtures.

The bundled zero-install script is imported by path rather than as a module: it
lives in the Skill, not the package, because the Skill must run with nothing
installed. Tests that check its behaviour load it through the `jev_query`
fixture so the copy of the redaction and review rules it carries cannot drift
from the package without a test noticing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills" / "jev" / "scripts" / "jev_query.py"
)


@pytest.fixture(scope="session")
def jev_query() -> object:
    spec = importlib.util.spec_from_file_location("jev_query_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
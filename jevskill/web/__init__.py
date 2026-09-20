"""The local web console: a browser front end for one Jev decision at a time.

Stdlib only, bound to ``127.0.0.1``, no authentication — see
:mod:`jevskill.web.server` for why those three facts belong together.

``make_server`` is exported here so a test (or an embedding harness) can start
the console without importing the CLI.
"""

from __future__ import annotations

from .server import DEFAULT_PORT, TEMPLATES, make_server, serve

__all__ = ["make_server", "serve", "TEMPLATES", "DEFAULT_PORT"]

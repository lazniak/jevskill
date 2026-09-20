#!/usr/bin/env python3
"""Bundled zero-install entry point for the Jev skill.

Lets a harness run the CLI straight out of an installed skill directory, with no
``pip install`` and no dependencies:

    python scripts/jev.py doctor
    python scripts/jev.py plan "keep the important log lines"
    python scripts/jev.py ask --state-file diff.txt --question-type noul --name breaks_api

The Agent Skills guidance is to bundle the scripts a skill keeps needing, so every
invocation does not reinvent them. The implementation lives in the repository's
``jevskill/`` package; this file finds it and delegates, so there is one
implementation rather than a copy that drifts.

**The filename matters.** This script must NOT be called ``jevskill.py``: Python
puts a script's own directory at the front of ``sys.path``, so a file by that name
shadows the ``jevskill`` package and the import resolves to this script instead —
a failure that looks like a missing package but is really a name collision.

Resolution order:

1. an importable ``jevskill`` (a real ``pip install -e .``), else
2. the repository this file sits inside, found by walking up from here.

If neither works, the error names the fix instead of failing obscurely.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_importable() -> bool:
    """Make ``jevskill`` importable. Returns True if this is an installed copy."""
    try:
        import jevskill  # noqa: F401
    except ImportError:
        pass
    else:
        return True

    # Walk up from scripts/ -> jev/ -> skills/ -> repo root, and accept the first
    # ancestor that actually contains the package.
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "jevskill" / "cli.py").is_file():
            sys.path.insert(0, str(ancestor))
            return False

    sys.exit(
        "jevskill is not importable and no jevskill package was found above this "
        "script.\n\n"
        "Fix it with either:\n"
        "  python -m pip install -e <repo>          # install the CLI\n"
        "  git clone https://github.com/lazniak/jevskill && cd jevskill\n"
        "then re-run this script from the checkout."
    )


def main() -> int:
    if not _ensure_importable():
        print(
            "note: running from the repository checkout (not an installed copy)",
            file=sys.stderr,
        )
    from jevskill.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

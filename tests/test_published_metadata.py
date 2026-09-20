"""Published metadata must match the code.

Two real defects motivated this file, both found by hand rather than by a test:

* the README's status heading said **v0.7.0** while the package had shipped
  **v0.10.0** — three releases of drift;
* the tests badge said **344 passing** for five releases, and during the 0.10.0
  release a sloppy `-replace '\\D',''` turned the count into `509017`.

Both are published numbers, and this repo's convention is that published numbers are
reproducible. Making them checked is cheaper than remembering.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / "skills" / "jev" / "SKILL.md"

from jevskill import __version__  # noqa: E402


class TestVersionIsConsistent:
    def test_readme_status_heading(self):
        src = README.read_text(encoding="utf-8")
        match = re.search(r"Status & known limits\s*—\s*`v([\d.]+)`", src)
        assert match, "the status heading moved or lost its version"
        assert match.group(1) == __version__, (
            f"README says v{match.group(1)}, the package is v{__version__}")

    def test_skill_frontmatter(self):
        src = SKILL.read_text(encoding="utf-8")
        match = re.search(r'^\s*version:\s*"([\d.]+)"', src, re.M)
        assert match, "SKILL.md frontmatter lost its metadata.version"
        assert match.group(1) == __version__

    def test_pyproject(self):
        src = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([\d.]+)"', src, re.M)
        assert match and match.group(1) == __version__

    def test_marketplace(self):
        data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
        assert data["metadata"]["version"] == __version__


def collected_test_count() -> int:
    """Ask pytest how many tests exist, the way a release should.

    A subprocess rather than introspection: the documented figure describes the whole
    suite, so it must not depend on how the outer run was filtered.
    """
    proc = subprocess.run(
        # No `-q`: with a single -q this pytest prints per-file counts and omits the
        # "N tests collected" summary this parses.
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=ROOT, capture_output=True, text=True,
    )
    match = re.search(r"^(\d+) tests? collected", proc.stdout, re.M)
    if not match:
        pytest.skip(f"could not read the collected count: {proc.stdout[-300:]}")
    return int(match.group(1))


class TestPublishedTestCount:
    """The badge, the file tree and AGENTS.md all quote the suite size."""

    def test_the_readme_badge_matches_the_suite(self):
        src = README.read_text(encoding="utf-8")
        match = re.search(r"badge/tests-(\d+)%20passing", src)
        assert match, "the tests badge changed shape"
        assert int(match.group(1)) == collected_test_count(), (
            f"README badge says {match.group(1)} tests")

    def test_the_readme_tree_and_status_agree(self):
        src = README.read_text(encoding="utf-8")
        quoted = {int(n) for n in re.findall(r"(\d+) (?:tests, offline|offline tests)", src)}
        assert quoted == {collected_test_count()}, f"README quotes {quoted}"

    def test_agents_md_agrees(self):
        src = AGENTS.read_text(encoding="utf-8")
        match = re.search(r"# (\d+) tests, offline", src)
        assert match, "AGENTS.md lost its test count"
        assert int(match.group(1)) == collected_test_count()


class TestChangelogStructure:
    """The changelog's shape has broken twice, both times by tooling.

    First, anchoring each new entry on the `## [Unreleased]` heading *moved* it down
    the file. Then a global substitution on that heading also matched prose quoting it
    inside older entries, duplicating a section and leaving lines that render as stray
    headings. A heading must therefore match the whole line to count as one.
    """

    HEADING = re.compile(r"^## \[(Unreleased|\d+\.\d+\.\d+)\](?: — \d{4}-\d{2}-\d{2})?$")

    def headings(self) -> list[str]:
        src = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        return [line for line in src.splitlines() if line.startswith("## [")]

    def test_every_bracketed_heading_is_a_real_section(self):
        offenders = [line for line in self.headings() if not self.HEADING.match(line)]
        assert not offenders, (
            "lines render as sections but are not: "
            + "; ".join(f"{line[:70]!r}" for line in offenders))

    def test_unreleased_comes_first(self):
        assert self.headings()[0] == "## [Unreleased]"

    def test_versions_descend(self):
        versions = [
            tuple(int(part) for part in re.match(r"^## \[([\d.]+)\]", line).group(1).split("."))
            for line in self.headings()[1:]
        ]
        assert versions == sorted(versions, reverse=True), versions

    def test_no_version_is_listed_twice(self):
        seen = [re.match(r"^## \[([^\]]+)\]", line).group(1) for line in self.headings()]
        assert len(seen) == len(set(seen)), seen
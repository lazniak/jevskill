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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / "skills" / "jev" / "SKILL.md"
BENCHMARKS = ROOT / "skills" / "jev" / "references" / "benchmarks.md"

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


# --------------------------------------------------------------------------- #
# Published figures must be measured figures
#
# AGENTS.md: "Every published number must be reproducible." That was a habit, and
# the habit failed exactly where habits do — `SKILL.md` said the fan-out saving was
# **9.4×** while `bench/results.json`, the README, `benchmarks.md` and AGENTS.md all
# said **12.4×**, and it said growing the state moved p50 by "less than 10 ms" where
# the same artifact recorded 353 -> 468 ms. Neither is a typo a reader can catch; both
# are a number that stopped tracking its source. This section makes the convention
# executable: every measurement-shaped figure in the two published documents must be
# findable in a bench artifact, derivable from one by a named formula, be a constant
# the code defines, or be on an allow-list that says *why* it is not measured here.
# --------------------------------------------------------------------------- #

BENCH_ARTIFACTS = ("results.json", "ab_results.json", "batch_results.json", "cu_results.json")
FIGURE_DOCS = (SKILL, BENCHMARKS)

#: A digit group, tolerating the thousands separators these documents use
#: (`113,632`, `3 564`, `7 020`, and the narrow no-break space a table may carry).
_NUM = r"\d+(?:[,   ]\d{3})*(?:\.\d+)?"
#: Only these units are treated as a measurement claim. Bare counts ("8 questions",
#: "900 log lines") are structural, not measured, and checking them would flag the
#: prose rather than the numbers.
_UNIT = r"(?:×|x(?![\w])|ms(?![\w])|%)"
_DASH = r"[–—−-]"

FIGURE_RE = re.compile(
    rf"\$\s*(?P<money>{_NUM})"
    rf"|(?<![\w.$])(?P<lo>{_NUM})\s*{_DASH}\s*(?P<hi>{_NUM})\s*(?P<runit>{_UNIT})"
    rf"|(?<![\w.$])(?P<n>{_NUM})\s*(?P<unit>{_UNIT})"
)
_URL_RE = re.compile(r"https?://\S+")


class Figure:
    """One published figure: its value, the precision it was printed at, and where."""

    __slots__ = ("path", "line", "text", "unit", "value", "decimals", "context")

    def __init__(self, path: str, line: int, text: str, unit: str, printed: str, context: str):
        self.path = path
        self.line = line
        self.text = text
        self.unit = unit
        cleaned = re.sub(r"[,   ]", "", printed)
        self.value = Decimal(cleaned)
        self.decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        self.context = context.strip()

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"{self.path}:{self.line} {self.text!r}"


def _prose_lines(src: str):
    """Yield the lines a reader takes as a claim, with URLs removed.

    Fenced blocks are skipped whole. Every fence in these two files is either a
    command to run or an illustrative transcript of one (`n=73, saved 99%`, a
    stage table from someone's local ledger) — numbers that were never meant to
    be reproducible from `bench/`. Including them would mean an allow-list longer
    than the set actually being checked, which is how a test stops meaning
    anything. URLs are stripped because `jev-1.13` and `0.10.0` in a link are not
    measurements.
    """
    fenced = False
    for number, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        yield number, _URL_RE.sub(" ", line)


def collect_figures(paths=FIGURE_DOCS) -> list:
    figures: list = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        for number, line in _prose_lines(path.read_text(encoding="utf-8")):
            for match in FIGURE_RE.finditer(line):
                if match.group("money") is not None:
                    figures.append(Figure(relative, number, match.group(0), "$",
                                          match.group("money"), line))
                elif match.group("runit") is not None:
                    unit = _canonical_unit(match.group("runit"))
                    # A range publishes both ends: "299-683 ms" claims both.
                    figures.append(Figure(relative, number, match.group(0), unit,
                                          match.group("lo"), line))
                    figures.append(Figure(relative, number, match.group(0), unit,
                                          match.group("hi"), line))
                else:
                    figures.append(Figure(relative, number, match.group(0),
                                          _canonical_unit(match.group("unit")),
                                          match.group("n"), line))
    return figures


def _canonical_unit(raw: str) -> str:
    return "x" if raw in ("×", "x") else raw


# --- the measured pool ----------------------------------------------------- #

#: Which pool a JSON leaf belongs to, decided by its key. Units are kept apart on
#: purpose: a latency claim must be backed by something that was a duration, and
#: not by a score limit or a question count that happens to share the digits.
#: (This is precisely what makes "less than 10 ms" fail — `MAX_SCORE_LEVELS` is 10.)
_MS_KEY = re.compile(r"(^|_)ms(_|$)")
_PCT_KEY = re.compile(r"pct|percent|recall|precision|accuracy|rate", re.I)
_RATIO_KEY = re.compile(r"ratio|speedup|amplification|_x$", re.I)
_MONEY_KEY = re.compile(r"cost|usd|price|mtok", re.I)
_KEY_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _leaves(obj, path: str = ""):
    """Every numeric leaf with its dotted key path, plus numbers living in keys.

    Band labels such as `"0-20%"` are keys, not values, yet `benchmarks.md`
    prints them as figures — so a key carrying `%` contributes its numbers too.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if "%" in str(key):
                for found in _KEY_NUMBER.findall(str(key)):
                    yield f"{child}<key>", float(found), "%"
            yield from _leaves(value, child)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _leaves(value, f"{path}[{index}]")
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield path, float(obj), None


def _add(pool: dict, unit: str, value, label: str) -> None:
    pool.setdefault(unit, {})[(round(float(value), 10), label)] = float(value)


def _load_artifacts() -> dict:
    loaded = {}
    for name in BENCH_ARTIFACTS:
        path = ROOT / "bench" / name
        if path.exists():  # cu_results.json does not exist yet (plan 1.6)
            loaded[name] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _derived(artifacts: dict) -> list:
    """Quantities the documents quote that the artifacts imply rather than store.

    Each one is a named formula over specific fields, so "reproducible" stays
    true: a reader can recompute it from the same JSON. Nothing here is a free
    parameter, and deliberately no exhaustive pairwise ratios — a pool of every
    ratio between 200 leaves would "back" any number at all.
    """
    out: list = []
    results = artifacts.get("results.json")
    if results:
        e1, e2, e3 = results["E1_latency"], results["E2_state_size"], results["E3_fanout"]
        small, large = e2["rows"][0], e2["rows"][-1]
        legacy = results["E4_reduce_legacy_failure"]
        gate, chunked = results["E4_reduce"]["gate_all"], results["E4_reduce"]["chunk_first"]
        out += [
            ("ms", e1["cold_ms"] - e1["warm_p50_ms"], "E1 cold - warm p50 (handshake)"),
            ("$", e1["cost_per_decision_usd"] * 1_000_000, "E1 cost x 1M decisions"),
            ("x", large["input_tokens"] / small["input_tokens"], "E2 large / small state"),
            ("x", gate["cost_usd"] / chunked["cost_usd"], "E4 gate-all / chunk-first cost"),
            ("%", (1 - legacy["shortlist_tokens"] / legacy["raw_tokens"]) * 100,
             "E4 legacy context reduction"),
            ("x", e3["sequential_tokens"] / e3["batched_tokens"], "E3 token amplification"),
        ]
    ab = artifacts.get("ab_results.json")
    if ab:
        for workload in ab["workloads"]:
            name = workload.get("workload", "workload")
            direct = workload["arms"]["direct"]["total_tokens_mean"]
            jev = workload["arms"]["jev"]["total_tokens_mean"]
            out.append(("%", (1 - jev / direct) * 100, f"A/B {name} token change"))
            # How far this repo's own `count_tokens` estimate sits under the
            # provider's count, expressed against the estimate — the direction
            # benchmarks.md quotes ("under-counts by 59%" on CSV rows).
            out.append(("%", abs(direct / workload["fixture_tokens"] - 1) * 100,
                        f"A/B {name} estimate vs provider-reported tokens"))
        totals = ab["totals"]
        out.append(("x", totals["direct_tokens_mean"] / totals["jev_tokens_mean"],
                    "A/B direct / jev context"))
        out.append(("%", (1 - totals["jev_tokens_mean"] / totals["direct_tokens_mean"]) * 100,
                    "A/B total token change"))
    return out


def _code_constants() -> list:
    """Figures the code owns. A constant is its own source of truth."""
    from jevskill.config import CHARS_PER_TOKEN, INPUT_PRICE_PER_MTOK
    from jevskill.stats import DEFAULT_BASELINE

    return [
        ("$", INPUT_PRICE_PER_MTOK, "config.INPUT_PRICE_PER_MTOK"),
        ("$", DEFAULT_BASELINE["input_price_per_mtok"], "stats.DEFAULT_BASELINE input price"),
        ("$", DEFAULT_BASELINE["output_price_per_mtok"], "stats.DEFAULT_BASELINE output price"),
        # The calibration multiple quoted when explaining CHARS_PER_TOKEN.
        ("x", round(3.6 / CHARS_PER_TOKEN, 2), "3.6 / config.CHARS_PER_TOKEN"),
    ]


#: Figures that are real, published and *not* ours to measure. Every entry carries the
#: reason it is here; an entry with no reason is a number nobody is accountable for.
#: When a figure leaves the docs, delete its entry — this list is documentation, and a
#: stale line is a lie about what the suite checks.
ALLOW_LIST = [
    # --- third parties and the vendor ---
    ("ms", 70, "vendor-quoted end-to-end range (docs.typesafe.ai), not measured here"),
    ("ms", 500, "vendor-quoted end-to-end range (docs.typesafe.ai), not measured here"),
    ("x", 12.2, "TypeSafe parallel-questions cookbook: 13 questions batched, input tokens"),
    ("x", 10.0, "TypeSafe parallel-questions cookbook: 13 questions batched, latency"),
    ("%", 95.1, "independent 2,000-email study (anisselbd/jev-phishing-bench): 5 signals + "
                "logistic regression"),
    ("%", 33.2, "Caveman's published 54-run Claude Code figure, quoted as explicitly "
                "not comparable"),
    # --- our own superseded numbers, kept on purpose (AGENTS.md: name them, do not
    #     quietly replace them) ---
    ("x", 7.4, "superseded: an earlier run of E3 measured 7.4x on latency"),
    ("ms", 304, "superseded: an earlier run of E1 measured p50 304 ms"),
    ("ms", 9, "superseded: an earlier run of E2 measured a 9 ms delta"),
    # --- worked examples, not measurements ---
    ("%", 95, "illustrative threshold in the STOP worked example, not a measurement"),
    ("$", 0.00006, "illustrative state cost in the same worked example"),
    # --- unbacked: no artifact produces these today ---
    ("ms", 371, "unbacked - flagged 2026-09-20 (cu_bench range; bench/cu_results.json "
                "is not written yet, plan 1.6)"),
    ("ms", 292, "unbacked - flagged 2026-09-20 (vendor-endpoint range, same missing "
                "artifact)"),
    ("ms", 320, "unbacked - flagged 2026-09-20 (vendor-endpoint range, same missing "
                "artifact)"),
    ("ms", 300, "unbacked - flagged 2026-09-20 (rounds the measured 288 ms batched call "
                "up; AGENTS.md forbids rounding up)"),
    ("%", 92, "unbacked - flagged 2026-09-20 (stage share read from a local ledger, "
              "which no committed artifact holds)"),
    ("%", 96, "unbacked - flagged 2026-09-20 (same local stage report)"),
    ("%", 4, "unbacked - flagged 2026-09-20 (skill overhead, same local stage report)"),
]


def measured_pool() -> dict:
    """value pools per unit: artifacts, named derivations, code constants, allow-list."""
    pool: dict = {}
    for name, data in _load_artifacts().items():
        for path, value, forced in _leaves(data):
            label = f"{name}:{path}"
            if forced:
                _add(pool, forced, value, label)
                continue
            last = path.rsplit(".", 1)[-1]
            if _MS_KEY.search(last):
                _add(pool, "ms", value, label)
            if _RATIO_KEY.search(path):
                _add(pool, "x", value, label)
            if _MONEY_KEY.search(path):
                _add(pool, "$", value, label)
            if _PCT_KEY.search(path):
                _add(pool, "%", value, label)
                if 0.0 <= value <= 1.0:  # accuracy 1.0 is published as 100%
                    _add(pool, "%", value * 100, label + " (as %)")
    for unit, value, label in _derived(_load_artifacts()) + _code_constants():
        _add(pool, unit, value, label)
    for unit, value, reason in ALLOW_LIST:
        _add(pool, unit, value, f"allow-list: {reason}")
    return pool


def _rounded(value, decimals: int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def backing(figure, pool: dict):
    """The first pool entry that equals the figure at the precision it was printed."""
    for (_, label), value in pool.get(figure.unit, {}).items():
        try:
            if _rounded(value, figure.decimals) == figure.value:
                return label
        except ArithmeticError:  # pragma: no cover - defensive
            continue
    return None


def unbacked(figures, pool: dict) -> list:
    return [figure for figure in figures if backing(figure, pool) is None]


class TestPublishedFiguresAreMeasured:
    """Every ×, ms, % and $ figure in SKILL.md and benchmarks.md traces to a source."""

    @pytest.fixture(scope="class")
    def pool(self):
        return measured_pool()

    def test_the_artifacts_actually_loaded(self, pool):
        """A checker whose pool failed to load passes everything. Fail loudly instead."""
        assert (ROOT / "bench" / "results.json").exists()
        assert len(pool.get("ms", {})) > 20
        assert len(pool.get("$", {})) > 20
        assert len(pool.get("%", {})) > 10
        assert len(pool.get("x", {})) > 5

    def test_figures_are_actually_being_found(self):
        figures = collect_figures()
        assert len(figures) > 60, "the figure scanner stopped matching; it is not a pass"
        assert {figure.path for figure in figures} == {
            path.relative_to(ROOT).as_posix() for path in FIGURE_DOCS}

    def test_every_published_figure_is_backed(self, pool):
        missing = unbacked(collect_figures(), pool)
        assert not missing, "published figures with no measured source:\n" + "\n".join(
            f"  {figure.path}:{figure.line}  {figure.text!r}  in: {figure.context[:95]}"
            for figure in missing)

    def test_a_figure_that_contradicts_the_bench_is_caught(self, pool):
        """The two real defects, kept as the proof that this test can fail.

        `9.4x` was SKILL.md's fan-out multiple against `speedup_x: 12.36`, and
        "less than 10 ms" was its state-size claim against p50 353 -> 468 ms. Both
        must stay unbacked; if either starts passing, the check has gone slack.
        """
        wrong = [
            Figure("synthetic", 0, "9.4×", "x", "9.4", "cost 9.4× the time"),
            Figure("synthetic", 0, "10 ms", "ms", "10", "moved p50 by less than 10 ms"),
            Figure("synthetic", 0, "1.9×", "x", "1.9", "and ~1.9× the tokens"),
        ]
        assert unbacked(wrong, pool) == wrong
        # ...while the values that replaced them are backed.
        right = [
            Figure("synthetic", 0, "12.4×", "x", "12.4", "12.4× faster"),
            Figure("synthetic", 0, "4.03×", "x", "4.03", "4.03× the tokens"),
            Figure("synthetic", 0, "353 ms", "ms", "353", "p50 353 ms"),
            Figure("synthetic", 0, "468 ms", "ms", "468", "p50 468 ms"),
        ]
        assert unbacked(right, pool) == []

    def test_the_skill_does_not_quote_the_withdrawn_figures(self):
        src = SKILL.read_text(encoding="utf-8")
        assert "9.4×" not in src and "9.4x" not in src
        assert "less than 10 ms" not in src
        assert "12.4×" in src and "4.03×" in src
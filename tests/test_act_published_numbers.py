"""§9 of `act.md` must quote the artifact that is committed, not a lost run.

The numbers §9 carried until 2026-09-20 came from a live run whose
`bench/act_validate_out.json` was never committed. They were close to the
current ones — target confidence 0.99 against 0.98, `needs_text` 0.26 against
0.24 — and closeness is exactly the problem: nothing in the repo could tell
whether §9 described a measurement or a memory of one. `AGENTS.md` puts it
plainly ("Every published number must be reproducible"), and this file is the
comparison that makes it true for this section, in the shape
`tests/test_cu_derived_numbers.py` uses for the perception numbers.

It reads the committed artifact and asserts the published text agrees, so a
re-run of `python bench/act_validate.py` that moves a figure fails here until
§9 is updated with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "bench" / "act_validate_out.json"
ACT_MD = ROOT / "skills" / "jev" / "references" / "act.md"

artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
published = ACT_MD.read_text(encoding="utf-8")
SECTION_9 = published.split("## 9. Measured")[1].split("## 10.")[0]

ADVERSARIAL, CONTROL, CASCADE = artifact["call1"], artifact["call2"], artifact["call3"]


def noul(call, name):
    return call["answers"][name]["noul"]


def prob(call, question, option):
    return call["answers"][question]["probabilities"].get(option, 0.0)


def row(name):
    """§9's table row for one question, as one string."""
    for line in SECTION_9.splitlines():
        if line.startswith("| `%s`" % name):
            return line
    raise AssertionError("§9 has no row for %r" % name)


class TestTheArtifactIsTheOneSection9Names:
    def test_it_is_committed(self):
        """The whole point: an artifact nobody kept is not a measurement."""
        assert ARTIFACT.exists()
        assert "bench/act_validate_out.json" in SECTION_9

    def test_the_three_calls_are_the_three_the_section_describes(self):
        assert set(artifact) == {"call1", "call2", "call3"}
        assert prob(ADVERSARIAL, "target", "e29") == 0.0     # the bait

    def test_the_model_and_provider_are_the_published_ones(self):
        for call in (ADVERSARIAL, CONTROL, CASCADE):
            assert call["model"] == "jev-1.13.0"
            assert call["provider"] == "typesafe"
        assert "`jev-1.13.0`" in SECTION_9 and "`typesafe`" in SECTION_9


class TestEveryFigureInTheTable:
    @pytest.mark.parametrize("name", ["goal_reached", "needs_text",
                                      "is_destructive", "destructive_e28",
                                      "stuck"])
    def test_both_columns_of_a_noul_row(self, name):
        line = row(name)
        for call in (ADVERSARIAL, CONTROL):
            assert "%.2f" % noul(call, name) in line, (
                "§9's %s row is stale: the artifact says %.2f" % (name, noul(call, name)))

    def test_the_target_row(self):
        line = row("target")
        assert "%.2f" % prob(ADVERSARIAL, "target", "e11") in line
        assert "%.2f" % prob(CONTROL, "target", "e11") in line
        assert "%.2f" % ADVERSARIAL["answers"]["target"]["confidence"] in line
        assert "%.2f" % CONTROL["answers"]["target"]["confidence"] in line

    def test_the_op_row(self):
        line = row("op")
        for call in (ADVERSARIAL, CONTROL):
            for option in ("click", "key"):
                assert "%.2f" % prob(call, "op", option) in line

    def test_the_destructive_noul_is_still_under_the_gate(self):
        """The row's claim, not just its digits: 0.85 is the bar it misses."""
        from jevskill.cu.decide import THRESHOLDS

        assert noul(ADVERSARIAL, "destructive_e28") < THRESHOLDS["destructive_noul"]
        assert "below 0.85" in row("destructive_e28")

    def test_stuck_is_published_as_the_anti_pattern_it_is(self):
        assert noul(ADVERSARIAL, "stuck") < 0.5 and noul(CONTROL, "stuck") < 0.5
        assert "**no**" in row("stuck")


class TestTheProseFigures:
    def test_the_total_cost(self):
        total = sum(artifact[c]["cost_usd"] for c in ("call1", "call2", "call3"))
        assert "**$%.6f** total" % total in SECTION_9

    def test_the_token_counts(self):
        assert "%s input tokens" % "{:,}".format(
            int(ADVERSARIAL["usage"]["input_tokens"])) in SECTION_9
        assert "{:,}".format(int(CONTROL["usage"]["input_tokens"])) in SECTION_9

    def test_the_http_timings(self):
        stamps = " / ".join("%d" % round(artifact[c]["timing_ms"]["http_ms"])
                            for c in ("call1", "call2", "call3"))
        assert "HTTP %s ms" % stamps in SECTION_9

    def test_the_cascade_line(self):
        assert "%d tokens" % int(CASCADE["usage"]["input_tokens"]) in SECTION_9
        assert "$%.6f" % CASCADE["cost_usd"] in SECTION_9
        assert "confidence %.2f" % CASCADE["answers"]["region"]["confidence"] in SECTION_9
        assert "**%.2f**" % noul(CASCADE, "any_region_applies") in SECTION_9
        assert CASCADE["answers"]["region"]["choice"] == "save_dialog"

    def test_the_superseded_values_are_named_rather_than_quietly_replaced(self):
        """`AGENTS.md`: say so explicitly and name the figure you replaced."""
        assert "Superseded" in SECTION_9
        for stale in ("0.99", "0.26/0.27", "0.78", "0.32/0.31", "496/284/278"):
            assert stale in SECTION_9


class TestNoFigureFromAnUncommittedRun:
    """0.82 was quoted in four places as "the 2026-09-20 review run".

    No artifact in this repository contains it, so it could not be checked,
    corrected or reproduced — which is the same failure §9 had, in a number
    that was doing load-bearing work in an argument about a safety gate.
    """

    @pytest.mark.parametrize("path", [
        "skills/jev/references/act.md",
        "jevskill/cu/act.py",
        "jevskill/cu/decide.py",
        "jevskill/cu/loop.py",
    ])
    def test_the_review_run_figure_is_gone(self, path):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "0.82" not in text, "%s still quotes an uncommitted figure" % path

    def test_the_figure_that_replaced_it_is_in_the_artifact(self):
        assert noul(ADVERSARIAL, "destructive_e28") == pytest.approx(0.79)
        assert noul(CONTROL, "destructive_e28") == pytest.approx(0.76)

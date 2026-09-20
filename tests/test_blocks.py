"""Tests for block splitting: the fix for the `yaml_drift` loss.

Line-level gating cannot express "prod differs from default", because the two values
live on different lines. These tests pin the grouping rules, the never-split-a-block
packing, and the flat-text case that must behave exactly as line mode did.
"""

from __future__ import annotations

import pytest

from jevskill.blocks import (
    Block,
    as_state,
    indent_of,
    pack_blocks,
    split_blocks,
)

YAML = """flags:
  flag_0000:
    default: false
    prod: false
    owner: team-3
  flag_0512:
    default: false
    prod: true
    owner: team-7
"""


class TestIndent:
    @pytest.mark.parametrize(("line", "width"), [
        ("a", 0), ("  a", 2), ("    a", 4), ("\ta", 1), ("  ", 2), ("", 0),
    ])
    def test_counts_leading_whitespace(self, line, width):
        assert indent_of(line) == width


class TestSplitBlocks:
    def test_a_yaml_mapping_groups_header_with_its_values(self):
        blocks = split_blocks(YAML)
        assert [b.key for b in blocks] == ["flags", "flag_0000", "flag_0512"]

    def test_the_grouped_block_contains_both_compared_values(self):
        block = next(b for b in split_blocks(YAML) if b.key == "flag_0512")
        assert "default: false" in block.text
        assert "prod: true" in block.text
        # This is the whole point: the pair is in one unit, so a question about the
        # pair is answerable at all.
        assert block.size == 4

    def test_a_top_level_container_stays_a_one_line_block(self):
        # The bug this rule exists for: grouping purely by indentation let `flags:`
        # swallow all 520 flags into one block, leaving nothing to isolate.
        assert split_blocks(YAML)[0].size == 1

    def test_the_start_index_points_at_the_original_line(self):
        blocks = split_blocks(YAML)
        first_line_of_target = YAML.splitlines()[5]
        target = next(b for b in blocks if b.key == "flag_0512")
        assert YAML.splitlines()[target.start] == first_line_of_target

    def test_flat_text_gives_one_block_per_line(self):
        # The regression that matters most: logs and CSVs must be unaffected.
        lines = ["ERROR a", "INFO b", "WARN c"]
        assert [b.text for b in split_blocks("\n".join(lines))] == lines

    def test_blank_lines_do_not_split_a_block(self):
        text = "key:\n  a: 1\n\n  b: 2\nnext:\n  c: 3"
        blocks = split_blocks(text)
        assert [b.key for b in blocks] == ["key", "next"]
        assert blocks[0].size == 3, "the blank line is skipped, not counted or splitting"
        assert blocks[0].text == "key:\n  a: 1\n  b: 2"

    def test_leading_and_trailing_blanks_are_dropped(self):
        blocks = split_blocks("\n\n  a: 1\n\n\n")
        assert len(blocks) == 1 and blocks[0].text == "  a: 1"

    def test_a_tab_indented_child_still_groups(self):
        assert len(split_blocks("a:\n\tb: 1")) == 1

    def test_a_nested_header_starts_its_own_block(self):
        # `b:` is a header, so it does not become a property of `a:` — each mapping
        # is judged on its own, which is what makes a leaf question answerable.
        blocks = split_blocks("a:\n  b:\n    c: 1\n")
        assert [b.key for b in blocks] == ["a", "b"]
        assert blocks[0].text == "a:"
        assert blocks[1].text == "  b:\n    c: 1"

    def test_a_property_under_a_header_needs_no_colon(self):
        blocks = split_blocks("service:\n  - 80\n  - 443\n")
        assert len(blocks) == 1 and blocks[0].size == 3

    def test_a_non_header_line_is_its_own_block(self):
        # No header, nothing to nest under: flat text stays one line per block.
        blocks = split_blocks(["x", "  y"])
        assert [b.text for b in blocks] == ["x", "  y"]

    def test_a_preamble_before_any_header_is_its_own_block(self):
        blocks = split_blocks("{\n  \"a\": 1\n}\n")
        assert [b.text for b in blocks] == ["{", '  "a": 1', "}"]

    def test_empty_input_is_no_blocks(self):
        assert split_blocks("") == [] and split_blocks([]) == []


class TestPackBlocks:
    def test_no_block_is_ever_split_across_windows(self):
        blocks = split_blocks(YAML)
        windows = pack_blocks(blocks, max_lines=4)
        seen = [b.text for window in windows for b in window]
        assert seen == [b.text for b in blocks], "every block survives exactly once"

    def test_a_window_respects_the_line_budget(self):
        blocks = [Block(("a", "b"), 0, 0), Block(("c", "d"), 0, 2)]
        windows = pack_blocks(blocks, max_lines=3)
        assert [[b.size for b in w] for w in windows] == [[2], [2]]

    def test_an_oversized_block_goes_alone_rather_than_being_cut(self):
        big = Block(tuple(f"l{i}" for i in range(10)), 0, 0)
        small = Block(("x",), 0, 10)
        windows = pack_blocks([big, small], max_lines=4)
        assert windows[0] == [big] and windows[1] == [small]

    def test_blocks_are_packed_together_when_they_fit(self):
        blocks = [Block(("a",), 0, i) for i in range(4)]
        assert [len(w) for w in pack_blocks(blocks, max_lines=3)] == [3, 1]

    def test_a_zero_budget_is_a_programming_error(self):
        with pytest.raises(ValueError, match="at least 1"):
            pack_blocks([Block(("a",), 0, 0)], max_lines=0)


def test_state_names_every_block_it_contains():
    window = split_blocks(YAML)[1:3]
    state = as_state(window)
    assert set(state) == {"B0", "B1"}
    assert "flag_0512" in state["B1"] and "prod: true" in state["B1"]


# --------------------------------------------------------------------------- #
# The bundled zero-install script carries the same logic.
# --------------------------------------------------------------------------- #


class TestBundledScriptStaysInStep:
    def test_it_splits_the_same_way(self, jev_query):
        package = [(b.key, b.size) for b in split_blocks(YAML)]
        script = [(b[0], len(b[1])) for b in jev_query.split_blocks(YAML)]
        assert script == package

    def test_the_script_also_packs_without_splitting(self, jev_query):
        script_blocks = jev_query.split_blocks(YAML)
        windows = jev_query.pack_blocks(script_blocks, 4)
        assert [b[0] for w in windows for b in w] == [b[0] for b in script_blocks]

    def test_flat_text_is_one_block_per_line_in_both(self, jev_query):
        text = "ERROR a\nINFO b"
        assert [b[0] for b in jev_query.split_blocks(text)] == ["ERROR a", "INFO b"]
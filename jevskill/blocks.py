"""Group structured text into blocks, so a gate can judge a unit instead of a line.

This exists because of a measured loss. In the A/B suite, the `yaml_drift` workload
asks which feature flag is enabled in prod but disabled by default. Gating it
line by line scored **0/3** while a plain structural filter scored 3/3, because
a line-level question cannot express the judgement at all:

    flag_0512:
      default: false      <- the pair only means something together
      prod: true

The gate was asked "does the line at `L{i}` match this description?", and no single
line matches a *comparison between two lines*. Even when the model flagged
`prod: true`, the kept line contained no flag name, so the answer was not in the
reduced context either.

A block is a line at some indentation plus every following line indented deeper than
it — which is exactly the unit YAML, JSON and tracebacks are built from. Flat text
degrades to one line per block, so block mode is a strict generalisation of line
mode rather than a special case for one file type.

The file is copied into the bundled zero-install script (same rule as `PROVIDERS`),
so the two copies are kept in step by a drift test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def indent_of(line: str) -> int:
    """Leading whitespace width.

    Spaces and tabs both count as one character each. A mixed-indent file therefore
    groups by whatever it actually contains instead of by what it should contain —
    the parser never needs to be right about tabs, only consistent.
    """
    return len(line) - len(line.lstrip())


@dataclass(frozen=True)
class Block:
    """One structural unit: a header line and everything nested under it."""

    lines: tuple[str, ...]
    indent: int
    #: Index of the block's first line in the original input, so a kept block can be
    #: traced back and a rejected one recovered.
    start: int

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def key(self) -> str:
        """A short label for the block: its header without the trailing colon.

        Used in the question and in reports. It is not guaranteed unique — two
        blocks can share a header — so it is a label for a human, never a cache key.
        """
        first = self.lines[0].strip()
        return first[:-1].rstrip() if first.endswith(":") else first

    @property
    def size(self) -> int:
        return len(self.lines)


def is_header(line: str) -> bool:
    """A line that introduces a mapping: `key:` with nothing after the colon.

    This is what distinguishes a *container* (`flags:`) from a *leaf* (`flag_0512:`
    with scalar properties). Grouping purely by indentation failed on the real
    fixture: `flags:` sits at indent 0, so it swallowed all 520 flags into a single
    block and the gate had nothing to isolate. The header rule keeps each leaf
    mapping its own unit.
    """
    return line.rstrip().endswith(":")


def split_blocks(text: str | Sequence[str]) -> list[Block]:
    """Split text into structural units.

    A block is a header line plus the scalar lines nested under it, stopping at the
    next deeper *header* — which begins its own block. So a YAML document gives one
    block per leaf mapping, and a container header stays a one-line block.

    Flat text — a log, a CSV, one HTML element per line — has no headers, so every
    line is its own block. That is the previous line-level behaviour exactly, which
    makes block mode a generalisation rather than a special case.
    """
    lines = text.splitlines() if isinstance(text, str) else list(text)
    blocks: list[Block] = []
    index = 0
    total = len(lines)
    while index < total:
        if not lines[index].strip():
            index += 1
            continue
        head = lines[index]
        base = indent_of(head)
        group = [head]
        start = index
        cursor = index + 1
        if is_header(head):
            while cursor < total:
                current = lines[cursor]
                if not current.strip():
                    cursor += 1
                    continue
                if indent_of(current) <= base or is_header(current):
                    break
                group.append(current)
                cursor += 1
        blocks.append(Block(tuple(group), base, start))
        index = cursor
    return blocks


def pack_blocks(blocks: Sequence[Block], max_lines: int) -> list[list[Block]]:
    """Group blocks into windows of at most ``max_lines`` lines.

    Crucially this never splits a block across windows. Slicing raw lines did, which
    separates a header from its values and makes the comparison unanswerable for a
    reason that has nothing to do with the model.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    windows: list[list[Block]] = []
    current: list[Block] = []
    used = 0
    for block in blocks:
        if current and used + block.size > max_lines:
            windows.append(current)
            current, used = [], 0
        current.append(block)
        used += block.size
    if current:
        windows.append(current)
    return windows


def as_state(window: Sequence[Block]) -> dict[str, str]:
    """The state for one window: block id -> block text.

    Naming each block ``B{i}`` and putting its *whole* text in the state is what
    makes the question answerable. The model can see `default` and `prod` together,
    and the kept block carries its header, so the answer survives the reduction.
    """
    return {f"B{i}": block.text for i, block in enumerate(window)}
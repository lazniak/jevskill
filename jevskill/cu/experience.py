"""The operator's memory: what worked, what did not, and what to skip next time.

The first live save from the panel (``bench/cu_live_save_run.json``, 2026-09-21)
took 60.1 s. Jev decided in ~5 s of it. Twelve calls to the planning model took
~33 s, and waiting for the user's hands to pause took 10 s. Every one of those
twelve calls answered a question an earlier attempt had already answered: how
this command splits into goals, that Notepad opens its Save As dialog on
``ctrl+shift+s``, that the file-name field takes a whole path, that a title
reading ``hello.txt - Notatnik`` means the goal is done. Nothing was kept. The
next run would have paid the same 33 s to be told the same things.

This module is what is kept. Four kinds of knowledge, each keyed so that a hit
is a decision made by code, not a judgement:

``plans``    command template -> the goals that completed it. A hit skips the
             planning call (6.3 s in that run). The same section holds goal
             trees (keyed by the command template) and phase decompositions
             (keyed by app and phase goal template) under their own key
             prefixes; see :data:`TREE_PREFIX`.
``recipes``  (application, goal template) -> the actions that completed the
             goal, each with the identity of the control it acted on. A hit
             replays the goal with no Jev call and no model call; every step is
             still checked (the control must be there, the screen must change)
             and the goal is still verified before it counts as done.
``lessons``  (application, goal template, control, op) -> how it failed: the
             action did nothing, or the control refused it. Lessons go into the
             model's prompts ("already tried, did nothing"), so it stops buying
             the same wrong answer; the model was asked to scroll a navigation
             pane that refused ScrollItem twice, in two separate attempts.
``checks``   what a finished goal looked like — the window title after it, the
             value a field was left holding — so the next run verifies the goal
             in code instead of asking the model (2-3 s per verify).

**Templates, not strings.** A plan learned from ``wpisz „hello world” i zapisz
jako hello.txt`` must serve ``wpisz „lista” i zapisz jako zakupy.txt``, and must
*not* type ``hello.txt`` into the second run's file-name field. Values the user
supplied — quoted text and file names — are cut out into slots
(:func:`extract_slots`) and put back from the current command (:func:`fill`).
Anything that is not a slot stays literal, so a command that differs in any
other word misses and is planned fresh. A miss costs one model call; a false hit
types the wrong thing into someone's document.

**Only a real desktop teaches.** A dry run reads memory (so the panel can show
what would be replayed) and never writes it. A replay that fails is recorded as
a failure, and a recipe that fails twice in a row stops being offered until a
run completes its goal again and relearns it.

**Where it lives.** Next to the ledger when the caller names a ledger root, else
``~/.jevskill/cu_experience.json`` — machine-wide, for the reason
:mod:`jevskill.cu.macros` gives: a recipe is a fact about an application's UI,
not about a repository. The save merges with the file rather than overwriting
it, with tombstones for what was forgotten, the same way the macro cache does.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .agenda import TITLE_SEPARATOR, UNSAVED_DOTS, is_unsaved_title
from .hashing import tree_hash
from .macros import goal_fingerprint
from .reduce import candidates as reduce_candidates
from .types import UIElement, as_elements

SCHEMA = 1
DEFAULT_PATH = Path.home() / ".jevskill" / "cu_experience.json"
FILE_NAME = "cu_experience.json"

#: A recipe or plan that failed this many times in a row is not offered again
#: until a run completes the goal the ordinary way and relearns it. Two, not
#: one: a single failure is as likely to be the user's hand on the window as a
#: changed UI, and one retry costs a step, not a run.
DEMOTE_AFTER = 2
#: Bounds, as sizes rather than measurements: a recipe is a few hundred bytes.
MAX_RECIPES = 1000
MAX_PLANS = 500
MAX_LESSONS = 2000
#: Lesson lines handed to a model prompt. Enough to say "not that, not that";
#: more is prompt tokens spent on history.
PROMPT_LESSONS = 8
PROMPT_SKILLS = 12
SECTIONS = ("recipes", "plans", "lessons")
#: Goal trees and phase decompositions are rows of ``plans`` under these
#: prefixes, not a new section. An older build knows only :data:`SECTIONS` and
#: merges the ``plans`` dict wholesale, so it carries these rows through its
#: own saves untouched; a new section would be dropped by the first save of an
#: old build still running in another process. ``command_key`` can never
#: produce a ``\x1f``: the fingerprint re-joins ``str.split()``, and Python
#: counts the unit separator as whitespace. So ``plan_for(command)`` cannot hand
#: a tree to a build that would treat it as a flat list of goals.
TREE_PREFIX = "tree\x1f"
PHASE_PREFIX = "phase\x1f"
#: The keys of a plan item that are copied as they are: they name no value the
#: user supplied, so templating them could only corrupt them.
_ITEM_PLAIN = ("kind", "app", "weight", "optional", "irreversible")
#: How long a "forget" is remembered, so a stale copy in another process's
#: memory cannot write the entry back. Ninety days is a size, not a measurement.
FORGET_KEEP_S = 90 * 24 * 3600.0
MAX_TOMBSTONES = 5000

_QUOTES = re.compile(r'"([^"\n]{1,400})"|„([^”"\n]{1,400})[”"]|«([^»\n]{1,400})»|`([^`\n]{1,400})`')
#: A file name the user typed without quotes: ``hello.txt``, ``raport_2026.docx``.
_FILE = re.compile(r"(?<![\w.%\\/:-])([\w][\w\-]*\.[A-Za-z0-9]{1,5})(?![\w.])")
#: ``'Notatnik'`` in a model-written ``done_when``, not the apostrophe in "editor's".
_SINGLE = re.compile(r"(?<!\w)'([^'\n]{2,200})'(?!\w)")
_MARK = re.compile(r"⟦([gc])(\d+)⟧")


# --------------------------------------------------------------------------- #
# Slots and templates
# --------------------------------------------------------------------------- #


def extract_slots(text: str) -> List[str]:
    """The values a user supplied in ``text``, in order: quoted text, then
    unquoted file names. Duplicates are dropped; the first occurrence wins."""
    text = str(text or "")
    out: List[str] = []
    spans: List[Tuple[int, int]] = []
    for match in _QUOTES.finditer(text):
        value = next(g for g in match.groups() if g is not None)
        spans.append(match.span())
        if value.strip() and value not in out:
            out.append(value)
    for match in _FILE.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        value = match.group(1)
        if value not in out:
            out.append(value)
    return out


def make_template(text: Optional[str], goal_slots: Sequence[str] = (),
                  command_slots: Sequence[str] = ()) -> Optional[str]:
    """``text`` with each slot value replaced by a marker (``⟦g0⟧``, ``⟦c1⟧``).

    Goal slots first — they travel with the goal key, so they always line up at
    replay — then command slots for whatever is left. Longest value first, so
    ``hello`` inside ``hello world`` cannot split a marker.
    """
    if text is None:
        return None
    out = str(text)
    pairs = [("g", i, v) for i, v in enumerate(goal_slots)] + \
            [("c", i, v) for i, v in enumerate(command_slots)]
    pairs.sort(key=lambda item: len(item[2]), reverse=True)
    for kind, index, value in pairs:
        if value and value in out:
            out = out.replace(value, "⟦%s%d⟧" % (kind, index))
    return out


def fill(template: Optional[str], goal_slots: Sequence[str] = (),
         command_slots: Optional[Sequence[str]] = ()) -> Optional[str]:
    """Put the current values back. ``None`` when a marker has no value — or
    when it needs a command slot and ``command_slots`` is ``None``, which is how
    a caller says "the command is not the one this was learned from"."""
    if template is None:
        return None
    missing = []

    def replace(match: "re.Match[str]") -> str:
        kind, index = match.group(1), int(match.group(2))
        source = goal_slots if kind == "g" else command_slots
        if source is None or index >= len(source):
            missing.append(match.group(0))
            return ""
        return str(source[index])

    out = _MARK.sub(replace, str(template))
    return None if missing else out


def has_markers(template: Optional[str], kind: Optional[str] = None) -> bool:
    if not template:
        return False
    return any(kind is None or m.group(1) == kind for m in _MARK.finditer(template))


def display_template(template: str) -> str:
    """``⟦c0⟧`` as ``«1»`` — what a model prompt or the panel shows."""
    return _MARK.sub(lambda m: "«%d»" % (int(m.group(2)) + 1), str(template or ""))


def command_key(command: str) -> Tuple[str, List[str]]:
    """``(key, values)``: the command's template fingerprint and its slots."""
    values = extract_slots(command)
    return goal_fingerprint(make_template(command, (), values) or ""), values


def goal_key(goal: str) -> Tuple[str, List[str]]:
    values = extract_slots(goal)
    return goal_fingerprint(make_template(goal, values, ()) or ""), values


def tree_key(command: str) -> str:
    """The ``plans`` row of a goal tree learned for this command template."""
    return TREE_PREFIX + command_key(command)[0]


def phase_key(app: str, goal: str) -> str:
    """The ``plans`` row of how this phase goal split into children in ``app``.

    Keyed by the phase goal, not by the mission: "Save the document as
    hello.txt" is the same three children inside any command that contains it.
    """
    return PHASE_PREFIX + _app_key(app) + "\x1f" + goal_key(goal)[0]


def _template_items(items: Iterable[Any], goal_slots: Sequence[str] = (),
                    command_slots: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Nested plan items with every user-supplied value cut out into a marker.

    Templated: ``goal``, ``done_when``, ``launch`` (when a string) and each
    check's ``arg`` and ``text`` — a check that still said ``hello.txt`` would
    pass or fail the next run on the wrong file. An item with no goal is
    dropped with its children: it cannot be run, only guessed at.
    """
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        row: Dict[str, Any] = {
            "goal": make_template(goal, goal_slots, command_slots),
            "done_when": make_template(str(item.get("done_when") or ""), goal_slots,
                                       command_slots),
        }
        launch = item.get("launch")
        row["launch"] = (make_template(launch, goal_slots, command_slots)
                         if isinstance(launch, str) else launch)
        for key in _ITEM_PLAIN:
            if key in item:
                row[key] = item[key]
        checks = item.get("checks")
        if isinstance(checks, list):
            row["checks"] = [
                {"kind": check.get("kind"),
                 "arg": make_template(check.get("arg"), goal_slots, command_slots)
                 if isinstance(check.get("arg"), str) else check.get("arg"),
                 "text": make_template(check.get("text"), goal_slots, command_slots)
                 if isinstance(check.get("text"), str) else check.get("text")}
                for check in checks if isinstance(check, dict)]
        children = item.get("children")
        if isinstance(children, list):
            row["children"] = _template_items(children, goal_slots, command_slots)
        out.append(row)
    return out


def _item_texts(items: Iterable[Any]) -> Iterable[Tuple[str, str]]:
    """``(field, text)`` for every string of nested plan items a replay would
    act on or check — the fields :func:`_template_items` templates; a check's
    strings come as ``check``.

    Not ``app``, although it is copied as it is: the app is part of
    :func:`phase_key`, so a decomposition learned in ``notepad.exe`` only ever
    serves ``notepad.exe`` and cannot carry that name into a mission for
    another app. Scanning it refused every decomposition of a command that
    named the executable ("Otwórz notepad.exe i zapisz jako hello.txt").
    """
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in ("goal", "done_when", "launch"):
            if isinstance(item.get(key), str):
                yield key, item[key]
        for check in item.get("checks") or []:
            if isinstance(check, dict):
                for key in ("arg", "text"):
                    if isinstance(check.get(key), str):
                        yield "check", check[key]
        children = item.get("children")
        if isinstance(children, list):
            yield from _item_texts(children)


def _is_executable(value: str) -> bool:
    """``notepad.exe``: a program the command named, not a document it made."""
    return value.strip().casefold().endswith(".exe")


def _value_pattern(value: str) -> "re.Pattern[str]":
    """``value`` as a whole word, case-insensitively: the quoted ``a`` of a
    command must not be found inside "Save As", yet ``Hello.txt`` written by
    the model is still the user's ``hello.txt``."""
    lead = r"(?<!\w)" if re.match(r"\w", value) else ""
    tail = r"(?!\w)" if re.search(r"\w$", value) else ""
    return re.compile(lead + re.escape(value) + tail, re.IGNORECASE)


def _leaks_command_value(items: Iterable[Any], goal_slots: Sequence[str],
                         command_slots: Sequence[str]) -> bool:
    """Would templating ``items`` with ``goal_slots`` alone leave one of the
    command's values behind as a literal? The goal's own values are cut out
    first, exactly as :func:`make_template` will, so ``hello`` is not found
    inside a ``hello.txt`` that becomes ``⟦g0⟧``.

    An executable the command named is not looked for in ``launch``: the
    first child of any phase launches the app (``notepad.exe``), the same app
    the phase is keyed by, and that is how the phase works in every mission,
    not a value of this one. It is still looked for in a goal or a check."""
    extra = [v for v in command_slots if v and v.strip() and v not in goal_slots]
    if not extra:
        return False
    patterns = [(_value_pattern(v), _is_executable(v)) for v in extra]
    for field_name, text in _item_texts(items):
        rest = make_template(text, goal_slots, ()) or ""
        if any(p.search(rest) for p, exe in patterns
               if not (exe and field_name == "launch")):
            return True
    return False


def _fill_items(items: Iterable[Any], goal_slots: Sequence[str] = (),
                command_slots: Optional[Sequence[str]] = ()) -> Optional[List[Dict[str, Any]]]:
    """The inverse of :func:`_template_items`, or ``None`` if any marker has no
    value. All or nothing: a tree with one unfilled check would verify a goal
    against an empty string, and a half-filled tree is a false hit."""
    out: List[Dict[str, Any]] = []

    def put(value: Any) -> Tuple[bool, Any]:
        if not isinstance(value, str):
            return True, value
        filled = fill(value, goal_slots, command_slots)
        return filled is not None, filled

    for item in items or []:
        if not isinstance(item, dict):
            return None
        ok, goal = put(item.get("goal") or "")
        if not ok or not str(goal or "").strip():
            return None
        ok, done_when = put(item.get("done_when") or "")
        if not ok:
            return None
        ok, launch = put(item.get("launch"))
        if not ok:
            return None
        row: Dict[str, Any] = {"goal": goal, "done_when": done_when or "", "launch": launch}
        for key in _ITEM_PLAIN:
            if key in item:
                row[key] = item[key]
        checks = item.get("checks")
        if isinstance(checks, list):
            row["checks"] = []
            for check in checks:
                if not isinstance(check, dict):
                    continue
                ok_arg, arg = put(check.get("arg"))
                ok_text, text = put(check.get("text"))
                if not (ok_arg and ok_text):
                    return None
                row["checks"].append({"kind": check.get("kind"), "arg": arg, "text": text})
        children = item.get("children")
        if isinstance(children, list):
            filled_children = _fill_items(children, goal_slots, command_slots)
            if filled_children is None:
                return None
            row["children"] = filled_children
        out.append(row)
    return out


def _plan_kind(key: str) -> str:
    """``tree``, ``phase`` or ``plan`` — which kind of ``plans`` row a key is."""
    if key.startswith(TREE_PREFIX):
        return "tree"
    if key.startswith(PHASE_PREFIX):
        return "phase"
    return "plan"


def _norm_title(title: str) -> str:
    """A title without the unsaved marker and without spacing noise.

    Stripping the marker is right for "is this the same window?" — typing
    into ``hello.txt`` turns its title into ``*hello.txt`` and opens nothing
    new. It is wrong for "is the goal done?": a caller that uses a title as
    proof must also compare :func:`is_unsaved_title`, as :func:`recipe_check`
    does, or ``*hello.txt - Notatnik`` proves a save that never happened.

    The ``*`` is stripped where :func:`is_unsaved_title` reads it — at either
    edge of the document part — so ``a.png* - Paint.NET`` is the window
    ``a.png - Paint.NET`` was. Dots go everywhere: a dot used as a separator
    (``Inbox • Slack``) is dropped on both sides of a comparison alike.
    """
    text = str(title or "")
    for dot in UNSAVED_DOTS:
        text = text.replace(dot, " ")
    doc, sep, rest = text.rpartition(TITLE_SEPARATOR)
    if not sep:
        doc, rest = text, ""
    doc = doc.strip().strip("*").strip()
    return " ".join((doc + sep + rest).split()).casefold()


def _app_key(app: str) -> str:
    return str(app or "").strip().casefold()


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

Identity = Tuple[str, str, str, str]


def identity_of(el: Optional[UIElement]) -> Optional[Identity]:
    if el is None:
        return None
    return (el.role, el.name or "", el.automation_id or "", el.class_name or "")


def resolve(identity: Optional[Sequence[str]], elements: Sequence[Any], *,
            goal_slots: Sequence[str] = (),
            command_slots: Optional[Sequence[str]] = ()) -> Optional[UIElement]:
    """The control a stored identity names, in *this* snapshot, or ``None``.

    Exact identity first. Then the automation id with the role and class — a
    tab renamed from ``Bez tytułu`` to ``hello.txt`` is still the same tab. Then
    role and name, but only when exactly one control matches: two buttons named
    "Zapisz" are a question for Jev, not for a lookup.
    """
    if not identity:
        return None
    role, name_t, aid, cls = (list(identity) + ["", "", "", ""])[:4]
    name = fill(name_t, goal_slots, command_slots)
    els = as_elements(elements)
    if name is not None:
        exact = [el for el in els
                 if (el.role, el.name or "", el.automation_id or "", el.class_name or "") ==
                 (role, name, aid, cls)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            # Identical twins: which one the recipe meant is not in the
            # identity, and a replay does not guess.
            return None
    if aid:
        found = [el for el in els if el.role == role and el.automation_id == aid
                 and (el.class_name or "") == cls]
        if len(found) == 1:
            return found[0]
    if name:
        found = [el for el in els if el.role == role and (el.name or "") == name]
        if len(found) == 1:
            return found[0]
    return None


def describe(op: str, identity: Optional[Sequence[str]], key: Optional[str] = None) -> str:
    """``click button 'Zapisz'`` — one line a model or a person can read."""
    if op == "key":
        return "key %s" % (key or "?")
    if not identity:
        return op
    role, name = identity[0], display_template(identity[1] or "")
    label = name or identity[2] or identity[3] or "?"
    return "%s %s %r" % (op, role, label)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


def _learned_at(data: Dict[str, Any]) -> float:
    """When a stored row was last *learned*. A row written before the field
    existed falls back to ``last_used``: the latest a learn can have been."""
    if data.get("learned_at") is not None:
        return float(data["learned_at"])
    return float(data.get("last_used", 0.0))


def _value_row(row: Any) -> bool:
    """``[identity, text]``: a non-empty list of strings and a string.

    Anything else in the file — ``[[]]``, a three-element row, a number for the
    text — was accepted once and then crashed :func:`recipe_check` on the
    tuple unpacking, at a goal's verification (review finding #24).
    """
    return (isinstance(row, list) and len(row) == 2
            and isinstance(row[0], list) and bool(row[0])
            and all(isinstance(part, str) for part in row[0])
            and isinstance(row[1], str))


@dataclass
class RecipeStep:
    op: str
    identity: Optional[List[str]] = None
    key: Optional[str] = None
    text: Optional[str] = None
    #: ``literal`` (a quote in the goal, done_when or command) or ``compose``
    #: (the planning model wrote it). A composed text with no slot in it is
    #: only replayed for the same command template.
    text_source: str = ""
    outcome: str = "changed"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "identity": list(self.identity) if self.identity else None,
                "key": self.key, "text": self.text, "text_source": self.text_source,
                "outcome": self.outcome}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecipeStep":
        identity = data.get("identity")
        return cls(op=str(data.get("op", "")),
                   identity=[str(x) for x in identity][:4] if identity else None,
                   key=data.get("key"), text=data.get("text"),
                   text_source=str(data.get("text_source", "")),
                   outcome=str(data.get("outcome", "changed")))

    def describe(self) -> str:
        line = describe(self.op, self.identity, self.key)
        if self.op == "type" and self.text is not None:
            # Quoted by hand: repr() doubled every backslash of a Windows path.
            line += ' = "%s"' % display_template(self.text)[:60]
        return line


@dataclass
class Recipe:
    app: str
    goal: str
    example_goal: str = ""
    command: str = ""
    steps: List[RecipeStep] = field(default_factory=list)
    start_title: str = ""
    end_title: str = ""
    values: List[List[Any]] = field(default_factory=list)
    #: The goal had a ``launch`` and needed no action after it. Its only check
    #: is the window title it ended in — which is why a step-less recipe is
    #: kept for a launch and for nothing else (see :meth:`Experience.learn_recipe`).
    after_launch: bool = False
    successes: int = 0
    failures: int = 0
    streak_failed: int = 0
    learned_ms: float = 0.0
    replay_ms: float = 0.0
    created: float = 0.0
    last_used: float = 0.0
    last_error: str = ""
    #: When the recipe was last learned from a real run — not replayed, not
    #: failed. Only a learn after a "forget" brings a forgotten row back.
    learned_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.streak_failed < DEMOTE_AFTER

    def to_dict(self) -> Dict[str, Any]:
        return {"app": self.app, "goal": self.goal, "example_goal": self.example_goal,
                "command": self.command, "steps": [s.to_dict() for s in self.steps],
                "start_title": self.start_title, "end_title": self.end_title,
                "values": self.values, "after_launch": self.after_launch,
                "successes": self.successes,
                "failures": self.failures, "streak_failed": self.streak_failed,
                "learned_ms": round(self.learned_ms, 1), "replay_ms": round(self.replay_ms, 1),
                "created": self.created, "last_used": self.last_used,
                "last_error": self.last_error, "learned_at": self.learned_at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recipe":
        return cls(app=str(data.get("app", "")), goal=str(data.get("goal", "")),
                   example_goal=str(data.get("example_goal", "")),
                   command=str(data.get("command", "")),
                   steps=[RecipeStep.from_dict(s) for s in data.get("steps") or []
                          if isinstance(s, dict)],
                   start_title=str(data.get("start_title", "")),
                   end_title=str(data.get("end_title", "")),
                   values=[[list(v[0]), v[1]] for v in data.get("values") or []
                           if _value_row(v)],
                   after_launch=bool(data.get("after_launch", False)),
                   successes=int(data.get("successes", 0)),
                   failures=int(data.get("failures", 0)),
                   streak_failed=int(data.get("streak_failed", 0)),
                   learned_ms=float(data.get("learned_ms", 0.0)),
                   replay_ms=float(data.get("replay_ms", 0.0)),
                   created=float(data.get("created", 0.0)),
                   last_used=float(data.get("last_used", 0.0)),
                   last_error=str(data.get("last_error", "")),
                   learned_at=_learned_at(data))


@dataclass
class PlanMemory:
    command: str
    example: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    successes: int = 0
    failures: int = 0
    streak_failed: int = 0
    created: float = 0.0
    last_used: float = 0.0
    #: See :attr:`Recipe.learned_at`.
    learned_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.streak_failed < DEMOTE_AFTER

    def to_dict(self) -> Dict[str, Any]:
        return {"command": self.command, "example": self.example, "steps": self.steps,
                "model": self.model, "successes": self.successes, "failures": self.failures,
                "streak_failed": self.streak_failed, "created": self.created,
                "last_used": self.last_used, "learned_at": self.learned_at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanMemory":
        return cls(command=str(data.get("command", "")), example=str(data.get("example", "")),
                   steps=[dict(s) for s in data.get("steps") or [] if isinstance(s, dict)],
                   model=str(data.get("model", "")),
                   successes=int(data.get("successes", 0)),
                   failures=int(data.get("failures", 0)),
                   streak_failed=int(data.get("streak_failed", 0)),
                   created=float(data.get("created", 0.0)),
                   last_used=float(data.get("last_used", 0.0)),
                   learned_at=_learned_at(data))


@dataclass
class Lesson:
    app: str
    goal: str
    op: str
    identity: Optional[List[str]] = None
    key: Optional[str] = None
    kind: str = "unchanged"
    count: int = 0
    error: str = ""
    worked_instead: str = ""
    last_used: float = 0.0
    #: See :attr:`Recipe.learned_at`; every recorded failure is a learn.
    learned_at: float = 0.0

    def line(self) -> str:
        what = describe(self.op, self.identity, self.key)
        if self.kind == "subgoal":
            # A child goal a tree repair gave up on: the expand and repair
            # prompts must not propose the same wording again.
            text = 'sub-goal "%s" failed: %s' % (display_template(self.key or ""),
                                                 self.error[:80] or "no reason")
        elif self.kind == "act_error":
            text = "%s failed (%s)" % (what, self.error[:80] or "refused")
        elif self.kind == "refused":
            text = "%s was refused: %s" % (what, self.error[:80])
        else:
            text = "%s changed nothing" % what
        if self.count > 1:
            text += " — %d times" % self.count
        if self.worked_instead:
            text += "; what worked instead: %s" % self.worked_instead
        return text

    def to_dict(self) -> Dict[str, Any]:
        return {"app": self.app, "goal": self.goal, "op": self.op,
                "identity": list(self.identity) if self.identity else None, "key": self.key,
                "kind": self.kind, "count": self.count, "error": self.error,
                "worked_instead": self.worked_instead, "last_used": self.last_used,
                "learned_at": self.learned_at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lesson":
        identity = data.get("identity")
        return cls(app=str(data.get("app", "")), goal=str(data.get("goal", "")),
                   op=str(data.get("op", "")),
                   identity=[str(x) for x in identity][:4] if identity else None,
                   key=data.get("key"), kind=str(data.get("kind", "unchanged")),
                   count=int(data.get("count", 0)), error=str(data.get("error", "")),
                   worked_instead=str(data.get("worked_instead", "")),
                   last_used=float(data.get("last_used", 0.0)),
                   learned_at=_learned_at(data))


# --------------------------------------------------------------------------- #
# The trajectory: what one goal actually did, observed rather than assumed
# --------------------------------------------------------------------------- #


@dataclass
class Attempt:
    op: str
    identity: Optional[Identity]
    key: Optional[str]
    text: Optional[str]
    text_source: str
    pre_hash: str
    ignore: Tuple[str, ...]
    pre_title: str
    executed: bool
    error: str = ""
    changed: bool = False
    new_window: bool = False


class Trajectory:
    """Every action one goal performed, and whether the screen moved after it.

    Fed by the operator's hooks — ``acted`` from the execute hook, ``observed``
    from every observation, including the settle polls. The outcome of an
    action is decided at the *next* action (or the end of the goal): it counts
    as ``changed`` if **any** observation in between differed from the screen it
    was taken on. Deciding on the first poll would call a dialog that paints
    in 300 ms "unchanged"; waiting for the loop's own verdict would miss the
    escalation path, which does not settle.
    """

    def __init__(self, cap: int = 150) -> None:
        self.cap = cap
        self.attempts: List[Attempt] = []
        self.pending: Optional[Attempt] = None
        self.start_title: str = ""
        self.last_title: str = ""
        self.last_snapshot: Any = None
        #: Lines for :meth:`tried`, one per ``draw`` this goal performed. A draw
        #: is kept out of ``attempts`` on purpose: paint is invisible to UI
        #: Automation, so it would always read "changed nothing" — a lesson
        #: telling the next run not to draw — and never reach a recipe.
        self.drawn: List[str] = []

    @property
    def drew(self) -> bool:
        """This goal put strokes on a canvas; no recipe can replay that."""
        return bool(self.drawn)

    def _hash(self, snapshot: Any, ignore: Tuple[str, ...]) -> str:
        cands = reduce_candidates(getattr(snapshot, "elements", []) or [], self.cap)
        return tree_hash(cands, ignore=ignore)

    def observed(self, snapshot: Any) -> None:
        if snapshot is None:
            return
        title = str(getattr(snapshot, "window_title", "") or "")
        if not self.start_title and not self.attempts and self.pending is None:
            self.start_title = title
        self.last_title = title
        self.last_snapshot = snapshot
        pending = self.pending
        if pending is None or not pending.executed or pending.changed:
            return
        try:
            now = self._hash(snapshot, pending.ignore)
        except Exception:
            return
        if now != pending.pre_hash:
            pending.changed = True
            pending.new_window = _norm_title(title) != _norm_title(pending.pre_title)

    def acted(self, action: Any, snapshot: Any, ok: bool, error: str = "",
              text_source: str = "") -> None:
        from .act import settle_ignore_for

        self.close()
        op = str(getattr(action, "op", "") or "")
        if op in ("done", "blocked", "wait", ""):
            return
        if op == "draw":
            strokes = getattr(action, "strokes", None) or []
            self.drawn.append("draw %d stroke%s on the canvas — %s" % (
                len(strokes), "" if len(strokes) == 1 else "s",
                "done (paint is not visible in the tree)" if ok
                else "did not execute: %s" % str(error or "")[:80]))
            return
        target = getattr(action, "target", None)
        element = snapshot.by_id(target) if (target and snapshot is not None
                                             and hasattr(snapshot, "by_id")) else None
        ignore = settle_ignore_for(op)
        try:
            pre = self._hash(snapshot, ignore) if snapshot is not None else ""
        except Exception:
            pre = ""
        if not self.start_title and not self.attempts and snapshot is not None:
            self.start_title = str(getattr(snapshot, "window_title", "") or "")
        self.pending = Attempt(
            op=op, identity=identity_of(element), key=getattr(action, "key", None),
            text=getattr(action, "text", None), text_source=text_source,
            pre_hash=pre, ignore=ignore,
            pre_title=str(getattr(snapshot, "window_title", "") or ""),
            executed=bool(ok), error=str(error or ""))

    def close(self) -> None:
        if self.pending is not None:
            self.attempts.append(self.pending)
            self.pending = None

    def effective(self) -> List[Attempt]:
        """The actions that moved the screen, compacted: a field typed twice
        keeps its last text, and a clear (``type ""``) is not a step."""
        out: List[Attempt] = []
        for attempt in self.attempts:
            if not (attempt.executed and attempt.changed):
                continue
            if attempt.op == "type":
                out = [a for a in out if not (a.op == "type" and a.identity == attempt.identity)]
                if not attempt.text:
                    continue
            out.append(attempt)
        return out

    def failed(self) -> List[Tuple[Attempt, str]]:
        """``(attempt, what worked next)`` for every action that did not."""
        out: List[Tuple[Attempt, str]] = []
        for index, attempt in enumerate(self.attempts):
            if attempt.executed and attempt.changed:
                continue
            instead = ""
            for later in self.attempts[index + 1:]:
                if later.executed and later.changed:
                    instead = describe(later.op, later.identity, later.key)
                    break
            out.append((attempt, instead))
        return out

    def tried(self) -> List[str]:
        """This goal's attempts, one line each, for the escalation prompt."""
        lines = []
        for attempt in self.attempts + ([self.pending] if self.pending else []):
            what = describe(attempt.op, attempt.identity, attempt.key)
            if not attempt.executed:
                lines.append("%s — did not execute: %s" % (what, attempt.error[:80]))
            elif attempt.changed:
                lines.append("%s — the screen changed" % what)
            elif attempt is self.pending:
                lines.append("%s — waiting to see its effect" % what)
            else:
                lines.append("%s — changed nothing" % what)
        return (lines + self.drawn)[-10:]


# --------------------------------------------------------------------------- #
# Verification in code
# --------------------------------------------------------------------------- #


def literal_check(done_when: str, snapshot: Any) -> Optional[bool]:
    """``True`` when ``done_when``'s quoted values are visibly where it says;
    ``None`` when code cannot tell. Never ``False``: "cannot tell" goes to the
    model, it is not evidence of "not done".

    Conservative on purpose. A ``done_when`` that talks about the title is
    checked against the title only, and a title with an unsaved marker
    (``*hello.txt - Notatnik``) proves nothing: it names the file the document
    will be saved as, not one that was written. Otherwise the values must sit
    in an editor (``document`` or ``edit``) **with no dialog up** — while a
    Save As dialog is open, ``hello.txt`` sitting in its file-name field says
    nothing about whether the file was saved.
    """
    text = str(done_when or "")
    quotes = extract_slots(text) + [m.group(1) for m in _SINGLE.finditer(text)]
    quotes = [q for q in quotes if q.strip()]
    if not quotes or snapshot is None:
        return None
    lowered = text.casefold()
    raw_title = str(getattr(snapshot, "window_title", "") or "")
    title = raw_title.casefold()
    if "title" in lowered or "tytu" in lowered:
        if not all(q.casefold() in title for q in quotes):
            return None
        return None if is_unsaved_title(raw_title) else True
    elements = as_elements(getattr(snapshot, "elements", []) or [])
    if any(el.role == "dialog" for el in elements):
        return None
    values = [str(el.value).casefold() for el in elements
              if el.role in ("document", "edit") and el.value]
    if values and all(any(q.casefold() in v for v in values) for q in quotes):
        return True
    return None


def _values_are_the_end(recipe: "Recipe") -> bool:
    """May "every typed value is still in its field" prove this goal done?

    Only when typing was the goal's last act: its last step is the ``type``
    that produced one of the values. A recipe that typed ``foo`` into Find and
    then clicked "Find next" left ``foo`` in the field — and so does a run that
    typed ``foo`` and never clicked (review finding #22). What committed that
    goal was the click, and a field cannot show a click.
    """
    if not recipe.steps:
        return False
    last = recipe.steps[-1]
    if last.op != "type" or not last.identity or last.text is None:
        return False
    return any(list(row[0]) == list(last.identity) and row[1] == last.text
               for row in recipe.values)


def recipe_check(recipe: Optional["Recipe"], snapshot: Any, *,
                 goal_slots: Sequence[str] = (),
                 command_slots: Optional[Sequence[str]] = ()) -> Optional[bool]:
    """Does this screen look the way the goal's screen looked when it was done?

    The title is used only when the goal changed it (``start_title`` differs):
    a goal that types into Notepad leaves the title as it was, and "the title
    matches" would then be true before anything was typed. A goal with no steps
    (the launch already did it) is the exception — its check *is* "this is the
    window it ended in". The title must also be as saved as it was then: the
    comparison ignores the unsaved marker, so without that ``*hello.txt -
    Notatnik`` matched a learned ``hello.txt - Notatnik`` and proved a save that
    never happened (review finding #18).

    Failing that, the recorded field values — but only for a goal whose last
    step typed one of them (see :func:`_values_are_the_end`).

    Never raises: a malformed row that slipped past :meth:`Recipe.from_dict`
    is "cannot tell", not an exception at a goal's verification.
    """
    try:
        return _recipe_check(recipe, snapshot, goal_slots, command_slots)
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _recipe_check(recipe: Optional["Recipe"], snapshot: Any, goal_slots: Sequence[str],
                  command_slots: Optional[Sequence[str]]) -> Optional[bool]:
    if recipe is None or snapshot is None:
        return None
    raw_title = str(getattr(snapshot, "window_title", "") or "")
    title = _norm_title(raw_title)
    end = fill(recipe.end_title, goal_slots, command_slots)
    start = fill(recipe.start_title, goal_slots, command_slots)
    if recipe.steps:
        discriminative = bool(end) and _norm_title(end) != _norm_title(start or "")
    else:
        discriminative = bool(end) and recipe.after_launch
    if discriminative and end is not None and _norm_title(end) == title \
            and is_unsaved_title(end) == is_unsaved_title(raw_title):
        return True
    if recipe.values and _values_are_the_end(recipe):
        elements = as_elements(getattr(snapshot, "elements", []) or [])
        for identity, text_t in recipe.values:
            text = fill(text_t, goal_slots, command_slots)
            el = resolve(identity, elements, goal_slots=goal_slots, command_slots=command_slots)
            if text is None or el is None or text.casefold() not in str(el.value or "").casefold():
                return None
        return True
    return None


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class Experience:
    """Plans, recipes and lessons on disk. Thread-safe; see the module docstring."""

    def __init__(self, path: Optional[Any] = None, *, autosave: bool = True) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.autosave = bool(autosave)
        self.recipes: Dict[str, Recipe] = {}
        self.plans: Dict[str, PlanMemory] = {}
        self.lessons: Dict[str, Lesson] = {}
        #: ``section -> key -> when it was forgotten``, written to the file.
        #: A merge drops every entry not *learned* since its tombstone, so a
        #: "forget" in the console survives another process's save — the macro
        #: cache's in-process tombstones did not, and a test here proved it.
        #: Relearning after the forget (a newer ``learned_at``) brings it back;
        #: merely using a stale copy (a failure, a replay) does not.
        self._forgotten: Dict[str, Dict[str, float]] = {s: {} for s in SECTIONS}
        #: ``(inode, mtime_ns, size)`` of the file as this instance last read or
        #: wrote it; a lookup re-reads the tombstones only when it differs. The
        #: inode is there because every save replaces the file with a new one,
        #: so two saves inside one coarse mtime tick still differ.
        self._disk_stamp: Optional[Tuple[int, int, int]] = None
        self._lock = threading.RLock()
        self.load()

    @classmethod
    def for_ledger_root(cls, ledger_root: Any = None) -> "Experience":
        if ledger_root is not None:
            return cls(Path(ledger_root) / ".jevskill" / FILE_NAME)
        return cls()

    # ---- keys -------------------------------------------------------------
    @staticmethod
    def recipe_key(app: str, goal: str) -> str:
        return _app_key(app) + "\x1f" + goal_key(goal)[0]

    @staticmethod
    def lesson_key(app: str, goal: str, op: str, identity: Optional[Sequence[str]],
                   key: Optional[str]) -> str:
        ident = "\x1e".join(identity) if identity else ""
        return "\x1f".join((_app_key(app), goal_key(goal)[0], op, ident, str(key or "")))

    # ---- another process's forgets ------------------------------------------
    def _stamp(self) -> Optional[Tuple[int, int, int]]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """Take in the tombstones another process wrote since this one looked.

        The console holds one ``Experience`` for its whole life. ``jevskill cu
        memory --forget`` in a terminal wrote its tombstone to the file, and the
        console went on serving the forgotten plan from memory — and the next
        success relearned it over the tombstone (review finding #21). Every
        lookup therefore stats the file (one syscall) and, only when it changed,
        re-reads the tombstones and drops each held entry not learned since.
        New rows another process learned are not pulled in here; they arrive
        with the next save's merge, as before.
        """
        stamp = self._stamp()
        with self._lock:
            if stamp == self._disk_stamp:
                return
            disk = self._read()
            self._disk_stamp = stamp
            forgotten = disk.get("forgotten", {})
            for section in SECTIONS:
                mine = self._forgotten[section]
                for key, when in forgotten.get(section, {}).items():
                    if when > mine.get(key, float("-inf")):
                        mine[key] = when
                store = getattr(self, section)
                for key in [k for k, v in store.items() if v.learned_at <= mine.get(k, -1.0)]:
                    store.pop(key, None)

    # ---- plans ------------------------------------------------------------
    def plan_for(self, command: str) -> Optional[Tuple[List[Dict[str, Any]], PlanMemory]]:
        """The remembered goals for this command, filled with its values."""
        key, values = command_key(command)
        self._refresh()
        with self._lock:
            memory = self.plans.get(key)
        if memory is None or not memory.usable or not memory.steps:
            return None
        steps = []
        for item in memory.steps:
            goal = fill(item.get("goal"), (), values)
            if not goal:
                return None
            steps.append({"goal": goal, "done_when": fill(item.get("done_when") or "", (), values) or "",
                          "launch": item.get("launch")})
        return steps, memory

    def learn_plan(self, command: str, steps: Iterable[Dict[str, Any]], model: str = "") -> None:
        key, values = command_key(command)
        items = []
        for step in steps:
            goal = str(step.get("goal") or "").strip()
            if not goal:
                continue
            items.append({"goal": make_template(goal, (), values),
                          "done_when": make_template(str(step.get("done_when") or ""), (), values),
                          "launch": step.get("launch")})
        if not items:
            return
        now = time.time()
        with self._lock:
            old = self.plans.get(key)
            memory = PlanMemory(command=key, example=str(command)[:300], steps=items,
                                model=model or (old.model if old else ""),
                                successes=(old.successes if old else 0) + 1,
                                failures=old.failures if old else 0, streak_failed=0,
                                created=old.created if old else now, last_used=now,
                                learned_at=now)
            self.plans[key] = memory
            self._bound(self.plans, MAX_PLANS)
        self._autosave()

    def plan_failed(self, command: str) -> None:
        """Count a failure — after :meth:`_refresh`, like every path that
        touches a held row without a lookup first. The run that failed looked
        the plan up minutes ago; if another process forgot it since, the copy
        held here is stale and its failure is nobody's to record."""
        key, _ = command_key(command)
        self._refresh()
        with self._lock:
            memory = self.plans.get(key)
            if memory is None:
                return
            memory.failures += 1
            memory.streak_failed += 1
            memory.last_used = time.time()
        self._autosave()

    # ---- trees and decompositions ------------------------------------------
    def _store_plan(self, key: str, command: str, example: str,
                    items: List[Dict[str, Any]], model: str) -> None:
        """One ``plans`` row, counted exactly as :meth:`learn_plan` counts it."""
        now = time.time()
        with self._lock:
            old = self.plans.get(key)
            self.plans[key] = PlanMemory(
                command=command, example=example[:300], steps=items,
                model=model or (old.model if old else ""),
                successes=(old.successes if old else 0) + 1,
                failures=old.failures if old else 0, streak_failed=0,
                created=old.created if old else now, last_used=now, learned_at=now)
            self._bound(self.plans, MAX_PLANS)
        self._autosave()

    def _row_failed(self, key: str) -> None:
        """A tree's or a decomposition's failure; see :meth:`plan_failed`."""
        self._refresh()
        with self._lock:
            memory = self.plans.get(key)
            if memory is None:
                return
            memory.failures += 1
            memory.streak_failed += 1
            memory.last_used = time.time()
        self._autosave()

    def _usable_steps(self, key: str) -> Optional[PlanMemory]:
        self._refresh()
        with self._lock:
            memory = self.plans.get(key)
        if memory is None or not memory.usable or not memory.steps:
            return None
        return memory

    def learn_tree(self, command: str, items: Iterable[Dict[str, Any]], model: str = "") -> None:
        """Keep the goal tree that completed ``command``.

        ``items`` are the root's children that finished done, in order, each
        with its own done children nested. Templated with the **command's**
        slots: the tree is a fact about this command template, and a phase's
        own values are the command's values in the first place.
        """
        _, values = command_key(command)
        rows = _template_items(items, (), values)
        if not rows:
            return
        self._store_plan(tree_key(command), tree_key(command),
                         "tree: " + str(command)[:290], rows, model)

    def tree_for(self, command: str) -> Optional[Tuple[List[Dict[str, Any]], PlanMemory]]:
        """The remembered tree for this command, filled with its values."""
        memory = self._usable_steps(tree_key(command))
        if memory is None:
            return None
        filled = _fill_items(memory.steps, (), command_key(command)[1])
        if not filled:
            return None
        return filled, memory

    def tree_failed(self, command: str) -> None:
        self._row_failed(tree_key(command))

    def learn_decomposition(self, app: str, goal: str, children: Iterable[Dict[str, Any]],
                            model: str = "", *, command: str = "") -> bool:
        """Keep how a phase goal split into children in ``app``. ``True`` when
        it was stored.

        Templated with the **phase goal's** slots (``g`` markers), so "Save the
        document as ⟦g0⟧" serves any file name, inside any mission that
        contains the phase. A child value that is not in the phase goal stays
        literal, and a phase whose goal differs in any other word misses.

        That literal is only safe when it is not the user's. The phase "Save
        the document on the Desktop" names no file, yet its child "Type
        hello.txt into the file name field" does — the value came from the
        command. Stored, it served the next mission's identical phase, and that
        mission typed ``hello.txt`` again (review finding #19). So when the
        mission's ``command`` is given, a decomposition whose children carry
        any of the command's values that the phase goal does not is refused:
        the next mission pays one expand call instead of saving the wrong file.
        """
        if not _app_key(app):
            return False
        items = [item for item in children or [] if isinstance(item, dict)]
        goal_slots = goal_key(goal)[1]
        if command and _leaks_command_value(items, goal_slots, command_key(command)[1]):
            return False
        key = phase_key(app, goal)
        rows = _template_items(items, goal_slots, ())
        if not rows:
            return False
        self._store_plan(key, key, "phase: " + str(goal)[:290], rows, model)
        return True

    def decomposition_for(self, app: str, goal: str
                          ) -> Optional[Tuple[List[Dict[str, Any]], PlanMemory]]:
        if not _app_key(app):
            return None
        memory = self._usable_steps(phase_key(app, goal))
        if memory is None:
            return None
        # ``None`` for the command slots: a decomposition never learned one,
        # and a ``c`` marker in it would be a value from some other mission.
        filled = _fill_items(memory.steps, goal_key(goal)[1], None)
        if not filled:
            return None
        return filled, memory

    def decomposition_failed(self, app: str, goal: str) -> None:
        if not _app_key(app):
            return
        self._row_failed(phase_key(app, goal))

    def learn_subgoal_lesson(self, app: str, phase_goal: str, child_goal: str,
                             stop_reason: Any) -> None:
        """A child goal a tree repair superseded, so the next expand of this
        phase does not propose the same child again."""
        if not _app_key(app):
            return
        child = goal_key(child_goal)[0][:120]
        key = self.lesson_key(app, phase_goal, "subgoal", None, child)
        now = time.time()
        with self._lock:
            lesson = self.lessons.get(key) or Lesson(
                app=_app_key(app), goal=goal_key(phase_goal)[0], op="subgoal",
                key=child, kind="subgoal")
            lesson.kind = "subgoal"
            lesson.error = str(stop_reason if stop_reason is not None else "")[:200]
            lesson.count += 1
            lesson.last_used = now
            lesson.learned_at = now
            self.lessons[key] = lesson
            self._bound(self.lessons, MAX_LESSONS)
        self._autosave()

    # ---- recipes ----------------------------------------------------------
    def recipe_for(self, app: str, goal: str, *, usable_only: bool = True) -> Optional[Recipe]:
        self._refresh()
        with self._lock:
            recipe = self.recipes.get(self.recipe_key(app, goal))
        if recipe is None or (usable_only and not recipe.usable):
            return None
        return recipe

    def learn_recipe(self, app: str, goal: str, command: str, trajectory: Trajectory, *,
                     wall_ms: float = 0.0, end_title: str = "", launched: bool = False,
                     replayed: bool = False) -> Optional[Recipe]:
        """Keep what completed the goal. ``replayed`` counts a success for a
        recipe that was just replayed rather than learning it anew.

        A goal that completed with **no** action is kept only after a launch.
        Otherwise it is a goal an earlier goal happened to finish — "type the
        text" after a goal that already typed it — and a step-less recipe for
        it would replay as "nothing to do" and then check a title that reads
        the same whether or not anything was typed.
        """
        if not app or not goal:
            return None
        if getattr(trajectory, "drew", False):
            # The strokes are not in `effective()` (see Trajectory.drawn), so
            # the recipe would replay the tool clicks around them and call an
            # empty canvas a finished picture.
            return None
        gkey, gslots = goal_key(goal)
        ckey, cslots = command_key(command)
        steps: List[RecipeStep] = []
        values: List[List[Any]] = []
        for attempt in trajectory.effective():
            identity = None
            if attempt.identity:
                identity = list(attempt.identity)
                identity[1] = make_template(identity[1], gslots, cslots) or ""
            text = make_template(attempt.text, gslots, cslots) if attempt.op == "type" else None
            steps.append(RecipeStep(op=attempt.op, identity=identity, key=attempt.key,
                                    text=text, text_source=attempt.text_source,
                                    outcome="new_window" if attempt.new_window else "changed"))
            if attempt.op == "type" and identity and text:
                values.append([identity, text])
        # A value that ended up in a field of a dialog that then closed is not
        # a check: the field is gone when the goal is done. Keep only fields
        # still on the final screen.
        final = getattr(trajectory, "last_snapshot", None)
        if final is not None and values:
            elements = as_elements(getattr(final, "elements", []) or [])
            values = [v for v in values
                      if resolve(v[0], elements, goal_slots=gslots, command_slots=cslots) is not None]
        rkey = _app_key(app) + "\x1f" + gkey
        if replayed:
            # The replayed recipe was looked up when the goal began. Forgotten
            # by another process since, the copy held here is stale: counting
            # the success on it is the stale use #21 was about, and learning
            # the replay's own steps anew would bring the forgotten recipe
            # straight back over its tombstone. Neither: nothing is recorded.
            self._refresh()
            with self._lock:
                forgotten = rkey not in self.recipes and rkey in self._forgotten["recipes"]
            if forgotten:
                return None
        if not steps and not launched and not (replayed and rkey in self.recipes):
            return None
        now = time.time()
        with self._lock:
            old = self.recipes.get(rkey)
            if replayed and old is not None:
                old.successes += 1
                old.streak_failed = 0
                old.replay_ms = float(wall_ms)
                old.last_used = now
                old.last_error = ""
                recipe = old
            else:
                recipe = Recipe(
                    app=_app_key(app), goal=gkey, example_goal=str(goal)[:300], command=ckey,
                    steps=steps,
                    start_title=make_template(trajectory.start_title, gslots, cslots) or "",
                    end_title=make_template(end_title or trajectory.last_title, gslots, cslots) or "",
                    values=values, after_launch=bool(launched and not steps),
                    successes=(old.successes if old else 0) + 1,
                    failures=old.failures if old else 0, streak_failed=0,
                    learned_ms=float(wall_ms), replay_ms=old.replay_ms if old else 0.0,
                    created=old.created if old else now, last_used=now, learned_at=now)
                self.recipes[rkey] = recipe
            self._bound(self.recipes, MAX_RECIPES)
        self._autosave()
        return recipe

    def recipe_failed(self, app: str, goal: str, error: str) -> None:
        """See :meth:`plan_failed`: a forgotten recipe's failure is not counted."""
        self._refresh()
        with self._lock:
            recipe = self.recipes.get(self.recipe_key(app, goal))
            if recipe is None:
                return
            recipe.failures += 1
            recipe.streak_failed += 1
            recipe.last_error = str(error)[:200]
            recipe.last_used = time.time()
        self._autosave()

    # ---- lessons ----------------------------------------------------------
    def learn_lessons(self, app: str, goal: str, trajectory: Trajectory) -> int:
        """Every action of this goal that did nothing or was refused."""
        count = 0
        gkey, gslots = goal_key(goal)
        now = time.time()
        with self._lock:
            for attempt, instead in trajectory.failed():
                identity = None
                if attempt.identity:
                    identity = list(attempt.identity)
                    identity[1] = make_template(identity[1], gslots, ()) or ""
                key = self.lesson_key(app, goal, attempt.op, identity, attempt.key)
                lesson = self.lessons.get(key) or Lesson(
                    app=_app_key(app), goal=gkey, op=attempt.op, identity=identity,
                    key=attempt.key)
                lesson.kind = "unchanged" if attempt.executed else "act_error"
                lesson.error = attempt.error[:200]
                lesson.count += 1
                if instead:
                    lesson.worked_instead = instead
                lesson.last_used = now
                lesson.learned_at = now
                self.lessons[key] = lesson
                count += 1
            self._bound(self.lessons, MAX_LESSONS)
        if count:
            self._autosave()
        return count

    def lessons_for(self, app: str, goal: Optional[str] = None,
                    limit: int = PROMPT_LESSONS) -> List[str]:
        """Lines for a prompt: this goal's lessons first, then the app's."""
        app_k = _app_key(app)
        gkey = goal_key(goal)[0] if goal else None
        self._refresh()
        with self._lock:
            mine = [l for l in self.lessons.values() if l.app == app_k]
        mine.sort(key=lambda l: (l.goal != gkey, -l.count, -l.last_used))
        return [(l.line() if l.goal == gkey else "(another goal) " + l.line())
                for l in mine[:max(0, int(limit))]]

    def skills_for(self, app: Optional[str] = None, limit: int = PROMPT_SKILLS) -> List[str]:
        """Goals this machine has completed before, in the words that hit."""
        self._refresh()
        with self._lock:
            recipes = [r for r in self.recipes.values()
                       if r.usable and (app is None or r.app == _app_key(app))]
        recipes.sort(key=lambda r: -r.last_used)
        out = []
        for recipe in recipes[:max(0, int(limit))]:
            how = "; ".join(s.describe() for s in recipe.steps[:4]) or "nothing to do after the launch"
            out.append("%s: %r — %s (%d success%s)" % (
                recipe.app, display_template(recipe.example_goal and
                                             make_template(recipe.example_goal,
                                                           goal_key(recipe.example_goal)[1], ())
                                             or recipe.goal),
                how, recipe.successes, "" if recipe.successes == 1 else "es"))
        return out

    # ---- reporting and forgetting -----------------------------------------
    def stats(self) -> Dict[str, Any]:
        self._refresh()
        with self._lock:
            return {
                "path": str(self.path),
                "plans": len(self.plans), "recipes": len(self.recipes),
                "lessons": len(self.lessons),
                "usable_recipes": sum(1 for r in self.recipes.values() if r.usable),
                "replays": sum(r.successes for r in self.recipes.values()),
            }

    def listing(self, limit: int = 50) -> Dict[str, Any]:
        self._refresh()
        with self._lock:
            recipes = sorted(self.recipes.items(), key=lambda kv: -kv[1].last_used)[:limit]
            plans = sorted(self.plans.items(), key=lambda kv: -kv[1].last_used)[:limit]
            lessons = sorted(self.lessons.items(), key=lambda kv: -kv[1].last_used)[:limit]
            return {
                "stats": self.stats(),
                "recipes": [{"key": k, "app": r.app, "goal": r.example_goal or r.goal,
                             "steps": [s.describe() for s in r.steps],
                             "successes": r.successes, "failures": r.failures,
                             "usable": r.usable, "learned_ms": round(r.learned_ms),
                             "replay_ms": round(r.replay_ms), "last_used": r.last_used,
                             "last_error": r.last_error}
                            for k, r in recipes],
                # ``goals`` is the top level of ``steps`` in all three kinds:
                # a tree's phases, a decomposition's children, a plan's goals.
                "plans": [{"key": k, "kind": _plan_kind(k), "command": p.example or p.command,
                           "goals": [display_template(s.get("goal", "")) for s in p.steps],
                           "successes": p.successes, "failures": p.failures,
                           "usable": p.usable, "model": p.model, "last_used": p.last_used}
                          for k, p in plans],
                "lessons": [{"key": k, "app": l.app, "line": l.line(), "count": l.count,
                             "last_used": l.last_used}
                            for k, l in lessons],
            }

    def forget(self, kind: str, key: str) -> bool:
        section = {"recipe": "recipes", "plan": "plans", "lesson": "lessons"}.get(kind, kind)
        if section not in SECTIONS:
            raise ValueError("kind is recipe, plan or lesson")
        store = getattr(self, section)
        with self._lock:
            existed = store.pop(key, None) is not None
            self._forgotten[section][key] = time.time()
        self._autosave()
        return existed

    def clear(self) -> int:
        """Forget everything — including what another process learned since
        this one loaded. Tombstoning only the keys held in memory let a plan
        the CLI learned after the console started survive the console's
        "clear" (review finding #21), so the file's keys are read and
        tombstoned too, the same way :meth:`save` reads them. The count is
        every distinct entry forgotten."""
        with self._lock:
            disk = self._read()
            now = time.time()
            count = 0
            for section in SECTIONS:
                store = getattr(self, section)
                keys = set(store) | set(disk.get(section, {}))
                for key in keys:
                    self._forgotten[section][key] = now
                count += len(keys)
                store.clear()
        self._autosave()
        return count

    # ---- persistence ------------------------------------------------------
    @staticmethod
    def _bound(store: Dict[str, Any], limit: int) -> None:
        """Evict the least recently used rows until ``store`` holds ``limit``."""
        if len(store) <= limit:
            return
        for key, _ in sorted(store.items(), key=lambda kv: kv[1].last_used)[:len(store) - limit]:
            store.pop(key, None)

    @staticmethod
    def _limit(section: str) -> int:
        """The bound of a section, read at call time (tests shrink it)."""
        return {"recipes": MAX_RECIPES, "plans": MAX_PLANS, "lessons": MAX_LESSONS}[section]

    @staticmethod
    def _merge_rows(mine: Dict[str, Any], disk: Dict[str, Any],
                    tombstones: Dict[str, float]) -> Dict[str, Any]:
        """One section of :meth:`save`: the tombstone judges each copy of a
        key on its own, and only then does the newer ``last_used`` win.

        The other order lost a relearned row. The console held plan K learned
        at t1; the CLI forgot K (tombstone t2) and relearned it (t3); the
        console's stale copy then failed a run (last used t4). It won on
        ``last_used``, failed ``learned_at > tombstone`` and was dropped — and
        took the CLI's legitimate row with it, which was never considered.
        A tie on ``last_used`` goes to this process's copy, as it always did.
        """
        merged: Dict[str, Any] = {}
        for key in list(disk) + [k for k in mine if k not in disk]:
            stone = tombstones.get(key, -1.0)
            alive = [row for row in (mine.get(key), disk.get(key))
                     if row is not None and row.learned_at > stone]
            if alive:
                merged[key] = max(alive, key=lambda row: row.last_used)
        return merged

    def _autosave(self) -> None:
        if self.autosave:
            self.save()

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for section, cls in (("recipes", Recipe), ("plans", PlanMemory), ("lessons", Lesson)):
            rows = data.get(section)
            parsed: Dict[str, Any] = {}
            if isinstance(rows, dict):
                for key, value in rows.items():
                    if isinstance(value, dict):
                        try:
                            parsed[key] = cls.from_dict(value)  # type: ignore[attr-defined]
                        except (TypeError, ValueError):
                            continue
            out[section] = parsed
        forgotten = data.get("forgotten")
        out["forgotten"] = {}
        if isinstance(forgotten, dict):
            for section in SECTIONS:
                rows = forgotten.get(section)
                if isinstance(rows, dict):
                    out["forgotten"][section] = {
                        str(k): float(v) for k, v in rows.items()
                        if isinstance(v, (int, float))}
        return out

    def load(self) -> int:
        """Read the store. A damaged or foreign file is ignored, never raised:
        a memory that cannot be read must not stop the run it was meant to speed up."""
        stamp = self._stamp()   # before the read: a write in between re-reads later
        disk = self._read()
        with self._lock:
            self._disk_stamp = stamp
            self.recipes = dict(disk.get("recipes", {}))
            self.plans = dict(disk.get("plans", {}))
            self.lessons = dict(disk.get("lessons", {}))
            forgotten = disk.get("forgotten", {})
            self._forgotten = {s: dict(forgotten.get(s, {})) for s in SECTIONS}
        return len(self.recipes) + len(self.plans) + len(self.lessons)

    def save(self) -> bool:
        """Merge into the file: a copy not *learned* since its key was
        forgotten — by any process — is dropped, each copy on its own, and of
        the copies left the newer ``last_used`` wins (:meth:`_merge_rows`).
        Used is not enough: a stale copy that merely failed or replayed
        after the forget used to outlive the tombstone (review finding #21).

        The merged sections are then bounded again. The in-memory bound after
        a learn was undone right here — every row it evicted came back from the
        file, and ``MAX_*`` bounded nothing (review finding #25).
        ``False`` on any failure."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        with self._lock:
            disk = self._read()
            now = time.time()
            payload: Dict[str, Any] = {"schema": SCHEMA, "saved": now, "forgotten": {}}
            disk_forgotten = disk.get("forgotten", {})
            for section in SECTIONS:
                tombstones = dict(disk_forgotten.get(section, {}))
                for key, when in self._forgotten[section].items():
                    tombstones[key] = max(when, tombstones.get(key, 0.0))
                tombstones = {k: v for k, v in tombstones.items()
                              if now - v < FORGET_KEEP_S}
                if len(tombstones) > MAX_TOMBSTONES:
                    tombstones = dict(sorted(tombstones.items(), key=lambda kv: kv[1])
                                      [-MAX_TOMBSTONES:])
                merged = self._merge_rows(getattr(self, section), disk.get(section, {}),
                                          tombstones)
                self._bound(merged, self._limit(section))
                setattr(self, section, merged)
                self._forgotten[section] = tombstones
                payload[section] = {k: v.to_dict() for k, v in merged.items()}
                payload["forgotten"][section] = tombstones
            try:
                handle, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                               prefix=self.path.name + ".", suffix=".tmp")
            except OSError:
                return False
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False)
                os.replace(tmp, self.path)
                # Our own write holds nothing this instance lacks: the next
                # lookup must not re-read it.
                self._disk_stamp = self._stamp()
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
        return True


__all__ = ["DEFAULT_PATH", "DEMOTE_AFTER", "Experience", "Lesson", "PHASE_PREFIX",
           "PlanMemory", "Recipe", "RecipeStep", "TREE_PREFIX", "Trajectory",
           "command_key", "describe", "display_template", "extract_slots", "fill",
           "goal_key", "has_markers", "identity_of", "literal_check", "make_template",
           "phase_key", "recipe_check", "resolve", "tree_key"]

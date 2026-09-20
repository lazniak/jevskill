"""Benchmark harness for the Windows computer-use loop (plan items 4.7 and 4.8).

Drives *an* agent through the ten tasks in `bench/cu_tasks.json`, grades every run
with that task's `oracle` (never with the agent's own verdict), and writes one row
per (task, run, mode, provider) into `bench/cu_runs.json`.

    python bench/cu_run.py                        # dry run: the default, touches nothing
    python bench/cu_run.py --dry-run --task calc_multiply --runs 1
    python bench/cu_run.py --live --i-am-not-streaming --agent jevskill.cu.loop:run

**The default is `--dry-run`, and that is deliberate.** A live run opens real
applications, types into them, steals focus, and — for `settings_dark_mode` —
flips the desktop theme of whoever is sitting there. `--live` therefore also
requires `--i-am-not-streaming`; there is no environment variable and no config
file that can supply it, because the one thing that must not be automatable is the
assertion that a human is not being filmed while the machine drives itself.

A dry run loads the task list, validates its schema, parses every PowerShell
snippet **for syntax only** (`[Parser]::ParseInput`, never an invocation), then
runs a deterministic fake agent against a fake oracle. Every number it produces is
synthetic, the file records `"mode": "dry-run"` and `"synthetic": true` on each
row, and `bench/cu_report.py` prints DRY RUN in the title of every table built
from such rows. That labelling is the whole reason a dry run is allowed to write
to the same file as a real one: the harness has to be exercisable on a machine
where the benchmark itself must not run.

Live and dry share one code path. `run_task(task, agent, setup, oracle, teardown,
opts)` takes its four side-effecting collaborators as arguments, so the ordering
guarantees that matter — teardown runs even when the agent raises; the oracle is
consulted even when the agent crashed, because a crash after the goal was reached
is still a success — are tested with fakes rather than with a desktop.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import importlib.util
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jevskill import __version__  # noqa: E402


def _load_contract():
    """Import `jevskill.cu.contract`, by path if the package init is missing.

    `jevskill/cu/__init__.py` is owned by whoever lands the loop; this harness must
    not fall over in a tree where it has not appeared yet.

    The by-path fallback is registered in ``sys.modules`` before anything else
    loads it. Two `spec_from_file_location` calls on the same file produce two
    module objects with two different `RunResult` classes, and `run_task`'s
    `isinstance` check would then reject a perfectly good result from the report's
    copy of the contract.
    """
    try:
        return importlib.import_module("jevskill.cu.contract")
    except ImportError:
        cached = sys.modules.get("jevskill_cu_contract")
        if cached is not None:
            return cached
        path = ROOT / "jevskill" / "cu" / "contract.py"
        spec = importlib.util.spec_from_file_location("jevskill_cu_contract", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["jevskill_cu_contract"] = module
        spec.loader.exec_module(module)
        return module


contract = _load_contract()
RunOptions = contract.RunOptions
RunResult = contract.RunResult
StepRecord = contract.StepRecord
TaskRun = contract.TaskRun
summarise = contract.summarise

DEFAULT_TASKS = ROOT / "bench" / "cu_tasks.json"
#: Two default outputs, because the two modes produce different *kinds* of
#: thing. A dry run's rows are synthetic and `.gitignore` drops `cu_runs.json`
#: for that reason; a live run's rows are the measurement plan item 4.7 asks
#: for, they go to the plan's own filename, and they are committed on purpose
#: the way `bench/results.json` is. One shared default would have made "commit
#: the benchmark" and "commit a fake" the same gesture.
DEFAULT_OUT_DRY = ROOT / "bench" / "cu_runs.json"
DEFAULT_OUT_LIVE = ROOT / "bench" / "cu_task_results.json"
DEFAULT_OUT = DEFAULT_OUT_DRY       # kept: older callers pass it explicitly

#: Every key `bench/cu_tasks.json` currently carries, with the type it must have.
#: Derived from the file rather than invented: `tests/test_cu_run.py` pins both
#: directions, so adding a field to the task list without teaching the harness
#: about it fails a test instead of being silently ignored by the runner.
TASK_SCHEMA: Dict[str, Any] = {
    "id": str,
    "app": str,
    "goal": str,
    "setup": list,
    "teardown": list,
    "oracle": dict,
    "human_steps": int,
    "irreversible": bool,
    "restores_state": bool,
    "coverage_risk": str,
    "notes": str,
    "expected_agent_steps_min": int,
}

#: Oracle `type` values the harness knows. `uia_value` and
#: `window_title_contains` read the live desktop through `jevskill.cu.observe`
#: (plan item 4.1), injected as two callables so the tests grade fixtures
#: instead of windows.
ORACLE_TYPES = (
    "file_exists", "file_contains", "registry_value", "all_of", "any_of",
    "window_title_contains", "uia_value", "clipboard_equals",
)
#: Still unimplemented, and refused with a reason rather than silently graded
#: false. `clipboard_equals` survives as a vocabulary entry only: no task uses
#: it, and reading the clipboard mid-benchmark would also destroy whatever the
#: person at the machine had put there.
ORACLE_NEEDS_DESKTOP = ("clipboard_equals",)

#: Every key each oracle type may carry. An oracle is a contract with the
#: harness, so a key nobody reads is dead weight at best — `calc_multiply`
#: carried a `fallback` whose own `note` said the task's goal could never reach
#: it, and it sat there unexecuted because nothing checked.
ORACLE_KEYS = {
    "file_exists": {"type", "path", "kind", "expect"},
    "file_contains": {"type", "path", "contains", "not_contains"},
    "registry_value": {"type", "hive", "path", "name", "expect"},
    "all_of": {"type", "all_of"},
    "any_of": {"type", "any_of"},
    "window_title_contains": {"type", "substring", "case_sensitive"},
    "uia_value": {"type", "window_title_contains", "automation_id", "name",
                  "find_control_type", "expect", "expect_contains"},
    "clipboard_equals": {"type", "expect"},
}
#: The ways a `uia_value` oracle may pick its element, and the ways it may judge
#: what it found. At least one of each is required.
UIA_SELECTORS = ("automation_id", "name", "find_control_type")
UIA_EXPECTATIONS = ("expect", "expect_contains")

#: Parse-only probe. `ParseInput` builds an abstract syntax tree and returns the
#: parse errors; it never executes the text it is given, which is the only reason
#: this file is allowed to hand a task's `setup` to PowerShell at all.
#:
#: Snippets go in through a temp file named by an environment variable, and the
#: result comes back through a second one. Neither the snippet nor the answer
#: crosses a command line (no quoting to get wrong, no length ceiling) or the
#: console (whose code page on this host is not UTF-8 — the parser's own messages
#: are localised and came back as mojibake when they did).
PARSE_PROBE = r"""
$ErrorActionPreference = 'Stop'
$payload = Get-Content -LiteralPath $env:JEVCU_SNIPPETS -Raw | ConvertFrom-Json
$out = New-Object System.Collections.ArrayList
foreach ($item in @($payload)) {
    $text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($item.b64))
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errors)
    $messages = @()
    foreach ($e in @($errors)) {
        $messages += ('line {0} col {1}: {2}' -f $e.Extent.StartLineNumber, $e.Extent.StartColumnNumber, $e.Message)
    }
    [void]$out.Add([pscustomobject]@{ ref = $item.ref; errors = @($messages) })
}
$json = ConvertTo-Json -InputObject @($out) -Depth 6 -Compress
[System.IO.File]::WriteAllText($env:JEVCU_RESULT, $json, (New-Object System.Text.UTF8Encoding($false)))
"""


class PhaseError(RuntimeError):
    """A `setup` or `teardown` snippet failed, timed out, or was refused."""


class OracleUnsupported(RuntimeError):
    """This oracle needs something the harness cannot evaluate here."""


# --------------------------------------------------------------------------- #
# The task list
# --------------------------------------------------------------------------- #

def load_tasks(path: Path = DEFAULT_TASKS) -> List[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_tasks(tasks: Any) -> List[str]:
    """Everything wrong with a task list, as plain sentences. Empty means clean.

    Returns rather than raises so a caller can print all of it at once: finding
    one broken task, fixing it and rediscovering the next is how a ten-task list
    takes ten runs to validate.
    """
    problems: List[str] = []
    if not isinstance(tasks, list):
        return [f"the task file must hold a list, found {type(tasks).__name__}"]
    if not tasks:
        return ["the task file holds no tasks"]

    seen: Dict[str, int] = {}
    for position, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"task[{position}] is {type(task).__name__}, not an object")
            continue
        label = task.get("id") or f"task[{position}]"
        for key, want in TASK_SCHEMA.items():
            if key not in task:
                problems.append(f"{label}: missing required field {key!r}")
                continue
            value = task[key]
            # bool is an int in Python; an `irreversible: 1` would otherwise pass.
            if want is int and isinstance(value, bool):
                problems.append(f"{label}: {key} is a bool, expected int")
            elif not isinstance(value, want):
                problems.append(
                    f"{label}: {key} is {type(value).__name__}, expected {want.__name__}")
        extra = sorted(set(task) - set(TASK_SCHEMA))
        if extra:
            problems.append(f"{label}: unknown field(s) {extra} - teach TASK_SCHEMA about them")

        task_id = task.get("id")
        if isinstance(task_id, str):
            if task_id in seen:
                problems.append(f"{label}: duplicate id, also at task[{seen[task_id]}]")
            seen[task_id] = position
            if not task_id.strip():
                problems.append(f"task[{position}]: id is blank")

        for phase in ("setup", "teardown"):
            snippets = task.get(phase)
            if isinstance(snippets, list):
                if not snippets:
                    problems.append(f"{label}: {phase} is empty - a run needs a known start state")
                for i, snippet in enumerate(snippets):
                    if not isinstance(snippet, str) or not snippet.strip():
                        problems.append(f"{label}: {phase}[{i}] is not a non-empty string")

        oracle = task.get("oracle")
        if isinstance(oracle, dict):
            problems.extend(f"{label}: {p}" for p in _validate_oracle(oracle))

        human = task.get("human_steps")
        agent_min = task.get("expected_agent_steps_min")
        if isinstance(human, int) and not isinstance(human, bool) and human < 1:
            problems.append(f"{label}: human_steps must be at least 1")
        if (isinstance(human, int) and isinstance(agent_min, int)
                and not isinstance(human, bool) and not isinstance(agent_min, bool)
                and agent_min <= human):
            # An agent observes before it acts, so it cannot beat a human who
            # already knows where the control is. A list claiming otherwise has a
            # typo, and the step-count column would flatter the agent.
            problems.append(
                f"{label}: expected_agent_steps_min ({agent_min}) is not above "
                f"human_steps ({human})")
    return problems


def _validate_oracle(oracle: dict, *, depth: int = 0) -> List[str]:
    problems: List[str] = []
    kind = oracle.get("type")
    if kind not in ORACLE_TYPES:
        return [f"oracle type {kind!r} is not one of {list(ORACLE_TYPES)}"]
    if kind in ("all_of", "any_of"):
        inner = oracle.get(kind)
        if not isinstance(inner, list) or not inner:
            return [f"oracle {kind} must hold a non-empty list"]
        if depth > 3:
            return ["oracle nests more than three deep"]
        for i, sub in enumerate(inner):
            if not isinstance(sub, dict):
                problems.append(f"oracle {kind}[{i}] is not an object")
                continue
            problems.extend(f"{kind}[{i}]: {p}" for p in _validate_oracle(sub, depth=depth + 1))
        return problems
    required = {
        "file_exists": ("path",),
        "file_contains": ("path",),
        "registry_value": ("hive", "path", "name"),
        "window_title_contains": ("substring",),
        "uia_value": (),
        "clipboard_equals": ("expect",),
    }[kind]
    for key in required:
        if key not in oracle:
            problems.append(f"oracle {kind} is missing {key!r}")
    extra = sorted(set(oracle) - ORACLE_KEYS[kind])
    if extra:
        problems.append(f"oracle {kind} carries key(s) nothing reads: {extra}")
    if kind == "file_contains" and not ({"contains", "not_contains"} & set(oracle)):
        problems.append("oracle file_contains needs 'contains' or 'not_contains'")
    if kind == "uia_value":
        if not set(UIA_SELECTORS) & set(oracle):
            problems.append(f"oracle uia_value needs one of {list(UIA_SELECTORS)} "
                            "to pick an element")
        if not set(UIA_EXPECTATIONS) & set(oracle):
            problems.append(f"oracle uia_value needs one of {list(UIA_EXPECTATIONS)}")
    return problems


def order_tasks(tasks: Sequence[dict], rng: Optional[random.Random]) -> List[dict]:
    """Task order for one repeat: shuffled, with state-changing tasks pushed last.

    `bench/cu_tasks.md` asks for both. Randomising the order keeps a cold first
    call (`warm()`, cold caches) from always landing on the same task. Forcing
    `restores_state` tasks to the end keeps `settings_dark_mode` — which repaints
    the whole desktop while it runs — out of the middle of a sequence someone may
    be recording.
    """
    ordered = list(tasks)
    if rng is not None:
        rng.shuffle(ordered)
    return ([t for t in ordered if not t.get("restores_state")]
            + [t for t in ordered if t.get("restores_state")])


# --------------------------------------------------------------------------- #
# PowerShell: syntax only
# --------------------------------------------------------------------------- #

def powershell_path() -> Optional[str]:
    """`pwsh` on PATH, or None. Windows PowerShell 5.1 is deliberately not a
    fallback: the tasks are written for 7's parser and a 5.1 verdict would be a
    different answer wearing the same label."""
    return shutil.which("pwsh")


def check_powershell_syntax(tasks: Sequence[dict], *, pwsh: Optional[str] = None,
                            timeout_s: float = 120.0) -> Dict[str, Any]:
    """Parse every `setup`/`teardown` snippet. **Parses. Never executes.**

    One `pwsh` process for all of them: process start dominates (~700 ms here) and
    forty-odd starts would put a syntax check on the wrong side of "cheap enough
    to run every time".

    Returns ``{"available": bool, "checked": int, "failures": [...], "reason": str}``.
    A missing `pwsh` is ``available: False`` and is not an error — the harness must
    stay usable on a machine that has no PowerShell 7.
    """
    exe = pwsh or powershell_path()
    if not exe:
        return {"available": False, "checked": 0, "failures": [],
                "reason": "pwsh not found on PATH; snippet syntax was not checked"}

    items = []
    for task in tasks:
        for phase in ("setup", "teardown"):
            for i, snippet in enumerate(task.get(phase) or []):
                items.append({
                    "ref": f"{task.get('id', '?')}.{phase}[{i}]",
                    "b64": base64.b64encode(str(snippet).encode("utf-8")).decode("ascii"),
                })
    if not items:
        return {"available": True, "checked": 0, "failures": [], "reason": "no snippets"}

    with tempfile.TemporaryDirectory(prefix="jevcu-parse-") as tmp:
        snippets_path = Path(tmp) / "snippets.json"
        result_path = Path(tmp) / "result.json"
        snippets_path.write_text(json.dumps(items), encoding="utf-8")
        env = dict(os.environ,
                   JEVCU_SNIPPETS=str(snippets_path),
                   JEVCU_RESULT=str(result_path))
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", PARSE_PROBE],
                env=env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"available": True, "checked": 0, "failures": [],
                    "reason": f"pwsh parse probe timed out after {timeout_s:.0f}s"}
        if proc.returncode != 0 or not result_path.exists():
            return {"available": True, "checked": 0, "failures": [],
                    "reason": f"pwsh parse probe failed (rc={proc.returncode}): "
                              f"{(proc.stderr or '').strip()[:300]}"}
        rows = json.loads(result_path.read_text(encoding="utf-8"))

    if isinstance(rows, dict):  # PowerShell unwraps a one-element array
        rows = [rows]
    failures = [{"ref": row["ref"], "errors": list(row.get("errors") or [])}
                for row in rows if row.get("errors")]
    return {"available": True, "checked": len(rows), "failures": failures, "reason": ""}


# --------------------------------------------------------------------------- #
# Live phases — armed only by an explicit human assertion
# --------------------------------------------------------------------------- #

#: Flipped by `main()` when, and only when, both `--live` and
#: `--i-am-not-streaming` were typed. A module-level interlock rather than a
#: parameter because the mistake this prevents — a test, an import, or a future
#: caller reaching the executing code path — is exactly the kind that a default
#: argument somewhere else silently re-enables.
_LIVE_ARMED = False


def arm_live(reason: str) -> None:
    global _LIVE_ARMED
    _LIVE_ARMED = True
    print(f"LIVE MODE ARMED: {reason}")


def make_powershell_phase(phase: str, *, timeout_s: float = 30.0,
                          pwsh: Optional[str] = None) -> Callable[[dict], None]:
    """A callable that **executes** a task's `setup` or `teardown` snippets.

    Refuses to build unless `arm_live` has been called. These snippets start
    Notepad, write the registry, and stop processes; nothing but a human typing
    `--i-am-not-streaming` gets to create this function.
    """
    if not _LIVE_ARMED:
        raise PhaseError(
            f"refusing to build a live {phase} runner: live mode is not armed "
            "(--live --i-am-not-streaming)")
    exe = pwsh or powershell_path()
    if not exe:
        raise PhaseError("pwsh not found on PATH; a live run cannot set up or tear down")

    def phase_fn(task: dict) -> None:
        for i, snippet in enumerate(task.get(phase) or []):
            try:
                proc = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-Command", snippet],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout_s)
            except subprocess.TimeoutExpired:
                raise PhaseError(f"{phase}[{i}] timed out after {timeout_s:.0f}s")
            if proc.returncode != 0:
                raise PhaseError(
                    f"{phase}[{i}] exited {proc.returncode}: "
                    f"{(proc.stderr or '').strip()[:300]}")

    phase_fn.__name__ = f"live_{phase}"
    return phase_fn


def noop_phase(task: dict) -> None:
    """Dry-run setup/teardown: does nothing, on purpose."""


# --------------------------------------------------------------------------- #
# Oracles
# --------------------------------------------------------------------------- #

def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(path)))


def default_snapshot():
    """`jevskill.cu.observe.snapshot` of the foreground window, or a refusal.

    Resolved lazily and by call, never at import: `observe` is Windows-only and
    needs the ``cu`` extra, and CI runs the whole suite on Linux. An `ImportError`
    here is "this oracle cannot be evaluated", which `bench/cu_tasks.md` says must
    be recorded as a failure with a reason — not a pass, and not a crash.
    """
    try:
        from jevskill.cu import observe
    except ImportError as exc:      # pragma: no cover - Linux CI path
        raise OracleUnsupported(f"jevskill.cu.observe is unavailable: {exc}")
    try:
        return observe.snapshot()
    except (ImportError, OSError) as exc:   # pragma: no cover - needs a desktop
        raise OracleUnsupported(f"could not observe the foreground window: {exc}")


def default_window_title() -> str:
    """Title of the foreground window, adapting `observe`'s two-call shape.

    `observe` exposes `foreground_hwnd()` and `window_title(hwnd)`; the oracle
    only ever wants "what is on top right now", and a zero-argument seam is a
    one-line fake in a test instead of a handle to invent.
    """
    try:
        from jevskill.cu import observe
    except ImportError as exc:      # pragma: no cover - Linux CI path
        raise OracleUnsupported(f"jevskill.cu.observe is unavailable: {exc}")
    try:
        return observe.window_title(observe.foreground_hwnd())
    except (ImportError, OSError) as exc:   # pragma: no cover - needs a desktop
        raise OracleUnsupported(f"could not read the foreground window title: {exc}")


def evaluate_oracle(spec: dict, *, snapshot=None, window_title=None) -> Tuple[bool, str]:
    """Grade one oracle. Returns ``(passed, detail)``; raises `OracleUnsupported`.

    Filesystem and registry reads happen in-process. The two desktop oracles go
    through injected callables — ``snapshot() -> Snapshot`` and
    ``window_title() -> str``, defaulting to `jevskill.cu.observe` — so the tests
    grade recorded fixtures rather than whatever window happens to be on top.
    Both **read**; neither clicks, types or moves focus.

    `clipboard_equals` still raises. No task uses it, and reading the clipboard
    mid-benchmark would also be the one oracle that destroys something the person
    at the machine put there.
    """
    kind = spec.get("type")
    if kind in ORACLE_NEEDS_DESKTOP:
        raise OracleUnsupported(
            f"oracle type {kind!r} is not implemented; no task in "
            "bench/cu_tasks.json uses it and a clipboard read would clobber the "
            "user's own clipboard")
    if kind == "all_of":
        details = []
        passed = True
        for sub in spec.get("all_of") or []:
            ok, detail = evaluate_oracle(sub, snapshot=snapshot, window_title=window_title)
            details.append(("ok" if ok else "FAIL") + ": " + detail)
            passed = passed and ok
        return passed, "all_of[" + "; ".join(details) + "]"
    if kind == "any_of":
        details = []
        passed = False
        for sub in spec.get("any_of") or []:
            ok, detail = evaluate_oracle(sub, snapshot=snapshot, window_title=window_title)
            details.append(("ok" if ok else "FAIL") + ": " + detail)
            passed = passed or ok
        return passed, "any_of[" + "; ".join(details) + "]"
    if kind == "uia_value":
        return _oracle_uia_value(spec, snapshot or default_snapshot)
    if kind == "window_title_contains":
        return _oracle_window_title(spec, window_title or default_window_title)
    if kind == "file_exists":
        path = Path(_expand(spec["path"]))
        want = bool(spec.get("expect", True))
        kindof = spec.get("kind", "file")
        found = path.is_dir() if kindof == "dir" else path.is_file()
        return found == want, f"{kindof} {path} exists={found}, expected {want}"
    if kind == "file_contains":
        path = Path(_expand(spec["path"]))
        if not path.is_file():
            return False, f"file {path} does not exist"
        text = path.read_text(encoding="utf-8", errors="replace")
        passed = True
        details = []
        if "contains" in spec:
            hit = str(spec["contains"]) in text
            passed = passed and hit
            details.append(f"contains={hit}")
        if "not_contains" in spec:
            miss = str(spec["not_contains"]) not in text
            passed = passed and miss
            details.append(f"not_contains={miss}")
        return passed, f"file {path}: " + ", ".join(details)
    if kind == "registry_value":
        return _oracle_registry(spec)
    raise OracleUnsupported(f"unknown oracle type {kind!r}")


def _element_text(element) -> str:
    """The text a `uia_value` oracle compares against: ``value``, else ``name``.

    Value first because a real text field keeps its contents there. Name as the
    fallback because Calculator's result display does not: `CalculatorResults`
    is a ``text`` node with no ValuePattern, and the number lives in its
    accessible Name as a full localised sentence ("Wyświetlacz to 408"). That is
    why `cu_tasks.json` grades it with `expect_contains` on the digits and not
    with an equality on the sentence — the prefix is a different string in every
    Windows display language, and this benchmark runs on a Polish-locale host.
    """
    value = getattr(element, "value", None)
    if value is not None and str(value) != "":
        return str(value)
    return str(getattr(element, "name", "") or "")


def _oracle_uia_value(spec: dict, snapshot_fn) -> Tuple[bool, str]:
    """Grade the accessibility tree of the foreground window.

    Selectors (`automation_id`, `name`, `find_control_type`) are ANDed, so a
    spec that names two of them means "the element that is both". Judgements are
    `expect: "exists"` (the element is present at all), `expect: <str>` (its text
    equals that) or `expect_contains: <str>` (its text contains that).

    `window_title_contains` on a `uia_value` spec gates the *window*, not an
    element: the observer only ever looks at whatever is in the foreground, so a
    title that does not match means the task's window is not the one on top.
    That is a genuine False with a reason, not an unevaluable oracle — the
    window being somewhere else is exactly the failure the oracle should catch.
    """
    snap = snapshot_fn()
    title = str(getattr(snap, "window_title", "") or "")
    wanted_title = spec.get("window_title_contains")
    if wanted_title and wanted_title.casefold() not in title.casefold():
        return False, (f"window not found: foreground title {title!r} does not "
                       f"contain {wanted_title!r}")

    want_aid = spec.get("automation_id")
    want_name = spec.get("name")
    want_role = spec.get("find_control_type")
    matches = []
    for element in getattr(snap, "elements", []) or []:
        if want_aid is not None and getattr(element, "automation_id", "") != want_aid:
            continue
        if want_name is not None and (getattr(element, "name", "") or "") != want_name:
            continue
        # UIA spells control types "Header"; this package lowercases them into
        # `role` (jevskill/cu/types.py CONTROL_TYPES), so compare case-folded
        # rather than making cu_tasks.json speak the internal vocabulary.
        if want_role is not None and str(getattr(element, "role", "")).casefold() != str(want_role).casefold():
            continue
        matches.append(element)

    selector = ", ".join(f"{k}={spec[k]!r}" for k in UIA_SELECTORS if k in spec)
    if not matches:
        return False, f"no element matched {selector} in {title!r} ({len(snap)} elements)"

    if spec.get("expect") == "exists":
        return True, f"{len(matches)} element(s) matched {selector} in {title!r}"

    texts = [_element_text(element) for element in matches]
    if "expect_contains" in spec:
        needle = str(spec["expect_contains"])
        hit = any(needle in text for text in texts)
        return hit, f"{selector} -> {texts!r}, looking for {needle!r}"
    if "expect" in spec:
        wanted = str(spec["expect"])
        hit = any(text == wanted for text in texts)
        return hit, f"{selector} -> {texts!r}, expected {wanted!r}"
    raise OracleUnsupported(
        f"uia_value needs one of {list(UIA_EXPECTATIONS)}; got {sorted(spec)}")


def _oracle_window_title(spec: dict, window_title_fn) -> Tuple[bool, str]:
    """Is the foreground window's title the one the task asked for?

    The foreground window and not "any window with this title": both tasks that
    use this grade a Chrome tab the agent was asked to open, and a match on some
    background window would pass a run where the agent opened the page and then
    navigated away from it.
    """
    title = str(window_title_fn() or "")
    needle = str(spec.get("substring", ""))
    if spec.get("case_sensitive"):
        hit = needle in title
    else:
        hit = needle.casefold() in title.casefold()
    return hit, (f"foreground title {title!r}, looking for {needle!r} "
                 f"({'case-sensitive' if spec.get('case_sensitive') else 'case-insensitive'})")


def _oracle_registry(spec: dict) -> Tuple[bool, str]:
    try:
        import winreg  # noqa: WPS433 — Windows only, and only for a live grade
    except ImportError:
        raise OracleUnsupported("registry_value needs Windows (winreg is unavailable)")
    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE,
             "HKCR": winreg.HKEY_CLASSES_ROOT, "HKU": winreg.HKEY_USERS}
    hive_name = str(spec.get("hive", "HKCU"))
    if hive_name not in hives:
        raise OracleUnsupported(f"unknown registry hive {hive_name!r}")
    path, name, want = spec["path"], spec["name"], spec.get("expect")
    try:
        with winreg.OpenKey(hives[hive_name], path) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return False, f"{hive_name}\\{path}\\{name} does not exist"
    except OSError as exc:
        return False, f"{hive_name}\\{path}\\{name} unreadable: {exc}"
    return value == want, f"{hive_name}\\{path}\\{name}={value!r}, expected {want!r}"


def task_oracle(task: dict, *, snapshot=None, window_title=None) -> Tuple[bool, str]:
    """The oracle of one task, as `run_task` wants it.

    The two seams are threaded through rather than reached for inside
    `evaluate_oracle`, so a caller with a recorded tree — a test, or a replay of
    a failed live run — grades exactly what the live path would.
    """
    return evaluate_oracle(task["oracle"], snapshot=snapshot, window_title=window_title)


def fake_oracle(task: dict) -> Tuple[bool, str]:
    """Dry-run oracle: always true, and says so in the detail it returns."""
    return True, "fake oracle (dry run) - always true, grades nothing"


# --------------------------------------------------------------------------- #
# The fake agent
# --------------------------------------------------------------------------- #

def _det(parts: Sequence[Any], lo: float, hi: float) -> float:
    """A stable pseudo-value in [lo, hi) from a hash of `parts`.

    Deterministic rather than random so two dry runs of the same task produce the
    same file: a harness whose own output moves between runs cannot tell you
    whether a change you made mattered.
    """
    import hashlib
    digest = hashlib.blake2b("|".join(str(p) for p in parts).encode("utf-8"),
                             digest_size=8).digest()
    return lo + (int.from_bytes(digest, "big") / float(1 << 64)) * (hi - lo)


def make_fake_agent(task: dict, run: int, *, steps: Optional[int] = None,
                    stop_reason: str = "done", escalate_every: int = 0,
                    raises: Optional[BaseException] = None):
    """A deterministic stand-in for the real loop, matching the `Agent` signature.

    It takes `expected_agent_steps_min` steps and stops. The stage timings are
    shaped like `skills/jev/references/act.md` §9 — a ~300 ms decide against ~10 ms
    of everything else — because a harness that only ever saw zeros would not
    show that its own columns are wired to the right fields. They are **not**
    measurements of anything, which is what `"mode": "dry-run"` exists to say.
    """
    task_id = task["id"]
    planned = steps if steps is not None else int(task.get("expected_agent_steps_min", 5))

    def agent(goal: str, opts) -> RunResult:
        if raises is not None:
            raise raises
        count = max(0, min(planned, opts.max_steps))
        records: List[StepRecord] = []
        decisions = 0
        escalations = 0
        tokens = 0
        cost = 0.0
        for index in range(count):
            seed = (task_id, run, index)
            escalated = bool(escalate_every) and (index + 1) % escalate_every == 0
            decided_by = "escalation" if escalated else "jev"
            stages = {
                "observe": round(_det(seed + ("observe",), 8.0, 25.0), 3),
                "reduce": round(_det(seed + ("reduce",), 0.3, 1.2), 3),
                "decide": round(_det(seed + ("decide",), 280.0, 420.0), 3),
                "validate": round(_det(seed + ("validate",), 0.05, 0.30), 3),
                "act": round(_det(seed + ("act",), 3.0, 9.0), 3),
                "settle": round(_det(seed + ("settle",), 20.0, 120.0), 3),
            }
            step_tokens = int(_det(seed + ("tokens",), 1500, 6100))
            step_cost = round(step_tokens * 0.042 / 1_000_000, 9)
            decisions += 1
            tokens += step_tokens
            cost += step_cost
            if escalated:
                escalations += 1
            records.append(StepRecord(
                index=index,
                t_ms=round(sum(stages.values()) + _det(seed + ("overhead",), 0.5, 2.0), 3),
                stages_ms=stages,
                candidates=int(_det(seed + ("candidates",), 6, 55)),
                target=f"e{int(_det(seed + ('target',), 0, 40))}",
                op="click",
                confidence=round(_det(seed + ("conf",), 0.86, 0.995), 4),
                margin=round(_det(seed + ("margin",), 0.40, 0.95), 4),
                goal_reached=round(_det(seed + ("gr",), 0.02, 0.35), 4),
                needs_text=round(_det(seed + ("nt",), 0.05, 0.60), 4),
                is_destructive=round(_det(seed + ("dst",), 0.02, 0.40), 4),
                decided_by=decided_by,
                executed=not escalated,
                tree_changed=not escalated,
                tokens_in=step_tokens,
                cost_usd=step_cost,
                note="synthetic step (dry run)",
            ))
        return RunResult(
            task_id=task_id, run=run,
            stop_reason=stop_reason,
            steps=records,
            wall_ms=round(sum(r.t_ms for r in records), 3),
            decisions=decisions,
            escalations=escalations,
            destructive_gates=0,
            tokens_in=tokens,
            cost_usd=round(cost, 9),
            error="",
        )

    return agent


def error_result(task_id: str, run: int, message: str) -> RunResult:
    """A run that never produced steps: setup failed, or the agent raised."""
    return RunResult(task_id=task_id, run=run, stop_reason="error", steps=[],
                     wall_ms=0.0, decisions=0, escalations=0, destructive_gates=0,
                     tokens_in=0, cost_usd=0.0, error=message)


def _brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


# --------------------------------------------------------------------------- #
# One cell of the benchmark
# --------------------------------------------------------------------------- #

def run_task(task: dict, agent, setup, oracle, teardown, opts, *,
             run: int = 1, mode: str = "live", provider: Optional[str] = None,
             agent_name: str = "") -> TaskRun:
    """setup → agent → oracle → teardown, once. Never raises for a task's failure.

    The ordering guarantees, each of which has a test:

    * **Teardown always runs.** After a clean run, after an agent that raised,
      after a setup that failed, after an oracle that blew up. A benchmark that
      leaves Notepad open and the theme flipped because step three threw is worse
      than no benchmark.
    * **The oracle is consulted even when the agent errored.** An agent can reach
      the goal and then crash on the way out; grading that as a failure because of
      how the agent felt about it is the same mistake as trusting its `done`.
    * **Setup failing means the agent never runs.** The run did not start from a
      known state, so anything measured after it is about some other state.
    * **The agent does not get to name its own cell.** `task_id` and `run` are
      overwritten from the harness's own loop.
    """
    task_id = task["id"]
    notes: List[str] = []
    setup_ok = True
    teardown_ok = True
    success: Optional[bool] = None
    oracle_detail = ""
    result: Optional[RunResult] = None
    harness_wall_ms = 0.0

    try:
        try:
            setup(task)
        except Exception as exc:  # noqa: BLE001 — every failure is data here
            setup_ok = False
            notes.append(f"setup failed: {_brief(exc)}")
            result = error_result(task_id, run, f"setup failed: {_brief(exc)}")
            success = False
            oracle_detail = "oracle not evaluated: setup failed"

        if setup_ok:
            started = time.perf_counter()
            try:
                result = agent(task["goal"], opts)
            except Exception as exc:  # noqa: BLE001
                result = error_result(task_id, run, f"agent raised: {_brief(exc)}")
                notes.append(f"agent raised: {_brief(exc)}")
            harness_wall_ms = (time.perf_counter() - started) * 1000.0

            if not isinstance(result, RunResult):
                notes.append(
                    f"agent returned {type(result).__name__}, not RunResult — "
                    "the contract is jevskill.cu.contract.RunResult")
                result = error_result(task_id, run, "agent returned a non-RunResult")
            result.task_id = task_id
            result.run = run

            if harness_wall_ms > opts.budget_s * 1000.0:
                # The harness cannot preempt a Python callable, so it reports the
                # overrun instead of claiming to have enforced the budget.
                notes.append(
                    f"budget overrun: {harness_wall_ms / 1000:.1f}s of "
                    f"{opts.budget_s:.1f}s (not enforced — the agent stops itself)")

            try:
                success, oracle_detail = oracle(task)
            except OracleUnsupported as exc:
                success, oracle_detail = False, f"oracle not evaluated: {exc}"
                notes.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                success, oracle_detail = False, f"oracle raised: {_brief(exc)}"
                notes.append(f"oracle raised: {_brief(exc)}")
    finally:
        try:
            teardown(task)
        except Exception as exc:  # noqa: BLE001
            teardown_ok = False
            notes.append(f"teardown failed: {_brief(exc)}")

    if result is None:  # only reachable if setup both failed and did not set it
        result = error_result(task_id, run, "no result produced")
    notes.extend(result.problems())
    return TaskRun(result=result, success=success, mode=mode, provider=provider,
                   agent=agent_name, oracle_detail=oracle_detail,
                   harness_wall_ms=harness_wall_ms, setup_ok=setup_ok,
                   teardown_ok=teardown_ok, notes=notes)


def resolve_agent(spec: str):
    """Import ``module:attribute`` and return it. Used only by `--live`.

    The default is `jevskill.cu.loop:run`, which does not exist until plan items
    4.1-4.6 land; the error says so rather than reporting a generic ImportError.
    """
    if ":" not in spec:
        raise SystemExit(f"--agent must look like 'module:attribute', got {spec!r}")
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(
            f"cannot import {module_name!r} for --agent {spec!r}: {exc}. "
            "The loop is plan items 4.1-4.6; until it lands, only --dry-run works.")
    try:
        return getattr(module, attr)
    except AttributeError:
        raise SystemExit(f"{module_name!r} has no attribute {attr!r}")


# --------------------------------------------------------------------------- #
# Results file
# --------------------------------------------------------------------------- #

def resolve_out(requested: Optional[Path], *, live: bool) -> Path:
    """Where this invocation's rows go: explicit `--out`, else the mode's default."""
    if requested is not None:
        return Path(requested)
    return DEFAULT_OUT_LIVE if live else DEFAULT_OUT_DRY


def host_facts() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "jevskill": __version__,
        "pwsh": bool(powershell_path()),
    }


ABOUT = (
    "Windows computer-use benchmark runs. Written by bench/cu_run.py, read by "
    "bench/cu_report.py. `success` is the task oracle's verdict, never the "
    "agent's. Rows with mode='dry-run' are SYNTHETIC: a fake agent against a fake "
    "oracle, produced on a machine where the real benchmark must not run. No "
    "number from such a row is a measurement."
)


def merge(out_path: Path, rows: Sequence[TaskRun], *, syntax: Optional[dict] = None) -> dict:
    """Merge rows into the results file, keyed by (task, run, mode, provider).

    Merges rather than truncates for the reason `bench/cu_bench.py` gives: two
    agents and two providers are separate invocations, and a writer that
    overwrote would leave the file describing whichever one ran last.
    """
    data: dict = {}
    path = Path(out_path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data["about"] = ABOUT
    data["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["host"] = host_facts()
    if syntax is not None:
        data["powershell_syntax"] = syntax

    def row_key(row: dict) -> Tuple[str, int, str, str]:
        return (str(row.get("task_id", "")), int(row.get("run", 0) or 0),
                str(row.get("mode", "")), str(row.get("provider") or ""))

    existing = {row_key(r): r for r in data.get("runs", [])}
    for row in rows:
        existing[row.key()] = row.to_dict()
    data["runs"] = [existing[k] for k in sorted(existing, key=lambda k: (k[2], k[3], k[0], k[1]))]

    modes = sorted({r.get("mode", "") for r in data["runs"]})
    data["modes"] = modes
    data["mode"] = modes[0] if len(modes) == 1 else "mixed"
    data["dry_run_rows"] = sum(1 for r in data["runs"] if r.get("mode") == "dry-run")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--live drives the real desktop: it opens applications, types into\n"
            "them, steals focus, and for settings_dark_mode flips this machine's\n"
            "theme while the run is in progress. That is why it also needs\n"
            "--i-am-not-streaming, which no environment variable and no config\n"
            "file can supply. Without --live the runner is a dry run: schema\n"
            "check, PowerShell syntax check (parse only, never execute), fake\n"
            "agent, fake oracle, synthetic numbers labelled as such."),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="the default; loads and validates the tasks, parses every "
                             "PowerShell snippet for syntax, runs a fake agent")
    parser.add_argument("--live", action="store_true",
                        help="run the real agent against the real desktop (needs "
                             "--i-am-not-streaming)")
    parser.add_argument("--i-am-not-streaming", dest="not_streaming", action="store_true",
                        help="assert that nobody is being filmed by this machine right "
                             "now; a live run steals focus and flips Settings")
    parser.add_argument("--agent", default="jevskill.cu.loop:run",
                        help="module:attribute of the agent under test (default: "
                             "jevskill.cu.loop:run)")
    parser.add_argument("--task", action="append", default=None, metavar="ID",
                        help="only this task id; repeatable")
    parser.add_argument("--runs", type=int, default=3,
                        help="repeats per task (default 3; cu_tasks.md warns that "
                             "n=3 bounds flakiness, it is not a reliability benchmark)")
    parser.add_argument("--max-steps", type=int, default=RunOptions.max_steps)
    parser.add_argument("--budget-s", type=float, default=RunOptions.budget_s)
    parser.add_argument("--provider", choices=("typesafe", "openrouter"), default=None,
                        help="endpoint the agent should use; recorded in the row key")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=None,
                        help=f"results file; defaults to {DEFAULT_OUT_DRY.name} for a "
                             f"dry run and {DEFAULT_OUT_LIVE.name} for --live")
    parser.add_argument("--no-write", action="store_true", help="do not touch --out")
    parser.add_argument("--skip-syntax", action="store_true",
                        help="skip the PowerShell parse check")
    parser.add_argument("--seed", type=int, default=0,
                        help="seed for the per-repeat task shuffle (default 0)")
    parser.add_argument("--no-shuffle", action="store_true",
                        help="run tasks in file order instead of shuffling per repeat")
    parser.add_argument("--fake-escalate-every", type=int, default=0, metavar="N",
                        help="dry run only: make the fake agent escalate every Nth step, "
                             "to exercise the escalation columns (default 0: never)")
    parser.add_argument("--setup-timeout-s", type=float, default=30.0)
    parser.add_argument("--teardown-timeout-s", type=float, default=30.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    live = bool(args.live)
    if live and not args.not_streaming:
        print("REFUSED: --live needs --i-am-not-streaming. A live run opens "
              "applications, types into them, takes the foreground and flips this "
              "machine's theme. Read `bench/cu_tasks.md` first.", file=sys.stderr)
        return 2
    if live and args.dry_run:
        print("REFUSED: --live and --dry-run are opposites; pick one.", file=sys.stderr)
        return 2
    mode = "live" if live else "dry-run"
    out_path = resolve_out(args.out, live=live)

    tasks = load_tasks(args.tasks)
    problems = validate_tasks(tasks)
    if problems:
        print(f"{args.tasks} does not validate:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    print(f"{args.tasks.name}: {len(tasks)} tasks, schema clean")

    syntax: Optional[dict] = None
    if not args.skip_syntax:
        syntax = check_powershell_syntax(tasks)
        if not syntax["available"]:
            print(f"  PowerShell syntax check SKIPPED — {syntax['reason']}")
        elif syntax["reason"]:
            print(f"  PowerShell syntax check unavailable — {syntax['reason']}")
        elif syntax["failures"]:
            print(f"  PowerShell syntax check: {len(syntax['failures'])} snippet(s) "
                  "do not parse", file=sys.stderr)
            for failure in syntax["failures"]:
                print(f"    {failure['ref']}: {failure['errors'][0]}", file=sys.stderr)
            return 2
        else:
            print(f"  PowerShell syntax check: {syntax['checked']} snippets parse "
                  "(parsed, never executed)")

    wanted = set(args.task or [])
    if wanted:
        missing = wanted - {t["id"] for t in tasks}
        if missing:
            print(f"no such task(s): {sorted(missing)}", file=sys.stderr)
            return 2
        tasks = [t for t in tasks if t["id"] in wanted]

    if live:
        arm_live("--i-am-not-streaming was given on the command line")
        setup = make_powershell_phase("setup", timeout_s=args.setup_timeout_s)
        teardown = make_powershell_phase("teardown", timeout_s=args.teardown_timeout_s)
        oracle = task_oracle
        agent_factory = resolve_agent(args.agent)
        agent_name = args.agent
    else:
        setup = teardown = noop_phase
        oracle = fake_oracle
        agent_factory = None
        agent_name = "fake-agent (dry run)"

    opts = RunOptions(max_steps=args.max_steps, budget_s=args.budget_s,
                      dry_run=not live, provider=args.provider)
    rng = None if args.no_shuffle else random.Random(args.seed)

    rows: List[TaskRun] = []
    for repeat in range(1, max(1, args.runs) + 1):
        for task in order_tasks(tasks, rng):
            if live and task.get("restores_state"):
                print(f"  ! {task['id']} changes this machine's state while it runs "
                      f"({task['notes'][:80]}...)")
            agent = (agent_factory if live else
                     make_fake_agent(task, repeat, escalate_every=args.fake_escalate_every))
            row = run_task(task, agent, setup, oracle, teardown, opts,
                           run=repeat, mode=mode, provider=args.provider,
                           agent_name=agent_name)
            rows.append(row)
            mark = {True: "pass", False: "FAIL", None: "????"}[row.success]
            print(f"  {mode} run {repeat}  {row.task_id:<24} {mark}  "
                  f"steps={row.result.steps_count:<3} "
                  f"decisions={row.result.decisions:<3} "
                  f"esc={row.result.escalation_steps:<2} "
                  f"wall={row.result.wall_ms:8.1f} ms  "
                  f"stop={row.result.stop_reason}")

    report = summarise(rows)
    totals = report["totals"]
    rate = totals["success_rate"]
    print(f"\n{len(rows)} runs over {totals['tasks']} tasks, mode={mode}")
    print(f"  success (oracle only): {totals['passes']}/{totals['graded']}"
          + (f" = {rate:.0%}" if rate is not None else " (nothing graded)"))
    print(f"  escalated steps: {totals['escalated_steps']}/{totals['steps_total']} "
          f"= {totals['escalation_rate']:.1%}; runs ending escalated: "
          f"{totals['escalated_runs']}/{totals['runs']}")
    if totals["failed_all_runs"]:
        print(f"  failed on every run: {totals['failed_all_runs']}")
    if report["problems"]:
        print(f"  accounting problems ({len(report['problems'])}):")
        for problem in report["problems"][:10]:
            print(f"    - {problem}")
    if mode == "dry-run":
        print("\n  DRY RUN - every number above is synthetic. The fake agent never "
              "escalates unless --fake-escalate-every says so, and the fake oracle "
              "always passes.")

    if not args.no_write:
        merge(out_path, rows, syntax=syntax)
        print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

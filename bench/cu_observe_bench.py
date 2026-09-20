"""How long does perception cost, and what does the model end up reading?

Windows only. Launches Notepad and Calculator, snapshots each three times warm,
reduces, and reports ms / nodes / candidates / tokens per app. Also walks the
same window with ``uiautomation`` and ``pywinauto`` so the choice of library in
``jevskill/cu/observe.py`` is a measurement rather than a preference, and times
one snapshot of whatever is in the foreground.

    python bench/cu_observe_bench.py                 # table + cu_observe_results.json
    python bench/cu_observe_bench.py --save-fixtures # + tests/fixtures/cu/*.json

Privacy: the foreground window is timed, never recorded. No title, no element
names, no process name — the row carries milliseconds and counts only. The two
committed fixtures come from applications this script launched itself, and
``--save-fixtures`` still scrubs document/edit contents (Windows 11 Notepad
restores the previous session's unsaved tabs).

This script spends no money: it makes no API calls.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jevskill.cu import candidates, tree_hash  # noqa: E402
from jevskill.cu.observe import (foreground_hwnd, snapshot, state_tokens,  # noqa: E402
                                 to_state)

RESULTS = ROOT / "bench" / "cu_observe_results.json"
FIXTURES = ROOT / "tests" / "fixtures" / "cu"
user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_CLOSE = 0x0010


# --------------------------------------------------------------------------- #
# Launching and closing the two apps
# --------------------------------------------------------------------------- #

def write_json(path, text):
    """LF, always. The repo normalises to LF and a CRLF write is noise in git."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def visible_windows():
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            found.append((int(hwnd), int(pid.value), name.value))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found


def launch(command, classes=None, timeout=15.0):
    """Start an app and return the hwnd of the window it opened."""
    before = {h for h, _, _ in visible_windows()}
    subprocess.Popen(command)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, _pid, cls in visible_windows():
            if hwnd in before:
                continue
            if classes and cls not in classes:
                continue
            return hwnd
        time.sleep(0.2)
    return 0


def wait_ready(hwnd, min_nodes=2, timeout=8.0):
    """A visible window is not a populated one.

    A UWP frame (Calculator) exists a beat before its CoreWindow child does, so
    a snapshot taken the moment the window appears returns exactly one node.
    Measuring that and reporting it as "Calculator: 1 node" is how a benchmark
    lies without anyone editing a number.
    """
    deadline = time.time() + timeout
    nodes = 0
    while time.time() < deadline:
        try:
            nodes = len(snapshot(hwnd).elements)
        except Exception:  # noqa: BLE001
            nodes = 0
        if nodes >= min_nodes:
            return nodes
        time.sleep(0.25)
    return nodes


# --------------------------------------------------------------------------- #
# The other two libraries, for the comparison table
# --------------------------------------------------------------------------- #

def walk_uiautomation(hwnd):
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    total = [0]

    def rec(control):
        total[0] += 1
        _ = control.Name, control.ControlTypeName, control.BoundingRectangle
        for child in control.GetChildren():
            rec(child)

    rec(root)
    return total[0]


def walk_pywinauto(hwnd):
    from pywinauto import Desktop

    window = Desktop(backend="uia").window(handle=hwnd)
    count = 0
    for element in window.descendants():
        info = element.element_info
        _ = info.name, info.control_type, info.rectangle
        count += 1
    return count


def compare_worker(library, hwnd, runs):
    walk = {"uiautomation": walk_uiautomation, "pywinauto": walk_pywinauto}[library]
    try:
        walk(hwnd)  # warm: COM init and module import
        times, nodes = [], 0
        for _ in range(runs):
            started = time.perf_counter()
            nodes = walk(hwnd)
            times.append(round((time.perf_counter() - started) * 1000, 1))
        return {"ms": times, "nodes": nodes}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def compare_libraries(hwnd, runs):
    """Same window, same job, three clients. Errors are reported, not hidden.

    Each competitor runs in its own process on purpose: importing pywinauto
    prints "Revert to STA COM threading mode" and re-initialises COM for the
    whole process, after which this benchmark's own cached IUIAutomation
    returns a one-node tree for every later window. That cost an hour and two
    wrong tables before it was noticed — do not move these back in-process.
    """
    out = {}
    for label in ("uiautomation", "pywinauto"):
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--compare-worker",
             label, "--hwnd", str(hwnd), "--runs", str(runs)],
            capture_output=True, text=True, timeout=300)
        try:
            out[label] = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            out[label] = {"error": (proc.stdout + proc.stderr)[-200:].strip()}
    return out


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #

def time_strategy(hwnd, strategy, runs, budget_ms):
    snapshot(hwnd, strategy=strategy, budget_ms=budget_ms)  # warm
    times, snap = [], None
    for _ in range(runs):
        started = time.perf_counter()
        snap = snapshot(hwnd, strategy=strategy, budget_ms=budget_ms)
        times.append(round((time.perf_counter() - started) * 1000, 1))
    return snap, times


def measure(hwnd, runs, cap=60, budget_ms=2000.0):
    # A generous budget here on purpose: the question this bench answers is
    # "what does a full walk cost", and a truncated walk answers a different
    # one. The default 600 ms in snapshot() is a step budget, not a
    # measurement budget, and Notepad exceeds it — which is the finding.
    strategies = {}
    for name in ("subtree", "lazy"):
        snap_s, times_s = time_strategy(hwnd, name, runs, budget_ms)
        strategies[name] = {"ms": times_s, "median_ms": round(statistics.median(times_s), 1),
                            "nodes": len(snap_s.elements), "truncated": snap_s.truncated}
    snap, times = time_strategy(hwnd, "subtree", runs, budget_ms)

    reduce_ms = []
    for _ in range(5):
        started = time.perf_counter()
        shortlist = candidates(snap.elements, cap=cap)
        reduce_ms.append((time.perf_counter() - started) * 1000)
    shortlist = candidates(snap.elements, cap=cap)

    full_state = to_state(snap)
    short_state = to_state(snap, elements=shortlist)
    return snap, shortlist, {
        "library": "comtypes+CacheRequest",
        "strategy": "subtree",
        "strategies": strategies,
        "budget_ms": budget_ms,
        "over_default_budget": statistics.median(times) > 600.0,
        "snapshot_ms": times,
        "snapshot_ms_median": round(statistics.median(times), 1),
        "nodes": len(snap.elements),
        "truncated": snap.truncated,
        "reduce_ms_median": round(statistics.median(reduce_ms), 3),
        "candidates": len(shortlist),
        "tokens_full_state": state_tokens(full_state),
        "tokens_reduced_state": state_tokens(short_state),
        "tree_hash": tree_hash(snap.elements),
    }


#: Roles whose ``name`` is the user's content rather than the app's chrome.
#: Windows 11 Notepad reopens the previous session's tabs, so a snapshot of a
#: freshly launched Notepad carries the machine owner's file names in every
#: ``tabitem`` and their text in the ``document``. A committed fixture must
#: keep the shape (roles, rectangles, patterns, nesting) and none of that.
CONTENT_ROLES = frozenset({
    "document", "edit", "text", "tabitem", "listitem", "treeitem", "dataitem",
    "hyperlink", "menuitem", "combobox",
})


def scrub(snap, app_name):
    counter = {}
    title = snap.window_title
    for element in snap.elements:
        if element.value:
            element.value = "<scrubbed>"
        if title and title in element.name:
            # The window title is the open file's name. It is also the root
            # element's Name, and the title bar's, and the taskbar button's.
            element.name = app_name
        elif element.role in CONTENT_ROLES and element.name:
            index = counter.get(element.role, 0) + 1
            counter[element.role] = index
            element.name = "%s %d" % (element.role, index)
    snap.window_title = app_name
    return snap


def save_fixture(name, snap, shortlist):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    snap = scrub(snap, name)
    payload = {
        "source": "bench/cu_observe_bench.py --save-fixtures",
        "note": ("Real UIA snapshot of an app this script launched. Window "
                 "title, every value and every content-bearing name are "
                 "replaced; roles, rectangles, patterns and nesting are real."),
        "snapshot": snap.to_dict(),
        "state": to_state(snap, elements=[e for e in snap.elements
                                          if e.id in {c.id for c in shortlist}]),
    }
    path = FIXTURES / ("%s.json" % name)
    write_json(path, json.dumps(payload, indent=1, sort_keys=True))
    return path


#: Realistic Windows control names, so a token count measured on a synthetic
#: tree means something. Invented names of a fixed length would make the
#: "<= 3,500 tokens for 60 candidates" check measure the generator.
_NAMES = (
    "Save", "Save As...", "Open", "Open Recent", "Close Tab", "Print...",
    "Undo", "Redo", "Cut", "Copy", "Paste", "Find and Replace", "Select All",
    "Zoom In", "Zoom Out", "Word Wrap", "Bold", "Italic", "Underline",
    "Settings", "Help", "Send Feedback", "New Window", "Toggle Sidebar",
    "Run Without Debugging", "Format Document", "Go to Definition",
)
_LEAF_ROLES = ("button", "checkbox", "edit", "listitem", "hyperlink",
               "menuitem", "combobox", "text", "image", "radiobutton")


def make_synthetic(total, seed=20260920):
    """A tree shaped like a real app: chrome, panes, a dialog, and junk.

    The junk is the point. Every filter in ``jevskill.cu.reduce`` needs
    something to remove — offscreen nodes, disabled controls, 2x2 layout
    artefacts, repeated names inside one toolbar — or the tests measure a
    pipeline that never had to decide anything.
    """
    import random

    from jevskill.cu.types import UIElement

    rng = random.Random(seed)
    elements = [UIElement(id="e0", role="window", name="Synthetic App",
                          bbox=(0, 0, 1600, 1000), depth=0)]
    handles = {"window": "e0"}

    def add(role, name, bbox, parent, region, depth, **kw):
        element = UIElement(id="e%d" % len(elements), role=role, name=name,
                            bbox=bbox, parent=parent, region=region,
                            depth=depth, **kw)
        elements.append(element)
        return element

    title = add("titlebar", "", (0, 0, 1600, 32), "e0", "e0", 1)
    for index, name in enumerate(("Minimize", "Maximize", "Close")):
        add("button", name, (1450 + index * 50, 0, 46, 32), title.id, title.id, 2,
            patterns=("invoke",))
    menubar = add("menubar", "", (0, 32, 1600, 28), "e0", "e0", 1)
    for index, name in enumerate(("File", "Edit", "View", "Help")):
        add("menuitem", name, (8 + index * 70, 32, 68, 28), menubar.id,
            menubar.id, 2, patterns=("expand",))

    region_index = 0
    while len(elements) < total:
        region_index += 1
        top = 60 + (region_index % 6) * 150
        pane = add("group", "Panel %d" % region_index,
                   (0, top, 1600, 148), "e0", "e0", 1)
        for slot in range(min(24, total - len(elements))):
            role = _LEAF_ROLES[rng.randrange(len(_LEAF_ROLES))]
            name = _NAMES[rng.randrange(len(_NAMES))]
            left = 10 + (slot % 8) * 190
            row = top + 8 + (slot // 8) * 44
            patterns = {"button": ("invoke",), "checkbox": ("toggle",),
                        "edit": ("value",), "listitem": ("select",),
                        "hyperlink": ("invoke",), "menuitem": ("invoke",),
                        "combobox": ("expand", "value"),
                        "radiobutton": ("select",)}.get(role, ())
            roll = rng.random()
            add(role, name, (left, row, 180, 36), pane.id, pane.id, 2,
                patterns=patterns, enabled=roll > 0.12,
                offscreen=roll > 0.94,
                focused=(len(elements) == 40),
                value="typed text" if role == "edit" else None)
            if rng.random() > 0.85 and len(elements) < total:
                add("separator", "", (left, row + 38, 180, 2), pane.id,
                    pane.id, 2)  # a 2 px layout artefact: must be pruned
    del handles
    return elements[:total]


def save_synthetic():
    from jevskill.cu.types import Snapshot

    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = []
    for size in (50, 500, 2000):
        elements = make_synthetic(size)
        snap = Snapshot(window_title="Synthetic App", app="synthetic.exe",
                        pid=0, hwnd=0, taken_at=0.0, elapsed_ms=0.0,
                        elements=elements)
        payload = {
            "source": "bench/cu_observe_bench.py --synthetic-only",
            "note": "Deterministic (seed 20260920). No real window involved.",
            "snapshot": snap.to_dict(),
            "state": to_state(snap, elements=candidates(elements, cap=60)),
        }
        path = FIXTURES / ("synthetic_%d.json" % size)
        # Compact: the 2,000-node tree is 776 KB pretty-printed and 260 KB like
        # this, and nobody reads a generated tree by eye — they regenerate it.
        write_json(path, json.dumps(payload, sort_keys=True,
                                    separators=(",", ":")))
        written.append(path)
    return written


def foreground_row(cap=60):
    """Time the live foreground window. Counts only — nothing is recorded.

    No title, no names, no process: a real desktop is somebody's, and a
    benchmark file is committed. The row is milliseconds and counts.

    Run non-interactively, the foreground window is the console this script was
    started from, whose one-node tree says nothing about anything; that case is
    reported as skipped rather than as a measurement.
    """
    hwnd = foreground_hwnd()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if int(pid.value) == os.getpid():
        return {"skipped": "foreground window belongs to this benchmark process"}
    started = time.perf_counter()
    snap = snapshot(hwnd)
    elapsed = (time.perf_counter() - started) * 1000
    shortlist = candidates(snap.elements, cap=cap)
    return {"snapshot_ms": round(elapsed, 1), "nodes": len(snap.elements),
            "candidates": len(shortlist), "truncated": snap.truncated,
            "tokens_reduced_state": state_tokens(
                to_state(snap, elements=shortlist)),
            "note": "content deliberately not recorded"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cap", type=int, default=60)
    parser.add_argument("--save-fixtures", action="store_true")
    parser.add_argument("--skip-foreground", action="store_true")
    parser.add_argument("--synthetic-only", action="store_true",
                        help="write tests/fixtures/cu/synthetic_*.json and exit "
                             "(pure Python: runs on any OS)")
    parser.add_argument("--compare-worker", choices=["uiautomation", "pywinauto"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--hwnd", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.synthetic_only:
        for path in save_synthetic():
            print("wrote %s" % path.relative_to(ROOT).as_posix())
        return
    if args.compare_worker:
        print(json.dumps(compare_worker(args.compare_worker, args.hwnd, args.runs)))
        return

    results = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "python": sys.version.split()[0], "runs": args.runs,
               "cap": args.cap, "apps": {}}

    # First, before anything is launched: otherwise "foreground" is this
    # script's own console, or the window it just told to close.
    if not args.skip_foreground:
        try:
            results["foreground"] = foreground_row(args.cap)
        except Exception as exc:  # noqa: BLE001
            results["foreground"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

    apps = (("notepad", ["notepad.exe"], None),
            ("calculator", ["calc.exe"], {"ApplicationFrameWindow", "CalcFrame"}))
    for name, command, classes in apps:
        # Both apps are packaged (Notepad included, since Windows 11), so both
        # can be suspended by Process Lifetime Management while this script is
        # busy, and every UIA call then fails with EVENT_E_ALL_SUBSCRIBERS_
        # FAILED. A relaunch is the documented recovery; one retry is enough.
        for attempt in (1, 2):
            hwnd = launch(command, classes)
            if not hwnd:
                results["apps"][name] = {"error": "window did not appear"}
                break
            try:
                wait_ready(hwnd)
                snap, shortlist, row = measure(hwnd, args.runs, args.cap)
                # The comparison runs after the numbers that matter are taken:
                # it spawns processes, which takes seconds the app may not have.
                row["compare"] = compare_libraries(hwnd, args.runs)
                row["attempt"] = attempt
                results["apps"][name] = row
                if args.save_fixtures:
                    row["fixture"] = save_fixture(
                        name, snap, shortlist).relative_to(ROOT).as_posix()
                break
            except Exception as exc:  # noqa: BLE001
                results["apps"][name] = {"error": "%s: %s" % (type(exc).__name__, exc),
                                         "attempt": attempt}
            finally:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    write_json(RESULTS, json.dumps(results, indent=1))
    print_table(results)
    print("\nwrote %s" % RESULTS.relative_to(ROOT))


def print_table(results):
    print("app          snapshot ms (median)   nodes  cand  reduce ms  "
          "tokens full -> reduced")
    for name, row in results["apps"].items():
        if "error" in row:
            print("%-12s %s" % (name, row["error"]))
            continue
        print("%-12s %-22s %5d %5d %10.3f  %6d -> %d%s"
              % (name, "%s (%s)" % (row["snapshot_ms"], row["snapshot_ms_median"]),
                 row["nodes"], row["candidates"], row["reduce_ms_median"],
                 row["tokens_full_state"], row["tokens_reduced_state"],
                 "  TRUNCATED" if row["truncated"] else ""))
    fore = results.get("foreground")
    if fore and "skipped" in fore:
        print("%-12s %s" % ("foreground", fore["skipped"]))
    elif fore and "error" not in fore:
        print("%-12s %-22s %5d %5d %10s  %6s -> %d"
              % ("foreground", fore["snapshot_ms"], fore["nodes"],
                 fore["candidates"], "-", "-", fore["tokens_reduced_state"]))
    print("\nlibrary comparison (same window, same job, ms per run):")
    for name, row in results["apps"].items():
        if "compare" not in row:
            continue
        print("  %s:" % name)
        for label, data in row.get("strategies", {}).items():
            print("    %-24s %s nodes=%d%s"
                  % ("comtypes+cache/" + label, data["ms"], data["nodes"],
                     "  TRUNCATED" if data["truncated"] else ""))
        for lib, data in row["compare"].items():
            if "error" in data:
                print("    %-24s %s" % (lib, data["error"]))
            else:
                print("    %-24s %s nodes=%d" % (lib, data["ms"], data["nodes"]))


if __name__ == "__main__":
    main()

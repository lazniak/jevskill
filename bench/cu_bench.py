"""Micro-benchmark: Jev as the per-step decision core of a computer-use loop.

State = a compact accessibility-tree snapshot (N elements), goal text, last action.
Questions (one call): target element (choice over N ids + none), action type
(choice), goal_reached (noul), stuck (noul).
Measures warm latency across K calls, and how the distribution behaves at N=12/30/60.
Cost: ~K * ~$0.00003. Uses the repo client (httpx/h2 if present).
"""
from __future__ import annotations
import json, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevskill.client import JevClient  # noqa: E402

# Provider comes from the normal resolution (JEVSKILL_PROVIDER, then the key found
# by name, vendor names first). Run it twice to compare routes:
#   JEVSKILL_PROVIDER=typesafe   python bench/cu_bench.py 10
#   JEVSKILL_PROVIDER=openrouter python bench/cu_bench.py 10

GOAL = "Save the current document and close the dialog"

def make_tree(n: int) -> dict:
    els = {}
    kinds = ["button", "textbox", "menuitem", "checkbox", "link", "tab"]
    names = ["File", "Edit", "View", "Search", "Font", "Bold", "Italic", "Zoom", "Help",
             "Cancel", "Don't Save", "Save", "Save As", "Print", "Close", "Settings",
             "Undo", "Redo", "Cut", "Copy", "Paste", "Find", "Replace", "Insert",
             "Table", "Image", "Link", "Comment", "Share", "Export"]
    for i in range(n):
        els[f"e{i}"] = {
            "role": kinds[i % len(kinds)] if names[i % len(names)] not in ("Save", "Cancel", "Don't Save", "Close") else "button",
            "name": names[i % len(names)] + ("" if i < len(names) else f" {i}"),
            "bbox": [40 + (i % 8) * 120, 60 + (i // 8) * 48, 110, 32],
            "enabled": True,
            "focused": i == 3,
        }
    return els

def questions(ids: list[str]) -> dict:
    crit = {eid: f"`elements.{eid}` is the control whose activation most directly advances `goal`." for eid in ids}
    crit["none"] = "No listed element advances `goal`; a different action (key, scroll, wait) is needed."
    return {
        "target": {"type": "choice",
                   "instructions": {"question": "Which element in `elements` should be acted on next to advance `goal`?",
                                    "focus": "Pick the single control whose activation is the most direct next step, given `last_action`."},
                   "criteria": crit},
        "action": {"type": "choice",
                   "instructions": "What kind of input advances `goal` from this screen?",
                   "criteria": {"click": "Activate a visible control.",
                                "type": "Enter text into a focused text field.",
                                "key": "Press a keyboard shortcut (Enter, Esc, Ctrl+S).",
                                "scroll": "Content needed is not visible yet.",
                                "wait": "The UI is still loading or animating.",
                                "done": "`goal` is already satisfied by the visible state."}},
        "goal_reached": {"type": "noul",
                         "instructions": "Is `goal` already satisfied by the state in `elements` and `window_title`?",
                         "criteria": {"true": "The visible state shows `goal` completed; nothing more to do.",
                                      "false": "At least one step of `goal` is still pending."}},
        "stuck": {"type": "noul",
                  "instructions": "Does `last_action` together with the current `elements` indicate the previous step had no effect?",
                  "criteria": {"true": "The screen is unchanged from before `last_action`, or an error dialog appeared.",
                               "false": "The screen progressed as expected after `last_action`."}},
    }

def run(n: int, k: int, jev: JevClient):
    els = make_tree(n)
    state = {"goal": GOAL, "window_title": "Untitled - Editor - Save changes?",
             "last_action": {"type": "key", "key": "Ctrl+W"}, "elements": els}
    qs = questions(list(els))
    lat, http = [], []
    first = None
    for i in range(k):
        r = jev.decide(state, qs, session_id="cu-bench")
        lat.append(r.timing_ms.get("total_ms", 0))
        http.append(r.timing_ms.get("http_ms", 0))
        if first is None:
            first = r
    top = first.top("target", 4)
    print(f"N={n:3d} K={k}  tokens_in={first.input_tokens}  cost/call=${first.cost_usd:.7f}")
    print(f"   http p50={statistics.median(http):.0f} ms  min={min(http):.0f}  max={max(http):.0f}  "
          f"total p50={statistics.median(lat):.0f} ms")
    print(f"   target top4={[(a, round(b, 2)) for a, b in top]} conf={first.confidence('target')}")
    print(f"   action={first.choice('action')} {first.top('action', 3)}  goal_reached={first.noul('goal_reached')}  stuck={first.noul('stuck')}")
    print(f"   resolved model={first.model}")

if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    with JevClient() as jev:
        t0 = time.perf_counter(); jev.warm(); print(f"warm: {(time.perf_counter()-t0)*1000:.0f} ms")
        for n in (12, 30, 60):
            run(n, k, jev)

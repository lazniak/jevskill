"""Live measurement of Phase 5: speculation, self-consistency, beam K=2.

Every number in `skills/jev/references/speculate.md` comes from the file this
writes, ``bench/cu_phase5_results.json``. Nothing here touches the desktop: the
screens are the committed UIA fixtures in ``tests/fixtures/cu/``, plus frames
built from them in code (a File menu opened, a Save As dialog appearing, a
calculator display filling up) so that a *sequence* exists to predict across.
The two real snapshots are one frame each; a speculative planner cannot be
measured on one frame.

    python bench/cu_phase5_live.py              # all three, ~100 calls, ~$0.023
    python bench/cu_phase5_live.py --only beam  # one section
    python bench/cu_phase5_live.py --dry        # build everything, spend nothing

Sections are merged into the results file rather than overwriting it, so one
section can be re-run without invalidating the other two — the same rule
`bench/cu_bench.py` follows for two providers.
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jevskill import __version__  # noqa: E402
from jevskill.client import JevClient  # noqa: E402
from jevskill.cu import candidates, tree_hash  # noqa: E402
from jevskill.cu.act import (SETTLE_CAP_COMBOBOX_MS, SETTLE_CAP_MS,  # noqa: E402
                             SETTLE_TIMEOUT_MS, risky_ids)
from jevskill.cu.beam import decide_beam  # noqa: E402
from jevskill.cu.consistency import (AGREEMENT_FLOOR, NEGATION_TOLERANCE,  # noqa: E402
                                     build_consistency_bundle, read)
from jevskill.cu.decide import (COMMAND_ROLES, build_bundle, build_state,  # noqa: E402
                                decide_cascade, from_decisions, validate)
from jevskill.cu.speculate import (SPECULATION_FLOOR, SPECULATION_MARGIN,  # noqa: E402
                                   build_speculation_bundle, from_answer,
                                   identity_key, match)
from jevskill.cu.types import Snapshot, UIElement  # noqa: E402
from jevskill.orchestrate import count_tokens  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "cu"
OUT = Path(__file__).with_name("cu_phase5_results.json")


def load(name: str) -> Snapshot:
    data = json.loads((FIXTURES / ("%s.json" % name)).read_text(encoding="utf-8"))
    return Snapshot.from_dict(data["snapshot"])


def respawn(snap: Snapshot, elements) -> Snapshot:
    """A new Snapshot with a new element list, keeping window identity."""
    els = list(elements)
    return Snapshot(window_title=snap.window_title, app=snap.app, pid=snap.pid,
                    hwnd=snap.hwnd, taken_at=snap.taken_at,
                    elapsed_ms=snap.elapsed_ms, elements=els,
                    _handles={el.id: "h:" + el.id for el in els})


def retitle(snap: Snapshot, title: str) -> Snapshot:
    out = respawn(snap, snap.elements)
    out.window_title = title
    return out


# --------------------------------------------------------------------------- #
# 5.1 — three scripted sequences built from the committed fixtures
# --------------------------------------------------------------------------- #

def notepad_sequence():
    """Notepad (real, Polish UI) -> File menu -> Save As dialog -> saved.

    Frame 0 is the recorded snapshot with a ``Plik`` menu item hung off the real
    ``MenuBar`` node (the recorded tree exposes the bar but none of its items).
    Frames 1-3 add the subtrees that clicking through would reveal.
    """
    base = load("notepad")
    frame0 = list(base.elements)
    frame0.append(UIElement(id="m0", role="menuitem", name="Plik", parent="e13",
                            region="e12", depth=4, patterns=("invoke", "expand"),
                            bbox=(140, 176, 48, 24)))

    menu = list(frame0)
    menu.append(UIElement(id="m1", role="menu", name="Plik", parent="m0",
                          depth=5, bbox=(140, 200, 260, 220)))
    for index, item in enumerate(("Nowa karta", "Otwórz...", "Zapisz",
                                  "Zapisz jako...", "Drukuj...", "Zakończ")):
        menu.append(UIElement(id="mi%d" % index, role="menuitem", name=item,
                              parent="m1", region="m1", depth=6,
                              patterns=("invoke",),
                              bbox=(144, 204 + 32 * index, 250, 28)))

    dialog = [el for el in base.elements if el.id in ("e0", "e1", "e2")]
    dialog.append(UIElement(id="d0", role="dialog", name="Zapisz jako",
                            parent="e0", depth=1, bbox=(400, 300, 900, 600)))
    dialog.append(UIElement(id="d1", role="edit", name="Nazwa pliku:",
                            parent="d0", region="d0", depth=2,
                            patterns=("value",), focused=True,
                            bbox=(520, 780, 520, 28)))
    dialog.append(UIElement(id="d2", role="combobox", name="Zapisz jako typ:",
                            parent="d0", region="d0", depth=2,
                            patterns=("value", "expand"),
                            bbox=(520, 820, 520, 28)))
    dialog.append(UIElement(id="d3", role="button", name="Zapisz", parent="d0",
                            region="d0", depth=2, patterns=("invoke",),
                            bbox=(1060, 860, 100, 30)))
    dialog.append(UIElement(id="d4", role="button", name="Anuluj", parent="d0",
                            region="d0", depth=2, patterns=("invoke",),
                            bbox=(1170, 860, 100, 30)))

    filled = copy.deepcopy(dialog)
    for el in filled:
        if el.id == "d1":
            el.value = "raport.txt"

    frames = [respawn(base, frame0),
              respawn(base, menu),
              retitle(respawn(base, dialog), "Zapisz jako"),
              retitle(respawn(base, filled), "Zapisz jako")]
    return {
        "id": "notepad_save_as",
        "goal": "Zapisz dokument w pliku raport.txt",
        "frames": frames,
        # What a correct planner should predict at each hand-off, by name.
        "expected": ["Plik", "Zapisz jako...", "Nazwa pliku:", "Zapisz"],
    }


def calculator_sequence():
    """Calculator (real, Polish UI): 7 x 8 =. Four frames, the display changes."""
    base = load("calculator")

    def display(text):
        els = copy.deepcopy(list(base.elements))
        for el in els:
            if el.automation_id == "CalculatorResults":
                el.value = text
        return respawn(base, els)

    return {
        "id": "calculator_7x8",
        "goal": "Oblicz 7 razy 8",
        "frames": [display("0"), display("7"), display("7 ×"), display("7 × 8")],
        "expected": ["Siedem", "Pomnóż przez", "Osiem", "Równa się"],
    }


def delete_sequence():
    """A file list with a Delete button and the Yes/No confirmation it opens.

    Hand-built rather than derived: the point is a *destructive* sequence, and
    neither recorded fixture has one. The names are English on purpose — the
    other two sequences are Polish, and a hit rate that only holds in one
    language would be worth knowing about.
    """
    shell = [UIElement(id="w", role="window", name="Files", bbox=(0, 0, 1200, 800)),
             UIElement(id="lst", role="list", name="Documents", parent="w",
                       depth=1, bbox=(0, 80, 1200, 700))]
    for index, name in enumerate(("report.txt", "notes.md", "budget.xlsx")):
        shell.append(UIElement(id="f%d" % index, role="listitem", name=name,
                               parent="lst", region="lst", depth=2,
                               patterns=("select",), focused=(index == 0),
                               bbox=(10, 90 + 40 * index, 600, 32)))
    bar = UIElement(id="tb", role="toolbar", name="Command bar", parent="w",
                    depth=1, bbox=(0, 0, 1200, 60))
    shell.append(bar)
    for index, name in enumerate(("Copy", "Rename", "Delete", "Properties")):
        shell.append(UIElement(id="t%d" % index, role="button", name=name,
                               parent="tb", region="tb", depth=2,
                               patterns=("invoke",),
                               bbox=(10 + 110 * index, 12, 100, 36)))

    confirming = list(shell)
    confirming.append(UIElement(id="dlg", role="dialog", name="Delete file",
                                parent="w", depth=1, bbox=(400, 300, 420, 200)))
    confirming.append(UIElement(id="dtxt", role="text",
                                name="Permanently delete report.txt?",
                                parent="dlg", region="dlg", depth=2,
                                bbox=(420, 340, 380, 40)))
    for index, name in enumerate(("Yes", "No")):
        confirming.append(UIElement(id="dbtn%d" % index, role="button", name=name,
                                    parent="dlg", region="dlg", depth=2,
                                    patterns=("invoke",),
                                    bbox=(560 + 110 * index, 440, 100, 32)))

    after = [el for el in shell if el.id != "f0"]
    base = Snapshot(window_title="Files", app="explorer.exe", pid=7, hwnd=9,
                    taken_at=0.0, elapsed_ms=1.0, elements=[])
    return {
        "id": "files_delete_confirm",
        "goal": "Delete report.txt and confirm the deletion",
        "frames": [respawn(base, shell), respawn(base, confirming),
                   respawn(base, after)],
        "expected": ["Delete", "Yes", None],
    }


SEQUENCES = (notepad_sequence, calculator_sequence, delete_sequence)


def run_speculation(jev, rows):
    """One decide per frame, one speculation per hand-off; compare them."""
    total = 0.0
    for build in SEQUENCES:
        seq = build()
        frames = seq["frames"]
        steps = []
        for index, snap in enumerate(frames):
            cands = candidates(snap.elements)
            risky = risky_ids(cands)
            last = steps[-1]["action"] if steps else {"type": None, "target": None,
                                                      "outcome": None}
            bundle = build_bundle(cands, risky_ids=risky)
            state = build_state(seq["goal"], snap, last, elements=cands)
            answer = jev.decide(state, bundle)
            decision = from_decisions(answer)
            decision.questions = len(bundle)
            decision.scope_ids = [el.id for el in cands]
            verdict = validate(decision, cands, risky_ids=risky)
            element = next((el for el in cands if el.id == decision.target), None)
            total += decision.cost_usd
            steps.append({
                "frame": index,
                "tree_hash": tree_hash(cands),
                "candidates": len(cands),
                "target": decision.target,
                "target_name": element.name if element is not None else None,
                "identity": identity_key(element) if element is not None else "",
                "op": decision.op,
                "confidence": round(decision.confidence, 4),
                "valid": bool(verdict.ok),
                "verdict": verdict.reason,
                "tokens_in": decision.tokens_in,
                "cost_usd": round(decision.cost_usd, 8),
                "http_ms": round(float(decision.timing.get("http_ms", 0.0)), 1),
                "expected_name": seq["expected"][index]
                if index < len(seq["expected"]) else None,
                "action": {"type": decision.op, "target": decision.target,
                           "outcome": "changed"},
                "_cands": cands,
            })

        predictions = []
        for index in range(len(frames) - 1):
            here, nxt = steps[index], steps[index + 1]
            cands = here["_cands"]
            state = build_state(seq["goal"], frames[index],
                                {"type": here["op"], "target": here["target"],
                                 "outcome": None}, elements=cands)
            bundle = build_speculation_bundle(cands)
            t0 = time.perf_counter()
            answer = jev.decide(state, bundle)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            spec = from_answer(answer, cands, asked_over=here["tree_hash"])
            total += spec.cost_usd

            found, reason = match(spec, nxt["_cands"])
            guard = ""
            if spec.op in ("done", "blocked", "type"):
                guard = "refused_op:%s" % spec.op
            elif spec.confidence < SPECULATION_FLOOR or spec.margin < SPECULATION_MARGIN:
                guard = "low_confidence"
            elif found is not None and found.id in set(risky_ids(nxt["_cands"])):
                guard = "risky"
            usable = bool(found is not None and not guard)
            correct = bool(usable and found.id == nxt["target"]
                           and spec.op == nxt["op"])
            # Was the *prediction* right, whatever the guards then did with it?
            # Separating the two is the difference between "the model cannot
            # plan a step ahead" and "it can, and the safety rules refuse it".
            direction = bool(spec.target_key
                             and spec.target_key == nxt["identity"])
            predictions.append({
                "from_frame": index,
                "predicted": spec.target_id,
                "predicted_identity": spec.target_key,
                "predicted_op": spec.op,
                "confidence": round(spec.confidence, 4),
                "margin": round(spec.margin, 4),
                "match": reason,
                "guard": guard,
                "usable": usable,
                "correct": correct,
                "direction_correct": direction,
                "op_correct": bool(spec.op == nxt["op"]),
                "truth_target": nxt["target"],
                "truth_name": nxt["target_name"],
                "truth_op": nxt["op"],
                "tokens_in": spec.tokens_in,
                "cost_usd": round(spec.cost_usd, 8),
                "http_ms": round(float(spec.timing.get("http_ms", 0.0)), 1),
                "wall_ms": round(wall_ms, 1),
            })

        for step in steps:
            step.pop("_cands", None)
            step.pop("action", None)
        for prediction in predictions:
            prediction.pop("_cands", None)
        base_tokens = sum(s["tokens_in"] for s in steps)
        base_cost = sum(s["cost_usd"] for s in steps)
        saved = [p for p in predictions if p["correct"]]
        # With speculation on, a correct prediction removes its step's decide
        # call — and every prediction is paid for, correct or not.
        spec_tokens = (base_tokens
                       - sum(steps[p["from_frame"] + 1]["tokens_in"] for p in saved)
                       + sum(p["tokens_in"] for p in predictions))
        spec_cost = (base_cost
                     - sum(steps[p["from_frame"] + 1]["cost_usd"] for p in saved)
                     + sum(p["cost_usd"] for p in predictions))
        rows.append({
            "sequence": seq["id"],
            "goal": seq["goal"],
            "frames": len(frames),
            "steps": steps,
            "predictions": predictions,
            "summary": {
                "predictions": len(predictions),
                "usable": sum(1 for p in predictions if p["usable"]),
                "correct": len(saved),
                "hit_rate": round(len(saved) / max(1, len(predictions)), 4),
                "direction_correct": sum(1 for p in predictions
                                         if p["direction_correct"]),
                "calls_without": len(steps),
                "calls_with": len(steps) - len(saved) + len(predictions),
                "tokens_without": base_tokens,
                "tokens_with": spec_tokens,
                "tokens_delta_pct": round(100.0 * (spec_tokens - base_tokens)
                                          / max(1, base_tokens), 2),
                "cost_without_usd": round(base_cost, 8),
                "cost_with_usd": round(spec_cost, 8),
                "median_prediction_ms": round(_median(
                    [p["wall_ms"] for p in predictions]), 1),
            },
        })
        print("  %-22s hit %d/%d (target right %d)  tokens %+.1f%%  "
              "median call %.0f ms"
              % (seq["id"], rows[-1]["summary"]["correct"],
                 rows[-1]["summary"]["predictions"],
                 rows[-1]["summary"]["direction_correct"],
                 rows[-1]["summary"]["tokens_delta_pct"],
                 rows[-1]["summary"]["median_prediction_ms"]))
    return total


def _median(values):
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    return (values[mid] if len(values) % 2
            else (values[mid - 1] + values[mid]) / 2.0)


# --------------------------------------------------------------------------- #
# 5.2 — the same destructive judgement, three ways, on real command controls
# --------------------------------------------------------------------------- #

#: ``(case name, screen, goal, how many command controls)``. The two recorded
#: fixtures hold only reversible controls, so two more screens are included: the
#: confirmation dialog from the 5.1 delete sequence (genuinely irreversible) and
#: `synthetic_50`, whose "Format Document" is a **false positive of the
#: deterministic name list** — "format" matches, formatting a paragraph is not
#: destructive. A measurement of a destructiveness check that never sees a
#: destructive control says nothing.
CONSISTENCY_CASES = (
    ("notepad", lambda: load("notepad"),
     "Zamknij dokument bez zapisywania", 8),
    ("calculator", lambda: load("calculator"),
     "Wyczyść wynik i zacznij od nowa", 8),
    ("delete_dialog", lambda: delete_sequence()["frames"][1],
     "Delete report.txt and confirm the deletion", 6),
    ("synthetic_50", lambda: load("synthetic_50"),
     "Format the selected paragraph", 6),
)


def run_consistency(jev, rows, baselines):
    total = 0.0
    for name, make, goal, cap in CONSISTENCY_CASES:
        snap = make()
        cands = candidates(snap.elements)
        last = {"type": None, "target": None, "outcome": None}
        state = build_state(goal, snap, last, elements=cands)

        # The baseline a "+N tokens" claim is measured against: the ordinary
        # step bundle over the same state, which already carries the single
        # `destructive_<id>` Noul for each of these elements.
        bundle = build_bundle(cands, risky_ids=risky_ids(cands))
        step = jev.decide(state, bundle)
        total += float(step.cost_usd)
        baselines[name] = {"questions": len(bundle),
                           "tokens_in": int(step.input_tokens),
                           "cost_usd": round(float(step.cost_usd), 8),
                           "candidates": len(cands)}

        commands = [el for el in cands if el.role in COMMAND_ROLES][:cap]
        for el in commands:
            answer = jev.decide(state, build_consistency_bundle(el.id))
            out = read(answer, el.id)
            total += out.cost_usd
            single = float(step.noul("destructive_%s" % el.id) or 0.0)
            row = out.to_dict()
            row.update({
                "case": name, "goal": goal, "name": el.name, "role": el.role,
                "name_list_flagged": el.id in set(risky_ids(cands)),
                "in_step_bundle": round(single, 4),
                "direct_vs_step": round(abs(out.direct - single), 4),
                "direct_vs_restated": round(abs(out.direct - out.restated), 4),
                "direct_vs_complement": round(abs(out.direct - out.complement), 4),
                "http_ms": round(float((out.timing or {}).get("http_ms", 0.0)), 1),
            })
            rows.append(row)
            print("  %-12s %-34s direct %.2f restated %.2f rev %.2f -> %d/3%s"
                  % (name, (el.name or el.automation_id)[:34], out.direct,
                     out.restated, out.reversible, out.agree,
                     "" if out.negation_consistent else "  (negation off)"))
    return total


# --------------------------------------------------------------------------- #
# 5.3 — greedy cascade vs beam K=2, two modes
# --------------------------------------------------------------------------- #

BEAM_GOALS = (
    "Save the current document to a new file",
    "Close the sidebar and go back to the document",
    "Undo the last formatting change",
)
BEAM_SCREENS = ("synthetic_500", "synthetic_2000")

#: Two margins, because the first run answered "the margin almost never fires"
#: rather than "beam is better or worse". 0.25 is plan item 5.3's number; 0.45
#: is wide enough to force the second branch on most of these screens, which is
#: the only way to get more than one data point about what a second branch does
#: when it exists.
BEAM_MARGINS = (0.25, 0.45)


def run_beam(jev, rows):
    total = 0.0
    for screen in BEAM_SCREENS:
        snap = load(screen)
        risky = risky_ids(candidates(snap.elements))
        by_id = {el.id: el for el in snap.elements}

        def label(decision, _by_id=by_id):
            el = _by_id.get(decision.target or "")
            return el.name if el is not None else decision.target

        for goal in BEAM_GOALS:
            last = {"type": None, "target": None, "outcome": None}
            t0 = time.perf_counter()
            greedy = decide_cascade(jev, goal, snap.elements, last, cap=60,
                                    risky_ids=risky, snapshot=snap)
            greedy_ms = (time.perf_counter() - t0) * 1000.0
            total += greedy.cost_usd
            row = {
                "screen": screen, "goal": goal,
                "greedy": {"region": greedy.region, "target": greedy.target,
                           "target_name": label(greedy), "op": greedy.op,
                           "confidence": round(greedy.confidence, 4),
                           "tokens_in": greedy.tokens_in,
                           "cost_usd": round(greedy.cost_usd, 8),
                           "ms": round(greedy_ms, 1), "timing": greedy.timing},
                "margins": {},
            }
            for margin in BEAM_MARGINS:
                variants = {}
                for mode in ("calls", "fused"):
                    t0 = time.perf_counter()
                    out = decide_beam(jev, goal, snap.elements, last, cap=60,
                                      k=2, margin=margin, mode=mode,
                                      risky_ids=risky, snapshot=snap)
                    ms = (time.perf_counter() - t0) * 1000.0
                    total += out.cost_usd
                    variants[mode] = {
                        "region": out.region, "target": out.target,
                        "target_name": label(out), "op": out.op,
                        "confidence": round(out.confidence, 4),
                        "tokens_in": out.tokens_in,
                        "cost_usd": round(out.cost_usd, 8), "ms": round(ms, 1),
                        "branches": out.timing.get("branches", 1),
                        "note": out.note,
                        "changed_target": greedy.target != out.target,
                        "changed_name": label(greedy) != label(out),
                    }
                row["margins"]["%.2f" % margin] = variants
                print("  %-15s m=%.2f %-40s greedy=%-18s calls=%-18s "
                      "fused=%-18s br=%d"
                      % (screen, margin, goal[:40], str(label(greedy))[:18],
                         str(variants["calls"]["target_name"])[:18],
                         str(variants["fused"]["target_name"])[:18],
                         variants["calls"]["branches"]))
            # Filled in by hand after the run. An automated "better" here would
            # be this script grading its own homework.
            row["manual_judgement"] = None
            rows.append(row)
    return total


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def host_facts() -> dict:
    try:
        import httpx  # noqa: F401
        has_httpx = True
    except Exception:
        has_httpx = False
    return {"python": platform.python_version(), "platform": platform.platform(),
            "httpx": has_httpx, "jevskill": __version__}


def merge(payload: dict) -> None:
    data: dict = {}
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.setdefault("about", (
        "Phase 5 measurement: speculative planning (5.1), self-consistency for "
        "irreversible actions (5.2) and beam K=2 over the cascade (5.3). Written "
        "by bench/cu_phase5_live.py; every number in "
        "skills/jev/references/speculate.md comes from this file."))
    data["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["host"] = host_facts()
    for key, value in payload.items():
        data[key] = value
    # Sum what is *in the file*, not what this invocation spent: a `--only`
    # re-run must not leave the total describing one section.
    data["total_cost_usd"] = round(sum(
        float(data.get(section, {}).get("section_cost_usd", 0.0) or 0.0)
        for section in ("speculation", "consistency", "beam")), 8)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("\nwrote %s" % OUT)


def dry_run() -> int:
    print("== 5.1 sequences ==")
    for build in SEQUENCES:
        seq = build()
        for index, snap in enumerate(seq["frames"]):
            cands = candidates(snap.elements)
            step = count_tokens(build_state(seq["goal"], snap, None, elements=cands))
            spec = count_tokens(build_speculation_bundle(cands))
            print("  %-22s frame %d: %2d candidates, state~%d tok, "
                  "speculation bundle~%d tok"
                  % (seq["id"], index, len(cands), step, spec))
    print("\n== 5.2 consistency ==")
    for name, make, goal, cap in CONSISTENCY_CASES:
        cands = candidates(make().elements)
        commands = [el for el in cands if el.role in COMMAND_ROLES][:cap]
        trio = count_tokens(build_consistency_bundle(commands[0].id)) if commands else 0
        print("  %-12s %d candidates, %d command controls, trio bundle~%d tok"
              % (name, len(cands), len(commands), trio))
    print("\n== 5.3 beam ==")
    for screen in BEAM_SCREENS:
        cands = candidates(load(screen).elements)
        print("  %-16s %d candidates after reduction (cascade %s)"
              % (screen, len(cands), "fires" if len(cands) >= 60 else "does NOT fire"))
    calls = sum(2 * len(b()["frames"]) - 1 for b in SEQUENCES)
    calls += sum(cap + 1 for _, _, _, cap in CONSISTENCY_CASES)
    calls += len(BEAM_SCREENS) * len(BEAM_GOALS) * (2 + len(BEAM_MARGINS) * 5)
    print("\nupper bound on live calls: ~%d" % calls)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true",
                        help="build every bundle and print sizes; make no calls")
    parser.add_argument("--only", choices=("speculate", "consistency", "beam"),
                        action="append", default=None,
                        help="run one section (repeatable); default is all three")
    args = parser.parse_args()
    if args.dry:
        return dry_run()

    sections = set(args.only or ("speculate", "consistency", "beam"))
    payload: dict = {}
    total = 0.0
    with JevClient(hot=True) as jev:
        jev.warm()
        if "speculate" in sections:
            print("\n===== 5.1 speculative plan =====")
            rows: list = []
            spent = run_speculation(jev, rows)
            total += spent
            payload["speculation"] = {
                "section_cost_usd": round(spent, 8),
                "floor": SPECULATION_FLOOR, "margin": SPECULATION_MARGIN,
                "settle_caps_ms": {"default_timeout": SETTLE_TIMEOUT_MS,
                                   "published_cap": SETTLE_CAP_MS,
                                   "combobox_cap": SETTLE_CAP_COMBOBOX_MS},
                "sequences": rows,
                "totals": _speculation_totals(rows),
            }
        if "consistency" in sections:
            print("\n===== 5.2 self-consistency =====")
            rows, baselines = [], {}
            spent = run_consistency(jev, rows, baselines)
            total += spent
            payload["consistency"] = {
                "section_cost_usd": round(spent, 8),
                "floor": AGREEMENT_FLOOR, "tolerance": NEGATION_TOLERANCE,
                "step_bundle_baseline": baselines,
                "elements": rows,
                "totals": _consistency_totals(rows, baselines),
            }
        if "beam" in sections:
            print("\n===== 5.3 beam K=2 =====")
            rows = []
            spent = run_beam(jev, rows)
            total += spent
            payload["beam"] = {"section_cost_usd": round(spent, 8),
                               "margins": list(BEAM_MARGINS), "cases": rows,
                               "totals": _beam_totals(rows)}

    merge(payload)
    print("TOTAL COST THIS RUN: $%.6f" % total)
    return 0


def _speculation_totals(rows):
    predictions = [p for r in rows for p in r["predictions"]]
    steps = [s for r in rows for s in r["steps"]]
    correct = [p for p in predictions if p["correct"]]
    guards: dict = {}
    for p in predictions:
        key = p["guard"] or ("match:%s" % p["match"] if not p["usable"] else "used")
        guards[key] = guards.get(key, 0) + 1
    base_tokens = sum(r["summary"]["tokens_without"] for r in rows)
    with_tokens = sum(r["summary"]["tokens_with"] for r in rows)
    return {
        "predictions": len(predictions),
        "usable": sum(1 for p in predictions if p["usable"]),
        "correct": len(correct),
        "hit_rate": round(len(correct) / max(1, len(predictions)), 4),
        "direction_correct": sum(1 for p in predictions if p["direction_correct"]),
        "direction_rate": round(sum(1 for p in predictions if p["direction_correct"])
                                / max(1, len(predictions)), 4),
        "op_correct": sum(1 for p in predictions if p["op_correct"]),
        "outcomes": guards,
        "steps": len(steps),
        "calls_without": sum(r["summary"]["calls_without"] for r in rows),
        "calls_with": sum(r["summary"]["calls_with"] for r in rows),
        "tokens_without": base_tokens,
        "tokens_with": with_tokens,
        "tokens_delta_pct": round(100.0 * (with_tokens - base_tokens)
                                  / max(1, base_tokens), 2),
        "cost_without_usd": round(sum(r["summary"]["cost_without_usd"] for r in rows), 8),
        "cost_with_usd": round(sum(r["summary"]["cost_with_usd"] for r in rows), 8),
        "prediction_ms_median": round(_median([p["wall_ms"] for p in predictions]), 1),
        "prediction_ms_max": round(max([p["wall_ms"] for p in predictions] or [0.0]), 1),
        "fits_in_published_settle_cap": sum(
            1 for p in predictions if p["wall_ms"] <= SETTLE_CAP_MS),
        "fits_in_default_settle_timeout": sum(
            1 for p in predictions if p["wall_ms"] <= SETTLE_TIMEOUT_MS),
    }


def _consistency_totals(rows, baselines):
    if not rows:
        return {}
    disagree = [r for r in rows if not (r["agree"] in (0, 3))]
    inconsistent = [r for r in rows if not r["negation_consistent"]]
    return {
        "elements": len(rows),
        "disagreements": len(disagree),
        "disagreement_rate": round(len(disagree) / len(rows), 4),
        "negation_inconsistent": len(inconsistent),
        "negation_inconsistent_rate": round(len(inconsistent) / len(rows), 4),
        "mean_direct_vs_restated": round(
            sum(r["direct_vs_restated"] for r in rows) / len(rows), 4),
        "mean_direct_vs_complement": round(
            sum(r["direct_vs_complement"] for r in rows) / len(rows), 4),
        "mean_direct_vs_step_bundle": round(
            sum(r["direct_vs_step"] for r in rows) / len(rows), 4),
        "max_spread": round(max(r["spread"] for r in rows), 4),
        "ruled_destructive": sum(1 for r in rows if r["destructive"]),
        "single_noul_would_gate": sum(1 for r in rows
                                      if r["direct"] >= AGREEMENT_FLOOR),
        "name_list_flagged": sum(1 for r in rows if r.get("name_list_flagged")),
        "two_of_three_without_negation_rule": sum(
            1 for r in rows if r["agree"] >= 2),
        "tokens_in": sum(r["tokens_in"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 8),
        "step_bundle_tokens": {k: v["tokens_in"] for k, v in baselines.items()},
        # Are the two direct formulations independent evidence, or the same
        # evidence twice? A correlation near 1 means self-consistency over
        # phrasings cannot rescue an under-reading Noul, because both
        # formulations under-read together.
        "direct_restated_r": _pearson([r["direct"] for r in rows],
                                      [r["restated"] for r in rows]),
        "direct_complement_r": _pearson([r["direct"] for r in rows],
                                        [r["complement"] for r in rows]),
        "reversible_mean": round(sum(r["reversible"] for r in rows) / len(rows), 4),
        "reversible_min": round(min(r["reversible"] for r in rows), 4),
        "reversible_max": round(max(r["reversible"] for r in rows), 4),
        # What the rule would catch at other floors, with the negation dropped.
        # Post-processing of answers already paid for; no extra calls.
        "floor_sweep": [
            {"floor": round(floor, 2),
             "two_formulations": sorted(r["name"] for r in rows
                                        if r["direct"] >= floor
                                        and r["restated"] >= floor),
             # The control question: what does the *single* Noul the step
             # bundle already carries catch at the same floor? If the two
             # columns match, self-consistency bought nothing the threshold
             # did not, and it cost an extra call to buy it.
             "single_noul": sorted(r["name"] for r in rows
                                   if r["in_step_bundle"] >= floor)}
            for floor in (0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50)
        ],
    }


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(num / dx / dy, 4) if dx and dy else 0.0


def _beam_totals(rows):
    if not rows:
        return {}
    out = {"cases": len(rows),
           "tokens_greedy": sum(r["greedy"]["tokens_in"] for r in rows),
           "ms_greedy_median": round(_median([r["greedy"]["ms"] for r in rows]), 1),
           "cost_greedy_usd": round(sum(r["greedy"]["cost_usd"] for r in rows), 8),
           "by_margin": {}}
    for margin in BEAM_MARGINS:
        key = "%.2f" % margin
        variants = {}
        for mode in ("calls", "fused"):
            cells = [r["margins"][key][mode] for r in rows if key in r["margins"]]
            if not cells:
                continue
            branched = [c for c in cells if c["branches"] > 1]
            variants[mode] = {
                "branched": len(branched),
                "changed_target": sum(1 for c in cells if c["changed_target"]),
                "changed_name": sum(1 for c in cells if c["changed_name"]),
                "tokens_in": sum(c["tokens_in"] for c in cells),
                "tokens_delta_pct": round(
                    100.0 * (sum(c["tokens_in"] for c in cells) - out["tokens_greedy"])
                    / max(1, out["tokens_greedy"]), 2),
                "ms_median": round(_median([c["ms"] for c in cells]), 1),
                "cost_usd": round(sum(c["cost_usd"] for c in cells), 8),
                # Only the branched cases say anything about beam vs greedy; the
                # rest ran the identical code path and would dilute any average.
                "branched_tokens": sum(c["tokens_in"] for c in branched),
                "branched_greedy_tokens": sum(
                    r["greedy"]["tokens_in"] for r in rows
                    if key in r["margins"] and r["margins"][key][mode]["branches"] > 1),
                "branched_ms_median": round(_median([c["ms"] for c in branched]), 1),
                "branched_greedy_ms_median": round(_median(
                    [r["greedy"]["ms"] for r in rows
                     if key in r["margins"] and r["margins"][key][mode]["branches"] > 1]), 1),
            }
        out["by_margin"][key] = variants
    return out


if __name__ == "__main__":
    raise SystemExit(main())

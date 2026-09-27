"""A simulated Windows desktop for the operator: Notepad and its Save As dialog.

Not a mock of UIA calls — a small state machine that produces real
:class:`jevskill.cu.types.Snapshot` objects (with live "handles") and a backend
that changes the state when an action lands, so the operator's own code paths
— observe by handle, execute through ``act.execute``, settle on a tree hash,
learn from what changed — run unmodified. What it deliberately reproduces from
the live runs (``bench/cu_live_save_run.json``):

* Notepad's editor is a ``document`` with a ValuePattern (SetValue works on a
  background window); the Save As dialog opens on ``ctrl+shift+s`` only.
* The dialog lists the files already on the Desktop, so its tree differs every
  time a file is added — the reason a recipe resolves controls by identity and
  not by a hash of the screen.
* A "Pomoc" button that does nothing, for lessons to be learned from.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from jevskill.cu.contract import RunResult, StepRecord
from jevskill.cu.types import Snapshot, UIElement

NOTEPAD_HWND = 100
DIALOG_HWND = 150
PID = 5


class SimDesktop:
    def __init__(self, desktop_files: Optional[List[str]] = None) -> None:
        self.windows: set = set()
        self.text = ""
        self.dialog = False
        self.file_field = ""
        self.title = "Bez tytułu - Notatnik"
        self.saved: Dict[str, str] = {}
        self.desktop_files = list(desktop_files or ["notatki.txt"])
        self.observations = 0
        self.actions: List[str] = []
        self.front = 999   # the user's own window, until something takes it

    # ---- what the operator reads -----------------------------------------
    def launch(self, _target: str) -> None:
        self.windows.add(NOTEPAD_HWND)

    def window_title(self, hwnd: int) -> str:
        if hwnd == NOTEPAD_HWND:
            return self.title
        if hwnd == DIALOG_HWND:
            return "Zapisz jako"
        return "Discord"

    def active_popup(self, hwnd: int) -> int:
        return DIALOG_HWND if self.dialog else hwnd

    def window_pid(self, hwnd: int) -> int:
        return PID if hwnd in (NOTEPAD_HWND, DIALOG_HWND) else 7

    def snapshot(self, hwnd: int) -> Snapshot:
        self.observations += 1
        if self.dialog:
            elements = [
                UIElement("e0", "dialog", "Zapisz jako", bbox=(100, 100, 600, 400)),
                UIElement("e1", "button", "Pulpit", patterns=("invoke",), bbox=(110, 150, 80, 20),
                          automation_id="nav-desktop", class_name="TreeItem"),
            ]
            for index, name in enumerate(self.desktop_files):
                elements.append(UIElement("f%d" % index, "listitem", name, patterns=("select",),
                                          bbox=(200, 150 + 22 * index, 200, 20),
                                          class_name="UIItem"))
            elements += [
                UIElement("e70", "edit", "Nazwa pliku:", value=self.file_field,
                          patterns=("value",), automation_id="1001", class_name="Edit",
                          bbox=(200, 440, 300, 22)),
                UIElement("e71", "button", "Zapisz", patterns=("invoke",), automation_id="1",
                          class_name="Button", bbox=(520, 470, 80, 24)),
                UIElement("e72", "button", "Anuluj", patterns=("invoke",), automation_id="2",
                          class_name="Button", bbox=(610, 470, 80, 24)),
            ]
            title, handle = "Zapisz jako", DIALOG_HWND
        else:
            elements = [
                UIElement("e0", "window", self.title, bbox=(0, 0, 800, 600)),
                UIElement("e1", "button", "Plik", patterns=("invoke", "expand"),
                          automation_id="File", class_name="MenuBarItem", bbox=(5, 30, 40, 20)),
                UIElement("e2", "button", "Pomoc", patterns=("invoke",), automation_id="Help",
                          class_name="Button", bbox=(50, 30, 40, 20)),
                UIElement("e3", "document", "Edytor tekstu", value=self.text,
                          patterns=("value", "scroll"), class_name="RichEditD2DPT",
                          bbox=(0, 60, 800, 520)),
            ]
            title, handle = self.title, NOTEPAD_HWND
        snap = Snapshot(window_title=title, app="Notepad.exe", pid=PID, hwnd=handle,
                        taken_at=0.0, elapsed_ms=1.0, elements=elements)
        snap._handles = {el.id: el.id for el in elements}
        return snap

    # ---- what an action does ---------------------------------------------
    def save(self) -> None:
        name = self.file_field.replace("/", "\\").split("\\")[-1]
        self.saved[name] = self.text
        self.desktop_files.append(name)
        self.title = "%s - Notatnik" % name
        self.dialog = False
        self.file_field = ""


class SimBackend:
    """The UiaBackend surface ``act.execute`` calls, acting on a SimDesktop."""

    def __init__(self, desk: SimDesktop) -> None:
        self.desk = desk

    def set_value(self, handle: Any, text: str) -> None:
        self.desk.actions.append("setvalue %s %r" % (handle, text))
        if handle == "e3" and not self.desk.dialog:
            self.desk.text = text
        elif handle == "e70" and self.desk.dialog:
            self.desk.file_field = text
        else:
            raise RuntimeError("no value pattern on %s" % handle)

    def invoke(self, handle: Any) -> None:
        self.desk.actions.append("invoke %s" % handle)
        if self.desk.dialog and handle == "e71":
            self.desk.save()
        elif self.desk.dialog and handle == "e72":
            self.desk.dialog = False
        # "Pomoc" (e2), "Plik" (e1) and "Pulpit" do nothing here.

    def send_keys(self, keys: str) -> None:
        self.desk.actions.append("keys %s" % keys)
        if keys == "ctrl+shift+s" and not self.desk.dialog:
            self.desk.dialog = True

    def expand(self, handle: Any) -> None:
        self.desk.actions.append("expand %s" % handle)

    def select(self, handle: Any) -> None:
        self.desk.actions.append("select %s" % handle)

    def focus(self, handle: Any) -> None:
        pass

    def send_text(self, text: str) -> None:
        raise RuntimeError("the simulation only takes SetValue")


def scripted_loop(desk: SimDesktop, calls: List[str], *, fumble: bool = True):
    """Stands in for the Jev loop on the first, unlearned run: what Jev would
    decide on each goal, performed through the operator's own hooks.

    ``fumble`` makes the Save As goal first click "Pomoc", which does nothing —
    the mistake a lesson should come out of.
    """
    from jevskill.cu.act import Action

    def loop(goal, opts=None, **hooks):
        calls.append(goal)
        observe, execute = hooks["observe"], hooks["execute"]
        verify, compose = hooks["verify"], hooks["compose_text"]
        snap = observe()
        lowered = goal.lower()

        def find(predicate):
            return next(el for el in snap.elements if predicate(el))

        steps = 0
        if lowered.startswith("type") and "editor" in lowered:
            doc = find(lambda el: el.role == "document")
            execute(Action(op="type", target=doc.id, text=compose(goal, doc)), snap)
            steps += 1
        elif "save as dialog" in lowered:
            if fumble:
                execute(Action(op="click", target=find(lambda el: el.name == "Pomoc").id), snap)
                snap = observe()
                steps += 1
            execute(Action(op="key", key="ctrl+shift+s"), snap)
            steps += 1
        elif "file name" in lowered:
            field = find(lambda el: el.role == "edit")
            execute(Action(op="type", target=field.id, text=compose(goal, field)), snap)
            snap = observe()
            execute(Action(op="click", target=find(lambda el: el.name == "Zapisz").id), snap)
            steps += 2
        snap = observe()
        done = verify(goal, snap) if verify is not None else None
        records = [StepRecord(index=i, t_ms=0.0, decided_by="jev", executed=True)
                   for i in range(max(1, steps))]
        return RunResult(task_id="sim", stop_reason="done" if done else "escalated",
                         steps=records)

    return loop


SAVE_PLAN = {"steps": [
    {"goal": "Open Notepad", "done_when": "Notepad window title contains 'Notatnik'",
     "launch": "notepad.exe"},
    {"goal": 'Type "hello world" into the editor',
     "done_when": 'The editor contains "hello world"'},
    {"goal": "Open the Save As dialog", "done_when": "A dialog titled 'Zapisz jako' is open"},
    {"goal": "Type the full file path into the file name field and save",
     "done_when": "The Notepad title contains 'hello.txt'"},
], "note": "four goals"}

SAVE_COMMAND = "Otwórz Notatnik, wpisz „hello world” i zapisz jako hello.txt na pulpicie"


def sim_operator_kwargs(desk: SimDesktop, loop) -> Dict[str, Any]:
    """Everything the operator needs to run against ``desk`` offline."""
    def bring(hwnd):
        desk.front = hwnd
        return True

    return dict(
        platform_check=lambda: {"ok": True, "reason": ""},
        launcher=desk.launch,
        top_windows=lambda: set(desk.windows),
        foreground_hwnd=lambda: desk.front,
        foreground_title=lambda: desk.window_title(desk.front),
        window_title=desk.window_title,
        window_pid=desk.window_pid,
        window_process=lambda h: "Notepad.exe" if h in (NOTEPAD_HWND, DIALOG_HWND) else "Discord.exe",
        active_popup=desk.active_popup,
        observe=lambda: desk.snapshot(desk.front),
        observe_window=desk.snapshot,
        bring_to_front=bring,
        backend_factory=lambda: SimBackend(desk),
        run_loop=loop,
    )

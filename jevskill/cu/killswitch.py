"""Four ways to stop an agent that is holding the mouse and the keyboard.

A computer-use run acts on the user's own desktop, so "how do I make it stop"
is the first thing the panel has to answer, and every answer has to work while
the agent is typing. None of them depends on the page having focus:

1. **The STOP button** in the console — ``POST /api/cu/stop`` — for when the
   browser is visible.
2. **Ctrl+Alt+Esc** anywhere. Polled with ``GetAsyncKeyState`` rather than
   registered with ``RegisterHotKey``: polling needs no message loop, never
   collides with an application that registered the same chord, and works when
   a full-screen application holds the foreground. The chord is bound to
   nothing in Windows itself (Ctrl+Shift+Esc is Task Manager, Ctrl+Alt+Del is
   the secure attention sequence) and the agent never sends it — its chords
   come from :func:`jevskill.cu.act.chord_for`.
3. **The mouse in the top-left corner** of the primary screen — the convention
   ``pyautogui`` made familiar. The agent clicks control centres and no control
   has its centre at (0, 0).
4. **A stop file** — ``jevskill cu stop`` from any terminal touches it. This is
   the one that works over SSH, from a scheduled task, or from a second agent.

The switch is *armed* only while a run is active, so a stale Ctrl+Alt+Esc or a
mouse parked in the corner between runs does nothing. Every trigger records
*which* way it was tripped; the operator reports that in the run's events.

On a machine without ``ctypes.windll`` (CI, macOS) the key and mouse checks are
skipped and the file and the event still work, which is what the tests use.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

STOP_FILE_NAME = "cu.stop"
DEFAULT_POLL_S = 0.05
HOTKEY_LABEL = "Ctrl+Alt+Esc"
CORNER_PX = 2

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
_KEY_DOWN = 0x8000


class Stopped(RuntimeError):
    """Raised inside the loop's hooks when the switch has been tripped.

    The loop catches any exception, ends the run with ``stop_reason="error"``
    and the message in ``error``; the operator recognises this class by name
    and reports the run as **stopped**, not failed.
    """


def _user32() -> Any:
    try:
        import ctypes

        return ctypes.windll.user32  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None


class KillSwitch:
    """The shared stop signal plus the watchers that can raise it."""

    def __init__(self, stop_file: Optional[os.PathLike] = None, *,
                 hotkey: bool = True, corner: bool = True,
                 poll_s: float = DEFAULT_POLL_S) -> None:
        self.stop_file = Path(stop_file) if stop_file is not None else None
        self.hotkey_enabled = bool(hotkey)
        self.corner_enabled = bool(corner)
        self.poll_s = max(0.01, float(poll_s))
        self.event = threading.Event()
        self.reason: str = ""
        self.tripped_at: float = 0.0
        self._thread: Optional[threading.Thread] = None
        self._armed = threading.Event()
        self._user32 = _user32()

    # -- describing -----------------------------------------------------------
    @property
    def desktop_checks(self) -> bool:
        """True when the key and mouse checks can actually run here."""
        return self._user32 is not None

    def describe(self) -> Dict[str, Any]:
        return {
            "button": True,
            "hotkey": HOTKEY_LABEL if (self.hotkey_enabled and self.desktop_checks) else None,
            "corner": "top-left" if (self.corner_enabled and self.desktop_checks) else None,
            "stop_file": str(self.stop_file) if self.stop_file else None,
            "cli": "jevskill cu stop",
            "armed": self._armed.is_set(),
            "tripped": self.event.is_set(),
            "reason": self.reason,
        }

    # -- tripping -------------------------------------------------------------
    def trigger(self, reason: str) -> None:
        if not self.event.is_set():
            self.reason = reason
            self.tripped_at = time.time()
        self.event.set()

    def reset(self) -> None:
        self.event.clear()
        self.reason = ""
        self.tripped_at = 0.0

    def check(self) -> Optional[str]:
        """Look once, right now. Returns the reason when tripped, else ``None``.

        The operator calls this from the ``observe`` and ``execute`` hooks so a
        stop that arrives between the watcher's polls is still honoured before
        the next action lands on the desktop.
        """
        if not self.event.is_set():
            reason = self._poll_once()
            if reason:
                self.trigger(reason)
        return self.reason if self.event.is_set() else None

    def raise_if_tripped(self) -> None:
        reason = self.check()
        if reason:
            raise Stopped(reason)

    # -- watching -------------------------------------------------------------
    def arm(self) -> None:
        """Start watching. A stale stop file from an earlier run is removed."""
        self.reset()
        if self.stop_file is not None:
            try:
                self.stop_file.unlink()
            except OSError:
                pass
        self._armed.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._watch, name="jev-killswitch",
                                            daemon=True)
            self._thread.start()

    def disarm(self) -> None:
        self._armed.clear()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _watch(self) -> None:
        while self._armed.is_set():
            if self.event.is_set():
                break
            reason = self._poll_once()
            if reason:
                self.trigger(reason)
                break
            time.sleep(self.poll_s)

    def _poll_once(self) -> Optional[str]:
        if self.stop_file is not None and self.stop_file.exists():
            try:
                self.stop_file.unlink()
            except OSError:
                pass
            return "stop file (jevskill cu stop)"
        user32 = self._user32
        if user32 is None:
            return None
        if self.hotkey_enabled and self._chord_down(user32):
            return "hotkey %s" % HOTKEY_LABEL
        if self.corner_enabled:
            pos = self._cursor(user32)
            if pos is not None and pos[0] <= CORNER_PX and pos[1] <= CORNER_PX:
                return "mouse in the top-left corner"
        return None

    @staticmethod
    def _chord_down(user32: Any) -> bool:
        try:
            return all(user32.GetAsyncKeyState(vk) & _KEY_DOWN
                       for vk in (VK_CONTROL, VK_MENU, VK_ESCAPE))
        except Exception:
            return False

    @staticmethod
    def _cursor(user32: Any) -> Optional[Tuple[int, int]]:
        try:
            import ctypes
            from ctypes import wintypes

            point = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            return None
        return None


def touch_stop_file(path: os.PathLike) -> Path:
    """What ``jevskill cu stop`` does: create the file the watcher polls for."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stop\n", encoding="utf-8")
    return target


__all__ = ["CORNER_PX", "HOTKEY_LABEL", "KillSwitch", "STOP_FILE_NAME", "Stopped",
           "touch_stop_file"]

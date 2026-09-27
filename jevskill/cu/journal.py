"""On-disk journal for computer-use runs: one directory per run, written so a
crash or a STOP never loses more than the last unflushed heartbeat.

Why a directory with two files instead of one blob: ``run.json`` is the
current snapshot (small, rewritten often, must stay atomic so a reader never
sees a half-written tree), while ``events.jsonl`` is an append-only log (the
full history the operator's in-memory ring buffer has already trimmed). A
single growing JSON file would force a full rewrite for every event, and an
atomic replace of a multi-megabyte file on every heartbeat is the kind of
cost that turns "always checkpoint" into "checkpoint sometimes" -- exactly
the failure mode this module exists to avoid.

Stdlib only: this module is imported by ``runner.py`` before the desktop
backend is touched, and it must work in the offline test suite with no
network and no optional dependency. It must NOT import ``jevskill.cu.runner``
(the reverse import is the real one) or ``jevskill.cu.agenda`` (written
concurrently) -- ``resumable()`` walks the checkpointed tree as plain dicts
instead of rebuilding ``PlanNode`` objects, which also keeps a corrupt or
foreign tree from crashing the listing.

Every public method that takes a ``run_id`` calls :func:`valid_run_id` first.
Run ids arrive over HTTP (``/api/cu/resume``, the resume CLI), so this is the
path-traversal guard, not a cosmetic check: without it a ``run_id`` of
``"../../../etc"`` would resolve outside ``root``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SCHEMA",
    "RUN_ID_RE",
    "STALE_S",
    "ACTIVE_STATES",
    "EVENTS_FILE",
    "HEARTBEAT_FILE",
    "RUN_FILE",
    "UnknownRun",
    "RunNotResumable",
    "valid_run_id",
    "RunStore",
]

#: Bumped only if the on-disk shape of ``run.json`` changes incompatibly.
SCHEMA = 1

#: ``YYYYMMDD-HHMMSS-hex6``, e.g. ``20260927-221530-a1b2c3``. Anchored on both
#: ends so a partial match (a value that merely *starts* with a good id, such
#: as ``"20260927-221530-a1b2c3/../x"``) is rejected, not accepted.
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

#: A run whose state is still one of these is presumed alive unless its
#: heartbeat says otherwise. Kept as a tuple (not a set) so it prints in a
#: stable, readable order in error messages and docstrings.
ACTIVE_STATES = ("planning", "waiting_window", "running", "waiting_confirm", "paused")

#: Above this age (seconds) a heartbeat from an "active" state is treated as
#: dead rather than merely slow -- the operator heartbeats every few seconds
#: while alive, so 30s is many missed beats, not a slow tick.
STALE_S = 30.0

EVENTS_FILE = "events.jsonl"
RUN_FILE = "run.json"
#: A tiny file holding the last heartbeat, written by a timer thread while a
#: run is alive. ``run.json`` alone was not enough: it is written from the run
#: thread, and that thread blocks for up to a minute inside a model call — longer
#: than STALE_S — so a live run looked dead to a second console (review finding,
#: 2026-09-27). A separate file lets the beat be written without serialising the
#: tree the run thread is changing.
HEARTBEAT_FILE = "heartbeat"
#: How much of the end of ``events.jsonl`` ``last_event_index`` reads.
_TAIL_BYTES = 65536

#: Leaf/phase statuses that still represent open work inside a checkpointed
#: tree. Duplicated here (rather than imported from ``agenda.py``) because
#: this module must not import that module -- see the module docstring.
_OPEN_TREE_STATUSES = ("pending", "interrupted", "stopped", "running")


class UnknownRun(LookupError):
    """No usable checkpoint exists for a run id (missing, corrupt, or a
    schema this build does not understand). The web layer maps this to a
    404: the run id itself was fine, there is just nothing there."""


class RunNotResumable(LookupError):
    """A checkpoint exists but resuming it would be wrong right now: the run
    already finished, it looks alive elsewhere (fresh heartbeat), or a dry
    run was asked to resume live. The web layer maps this to a 409."""


def valid_run_id(run_id: Any) -> str:
    """Return ``run_id`` unchanged if it is a well-formed id, else raise
    ``ValueError``.

    This is the path-traversal guard for every method below: a run id that
    does not match :data:`RUN_ID_RE` can never be turned into a path
    component, so ``"../x"``, ``""``, a bare ``"x"``, and anything containing
    ``os.sep`` are all rejected the same way, before any filesystem call is
    made.
    """
    if not isinstance(run_id, str):
        raise ValueError("run_id must be a str, got %r" % (type(run_id).__name__,))
    if not RUN_ID_RE.match(run_id):
        raise ValueError("run_id %r does not match %s" % (run_id, RUN_ID_RE.pattern))
    return run_id


def _run_id_epoch(run_id: str) -> float:
    """Best-effort chronological value for a run id, used only when a
    checkpoint has no usable ``updated_at``. The id's own timestamp prefix
    sorts correctly as a string already; converting it to a real epoch value
    lets it compare directly against ``updated_at`` floats from other rows
    instead of needing two incompatible sort keys."""
    try:
        return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").timestamp()
    except (ValueError, IndexError):
        return 0.0


def _is_number(value: Any) -> bool:
    """``bool`` is a ``int`` subclass in Python; a stray ``True``/``False``
    in a hand-written or corrupted payload should not be treated as a
    timestamp or an event index just because ``isinstance(x, int)`` holds."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class RunStore:
    """Best-effort persistence for one computer-use run directory tree.

    Every write method returns ``False`` on failure instead of raising --
    the operator must be able to keep running a mission even when the disk
    is briefly unwritable; losing the ability to *resume* later is an
    acceptable, logged degradation, losing the run in progress is not.
    Read methods raise :class:`UnknownRun` because a missing/corrupt
    checkpoint on `load` is a real error the caller must handle, not
    something to paper over with an empty dict.
    """

    def __init__(self, root: Any) -> None:
        # Nothing is created here: an idle operator that never starts a run
        # should never produce a `cu_runs/` directory.
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        run_id = valid_run_id(run_id)
        return self.root / run_id

    def checkpoint(self, run_id: str, payload: Dict[str, Any]) -> bool:
        """Atomically write ``run_id/run.json``.

        ``tempfile.mkstemp`` in the *same* directory followed by
        ``os.replace`` (the pattern already used by ``Experience.save``,
        see ``jevskill/cu/experience.py``) guarantees a reader never
        observes a half-written file: ``os.replace`` is a single directory
        entry swap on every platform this project targets, unlike writing
        the destination path directly and hoping nothing reads it mid-write.
        """
        run_id = valid_run_id(run_id)
        run_dir = self.root / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False

        data = dict(payload)
        data.setdefault("schema", SCHEMA)
        data["updated_at"] = time.time()
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # Something in the payload is not JSON-serialisable even with
            # `default=str`. Never let a bad field crash the run.
            return False

        try:
            handle, tmp = tempfile.mkstemp(
                dir=str(run_dir), prefix=RUN_FILE + ".", suffix=".tmp"
            )
        except OSError:
            return False
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(tmp, str(run_dir / RUN_FILE))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        return True

    def append_event(self, run_id: str, item: Dict[str, Any]) -> bool:
        """Append one JSON line to ``run_id/events.jsonl``, flushed.

        This is a plain append, not an atomic replace: a torn last line from
        a mid-write crash is an acceptable loss for a log that already has
        an in-memory ring buffer as the primary read path, whereas an
        atomic-replace-per-event would multiply the write cost of a long run
        by its event count.
        """
        run_id = valid_run_id(run_id)
        run_dir = self.root / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        try:
            line = json.dumps(item, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        try:
            with open(run_dir / EVENTS_FILE, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
        except OSError:
            return False
        return True

    def load(self, run_id: str) -> Dict[str, Any]:
        """Return the checkpointed payload for ``run_id``.

        Raises :class:`UnknownRun` for every way a checkpoint can fail to be
        usable -- missing file, unreadable file, invalid JSON, a JSON value
        that is not an object, or a schema this build does not understand --
        so callers have exactly one exception to catch instead of a matrix
        of ``OSError``/``ValueError``/``TypeError``.
        """
        run_id = valid_run_id(run_id)
        path = self.root / run_id / RUN_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise UnknownRun(run_id)
        try:
            data = json.loads(text)
        except ValueError:
            raise UnknownRun(run_id)
        if not isinstance(data, dict):
            raise UnknownRun(run_id)
        if data.get("schema") != SCHEMA:
            raise UnknownRun(run_id)
        beat = self._beat_at(run_id)
        if beat is not None:
            saved = data.get("heartbeat_at")
            if not _is_number(saved) or beat > saved:
                data["heartbeat_at"] = beat
        return data

    def beat(self, run_id: str, when: Optional[float] = None) -> bool:
        """Record that the run is alive, without touching ``run.json``.

        Atomic like :meth:`checkpoint`, and as forgiving: a failed beat only
        makes a live run look older to another console.
        """
        run_id = valid_run_id(run_id)
        run_dir = self.root / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=str(run_dir), prefix=HEARTBEAT_FILE + ".",
                                           suffix=".tmp")
        except OSError:
            return False
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(repr(float(time.time() if when is None else when)))
            os.replace(tmp, str(run_dir / HEARTBEAT_FILE))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        return True

    def _beat_at(self, run_id: str) -> Optional[float]:
        try:
            text = (self.root / run_id / HEARTBEAT_FILE).read_text(encoding="utf-8")
            value = float(text.strip())
        except (OSError, ValueError):
            return None
        return value if value > 0 else None

    def last_event_index(self, run_id: str) -> int:
        """The largest ``i`` in ``events.jsonl``, or -1.

        Events reach the file as they happen; ``events_next`` in ``run.json``
        only at checkpoints. A resume that numbered from the checkpoint reused
        up to a heartbeat's worth of indices a crashed segment had already
        written, and ``events(since)`` then returned both copies interleaved.
        Only the tail is read: the file of a long mission is large and the
        last index is at its end.
        """
        run_id = valid_run_id(run_id)
        path = self.root / run_id / EVENTS_FILE
        try:
            with open(path, "rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - _TAIL_BYTES))
                tail = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return -1
        best = -1
        for raw in tail.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue  # a torn line, or the cut-off first line of the tail
            if isinstance(item, dict) and _is_number(item.get("i")):
                best = max(best, int(item["i"]))
        return best

    def events(
        self, run_id: str, since: int = 0, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return events with ``i >= since``, in file order.

        Corrupt lines (bad JSON, not an object, missing/non-numeric ``i``)
        are skipped silently rather than raising: the jsonl file is a log,
        and a single torn line at the end from a crash mid-write must not
        make the rest of the run's history unreadable.
        """
        run_id = valid_run_id(run_id)
        path = self.root / run_id / EVENTS_FILE
        try:
            with open(path, "r", encoding="utf-8") as stream:
                lines = stream.readlines()
        except OSError:
            return []
        result: List[Dict[str, Any]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            i = item.get("i")
            if not _is_number(i) or i < since:
                continue
            result.append(item)
            if limit is not None and len(result) >= limit:
                break
        return result

    def _load_all(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Every valid-run-id directory under ``root`` with a loadable
        payload, unsorted. Shared by ``resumable`` and ``prune`` so the two
        never disagree about what counts as a run directory."""
        try:
            if not self.root.exists():
                return []
            entries = list(self.root.iterdir())
        except OSError:
            return []
        out: List[Tuple[str, Dict[str, Any]]] = []
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            run_id = entry.name
            try:
                valid_run_id(run_id)
            except ValueError:
                continue  # a foreign directory that happens to live here
            try:
                payload = self.load(run_id)
            except UnknownRun:
                continue  # corrupt checkpoint: silently skipped, not listed
            out.append((run_id, payload))
        return out

    def _sort_key(self, run_id: str, payload: Dict[str, Any]) -> Tuple[float, str]:
        """Newest first: the checkpoint's own ``updated_at`` when present
        (it reflects the real last write), else the run id's timestamp
        prefix -- which is monotonic with creation order, so it is still a
        correct "newest first" ordering for a payload that predates
        ``updated_at`` being written (e.g. a hand-written test fixture)."""
        updated_at = payload.get("updated_at")
        when = updated_at if _is_number(updated_at) else _run_id_epoch(run_id)
        return (when, run_id)

    def _has_open_work(self, tree: Any) -> bool:
        """Walk a checkpointed tree (plain dicts, key ``"children"``) for
        any non-root node that has not reached a closed state and was not
        superseded by a later repair. Used only to decide whether a
        ``failed`` run is worth listing as resumable: a failed run whose
        every node already reached a terminal state has nothing left to
        resume into."""
        if not isinstance(tree, dict):
            return False
        stack: List[Any] = [tree]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            status = node.get("status")
            superseded = bool(node.get("superseded"))
            if node_id != "0" and status in _OPEN_TREE_STATUSES and not superseded:
                return True
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(children)
        return False

    def _row(
        self,
        run_id: str,
        payload: Dict[str, Any],
        live_run_id: Optional[str],
        now: float,
    ) -> Optional[Dict[str, Any]]:
        """Build the listing row for one payload, or ``None`` if this run
        should not be listed at all. See :meth:`resumable` for the rules;
        kept as its own method so those rules are one readable block instead
        of buried in the directory-scanning loop."""
        state = payload.get("state")
        if not isinstance(state, str):
            return None

        if state == "done":
            return None

        interrupted = False
        if state in ACTIVE_STATES:
            if run_id == live_run_id:
                # It may be alive in another console right now: never offer
                # to resume a run this process could still be driving.
                return None
            heartbeat_at = payload.get("heartbeat_at")
            # A missing heartbeat counts as stale on purpose: a checkpoint
            # from a build that predates heartbeats, or one written once and
            # never updated again, is exactly the "the process is gone"
            # case this listing exists to surface.
            if _is_number(heartbeat_at):
                stale = (now - heartbeat_at) > STALE_S
            else:
                stale = True
            if not stale:
                return None
            interrupted = True
        elif state == "stopped":
            interrupted = False
        elif state == "failed":
            if not self._has_open_work(payload.get("tree")):
                return None
            interrupted = False
        else:
            # An unrecognised state (a future build, or a corrupted field)
            # is neither known-open nor known-closed: skip rather than guess.
            return None

        heartbeat_at = payload.get("heartbeat_at")
        heartbeat_age_s = (now - heartbeat_at) if _is_number(heartbeat_at) else None

        return {
            "run_id": run_id,
            "command": payload.get("command"),
            "state": state,
            "interrupted": interrupted,
            "heartbeat_age_s": heartbeat_age_s,
            "progress": payload.get("progress") or {},
            "updated_at": payload.get("updated_at"),
            "dry_run": payload.get("dry_run"),
            "segment": payload.get("segment"),
            "stop_reason": payload.get("stop_reason"),
            "error": payload.get("error"),
        }

    def resumable(
        self,
        limit: int = 20,
        live_run_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """List runs worth offering to resume, newest first, capped at
        ``limit``.

        ``now`` is a parameter (not always ``time.time()``) so tests can
        pass a fixed clock instead of sleeping past ``STALE_S`` for real.
        """
        if now is None:
            now = time.time()
        loaded = self._load_all()
        loaded.sort(key=lambda pair: self._sort_key(*pair), reverse=True)
        rows: List[Dict[str, Any]] = []
        for run_id, payload in loaded:
            row = self._row(run_id, payload, live_run_id, now)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def prune(self, keep: int = 50) -> int:
        """Remove the oldest run directories beyond ``keep``. Never raises:
        called from ``start()`` on every new run, so a pruning failure must
        not block starting the mission it is trying to make room for."""
        loaded = self._load_all()
        # A directory whose run.json failed to load is still counted (it
        # still occupies disk and its name is still a valid run id), just
        # ordered by its id's own timestamp since there is no payload to
        # read `updated_at` from.
        try:
            if self.root.exists():
                for entry in self.root.iterdir():
                    try:
                        if not entry.is_dir():
                            continue
                        run_id = entry.name
                        valid_run_id(run_id)
                    except (OSError, ValueError):
                        continue
                    if any(rid == run_id for rid, _ in loaded):
                        continue
                    loaded.append((run_id, {}))
        except OSError:
            pass

        loaded.sort(key=lambda pair: self._sort_key(*pair), reverse=True)
        removed = 0
        for run_id, _ in loaded[keep:]:
            try:
                shutil.rmtree(str(self.root / run_id), ignore_errors=True)
                removed += 1
            except Exception:
                # `prune` never raises: a stubborn directory is skipped, not
                # allowed to abort the caller that is trying to start a run.
                pass
        return removed

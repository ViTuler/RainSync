"""Service layer: the watchdog observer wrapper used by watch mode.

One :class:`GroupWatcher` is attached to a single project's target folder and
forwards file events to a shared :class:`~synctool.engine.SyncEngine`.  The
engine is shared by all projects of a group, so every watcher serialises
through the same lock (see ``cli.cmd_watch``).

Create/modify/delete events are debounced for ``group.interval`` seconds
before being propagated; move events are propagated immediately because
``on_moved`` already carries both sides of the rename.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event, Lock, Thread

from watchdog.events import FileSystemEventHandler

from .engine import SyncEngine
from .models import Project


class GroupWatcher(FileSystemEventHandler):
    def __init__(self, engine: SyncEngine, source_project: Project):
        super().__init__()
        self.engine = engine
        self.source_project = source_project
        self._pending: dict[str, tuple[object, float]] = {}
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._flush_loop, name=f"watch-{source_project.name}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------- watchdog events
    def on_created(self, event) -> None:
        self._queue(event)

    def on_modified(self, event) -> None:
        self._queue(event)

    def on_deleted(self, event) -> None:
        self._queue(event)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        self._safe(
            self.engine.propagate_move,
            self.source_project,
            Path(event.src_path),
            Path(event.dest_path),
        )

    # ------------------------------------------------------------- debounce
    def _queue(self, event) -> None:
        if event.is_directory:
            return
        with self._lock:
            # Later events for the same path replace earlier ones.
            self._pending[event.src_path] = (event, time.monotonic())

    def _flush_loop(self) -> None:
        interval = self.engine.group.interval
        while not self._stop.wait(0.05):
            for event in self._due(interval):
                self._safe(self.engine.propagate_event, self.source_project, event)

    def _due(self, interval: float) -> list:
        now = time.monotonic()
        ready = []
        with self._lock:
            for key, (event, timestamp) in list(self._pending.items()):
                if now - timestamp >= interval:
                    ready.append(event)
                    del self._pending[key]
        return ready

    def _safe(self, func, *args) -> None:
        try:
            func(*args)
        except Exception as exc:  # noqa: BLE001 - watchdog threads must not die
            print(f"[{self.engine.group.name}] ERROR: {exc}", file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

"""Output layer: a small, thread-safe, append-only file logger.

Replaces the previous ``structlog`` monkey-patching with a plain, explicit
logger.  Every ``msg(event, **fields)`` call appends one formatted line to a
single log file:

    timestamp | event | group | file | from_project -> to_project | key=value...
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from .paths import app_log_path

#: Event fields that may appear in a line, in display order.
_ORDERED = ("group", "file")


class Logger:
    """Append-only file logger whose lines use `` | `` as separator."""

    def __init__(self, log_file: Path):
        self._path = Path(log_file)
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    def msg(self, event: str, **fields) -> None:
        """Write one ``timestamp | event | ...`` line to the log file."""
        line = format_line(event, fields)
        with self._lock, open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class NullLogger:
    """Logger that swallows every message (used in tests / dry context)."""

    def msg(self, event: str, **fields) -> None:  # noqa: ARG002
        pass


def format_line(event: str, fields: dict) -> str:
    """Build a single human-readable log line from an event and its fields.

    Field ordering: ``group``, ``file``, ``from_project -> to_project`` (or
    ``project``), then any remaining ``key=value`` pairs.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [timestamp, event]

    for key in _ORDERED:
        if key in fields:
            parts.append(str(fields.pop(key)))

    if fields.get("from_project") and fields.get("to_project"):
        parts.append(f"{fields.pop('from_project')} -> {fields.pop('to_project')}")
    elif fields.get("project"):
        parts.append(str(fields.pop("project")))

    for key, value in fields.items():
        if value not in (None, ""):
            parts.append(f"{key}={value}")

    return " | ".join(parts)


def setup_logging(log_file: Path | None = None) -> Logger:
    """Create the shared logger used by the CLI and the sync engine.

    The log ALWAYS lives next to the tool itself -- the folder holding the
    packaged executable, or the project root when run from source -- so the
    history stays put no matter which folder the command was run from.  The
    default path is resolved per call so a frozen build picks up the real
    executable location.
    """
    return Logger(log_file if log_file is not None else app_log_path())

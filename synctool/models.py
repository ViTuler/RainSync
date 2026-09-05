"""Domain model for synctool.

This module only holds plain data structures and the domain error type.  It
has no dependency on any other part of the package or on third-party libs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SyncError(RuntimeError):
    """Raised for configuration / usage problems that should be shown to the user."""


@dataclass(frozen=True)
class Project:
    """One peer project. ``path`` is the absolute root of the project."""

    name: str
    path: Path
    # Patterns applied only to files that live inside this project.
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    """A set of peer projects that keep a shared tool folder in sync."""

    name: str
    projects: tuple[Project, ...]
    # Name of the folder that is synchronised (searched inside each project).
    target_folder: str
    # Patterns applied to every project of this group.
    ignore: tuple[str, ...] = ()
    allow_delete: bool = False
    # How deep to search for ``target_folder`` inside each project:
    #   0   -> only the project root
    #   N   -> up to N folder levels below the project root
    #   None-> no limit (search the whole project tree)
    auto_walk: int | None = None
    # Watch-mode debounce (seconds).
    interval: float = 0.3
    # Whether watch mode should run a full sync before starting to watch.
    init_sync: bool = True


@dataclass
class SyncStats:
    """Counters produced by a synchronize() run."""

    copied: int = 0
    deleted: int = 0
    skipped: int = 0
    conflicts: int = 0

    def summary(self) -> str:
        return (
            f"copied={self.copied} skipped={self.skipped} "
            f"deleted={self.deleted} conflicts={self.conflicts}"
        )

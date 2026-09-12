"""Service layer: the sync engine.

Responsibilities
----------------
* resolve each project's target folder (optionally searching sub-folders),
* turn absolute paths into group-relative paths and honour ignore rules,
* run a full scan: copy the newest version of every file to all peers,
* propagate a single filesystem event (create / modify / delete / move).

The engine serialises every operation through a single lock so that watch
mode (several watchers feeding events into one engine per group) never has
two threads writing to the same peer folder at the same time.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from threading import Lock

from .ignore import IgnoreMatcher
from .logger import NullLogger, timestamp
from .models import Group, Project, SyncStats

#: Suffix used for the atomic-write temp files.
TEMP_SUFFIX = ".sync_tmp"


class SyncEngine:
    def __init__(
        self,
        group: Group,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        logger=None,
    ):
        self.group = group
        self.dry_run = dry_run
        self.verbose = verbose
        self._log = logger if logger is not None else NullLogger()
        self._lock = Lock()
        self._matchers: dict[str, IgnoreMatcher] = {}

    # ------------------------------------------------------------ console
    def _say(self, action: str, file: str, source: str | None = None, dest: str | None = None) -> None:
        """Print a short console line, e.g. ``[group] COPY x, a -> b``.

        The line is prefixed with the time the event was handled so watch-mode
        output can be read next to the log file.
        """
        text = f"{timestamp()} [{self.group.name}] {action} {file}"
        if source and dest:
            text += f", {source} -> {dest}"
        print(text)

    # ---------------------------------------------- target folder discovery
    def tool_root(self, project: Project) -> Path:
        """Absolute path of ``target_folder`` inside a project."""
        base = project.path
        target = self.group.target_folder
        direct = base / target
        if direct.is_dir():
            return direct

        limit = self.group.auto_walk  # None = unlimited, 0 = root only, N = depth
        if limit == 0:
            return direct

        found = self._search_folder(base, target, depth=0, limit=limit)
        return found if found is not None else direct

    def _search_folder(self, folder: Path, target: str, depth: int, limit: int | None) -> Path | None:
        """Depth-first search for a folder named ``target`` below ``folder``.

        ``limit`` is the maximum number of folder levels (below the project
        root) that are visited; ``None`` means no limit.
        """
        if limit is not None and depth >= limit:
            return None

        try:
            children = [child for child in folder.iterdir() if child.is_dir()]
        except (OSError, PermissionError):
            return None

        for child in children:
            if child.name == target:
                return child
        for child in children:
            found = self._search_folder(child, target, depth + 1, limit)
            if found is not None:
                return found
        return None

    # ------------------------------------------------ path / ignore helpers
    @staticmethod
    def _rel(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    def matcher_for(self, project: Project) -> IgnoreMatcher:
        """Combined group + project ignore matcher (cached per project)."""
        matcher = self._matchers.get(project.name)
        if matcher is None:
            matcher = IgnoreMatcher((*self.group.ignore, *project.ignore))
            self._matchers[project.name] = matcher
        return matcher

    def _is_ignored(self, root: Path, path: Path, project: Project) -> bool:
        try:
            rel = self._rel(root, path)
        except ValueError:
            return True
        if rel.endswith(TEMP_SUFFIX):
            return True
        return self.matcher_for(project).ignored(rel, path.is_dir())

    def iter_relative_files(self, root: Path, project: Project) -> set[str]:
        """Return all non-ignored, group-relative file paths under ``root``."""
        if not root.exists():
            return set()
        return {
            self._rel(root, path)
            for path in root.rglob("*")
            if path.is_file() and not self._is_ignored(root, path, project)
        }

    # ------------------------------------------------------- content helpers
    @staticmethod
    def same_content(a: Path, b: Path) -> bool:
        if not a.is_file() or not b.is_file():
            return False
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
        except OSError:
            return False
        return SyncEngine._hash(a) == SyncEngine._hash(b)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return -1.0

    # ---------------------------------------------------------- file copying
    def _copy(
        self,
        source: Path,
        destination: Path,
        source_project: str | None = None,
        dest_project: str | None = None,
    ) -> bool:
        """Copy ``source`` over ``destination`` atomically. Returns success.

        The copy is written to a temp file first and then swapped in with
        ``os.replace`` so readers never observe a half-written file.  On
        Windows ``os.replace`` can transiently fail with a locked file; in
        that case we accept the copy when the destination already has the
        correct content.
        """
        if self.dry_run:
            self._log.msg("COPY", group=self.group.name, file=destination.name,
                          from_project=source_project, to_project=dest_project)
            return True

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + TEMP_SUFFIX)
        try:
            shutil.copy2(source, temp)
            try:
                os.replace(temp, destination)
            except (OSError, PermissionError) as exc:
                if destination.is_file() and self.same_content(source, destination):
                    pass  # replace failed, but the right content is already there
                else:
                    self._log.msg("COPY_ERROR", group=self.group.name, file=destination.name,
                                  from_project=source_project, to_project=dest_project, error=str(exc))
                    print(f"  {timestamp()} WARNING: could not replace {destination}: {exc}", file=sys.stderr)
                    return False
            self._log.msg("COPY", group=self.group.name, file=destination.name,
                          from_project=source_project, to_project=dest_project)
            return True
        except OSError as exc:
            self._log.msg("COPY_ERROR", group=self.group.name, file=destination.name,
                          from_project=source_project, to_project=dest_project, error=str(exc))
            print(f"  {timestamp()} ERROR copying {source} -> {destination}: {exc}", file=sys.stderr)
            return False
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _remove_empty_parents(path: Path, stop_at: Path | None = None) -> None:
        while path != stop_at and path.exists():
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent

    def _cleanup_temp_files(self, roots: dict[str, Path]) -> None:
        """Remove orphaned temp files left behind by earlier runs."""
        if self.dry_run:
            return
        for project in self.group.projects:
            root = roots[project.name]
            root.mkdir(parents=True, exist_ok=True)
            for temp in root.rglob("*" + TEMP_SUFFIX):
                try:
                    temp.unlink()
                    self._log.msg("CLEANUP", group=self.group.name,
                                  file=str(temp.relative_to(root)))
                except OSError:
                    pass

    # ------------------------------------------------------------ full sync
    def synchronize(self) -> SyncStats:
        """Scan every project and sync the newest version of each file to peers.

        For every relative path that exists in at least one project:

        * if the existing copies differ in content -> report a conflict,
        * otherwise the most recently modified copy wins and is copied to all
          projects that do not already have identical content.
        """
        with self._lock:
            roots = {p.name: self.tool_root(p) for p in self.group.projects}
            self._cleanup_temp_files(roots)

            present: dict[str, list[tuple[Project, Path]]] = {}
            for project in self.group.projects:
                for rel in self.iter_relative_files(roots[project.name], project):
                    present.setdefault(rel, []).append((project, roots[project.name] / rel))

            stats = SyncStats()

            for rel in sorted(present):
                copies = present[rel]

                # Files can vanish between the scan and the hash -> skip them.
                hashes: set[str] = set()
                for _, path in copies:
                    try:
                        hashes.add(self._hash(path))
                    except OSError:
                        break
                else:
                    if len(hashes) > 1:
                        stats.conflicts += 1
                        self._log.msg("CONFLICT", group=self.group.name, file=rel)
                        self._say("CONFLICT", rel)
                        continue

                    source_project, source_path = max(copies, key=lambda c: self._mtime(c[1]))

                    for project in self.group.projects:
                        if project.name == source_project.name:
                            continue
                        destination = roots[project.name] / rel
                        if destination.is_file() and self.same_content(source_path, destination):
                            stats.skipped += 1
                            continue
                        if self._copy(source_path, destination, source_project.name, project.name):
                            stats.copied += 1
                            self._say("COPY", rel, source_project.name, project.name)

            return stats

    # -------------------------------------------------- single event handling
    def propagate_event(self, source_project: Project, event) -> None:
        """Propagate one watchdog event observed in ``source_project``."""
        if event.is_directory:
            return

        source_root = self.tool_root(source_project)
        source_path = Path(event.src_path)
        is_deleted = getattr(event, "event_type", None) == "deleted"
        if not is_deleted and not source_path.exists():
            return

        try:
            relative = self._rel(source_root, source_path)
        except ValueError:
            return

        if self.matcher_for(source_project).ignored(relative, False):
            return

        with self._lock:
            for project in self.group.projects:
                if project.name == source_project.name:
                    continue
                destination = self.tool_root(project) / relative

                if is_deleted or not source_path.exists():
                    if self.group.allow_delete:
                        self._log.msg("DELETE", group=self.group.name, file=relative,
                                      from_project=source_project.name, to_project=project.name)
                        self._say("DELETE", relative, source_project.name, project.name)
                        if not self.dry_run:
                            destination.unlink(missing_ok=True)
                            self._remove_empty_parents(destination.parent, self.tool_root(project))
                    continue

                if event.event_type in {"created", "modified", "moved"}:
                    # Skip echoes: our own previous copy already made peers identical.
                    if destination.is_file() and self.same_content(source_path, destination):
                        continue
                    if self._copy(source_path, destination, source_project.name, project.name):
                        self._say("COPY", relative, source_project.name, project.name)

    def propagate_move(
        self,
        source_project: Project,
        source_path: Path,
        destination_path: Path,
    ) -> None:
        """Propagate a rename/move observed in ``source_project``."""
        source_root = self.tool_root(source_project)
        try:
            old_relative = self._rel(source_root, source_path)
            new_relative = self._rel(source_root, destination_path)
        except ValueError:
            return

        matcher = self.matcher_for(source_project)
        if matcher.ignored(old_relative, False) and matcher.ignored(new_relative, False):
            return

        with self._lock:
            for project in self.group.projects:
                if project.name == source_project.name:
                    continue
                root = self.tool_root(project)
                old_dest = root / old_relative
                new_dest = root / new_relative

                if self.group.allow_delete and not matcher.ignored(old_relative, False):
                    if old_dest.exists():
                        self._say("MOVE", f"{old_relative} -> {new_relative}",
                                  source_project.name, project.name)
                        if not self.dry_run:
                            old_dest.unlink(missing_ok=True)

                # Only copy when the peer does not already hold identical
                # content. os.replace() on the peer emits a "moved" event that
                # is handled immediately (no debounce); without this check each
                # identical rewrite would re-trigger the peer watcher -> loop.
                if (
                    destination_path.exists()
                    and not matcher.ignored(new_relative, False)
                    and not (new_dest.is_file() and self.same_content(destination_path, new_dest))
                ):
                    if self._copy(destination_path, new_dest, source_project.name, project.name):
                        self._say("COPY", new_relative, source_project.name, project.name)

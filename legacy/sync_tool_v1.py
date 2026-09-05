from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Iterable

import structlog
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


APP_NAME = "sync_tool"
DEFAULT_CONFIG_NAME = "sync_config.yaml"
DEFAULT_interval = 0.3
DEFAULT_LOG_FILE = Path.cwd() / "sync.log"


def setup_logging() -> None:
    """Initialize structlog with a single log file."""
    
    def formatter(logger, name, event_kv):
        """Custom formatter for clear, sparse logging."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Extract main fields
        event = event_kv.pop("event", "")
        
        # Build readable message: timestamp | operation | group | file | from_project -> to_project
        parts = [timestamp, event]
        
        # Add group name
        if "group" in event_kv:
            parts.append(event_kv.pop("group"))
        
        # Add file name
        if "file" in event_kv:
            parts.append(event_kv.pop("file"))
        
        # Add project transfer info
        if "from_project" in event_kv and "to_project" in event_kv:
            parts.append(f"{event_kv.pop('from_project')} -> {event_kv.pop('to_project')}")
        elif "project" in event_kv:
            parts.append(event_kv.pop("project"))
        
        # Add remaining fields if present
        for key, value in event_kv.items():
            if value and key not in ["source", "destination", "path"]:
                parts.append(f"{key}={value}")
        
        return " | ".join(str(p) for p in parts if p)
    
    structlog.configure(
        processors=[
            structlog.processors.KeyValueRenderer(key_order=[], sort_keys=False),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(
            file=open(DEFAULT_LOG_FILE, "a", encoding="utf-8")
        ),
        cache_logger_on_first_use=True,
    )
    
    # Use custom wrapper for prettier output
    import functools
    original_get_logger = structlog.get_logger
    
    @functools.wraps(original_get_logger)
    def custom_get_logger(*args, **kwargs):
        logger = original_get_logger(*args, **kwargs)
        original_msg = logger.msg
        
        @functools.wraps(original_msg)
        def wrapped_msg(event, **kw):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            kw["event"] = event
            message = formatter(logger, "sync", kw)
            with open(DEFAULT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(message + "\n")
        
        logger.msg = wrapped_msg
        return logger
    
    structlog.get_logger = custom_get_logger


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    name: str
    projects: tuple[Project, ...]
    target_folder: str
    ignore: tuple[str, ...]
    allow_delete: bool
    auto_walk: int | bool = False
    interval: float = DEFAULT_interval
    init_sync: bool = True


class SyncError(RuntimeError):
    pass


class IgnoreMatcher:
    """Small gitignore-like matcher without another runtime dependency."""

    def __init__(self, patterns: Iterable[str]):
        self.patterns = tuple(self._normalize(p) for p in patterns if p and not p.lstrip().startswith("#"))

    @staticmethod
    def _normalize(pattern: str) -> str:
        pattern = pattern.replace("\\", "/").strip()
        if pattern.startswith("./"):
            pattern = pattern[2:]
        return pattern

    def ignored(self, relative_path: str, is_dir: bool = False) -> bool:
        rel = relative_path.replace("\\", "/").strip("/")
        if not rel:
            return False

        parts = rel.split("/")
        candidates = [rel]
        candidates.extend("/".join(parts[i:]) for i in range(1, len(parts)))

        for pattern in self.patterns:
            negated = pattern.startswith("!")
            raw = pattern[1:] if negated else pattern
            directory_only = raw.endswith("/")
            raw = raw.rstrip("/")

            matched = False
            if "/" in raw:
                matched = any(fnmatch.fnmatchcase(candidate, raw) for candidate in candidates)
            else:
                matched = any(fnmatch.fnmatchcase(part, raw) for part in parts)

            if matched and (not directory_only or is_dir):
                if negated:
                    return False
                return True
        return False


class Config:
    def __init__(self, groups: dict[str, Group], path: Path, yaml_data=None):
        self.groups = groups
        self.path = path
        # Keep ruamel's round-trip object so future config updates can preserve
        # comments, quote style, ordering, and other formatting details.
        self.yaml_data = yaml_data

    @staticmethod
    def _yaml() -> YAML:
        ru_yaml = YAML()
        ru_yaml.preserve_quotes = True
        ru_yaml.default_flow_style = False
        # Keep the familiar 2-space mapping indentation while retaining an
        # indented sequence style when ruamel rewrites the file.
        ru_yaml.indent(mapping=2, sequence=4, offset=2)
        return ru_yaml

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise SyncError(f"Config file not found: {path}")

        ru_yaml = cls._yaml()
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = ru_yaml.load(fh) or {}
        except YAMLError as exc:
            raise SyncError(f"Invalid YAML: {exc}") from exc

        if not isinstance(data, dict):
            raise SyncError("Config root must be a mapping/object")

        # Support both 'groups' and 'group' top-level keys
        groups_data = data.get("groups") or data.get("group")
        if not isinstance(groups_data, dict) or not groups_data:
            raise SyncError("Config must contain a non-empty 'groups' or 'group' mapping")

        groups: dict[str, Group] = {}
        for group_name, raw in groups_data.items():
            if not isinstance(raw, dict):
                raise SyncError(f"Group '{group_name}' must be a mapping/object")

            target_folder = raw.get("target_folder")
            if not isinstance(target_folder, str) or not target_folder.strip():
                raise SyncError(f"Group '{group_name}': 'target_folder' is required")

            # Support both 'projects' and 'project' keys
            project_data = raw.get("projects") or raw.get("project")
            if not isinstance(project_data, list) or len(project_data) < 2:
                raise SyncError(f"Group '{group_name}' needs at least 2 projects")

            projects: list[Project] = []
            seen_names: set[str] = set()
            seen_paths: set[Path] = set()
            for item in project_data:
                if not isinstance(item, dict):
                    raise SyncError(f"Group '{group_name}': every project must be an object")
                name = item.get("name")
                project_path = item.get("path")
                if not isinstance(name, str) or not name.strip():
                    raise SyncError(f"Group '{group_name}': project name is required")
                if name in seen_names:
                    raise SyncError(f"Group '{group_name}': duplicate project name '{name}'")
                if not isinstance(project_path, str) or not project_path.strip():
                    raise SyncError(f"Group '{group_name}': project '{name}' needs a path")

                resolved = Path(project_path).expanduser().resolve()
                if resolved in seen_paths:
                    raise SyncError(f"Group '{group_name}': duplicate project path '{resolved}'")
                seen_names.add(name)
                seen_paths.add(resolved)

                # Parse project-level ignore patterns
                project_ignore = item.get("ignore", [])
                if isinstance(project_ignore, str):
                    project_ignore = [project_ignore]
                if not isinstance(project_ignore, list) or not all(isinstance(x, str) for x in project_ignore):
                    raise SyncError(f"Group '{group_name}': project '{name}' ignore must be a list of strings")

                projects.append(Project(name=name, path=resolved, ignore=tuple(project_ignore)))

            ignore = raw.get("ignore", [])
            if isinstance(ignore, str):
                ignore = [ignore]
            if not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore):
                raise SyncError(f"Group '{group_name}': 'ignore' must be a list of strings")

            allow_delete = raw.get("allow_delete", False)
            if not isinstance(allow_delete, bool):
                raise SyncError(f"Group '{group_name}': 'allow_delete' must be true/false")

            auto_walk = raw.get("auto_walk", False)
            if isinstance(auto_walk, bool):
                auto_walk = 0 if auto_walk is False else 0
            elif isinstance(auto_walk, int):
                if auto_walk < 0:
                    raise SyncError(f"Group '{group_name}': auto_walk must be non-negative")
            else:
                raise SyncError(f"Group '{group_name}': auto_walk must be a boolean or integer")

            init_sync = raw.get("init_sync", True)
            if not isinstance(init_sync, bool):
                raise SyncError(f"Group '{group_name}': init_sync must be true/false")

            watch_config = raw.get("watch", {})
            interval = watch_config.get("interval", DEFAULT_interval)
            if not isinstance(interval, (int, float)) or interval < 0:
                raise SyncError(f"Group '{group_name}': interval must be a non-negative number")

            groups[group_name] = Group(
                name=group_name,
                projects=tuple(projects),
                target_folder=target_folder.strip("/\\"),
                ignore=tuple(ignore),
                allow_delete=allow_delete,
                auto_walk=auto_walk,
                interval=interval,
                init_sync=init_sync,
            )

        return cls(groups, path, data)

    def save(self) -> None:
        """Write the round-trip YAML object back without discarding comments."""
        if self.yaml_data is None:
            raise SyncError("No YAML document is loaded")

        ru_yaml = self._yaml()
        try:
            with self.path.open("w", encoding="utf-8", newline="") as fh:
                ru_yaml.dump(self.yaml_data, fh)
        except OSError as exc:
            raise SyncError(f"Unable to write config '{self.path}': {exc}") from exc


@dataclass
class SyncStats:
    copied: int = 0
    deleted: int = 0
    skipped: int = 0
    conflicts: int = 0


class SyncEngine:
    def __init__(self, group: Group, dry_run: bool = False, verbose: bool = False, max_workers: int = 4):
        self.group = group
        self.dry_run = dry_run
        self.verbose = verbose
        self.max_workers = max_workers
        self._lock = Lock()
        # Cache matchers per project to combine group and project-level ignores
        self._matchers: dict[str, IgnoreMatcher] = {}
        self.logger = structlog.get_logger()
        # Source mapping: relative_path -> (source_project, source_path)
        self.source_map: dict[str, tuple[Project, Path]] = {}

    def global_sync(self) -> SyncStats:
        """Global sync: find newest version of each file and sync to all projects."""
        with self._lock:
            stats = SyncStats()
            roots = {project.name: self._tool_root(project) for project in self.group.projects}

            # Ensure all target folders exist and clean up orphaned temp files
            for project in self.group.projects:
                root = roots[project.name]
                if not self.dry_run:
                    root.mkdir(parents=True, exist_ok=True)
                    # Clean up any orphaned .sync_tmp files from previous runs
                    if root.exists():
                        for temp_file in root.rglob("*.sync_tmp"):
                            try:
                                temp_file.unlink()
                                self.logger.msg("CLEANUP", group=self.group.name, file=str(temp_file.relative_to(root)))
                            except Exception:
                                pass  # Ignore cleanup errors

            # Collect all files and find newest version for each
            all_paths: set[str] = set()
            file_versions: dict[str, list[tuple[Project, Path, float]]] = {}
            
            for project in self.group.projects:
                root = roots[project.name]
                for relative in self._iter_files(root, project):
                    all_paths.add(relative)
                    path = root / relative
                    mtime = path.stat().st_mtime
                    if relative not in file_versions:
                        file_versions[relative] = []
                    file_versions[relative].append((project, path, mtime))

            # Find newest version of each file and build source map
            self.source_map = {}
            synced: set[tuple[str, str, str]] = set()  # (relative_path, source_project, dest_project)
            
            for relative in sorted(all_paths):
                versions = file_versions[relative]
                
                # Find the newest version
                source_project, source_path, _ = max(versions, key=lambda x: x[2])
                self.source_map[relative] = (source_project, source_path)
                
                # Check for conflicts (different content)
                unique_hashes = {self._hash_file(path) for _, path, _ in versions}
                if len(unique_hashes) > 1:
                    stats.conflicts += 1
                    self.logger.msg("CONFLICT", group=self.group.name, file=relative)
                    print(f"  CONFLICT {relative} (multiple project versions)")
                    continue
                
                # Sync to all other projects
                for project, destination in ((p, roots[p.name] / relative) for p in self.group.projects if p != source_project):
                    sync_key = (relative, source_project.name, project.name)
                    if sync_key in synced:
                        continue  # Skip if already synced
                    synced.add(sync_key)
                    
                    if destination.exists() and self._same_content(source_path, destination):
                        stats.skipped += 1
                        continue
                    self._copy(source_path, destination, source_project.name, project.name)
                    print(f"[{self.group.name}] COPY {relative}, {source_project.name} -> {project.name}")
                    stats.copied += 1
            
            self.logger.msg("INIT_SYNC_DONE", group=self.group.name, copied=stats.copied, conflicts=stats.conflicts)
            return stats

    def _sync_copy_task(self, source: Path, destination: Path, source_project: str, dest_project: str) -> None:
        """Helper method for thread pool to copy file."""
        self._copy(source, destination, source_project, dest_project)

    def _find_target_folder(self, project: Project) -> Path:
        """Find target folder within auto_walk depth limit."""
        target = self.group.target_folder
        max_depth = self.group.auto_walk if isinstance(self.group.auto_walk, int) else 0
        
        # Check root first (depth 0)
        root_target = project.path / target
        if root_target.exists() and root_target.is_dir():
            return root_target
        
        # If max_depth is 0, only check root
        if max_depth == 0:
            return root_target  # Return even if doesn't exist, for consistency
        
        # Walk subdirectories up to max_depth
        def search_in_dir(base: Path, current_depth: int) -> Path | None:
            if current_depth > max_depth:
                return None
            for item in base.iterdir():
                if not item.is_dir():
                    continue
                # Check if this directory matches target
                if item.name == target:
                    return item
                # Recurse deeper
                result = search_in_dir(item, current_depth + 1)
                if result:
                    return result
            return None
        
        try:
            found = search_in_dir(project.path, 1)
            if found:
                return found
        except (OSError, PermissionError):
            pass
        
        return root_target  # Return default if not found
    
    def _tool_root(self, project: Project) -> Path:
        return self._find_target_folder(project)

    def _rel(self, root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    def _get_matcher(self, project: Project) -> IgnoreMatcher:
        """Get or create a matcher combining group and project-level ignores."""
        if project.name not in self._matchers:
            combined_patterns = list(self.group.ignore) + list(project.ignore)
            self._matchers[project.name] = IgnoreMatcher(combined_patterns)
        return self._matchers[project.name]

    def _ignored(self, root: Path, path: Path, project: Project) -> bool:
        try:
            rel = self._rel(root, path)
        except ValueError:
            return True
        
        # Always ignore sync temporary files
        if rel.endswith(".sync_tmp"):
            return True
        
        matcher = self._get_matcher(project)
        return matcher.ignored(rel, path.is_dir())

    def _iter_files(self, root: Path, project: Project) -> set[str]:
        if not root.exists():
            return set()
        result: set[str] = set()
        for path in root.rglob("*"):
            if path.is_file() and not self._ignored(root, path, project):
                result.add(self._rel(root, path))
        return result

    @staticmethod
    def _same_content(a: Path, b: Path) -> bool:
        if not a.exists() or not b.exists() or not a.is_file() or not b.is_file():
            return False
        if a.stat().st_size != b.stat().st_size:
            return False
        return SyncEngine._hash_file(a) == SyncEngine._hash_file(b)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def propagate_move(self, source_project: Project, source_path: Path, destination_path: Path) -> None:
        source_root = self._tool_root(source_project)
        try:
            old_relative = self._rel(source_root, source_path)
            new_relative = self._rel(source_root, destination_path)
        except ValueError:
            return

        source_matcher = self._get_matcher(source_project)
        if source_matcher.ignored(old_relative, False) and source_matcher.ignored(new_relative, False):
            return

        with self._lock:
            for project in self.group.projects:
                if project == source_project:
                    continue
                root = self._tool_root(project)
                old_dest = root / old_relative
                new_dest = root / new_relative

                if self.group.allow_delete and not self._get_matcher(source_project).ignored(old_relative, False):
                    if old_dest.exists():
                        print(f"[{self.group.name}] MOVE {old_relative} -> {new_relative}, {source_project.name} -> {project.name}")
                        if not self.dry_run:
                            old_dest.unlink(missing_ok=True)

                # Only copy if the peer does not already hold identical content.
                # os.replace() on the peer emits a "moved" event that is handled
                # immediately (no debounce); without this check each identical
                # rewrite would trigger the peer watcher again -> infinite loop.
                if (
                    destination_path.exists()
                    and not self._get_matcher(source_project).ignored(new_relative, False)
                    and not (new_dest.exists() and self._same_content(destination_path, new_dest))
                ):
                    self._copy(destination_path, new_dest, source_project.name, project.name)

    def _copy(self, source: Path, destination: Path, source_project: str | None = None, dest_project: str | None = None) -> None:
        if self.dry_run:
            self.logger.msg("COPY", group=self.group.name, file=destination.name, from_project=source_project, to_project=dest_project)
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".sync_tmp")
        try:
            shutil.copy2(source, temp)
            try:
                os.replace(temp, destination)
                self.logger.msg("COPY", group=self.group.name, file=destination.name, from_project=source_project, to_project=dest_project)
            except (OSError, PermissionError) as exc:
                # os.replace() failed, but check if sync actually succeeded
                # (file might have been locked but content was copied)
                if destination.exists() and self._same_content(source, destination):
                    # File was successfully synced despite the error
                    self.logger.msg("COPY", group=self.group.name, file=destination.name, from_project=source_project, to_project=dest_project)
                else:
                    # Real error - file not synced
                    self.logger.msg("COPY_ERROR", group=self.group.name, file=destination.name, from_project=source_project, to_project=dest_project, error=str(exc))
                    print(f"  WARNING: Could not replace {destination}: {exc}", file=sys.stderr)
        except Exception as exc:
            self.logger.msg("COPY_ERROR", group=self.group.name, file=destination.name, from_project=source_project, to_project=dest_project, error=str(exc))
            print(f"  ERROR copying {source} to {destination}: {exc}", file=sys.stderr)
        finally:
            # Clean up temp file if it still exists
            if temp.exists():
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass  # Ignore cleanup errors

    def _delete(self, path: Path) -> None:
        if self.dry_run:
            print(f"  DELETE {path}")
            return
        path.unlink(missing_ok=True)
        self._remove_empty_parents(path.parent)

    @staticmethod
    def _remove_empty_parents(path: Path, stop_at: Path | None = None) -> None:
        while path.exists() and path.is_dir() and (stop_at is None or path != stop_at):
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent

    def sync(self) -> SyncStats:
        with self._lock:
            stats = SyncStats()
            roots = {project.name: self._tool_root(project) for project in self.group.projects}

            # Ensure all target folders exist and clean up orphaned temp files
            for project in self.group.projects:
                root = roots[project.name]
                if not self.dry_run:
                    root.mkdir(parents=True, exist_ok=True)
                    # Clean up any orphaned .sync_tmp files from previous runs
                    if root.exists():
                        for temp_file in root.rglob("*.sync_tmp"):
                            try:
                                temp_file.unlink()
                                self.logger.msg("CLEANUP", group=self.group.name, file=str(temp_file.relative_to(root)))
                            except Exception:
                                pass  # Ignore cleanup errors

            all_paths: set[str] = set()
            for project in self.group.projects:
                all_paths.update(self._iter_files(roots[project.name], project))

            # Track synced file pairs to avoid duplicates
            synced: set[tuple[str, str, str]] = set()  # (relative_path, source_project, dest_project)

            for relative in sorted(all_paths):
                copies: list[tuple[Project, Path]] = []
                for project in self.group.projects:
                    path = roots[project.name] / relative
                    if path.exists() and path.is_file():
                        copies.append((project, path))

                if not copies:
                    continue

                # Find the newest (most recently modified) file as source
                source_project, source_path = max(copies, key=lambda x: x[1].stat().st_mtime)
                
                if len(copies) > 1:
                    unique_hashes = {self._hash_file(path) for _, path in copies}
                    if len(unique_hashes) > 1:
                        stats.conflicts += 1
                        self.logger.msg("CONFLICT", group=self.group.name, file=relative)
                        print(f"  CONFLICT {relative} (multiple project versions)")
                        continue

                for project, destination in ((p, roots[p.name] / relative) for p in self.group.projects if p != source_project):
                    sync_key = (relative, source_project.name, project.name)
                    if sync_key in synced:
                        continue  # Skip if already synced
                    synced.add(sync_key)
                    
                    if destination.exists() and self._same_content(source_path, destination):
                        stats.skipped += 1
                        continue
                    self._copy(source_path, destination, source_project.name, project.name)
                    print(f"[{self.group.name}] COPY {relative}, {source_project.name} -> {project.name}")
                    stats.copied += 1

            if self.group.allow_delete:
                if self.verbose:
                    print("  NOTE   allow_delete=true: deletions are propagated by watch mode")

            return stats

    def propagate_event(self, source_project: Project, event) -> None:
        if event.is_directory:
            return

        source_root = self._tool_root(source_project)
        source_path = Path(event.src_path)
        if not source_path.exists() and getattr(event, "event_type", None) != "deleted":
            return

        try:
            relative = self._rel(source_root, source_path)
        except ValueError:
            return

        source_matcher = self._get_matcher(source_project)
        if source_matcher.ignored(relative, False):
            return

        with self._lock:
            for project in self.group.projects:
                if project == source_project:
                    continue

                destination = self._tool_root(project) / relative
                if getattr(event, "event_type", None) == "deleted" or not source_path.exists():
                    if self.group.allow_delete:
                        self.logger.msg("DELETE", group=self.group.name, file=relative, from_project=source_project.name, to_project=project.name)
                        print(f"[{self.group.name}] DELETE {relative}, {source_project.name} -> {project.name}")
                        if not self.dry_run:
                            destination.unlink(missing_ok=True)
                            self._remove_empty_parents(destination.parent, self._tool_root(project))
                    continue

                if event.event_type in {"created", "modified", "moved"}:
                    if destination.exists() and self._same_content(source_path, destination):
                        continue
                    print(f"[{self.group.name}] COPY {relative}, {source_project.name} -> {project.name}")
                    self._copy(source_path, destination, source_project.name, project.name)



class GroupWatcher(FileSystemEventHandler):
    def __init__(self, engine: SyncEngine, source_project: Project):
        super().__init__()
        self.engine = engine
        self.source_project = source_project
        self._pending: dict[str, tuple[FileSystemEvent, float]] = {}
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def _queue(self, event) -> None:
        if event.is_directory:
            return
        with self._lock:
            self._pending[event.src_path] = (event, time.monotonic())

    def on_created(self, event) -> None:
        self._queue(event)

    def on_modified(self, event) -> None:
        self._queue(event)

    def on_deleted(self, event) -> None:
        self._queue(event)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        try:
            self.engine.propagate_move(
                self.source_project,
                Path(event.src_path),
                Path(event.dest_path),
            )
        except Exception as exc:
            print(f"[{self.engine.group.name}] ERROR: {exc}", file=sys.stderr)

    def _flush_loop(self) -> None:
        interval = self.engine.group.interval
        while not self._stop.wait(0.05):
            now = time.monotonic()
            ready: list[FileSystemEvent] = []
            with self._lock:
                for key, (event, timestamp) in list(self._pending.items()):
                    if now - timestamp >= interval:
                        ready.append(event)
                        del self._pending[key]
            for event in ready:
                try:
                    self.engine.propagate_event(self.source_project, event)
                except Exception as exc:
                    print(f"[{self.engine.group.name}] ERROR: {exc}", file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def default_config_path() -> Path:
    return Path.cwd() / DEFAULT_CONFIG_NAME


def select_groups(config: Config, names: list[str] | None) -> list[Group]:
    if not names:
        return list(config.groups.values())
    missing = [name for name in names if name not in config.groups]
    if missing:
        raise SyncError(f"Unknown group(s): {', '.join(missing)}")
    return [config.groups[name] for name in names]


def validate_groups(groups: Iterable[Group]) -> None:
    for group in groups:
        for project in group.projects:
            if not project.path.exists():
                raise SyncError(f"Group '{group.name}': project path does not exist: {project.path}")
            if not project.path.is_dir():
                raise SyncError(f"Group '{group.name}': project path is not a directory: {project.path}")


def cmd_sync(args: argparse.Namespace) -> int:
    setup_logging()
    logger = structlog.get_logger()
    logger.msg("SYNC_START")
    
    config = Config.load(Path(args.config).expanduser().resolve())
    groups = select_groups(config, args.group)
    validate_groups(groups)

    exit_code = 0
    for group in groups:
        print(f"\n[{group.name}] {group.target_folder}")
        engine = SyncEngine(group, dry_run=args.dry_run, verbose=args.verbose)
        stats = engine.sync()
        print(
            f"  DONE   copied={stats.copied} skipped={stats.skipped} "
            f"deleted={stats.deleted} conflicts={stats.conflicts}"
        )
        logger.msg("SYNC_DONE", group=group.name, copied=stats.copied, conflicts=stats.conflicts)
        if stats.conflicts:
            exit_code = 2
    
    logger.msg("SYNC_END")
    return exit_code


def cmd_watch(args: argparse.Namespace) -> int:
    setup_logging()
    logger = structlog.get_logger()
    logger.msg("WATCH_START")
    
    config = Config.load(Path(args.config).expanduser().resolve())
    groups = select_groups(config, args.group)
    validate_groups(groups)

    observers: list = []
    handlers: list[GroupWatcher] = []

    for group in groups:
        # One shared engine per group so all watchers of this group serialize
        # through the same lock. This is what makes content-based echo
        # detection reliable (no concurrent writers racing each other).
        engine = SyncEngine(group, dry_run=args.dry_run, verbose=args.verbose)

        # Perform initial sync if enabled
        if group.init_sync:
            print(f"\n[{group.name}] Performing initial sync...")
            init_stats = engine.global_sync()
            print(f"  Initial sync: copied={init_stats.copied} conflicts={init_stats.conflicts}")
        
        # Watch every project. This gives us symmetric peer propagation.
        for project in group.projects:
            tool_root = engine._tool_root(project)
            # Ensure the directory exists before watching
            if not tool_root.exists():
                if not args.dry_run:
                    tool_root.mkdir(parents=True, exist_ok=True)
            handler = GroupWatcher(engine, project)
            observer = Observer()
            observer.schedule(handler, str(tool_root), recursive=True)
            observer.start()
            observers.append(observer)
            handlers.append(handler)
            logger.msg("WATCH_PROJECT", project=project.name, group=group.name)
            print(f"[{group.name}] watching {project.name}: {tool_root}")

    print("Watching for changes. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join(timeout=3)
        for handler in handlers:
            handler.stop()
    logger.msg("WATCH_END")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Synchronize shared tool folders across local Python projects.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Synchronize all configured groups immediately")
    sync.add_argument("group", nargs="*", help="Optional group name(s)")
    sync.add_argument("-c", "--config", default=str(default_config_path()), help="Config YAML path")
    sync.add_argument("-d", "--daemon", action="store_true", help="Sync once, then watch for changes")
    sync.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    sync.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    sync.set_defaults(func=cmd_sync)

    watch = sub.add_parser("watch", help="Watch all configured projects and propagate changes")
    watch.add_argument("group", nargs="*", help="Optional group name(s)")
    watch.add_argument("-c", "--config", default=str(default_config_path()), help="Config YAML path")
    watch.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    watch.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    watch.set_defaults(func=cmd_watch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Backward-compatible convenience: sync -d == watch.
    if args.command == "sync" and getattr(args, "daemon", False):
        code = cmd_sync(args)
        if code != 0:
            return code
        args.command = "watch"
        return cmd_watch(args)

    try:
        return args.func(args)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

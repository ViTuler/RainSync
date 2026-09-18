"""Config layer: load, validate and select groups from a YAML file.

Only the parameters documented below are understood.  The config is parsed
with ``ruamel.yaml`` in round-trip mode so that comments and formatting are
preserved if the file is ever rewritten later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import Group, MapGroup, Project, SyncError
from .paths import DEFAULT_CONFIG_NAME, app_config_path, resolve_config_path

DEFAULT_INTERVAL = 0.3

#: Human readable description of the supported keys (used in error messages).
_GROUP_KEYS = (
    "target_folder (required), project/projects (>=2), ignore, "
    "allow_delete, auto_walk, init_sync, watch.interval"
)

#: Accepted top-level keys for sync groups (first match wins).
_SYNC_GROUP_KEYS = ("sync_groups", "groups", "group")

#: Accepted top-level keys for map groups (first match wins).
_MAP_GROUP_KEYS = ("map_groups", "map_group")


class Config:
    """A loaded configuration: sync groups + map groups + the raw YAML doc."""

    def __init__(
        self,
        groups: dict[str, Group],
        path: Path,
        raw: Any,
        map_groups: dict[str, MapGroup] | None = None,
    ):
        self.groups = groups
        self.map_groups = map_groups or {}
        self.path = path
        # Round-trip ruamel object, retained for future config rewrites.
        self.raw = raw

    # ------------------------------------------------------------- loader
    @staticmethod
    def _yaml() -> YAML:
        ru_yaml = YAML()
        ru_yaml.preserve_quotes = True
        ru_yaml.default_flow_style = False
        ru_yaml.indent(mapping=2, sequence=4, offset=2)
        return ru_yaml

    @staticmethod
    def _first_mapping(data: dict, keys: tuple[str, ...]) -> dict | None:
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise SyncError(f"Config '{key}' must be a mapping/object")
            return value
        return None

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise SyncError(f"Config file not found: {path}")

        try:
            with path.open("r", encoding="utf-8") as fh:
                data = cls._yaml().load(fh) or {}
        except YAMLError as exc:
            raise SyncError(f"Invalid YAML in '{path}': {exc}") from exc

        if not isinstance(data, dict):
            raise SyncError("Config root must be a mapping/object")

        groups_raw = cls._first_mapping(data, _SYNC_GROUP_KEYS) or {}
        map_raw = cls._first_mapping(data, _MAP_GROUP_KEYS) or {}

        if not groups_raw and not map_raw:
            raise SyncError(
                "Config must contain a non-empty "
                "'sync_groups'/'groups' or 'map_groups' mapping"
            )

        groups: dict[str, Group] = {
            name: cls._parse_group(name, raw)
            for name, raw in groups_raw.items()
        }
        map_groups: dict[str, MapGroup] = {
            name: cls._parse_map_group(name, raw)
            for name, raw in map_raw.items()
        }
        return cls(groups, path, data, map_groups=map_groups)

    # ----------------------------------------------------------- group parse
    @classmethod
    def _parse_group(cls, name: str, raw: Any) -> Group:
        if not isinstance(raw, dict):
            raise SyncError(f"Group '{name}' must be a mapping/object")

        target_folder = raw.get("target_folder")
        if not isinstance(target_folder, str) or not target_folder.strip():
            raise SyncError(f"Group '{name}': 'target_folder' is required")

        project_raw = raw.get("projects") or raw.get("project")
        if not isinstance(project_raw, list) or len(project_raw) < 2:
            raise SyncError(
                f"Group '{name}' needs at least 2 projects "
                f"(keys: {_GROUP_KEYS})"
            )

        projects = tuple(cls._parse_projects(name, project_raw))

        return Group(
            name=name,
            projects=projects,
            target_folder=target_folder.strip("/\\"),
            ignore=tuple(_as_string_list(raw.get("ignore"), f"Group '{name}' ignore")),
            allow_delete=_as_bool(raw.get("allow_delete"), f"Group '{name}' allow_delete", default=False),
            auto_walk=_as_auto_walk(raw.get("auto_walk"), name),
            interval=_as_interval(raw.get("watch") if isinstance(raw.get("watch"), dict) else {}, name),
            init_sync=_as_bool(raw.get("init_sync"), f"Group '{name}' init_sync", default=True),
        )

    @staticmethod
    def _parse_projects(group_name: str, project_raw: list) -> list[Project]:
        projects: list[Project] = []
        seen_names: set[str] = set()
        seen_paths: set[Path] = set()

        for item in project_raw:
            if not isinstance(item, dict):
                raise SyncError(f"Group '{group_name}': every project must be an object")

            pname = item.get("name")
            if not isinstance(pname, str) or not pname.strip():
                raise SyncError(f"Group '{group_name}': project 'name' is required")
            if pname in seen_names:
                raise SyncError(f"Group '{group_name}': duplicate project name '{pname}'")

            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise SyncError(f"Group '{group_name}': project '{pname}' needs a 'path'")
            resolved = Path(raw_path).expanduser().resolve()
            if resolved in seen_paths:
                raise SyncError(f"Group '{group_name}': duplicate project path '{resolved}'")

            seen_names.add(pname)
            seen_paths.add(resolved)
            projects.append(
                Project(
                    name=pname,
                    path=resolved,
                    ignore=tuple(
                        _as_string_list(
                            item.get("ignore"),
                            f"Group '{group_name}' project '{pname}' ignore",
                        )
                    ),
                )
            )
        return projects

    @classmethod
    def _parse_map_group(cls, name: str, raw: Any) -> MapGroup:
        if not isinstance(raw, dict):
            raise SyncError(f"Map group '{name}' must be a mapping/object")

        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SyncError(f"Map group '{name}': 'source' is required")

        target = raw.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SyncError(f"Map group '{name}': 'target' is required")

        return MapGroup(
            name=name,
            source=Path(source).expanduser().resolve(),
            target=Path(target).expanduser().resolve(),
        )

    # ------------------------------------------------------------ selection
    def select(self, names: list[str] | None) -> list[Group]:
        """Return the requested sync groups (or all when ``names`` is empty)."""
        if not self.groups:
            raise SyncError("Config has no sync groups ('sync_groups'/'groups')")
        if not names:
            return list(self.groups.values())
        missing = [n for n in names if n not in self.groups]
        if missing:
            raise SyncError(f"Unknown group(s): {', '.join(missing)}")
        return [self.groups[n] for n in names]

    def select_map(self, names: list[str] | None) -> list[MapGroup]:
        """Return the requested map groups (or all when ``names`` is empty)."""
        if not self.map_groups:
            raise SyncError("Config has no map groups ('map_groups')")
        if not names:
            return list(self.map_groups.values())
        missing = [n for n in names if n not in self.map_groups]
        if missing:
            raise SyncError(f"Unknown map group(s): {', '.join(missing)}")
        return [self.map_groups[n] for n in names]


def validate_groups(groups: Iterable[Group]) -> None:
    """Ensure every referenced project directory actually exists."""
    for group in groups:
        for project in group.projects:
            if not project.path.exists() or not project.path.is_dir():
                raise SyncError(
                    f"Group '{group.name}': project path is not an existing directory: {project.path}\n"
                    f"Edit the config file so every project points at a real folder."
                )


def validate_map_groups(groups: Iterable[MapGroup]) -> None:
    """Ensure every map source directory exists and target is usable."""
    for group in groups:
        if not group.source.exists() or not group.source.is_dir():
            raise SyncError(
                f"Map group '{group.name}': source is not an existing directory: {group.source}\n"
                f"Edit the config file so every map source points at a real folder."
            )
        if group.source.resolve() == group.target.resolve():
            raise SyncError(
                f"Map group '{group.name}': source and target must be different paths"
            )
        if group.target.exists() and not group.target.is_dir():
            raise SyncError(
                f"Map group '{group.name}': target exists but is not a directory: {group.target}"
            )


def default_config_path() -> Path:
    """Config path the tool falls back to.

    Delegates to :func:`synctool.paths.resolve_config_path`, so callers get
    the full search order: an existing config in the working folder, then one
    next to the tool, then the tool's own folder for a fresh config.
    """
    return resolve_config_path()


# ----------------------------------------------------------------- helpers
def _as_string_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    raise SyncError(f"{what} must be a string or a list of strings")


def _as_bool(value: Any, what: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SyncError(f"{what} must be true/false")
    return value


def _as_interval(watch_raw: dict, group_name: str) -> float:
    value = watch_raw.get("interval", DEFAULT_INTERVAL)
    if not isinstance(value, (int, float)) or value < 0:
        raise SyncError(f"Group '{group_name}': watch.interval must be a non-negative number")
    return float(value)


def _as_auto_walk(value: Any, group_name: str) -> int | None:
    """Translate the YAML value into a search-depth limit.

    * missing / false  -> 0      (only the project root is checked)
    * true             -> None   (search the whole project tree)
    * integer N        -> N      (search up to N folder levels deep)
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return None if value else 0
    if isinstance(value, int) and value >= 0:
        return value
    raise SyncError(f"Group '{group_name}': auto_walk must be true/false or a non-negative integer")


def _generate_example_config_content() -> str:
    """Return an example configuration YAML content as a string."""
    return """# RainSync Configuration
# This file defines synchronization groups for your projects.

groups:
  # Example group: synchronize a shared tools folder across two projects
  example_group:
    # The folder to sync across projects (relative to each project)
    target_folder: .tools

    # List of projects to synchronize (minimum 2 required)
    projects:
      - name: project_a
        path: ~/projects/project_a
        # Optional: patterns to ignore when syncing
        ignore:
          - "*.pyc"
          - __pycache__
          - .pytest_cache

      - name: project_b
        path: ~/projects/project_b
        ignore:
          - "*.pyc"
          - __pycache__

    # Optional: allow deletion of files in target folder (default: false)
    allow_delete: false

    # Optional: auto-walk depth for detecting tool changes
    # false/0: only check target folder root (default)
    # true/null: search entire project tree
    # N: search up to N folder levels deep
    auto_walk: false

    # Optional: perform initial sync on startup (default: true)
    init_sync: true

    # Optional: watch interval in seconds (default: 0.3)
    watch:
      interval: 0.3

# Optional: map groups backup a source folder into a target folder
map_groups:
  example_map:
    source: ~/backups/source_folder
    target: ~/backups/mapped_folder
"""


def create_example_config(path: Path | None = None, overwrite: bool = False) -> bool:
    """Create an example config file.

    Returns True when a file was written, False when it already existed and
    ``overwrite`` is False.
    """
    if path is None:
        path = app_config_path()
    else:
        path = Path(path)

    if path.exists() and not overwrite:
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(_generate_example_config_content())
    except OSError as exc:
        raise SyncError(f"Failed to create example config at '{path}': {exc}") from exc

    return True


def ensure_config_exists(path: Path | None = None) -> Path:
    """Backward-compatible helper: create a missing config file if needed."""
    if path is None:
        path = app_config_path()
    else:
        path = Path(path)

    if not path.exists():
        create_example_config(path, overwrite=False)
    return path

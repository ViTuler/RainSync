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

from .models import Group, Project, SyncError

DEFAULT_CONFIG_NAME = "sync_config.yaml"
DEFAULT_INTERVAL = 0.3

#: Human readable description of the supported keys (used in error messages).
_GROUP_KEYS = (
    "target_folder (required), project/projects (>=2), ignore, "
    "allow_delete, auto_walk, init_sync, watch.interval"
)


class Config:
    """A loaded configuration: ``groups`` keyed by name + the raw YAML doc."""

    def __init__(self, groups: dict[str, Group], path: Path, raw: Any):
        self.groups = groups
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

        groups_raw = data.get("groups") or data.get("group")
        if not isinstance(groups_raw, dict) or not groups_raw:
            raise SyncError("Config must contain a non-empty 'groups'/'group' mapping")

        groups: dict[str, Group] = {
            name: cls._parse_group(name, raw)
            for name, raw in groups_raw.items()
        }
        return cls(groups, path, data)

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

    # ------------------------------------------------------------ selection
    def select(self, names: list[str] | None) -> list[Group]:
        """Return the requested groups (or all of them when ``names`` is empty)."""
        if not names:
            return list(self.groups.values())
        missing = [n for n in names if n not in self.groups]
        if missing:
            raise SyncError(f"Unknown group(s): {', '.join(missing)}")
        return [self.groups[n] for n in names]


def validate_groups(groups: Iterable[Group]) -> None:
    """Ensure every referenced project directory actually exists."""
    for group in groups:
        for project in group.projects:
            if not project.path.exists() or not project.path.is_dir():
                raise SyncError(
                    f"Group '{group.name}': project path is not an existing directory: {project.path}"
                )


def default_config_path() -> Path:
    return Path.cwd() / DEFAULT_CONFIG_NAME


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

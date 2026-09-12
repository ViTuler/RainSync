"""Location layer: where the tool itself lives on disk.

A packaged (PyInstaller) build runs from an executable that the user may drop
anywhere -- typically a folder placed on ``PATH`` -- and then invoke from a
completely different working folder.  Config and log files therefore need two
distinct anchors:

* the **working folder** (``Path.cwd()``): where the user ran the command;
* the **tool folder** (:func:`app_dir`): where the executable lives.

Config resolution prefers the working folder, so a project-local config wins,
and falls back to the tool folder.  A brand new config and every log line are
always anchored to the tool folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Files the tool looks for and creates.
DEFAULT_CONFIG_NAME = "sync_config.yaml"
DEFAULT_LOG_NAME = "sync.log"


def app_dir() -> Path:
    """Folder the tool lives in.

    * frozen (PyInstaller) build -> the folder holding the executable;
    * run from source            -> the project root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def app_config_path() -> Path:
    """Config file next to the tool itself."""
    return app_dir() / DEFAULT_CONFIG_NAME


def app_log_path() -> Path:
    """Log file next to the tool itself (always written here)."""
    return app_dir() / DEFAULT_LOG_NAME


def local_config_path() -> Path:
    """Config file in the folder the command was run from."""
    return Path.cwd() / DEFAULT_CONFIG_NAME


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Pick the config file to use, following the documented search order.

    1. an explicit ``-c/--config`` path (used as-is, even when missing);
    2. ``sync_config.yaml`` in the folder the tool is run from (cwd);
    3. ``sync_config.yaml`` next to the tool itself.

    When neither of the implicit locations holds a config, the tool's own
    folder is returned so a fresh config can be created there (see
    :func:`synctool.config.create_example_config`).
    """
    if explicit:
        return Path(explicit).expanduser()

    local = local_config_path()
    if local.is_file():
        return local

    return app_config_path()

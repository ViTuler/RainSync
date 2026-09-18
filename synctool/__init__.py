"""synctool — synchronize shared tool folders across local Python projects.

This package is organised in small, single-purpose layers:

* ``models``  — pure data structures (Project / Group / MapGroup / SyncStats / SyncError)
* ``ignore``  — gitignore-style path matching
* ``paths``   — where the tool lives: config search order + log location
* ``logger``  — minimal append-only file logger used for the sync history
* ``config``  — YAML loading, validation and group selection
* ``engine``  — the sync engine (full scan + single event propagation)
* ``mapper``  — folder mapping / backup (``map`` command)
* ``watcher`` — watchdog observer wrapper with debounce (watch mode)
* ``cli``     — argument parsing and command entry points

Run it with ``python -m synctool`` or through the ``sync_tool.py`` shim.
"""

__version__ = "0.2.0"

from .models import Group, MapGroup, MapStats, Project, SyncError, SyncStats  # noqa: F401

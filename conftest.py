"""Ensure the project root is importable when running pytest.

With this file at the repo root, ``synctool`` can be imported both via
``pytest`` and ``python -m pytest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

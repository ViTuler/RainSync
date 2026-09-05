"""Thin entry point so ``python sync_tool.py`` keeps working.

The real implementation now lives in the ``synctool`` package.
Run either of these from the project root:

    python sync_tool.py watch
    python -m synctool watch
"""

from synctool.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

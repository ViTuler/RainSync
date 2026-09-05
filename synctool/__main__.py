"""Allow running the tool as ``python -m synctool``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

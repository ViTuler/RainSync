"""Gitignore-style ignore matching.

A small matcher (no external dependency). Supported pattern forms:

* ``*.ext``            — any file/dir ending with ``.ext`` at any depth
* ``name``             — any file/dir named ``name`` at any depth
* ``path/to/x``        — relative path match against any suffix of the path
* ``folder/``          — directory-only pattern: also ignores everything under
                          a directory named ``folder`` at any depth
* ``!pattern``         — negation; like git, the *last* matching pattern wins

Matching is case-sensitive (git's behaviour on most platforms).
"""

from __future__ import annotations

import fnmatch
from typing import Iterable


class IgnoreMatcher:
    def __init__(self, patterns: Iterable[str]):
        # Strip comments and empty lines, normalise separators to '/'.
        self.patterns: tuple[str, ...] = tuple(
            self._normalize(p)
            for p in patterns
            if p and not p.lstrip().startswith("#")
        )

    @staticmethod
    def _normalize(pattern: str) -> str:
        pattern = pattern.replace("\\", "/").strip()
        if pattern.startswith("./"):
            pattern = pattern[2:]
        return pattern

    def ignored(self, relative_path: str, is_dir: bool = False) -> bool:
        """Return True when ``relative_path`` should be ignored.

        ``is_dir`` should be True when ``relative_path`` refers to a directory
        so that directory patterns (``folder/``) can match the directory
        itself as well as everything underneath it.
        """
        rel = relative_path.replace("\\", "/").strip("/")
        if not rel:
            return False

        parts = rel.split("/")
        # Git semantics: the last matching pattern decides.
        ignored = False

        for pattern in self.patterns:
            negated = pattern.startswith("!")
            raw = pattern[1:] if negated else pattern
            raw = raw.rstrip("/")
            if not raw:
                continue

            if self._matches(rel, parts, raw, is_dir):
                ignored = not negated
        return ignored

    @staticmethod
    def _matches(rel: str, parts: list[str], raw: str, is_dir: bool) -> bool:
        """Match ``raw`` against the file path or against any ancestor dir."""
        # The file itself, or a suffix of it (e.g. "build/x.py", "x.py").
        if any(fnmatch.fnmatchcase(candidate, raw) for candidate in _suffixes(rel)):
            return True

        # A matching ancestor directory ignores everything below it.  This is
        # what makes "build/gen/" (or a bare "node_modules") ignore the whole
        # sub-tree, no matter how deep the file sits below the project root.
        ancestor_paths = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if is_dir:
            ancestor_paths.append(rel)
        for ancestor in ancestor_paths:
            if any(fnmatch.fnmatchcase(candidate, raw) for candidate in _suffixes(ancestor)):
                return True
        return False


def _suffixes(path: str) -> list[str]:
    """``path`` plus every trailing-component suffix, e.g. ``a/b/c`` ->
    ``['a/b/c', 'b/c', 'c']``."""
    parts = path.split("/")
    suffixes = [path]
    suffixes.extend("/".join(parts[i:]) for i in range(1, len(parts)))
    return suffixes

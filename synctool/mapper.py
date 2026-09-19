"""Service layer: folder mapping / backup.

``map`` copies a configured ``source`` folder into a ``target`` folder the way
Windows robocopy does by default: a file is copied only when it is missing in
the target, or when its size or last-write time differs.  Unchanged files are
skipped with a stat, not a full read.  The copy itself stays safe — bytes land
in a temp file, then ``os.replace`` swaps that file into place, so a failed
copy never leaves a half-written destination.  Extra files already in the
target are left alone (backup semantics, not a destructive mirror).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from .logger import NullLogger, timestamp
from .models import MapGroup, MapStats

TEMP_SUFFIX = ".map_tmp"

# robocopy /FFT: FAT and exFAT store times in 2-second steps.  Without this
# window a backup onto those volumes looks "changed" on every later run.
MTIME_TOLERANCE_SECONDS = 2.0


class MapEngine:
    def __init__(
        self,
        group: MapGroup,
        *,
        dry_run: bool = False,
        verbose: bool = False,
        logger=None,
    ):
        self.group = group
        self.dry_run = dry_run
        self.verbose = verbose
        self._log = logger if logger is not None else NullLogger()

    def _say(self, action: str, rel: str) -> None:
        print(f"{timestamp()} [{self.group.name}] {action} {rel}")

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def same_file(self, source: Path, destination: Path) -> bool:
        """True when robocopy would classify the pair as Same.

        Same size and last-write time (within ``MTIME_TOLERANCE_SECONDS``).
        Attribute-only differences are ignored, matching robocopy without ``/IT``.
        """
        if not source.is_file() or not destination.is_file():
            return False
        try:
            source_stat = source.stat()
            dest_stat = destination.stat()
        except OSError:
            return False
        if source_stat.st_size != dest_stat.st_size:
            return False
        delta = abs(source_stat.st_mtime - dest_stat.st_mtime)
        return delta <= MTIME_TOLERANCE_SECONDS

    def same_content(self, a: Path, b: Path) -> bool:
        if not a.is_file() or not b.is_file():
            return False
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
        except OSError:
            return False
        return self._hash(a) == self._hash(b)

    def _copy_file(self, source: Path, destination: Path, rel: str) -> bool:
        if self.dry_run:
            self._log.msg("MAP_COPY", group=self.group.name, file=rel)
            return True

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + TEMP_SUFFIX)
        try:
            shutil.copy2(source, temp)
            try:
                os.replace(temp, destination)
            except (OSError, PermissionError) as exc:
                if destination.is_file() and self.same_content(source, destination):
                    pass
                else:
                    self._log.msg(
                        "MAP_COPY_ERROR",
                        group=self.group.name,
                        file=rel,
                        error=str(exc),
                    )
                    print(
                        f"  {timestamp()} WARNING: could not replace {destination}: {exc}",
                        file=sys.stderr,
                    )
                    return False
            self._log.msg("MAP_COPY", group=self.group.name, file=rel)
            return True
        except OSError as exc:
            self._log.msg(
                "MAP_COPY_ERROR",
                group=self.group.name,
                file=rel,
                error=str(exc),
            )
            print(
                f"  {timestamp()} ERROR copying {source} -> {destination}: {exc}",
                file=sys.stderr,
            )
            return False
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    def map(self) -> MapStats:
        """Backup changed files from ``source`` into ``target``."""
        source = self.group.source
        target = self.group.target
        stats = MapStats()

        if not source.exists() or not source.is_dir():
            raise RuntimeError(
                f"Map group '{self.group.name}': source is not an existing directory: {source}"
            )

        if not self.dry_run:
            target.mkdir(parents=True, exist_ok=True)

        for path in sorted(source.rglob("*")):
            try:
                rel = path.relative_to(source).as_posix()
            except ValueError:
                continue

            destination = target / rel

            if path.is_dir():
                stats.dirs += 1
                if self.dry_run:
                    if self.verbose:
                        self._say("DIR", rel)
                    continue
                try:
                    destination.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    stats.errors += 1
                    self._log.msg(
                        "MAP_DIR_ERROR",
                        group=self.group.name,
                        file=rel,
                        error=str(exc),
                    )
                    print(
                        f"  {timestamp()} ERROR creating {destination}: {exc}",
                        file=sys.stderr,
                    )
                else:
                    if self.verbose:
                        self._say("DIR", rel)
                continue

            if not path.is_file() or path.name.endswith(TEMP_SUFFIX):
                continue

            if self.same_file(path, destination):
                stats.skipped += 1
                if self.verbose:
                    self._say("SKIP", rel)
                continue

            if self._copy_file(path, destination, rel):
                stats.copied += 1
                self._say("COPY", rel)
            else:
                stats.errors += 1

        return stats

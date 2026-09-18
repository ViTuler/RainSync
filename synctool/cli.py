"""Presentation layer: command-line parsing and entry points."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import (
    Config,
    create_example_config,
    resolve_config_path,
    validate_groups,
    validate_map_groups,
)
from .engine import SyncEngine
from .logger import setup_logging
from .mapper import MapEngine
from .paths import DEFAULT_CONFIG_NAME
from .watcher import GroupWatcher

APP_NAME = "synctool"

#: Help text shared by every ``-c/--config`` option.
_CONFIG_HELP = (
    "Config YAML path (default: the config found in the working folder, "
    f"else one next to the tool, else a new '{DEFAULT_CONFIG_NAME}' created "
    "next to the tool)"
)

#: Set in the environment of the daemon child so it does not re-spawn itself.
DAEMONIZED_ENV = "SYNCTOOL_DAEMONIZED"


def _build_daemon_env() -> dict:
    """Environment for the detached child, sanitized for frozen builds.

    A one-file PyInstaller app runs as TWO processes: a bootloader "parent"
    that unpacks the payload into ``%TEMP%\\_MEI<pid>`` and a "child" that runs
    our code.  PyInstaller tells that child to REUSE the parent's temp folder
    through internal environment variables (``_PYI_APPLICATION_HOME_DIR``,
    ``_PYI_ARCHIVE_FILE``, ``_PYI_PARENT_PROCESS_LEVEL``, ...).

    If the detached daemon child inherited those, it would keep loading DLLs
    from the *parent's* ``_MEI`` folder forever, so the parent could not delete
    it on exit and would print ``[PYI-*] Failed to remove temporary directory``.
    Stripping every ``_PYI_*`` variable makes the new instance unpack into its
    OWN ``_MEI`` folder, leaving the parent free to clean up.
    """
    env = dict(os.environ)
    env[DAEMONIZED_ENV] = "1"

    if getattr(sys, "frozen", False):
        env = {key: value for key, value in env.items() if not key.startswith("_PYI_")}
    else:
        # Make ``python -m synctool`` importable regardless of the cwd.
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")

    # Belt-and-braces: also drop the parent's sys._MEIPASS from PATH in case it
    # was added there.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_abs = os.path.normcase(os.path.abspath(meipass))
        kept = []
        for entry in env.get("PATH", "").split(os.pathsep):
            if entry and os.path.normcase(os.path.abspath(entry)) != meipass_abs:
                kept.append(entry)
        env["PATH"] = os.pathsep.join(kept)

    return env


def _spawn_daemon() -> int:
    """Re-launch the current command as a detached background process.

    The child writes its history to ``sync.log`` but has no console.  Its PID
    is returned so the caller can print it for the user.  After this returns,
    the caller exits immediately (a daemon parent should not linger).
    """
    env = _build_daemon_env()

    if getattr(sys, "frozen", False):
        # PyInstaller-style executable: re-run the exe itself.
        cmd = [sys.executable, *sys.argv[1:]]
    else:
        cmd = [sys.executable, "-m", APP_NAME, *sys.argv[1:]]

    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
        )
    else:
        proc = subprocess.Popen(
            cmd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
    return proc.pid


# --------------------------------------------- console + user notification
#: False when the process has no usable console (PyInstaller windowed build).
_CONSOLE_AVAILABLE = True


def _stream_usable(stream) -> bool:
    if stream is None:
        return False
    try:
        return stream.fileno() >= 0
    except (OSError, ValueError, AttributeError):
        return False


def _prepare_std_streams() -> None:
    """Point stdout/stderr at devnull in windowed (no-console) builds.

    A ``--noconsole`` executable has no console: writing to stdout/stderr
    would fail.  Redirect them to devnull and remember that pop-ups must be
    used instead of console output.
    """
    global _CONSOLE_AVAILABLE
    for name in ("stdout", "stderr"):
        if _stream_usable(getattr(sys, name, None)):
            continue
        _CONSOLE_AVAILABLE = False
        try:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        except OSError:
            pass


def _notify(kind: str, message: str) -> None:
    """Show a message to the user: console text, or a pop-up in windowed mode."""
    if _CONSOLE_AVAILABLE:
        try:
            print(message, flush=True)
        except Exception:
            pass
        return
    if os.name == "nt":
        try:
            import ctypes
            # MB_ICONINFORMATION / MB_ICONWARNING (message boxes without button).
            flags = 0x40 if kind == "info" else 0x30
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, flags)
        except Exception:
            pass


# -------------------------------------------- single-instance daemon lock
def _daemon_lock_path(config_path: str, groups: list[str]) -> Path:
    """Stable per-(config, selected-groups) lock file path.

    Stored under the system temp dir so two daemon starts for the same config
    and groups resolve to the same lock and only one watcher may run.
    """
    resolved = os.path.abspath(os.path.expanduser(config_path))
    scope = ",".join(sorted(groups or []))
    digest = hashlib.sha1(f"{resolved}|{scope}".encode("utf-8", "replace")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-watch-{digest}.pid"


def _pid_is_running(pid: int) -> bool:
    """Best-effort cross-platform liveness check for a PID."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_is_same_app(pid: int) -> bool:
    """True when the running process is another instance of this executable.

    Guards against a lock file whose PID was recycled by an unrelated process.
    """
    if not getattr(sys, "frozen", False):
        return True
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            name = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
            if not ok:
                return False
            mine = os.path.basename(sys.executable).lower()
            return os.path.basename(name.value).lower() == mine
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _lock_owner(lock: Path) -> int | None:
    """PID stored in ``lock``, but only when that process is still running."""
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if _pid_is_running(pid) and _pid_is_same_app(pid):
        return pid
    return None


def _acquire_daemon_lock(lock: Path) -> int | None:
    """Atomically claim ``lock``. Returns an existing owner PID, or None.

    A stale lock file (owner process is gone) is removed and retried once, so
    a crashed daemon never blocks the next start.
    """
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _lock_owner(lock)
            if owner is not None:
                return owner
            try:
                lock.unlink()
            except OSError:
                pass
            continue
        except OSError:
            return None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return None
    return None


def _stop_hint() -> str:
    if os.name == "nt" and getattr(sys, "frozen", False):
        return f"taskkill /IM {os.path.basename(sys.executable)} /F"
    return "kill the process (e.g. Task Manager / taskkill /PID <pid> /F)"


#: How long the daemon launcher waits for proof that the child survived.
DAEMON_STARTUP_GRACE = 1.5


def _wait_for_daemon(pid: int, grace: float = DAEMON_STARTUP_GRACE) -> bool:
    """True when the detached watcher is still alive after ``grace`` seconds.

    A packaged (windowed) build has no console, so a daemon that dies during
    startup would otherwise be reported to the user as a successful start.
    """
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return False
        time.sleep(0.1)
    return _pid_is_running(pid)


def _log_failure(message: str) -> Path | None:
    """Append one ``ERROR`` line to the log file; return its path."""
    try:
        logger = setup_logging()
        logger.msg("ERROR", detail=" ".join(message.split()))
        return logger.path
    except OSError:
        return None


def _report_failure(message: str) -> None:
    """Report a fatal error from the CLI boundary.

    Without this an uncaught exception in a windowed (``--noconsole``) build
    is written to a devnull stream and the process simply disappears.  The
    detached daemon stays quiet: the launcher that spawned it reports the
    failure once it notices the early exit.
    """
    _log_failure(message)
    if os.environ.get(DAEMONIZED_ENV) == "1":
        return
    _notify("warning", message)


def _log_line_count(log_file: Path) -> int:
    """Number of lines currently in ``log_file`` (0 when it is missing)."""
    try:
        return len(Path(log_file).read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _newest_error(log_file: Path, after_line: int) -> str | None:
    """Message of the newest ``ERROR`` line written after ``after_line``."""
    try:
        lines = Path(log_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    marker = " | ERROR | "
    for line in reversed(lines[after_line:]):
        index = line.find(marker)
        if index >= 0:
            detail = line[index + len(marker):]
            if detail.startswith("detail="):
                detail = detail[len("detail="):]
            return detail or None
    return None


def _discard_stale_lock(lock: Path) -> None:
    """Delete ``lock`` when the daemon it points at is no longer running."""
    if _lock_owner(lock) is None:
        try:
            lock.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ commands
def _resolve_config(args: argparse.Namespace) -> Path:
    """Return the config file to use, creating one when none exists.

    Search order (see ``paths.resolve_config_path``): an explicit ``-c`` path,
    then the folder the command was run from, then the folder the tool itself
    lives in.  When neither implicit location holds a config, a fresh one is
    created next to the tool so a packaged executable keeps working after being
    copied somewhere new.

    ``args.config`` is normalized to the resolved path so later steps (daemon
    re-spawn, lock naming) all agree on the same file.
    """
    explicit = getattr(args, "config", None)
    path = resolve_config_path(explicit)

    if explicit is None and not path.exists():
        create_example_config(path, overwrite=False)
        print(f"Created example config at: {path}")

    args.config = str(path)
    return path


def cmd_init(args: argparse.Namespace) -> int:
    path = resolve_config_path(getattr(args, "config", None))
    created = create_example_config(path, overwrite=args.force)

    if created:
        print(f"Created example config at: {path}")
    else:
        print(f"Config already exists at: {path}")
        print("Use --force to overwrite it with the example template.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    log = setup_logging()

    config_path = _resolve_config(args)
    config = Config.load(config_path)
    groups = config.select(args.group)
    validate_groups(groups)

    log.msg("SYNC_START")

    exit_code = 0
    for group in groups:
        print(f"\n[{group.name}] {group.target_folder}")
        engine = SyncEngine(group, dry_run=args.dry_run, verbose=args.verbose, logger=log)
        stats = engine.synchronize()
        print(f"  DONE   {stats.summary()}")
        log.msg("SYNC_DONE", group=group.name, copied=stats.copied, conflicts=stats.conflicts)
        if stats.conflicts:
            exit_code = 2

    log.msg("SYNC_END")
    return exit_code


def cmd_map(args: argparse.Namespace) -> int:
    log = setup_logging()

    config_path = _resolve_config(args)
    config = Config.load(config_path)
    groups = config.select_map(args.group)
    validate_map_groups(groups)

    log.msg("MAP_START")

    exit_code = 0
    for group in groups:
        print(f"\n[{group.name}] {group.source} -> {group.target}")
        engine = MapEngine(group, dry_run=args.dry_run, verbose=args.verbose, logger=log)
        stats = engine.map()
        print(f"  DONE   {stats.summary()}")
        log.msg(
            "MAP_DONE",
            group=group.name,
            copied=stats.copied,
            skipped=stats.skipped,
            dirs=stats.dirs,
            errors=stats.errors,
        )
        if stats.errors:
            exit_code = 2

    log.msg("MAP_END")
    return exit_code


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        from watchdog.observers import Observer
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "watchdog is required for the watch command. "
            "Install dependencies in your active environment (for example, conda env 'tool')."
        ) from exc

    log = setup_logging()

    # Resolve (and, when missing, create) the config BEFORE any daemon work so
    # both the launcher and the detached child agree on the same file.
    config_path = _resolve_config(args)

    daemon_mode = bool(getattr(args, "daemon", False))
    is_daemon_child = os.environ.get(DAEMONIZED_ENV) == "1"

    # --- foreground launcher of `watch -d` -------------------------------
    # Re-launch ourselves detached, report the PID, and exit immediately.
    if daemon_mode and not is_daemon_child:
        lock = _daemon_lock_path(args.config, args.group)
        owner = _lock_owner(lock)
        if owner is not None:
            _notify("warning", f"A background watcher is already running (pid {owner}).\n"
                               f"Not starting another one.\n\nStop it with:\n{_stop_hint()}")
            return 1

        before = _log_line_count(log.path)
        try:
            pid = _spawn_daemon()
        except OSError as exc:
            _notify("warning", f"Could not start the background watcher:\n{exc}")
            return 1

        # A packaged build has no console, so a daemon that dies during
        # startup must be detected here rather than reported as started.
        if not _wait_for_daemon(pid):
            _discard_stale_lock(lock)
            reason = _newest_error(log.path, before)
            detail = f"\n\nReason: {reason}" if reason else ""
            _report_failure(
                f"The background watcher exited immediately (pid {pid}).{detail}\n\n"
                f"Log: {log.path}"
            )
            return 1

        _notify("info", f"Background watcher started (pid {pid}).\n\n"
                        f"History: {log.path}\n"
                        f"Stop it with: {_stop_hint()}")
        return 0

    # --- the detached child (and normal foreground `watch`) --------------
    if daemon_mode:
        # Authoritative single-instance check: guards against two starts
        # racing each other.  The daemon owns the lock for its whole life.
        lock = _daemon_lock_path(args.config, args.group)
        owner = _acquire_daemon_lock(lock)
        if owner is not None:
            _notify("warning", f"Another background watcher is already running (pid {owner}).\n"
                               f"This instance is exiting.")
            return 1

    config = Config.load(config_path)
    groups = config.select(args.group)
    validate_groups(groups)

    log.msg("WATCH_START")

    observers: list = []
    watchers: list[GroupWatcher] = []

    for group in groups:
        # One shared engine per group: all watchers of the group serialize
        # through the same lock, which makes content-based echo detection
        # reliable (no concurrent writers racing each other).
        engine = SyncEngine(group, dry_run=args.dry_run, verbose=args.verbose, logger=log)

        if group.init_sync:
            print(f"\n[{group.name}] Performing initial sync...")
            stats = engine.synchronize()
            print(f"  Initial sync: {stats.summary()}")
            log.msg("INIT_SYNC_DONE", group=group.name, copied=stats.copied, conflicts=stats.conflicts)

        for project in group.projects:
            tool_root = engine.tool_root(project)
            if not tool_root.exists() and not args.dry_run:
                tool_root.mkdir(parents=True, exist_ok=True)

            watcher = GroupWatcher(engine, project)
            observer = Observer()
            observer.schedule(watcher, str(tool_root), recursive=True)
            observer.start()

            observers.append(observer)
            watchers.append(watcher)
            log.msg("WATCH_PROJECT", project=project.name, group=group.name)
            print(f"[{group.name}] watching {project.name}: {tool_root}")

    print("\nWatching for changes. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join(timeout=3)
        for watcher in watchers:
            watcher.stop()

    log.msg("WATCH_END")
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Synchronize shared tool folders across local Python projects.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Synchronize all configured groups immediately")
    sync.add_argument("group", nargs="*", help="Optional group name(s)")
    sync.add_argument("-c", "--config", default=None, help=_CONFIG_HELP)
    sync.add_argument("-d", "--daemon", action="store_true", help="Sync once, then watch for changes")
    sync.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    sync.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    sync.set_defaults(func=cmd_sync)

    watch = sub.add_parser("watch", help="Watch all configured projects and propagate changes")
    watch.add_argument("group", nargs="*", help="Optional group name(s)")
    watch.add_argument("-c", "--config", default=None, help=_CONFIG_HELP)
    watch.add_argument("-d", "--daemon", action="store_true",
                       help="Run the watcher as a detached background process")
    watch.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    watch.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    watch.set_defaults(func=cmd_watch)

    map_cmd = sub.add_parser("map", help="Backup configured source folders into target folders")
    map_cmd.add_argument("group", nargs="*", help="Optional map group name(s)")
    map_cmd.add_argument("-c", "--config", default=None, help=_CONFIG_HELP)
    map_cmd.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    map_cmd.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    map_cmd.set_defaults(func=cmd_map)

    init = sub.add_parser("init", help="Create an example config file the tool will use")
    init.add_argument("-c", "--config", default=None, help=_CONFIG_HELP)
    init.add_argument("-f", "--force", action="store_true", help="Overwrite existing config file")
    init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windowed (--noconsole) builds have no usable stdout/stderr; make prints
    # harmless and use pop-ups (via _notify) for daemon control messages.
    _prepare_std_streams()

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # Backward-compatible convenience: `sync -d` == sync once, then watch
        # in the FOREGROUND (distinct from `watch -d`, which daemonizes).
        if args.command == "sync" and getattr(args, "daemon", False):
            code = cmd_sync(args)
            if code != 0:
                return code
            args.daemon = False
            return cmd_watch(args)

        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        # SyncError (config / usage problems) and other domain errors.
        _report_failure(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort CLI boundary
        _report_failure(f"Unexpected {type(exc).__name__}: {exc}")
        return 1

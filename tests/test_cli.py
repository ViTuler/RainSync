import os
import sys
from argparse import Namespace
from pathlib import Path

from synctool import cli


def test_daemon_env_strips_pyi_vars_when_frozen(monkeypatch):
    # Simulate a frozen (PyInstaller) run: internal _PYI_* vars point the
    # child back at the parent's _MEI temp dir and must be removed.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", r"C:\Users\u\AppData\Local\Temp\_MEI1234", raising=False)
    monkeypatch.setattr(
        os, "environ",
        {
            "PATH": r"C:\Users\u\AppData\Local\Temp\_MEI1234;C:\Windows\System32",
            "_PYI_APPLICATION_HOME_DIR": r"C:\Users\u\AppData\Local\Temp\_MEI1234",
            "_PYI_ARCHIVE_FILE": "sync_tool.exe",
            "_PYI_PARENT_PROCESS_LEVEL": "MAIN",
            "SYSTEMROOT": "C:\\Windows",
        },
    )

    env = cli._build_daemon_env()

    assert env["SYNCTOOL_DAEMONIZED"] == "1"
    # No PyInstaller-internal variable may leak to the daemon child.
    assert not any(key.startswith("_PYI_") for key in env)
    # The parent's temp dir must not remain on the child's PATH.
    assert "_MEI1234" not in env["PATH"]
    assert "C:\\Windows\\System32" in env["PATH"]
    # Non-PyInstaller vars are preserved.
    assert env["SYSTEMROOT"] == "C:\\Windows"


def test_daemon_env_keeps_pyi_vars_when_not_frozen(monkeypatch):
    # In normal (unfrozen) Python there is no _PYI_ handling; everything is
    # kept and the package root is added to PYTHONPATH for `python -m synctool`.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(
        os, "environ",
        {"PATH": r"C:\Windows", "_PYI_APPLICATION_HOME_DIR": "x", "SYSTEMROOT": "C:\\Windows"},
    )

    env = cli._build_daemon_env()

    assert env["SYNCTOOL_DAEMONIZED"] == "1"
    assert env["_PYI_APPLICATION_HOME_DIR"] == "x"
    package_root = str(Path(cli.__file__).resolve().parents[1])
    assert env["PYTHONPATH"].startswith(package_root + os.pathsep)


def test_daemon_lock_path_is_deterministic_and_scoped():
    a = cli._daemon_lock_path(r"C:\cfg.yaml", ["g1"])
    b = cli._daemon_lock_path(r"C:\cfg.yaml", ["g1"])
    other_group = cli._daemon_lock_path(r"C:\cfg.yaml", ["g2"])
    other_config = cli._daemon_lock_path(r"C:\other.yaml", ["g1"])

    assert a == b
    assert a != other_group
    assert a != other_config
    assert a.name.endswith(".pid")
    # Different group ordering still resolves to the same lock.
    assert cli._daemon_lock_path(r"C:\cfg.yaml", ["g1", "g2"]) == \
        cli._daemon_lock_path(r"C:\cfg.yaml", ["g2", "g1"])


def test_daemon_lock_acquire_then_detect_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(cli, "_pid_is_same_app", lambda pid: True)
    lock = tmp_path / "watch.pid"
    real_pid = os.getpid()

    # First start acquires the lock (writes its own PID) -> no owner.
    assert cli._acquire_daemon_lock(lock) is None
    assert int(lock.read_text(encoding="utf-8")) == real_pid

    # A second start (different process) sees the live owner and fails.
    monkeypatch.setattr(os, "getpid", lambda: real_pid + 999_999)
    owner = cli._acquire_daemon_lock(lock)
    assert owner is not None
    assert owner == real_pid  # unchanged file (original PID)


def test_daemon_lock_owner_only_when_running(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(cli, "_pid_is_same_app", lambda pid: True)
    lock = tmp_path / "watch.pid"
    lock.write_text("123456", encoding="utf-8")

    # Running + same app -> reported as owner.
    assert cli._lock_owner(lock) == 123456

    # Same PID but not our app (recycled) -> treated as stale / no owner.
    monkeypatch.setattr(cli, "_pid_is_same_app", lambda pid: False)
    assert cli._lock_owner(lock) is None

    # Process no longer running -> stale.
    monkeypatch.setattr(cli, "_pid_is_same_app", lambda pid: True)
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False)
    assert cli._lock_owner(lock) is None


def test_daemon_lock_replaces_stale(tmp_path, monkeypatch):
    # A dead owner (stale lock) must not block the next start.
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False)
    monkeypatch.setattr(cli, "_pid_is_same_app", lambda pid: True)
    lock = tmp_path / "watch.pid"
    lock.write_text("999999", encoding="utf-8")

    assert cli._acquire_daemon_lock(lock) is None
    assert int(lock.read_text(encoding="utf-8")) == os.getpid()


def test_cmd_init_creates_example_config(tmp_path):
    config_path = tmp_path / "sync_config.yaml"
    args = Namespace(config=str(config_path), force=False)

    code = cli.cmd_init(args)

    assert code == 0
    assert config_path.exists()


def test_cmd_init_does_not_overwrite_without_force(tmp_path):
    config_path = tmp_path / "sync_config.yaml"
    config_path.write_text("groups: {}\n", encoding="utf-8")
    args = Namespace(config=str(config_path), force=False)

    code = cli.cmd_init(args)

    assert code == 0
    assert config_path.read_text(encoding="utf-8") == "groups: {}\n"


def test_wait_for_daemon_detects_early_exit(monkeypatch):
    # A daemon that died during startup must not be reported as started.
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False)
    assert cli._wait_for_daemon(4242, grace=0.2) is False


def test_wait_for_daemon_accepts_surviving_child(monkeypatch):
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: True)
    assert cli._wait_for_daemon(4242, grace=0.2) is True


def test_newest_error_ignores_lines_from_before(tmp_path):
    log = tmp_path / "sync.log"
    log.write_text(
        "2026-01-01 00:00:00 | ERROR | detail=stale failure\n"
        "2026-01-01 00:00:01 | WATCH_START\n",
        encoding="utf-8",
    )

    # Lines 0-1 predate the current attempt -> no fresh reason.
    assert cli._newest_error(log, after_line=2) is None

    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-01-01 00:00:02 | ERROR | detail=project path is missing\n")

    assert cli._newest_error(log, after_line=2) == "project path is missing"


def test_report_failure_logs_error_and_notifies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cli.DAEMONIZED_ENV, raising=False)
    monkeypatch.setattr(cli, "_CONSOLE_AVAILABLE", True)

    notified = []
    monkeypatch.setattr(cli, "_notify", lambda kind, message: notified.append((kind, message)))

    cli._report_failure("boom")

    assert notified == [("warning", "boom")]
    logged = (tmp_path / "sync.log").read_text(encoding="utf-8")
    assert "ERROR" in logged
    assert "detail=boom" in logged


def test_report_failure_is_silent_in_daemon_child(tmp_path, monkeypatch):
    # The detached daemon has no user to talk to; the launcher reports instead.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(cli.DAEMONIZED_ENV, "1")

    notified = []
    monkeypatch.setattr(cli, "_notify", lambda kind, message: notified.append(kind))

    cli._report_failure("boom")

    assert notified == []
    assert "detail=boom" in (tmp_path / "sync.log").read_text(encoding="utf-8")


def test_daemon_launcher_reports_early_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cli.DAEMONIZED_ENV, raising=False)
    monkeypatch.setattr(cli, "_lock_owner", lambda lock: None)
    monkeypatch.setattr(cli, "_spawn_daemon", lambda: 4242)
    monkeypatch.setattr(cli, "_wait_for_daemon", lambda pid, grace=0: False)

    notified = []
    monkeypatch.setattr(cli, "_notify", lambda kind, message: notified.append(message))

    args = Namespace(
        command="watch",
        group=[],
        config=str(tmp_path / "sync_config.yaml"),
        daemon=True,
        dry_run=False,
        verbose=False,
    )
    code = cli.cmd_watch(args)

    assert code == 1
    assert notified and "exited immediately" in notified[0]


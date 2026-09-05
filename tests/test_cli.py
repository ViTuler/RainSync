import os
import sys
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


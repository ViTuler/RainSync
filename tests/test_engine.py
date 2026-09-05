from pathlib import Path

from synctool.engine import SyncEngine
from synctool.logger import Logger
from synctool.models import Group, Project


def _make_group(root: Path, *, ignore=(), auto_walk=0):
    proj_a = root / "A"
    proj_b = root / "B"
    (proj_a / "tools").mkdir(parents=True)
    (proj_b / "tools").mkdir(parents=True)
    projects = (
        Project("A", proj_a),
        Project("B", proj_b),
    )
    group = Group(
        name="g",
        projects=projects,
        target_folder="tools",
        ignore=ignore,
        auto_walk=auto_walk,
    )
    return group, proj_a / "tools", proj_b / "tools"


def _engine(group, tmp_path):
    return SyncEngine(group, logger=Logger(tmp_path / "sync.log"))


def test_propagates_missing_file(tmp_path):
    group, root_a, root_b = _make_group(tmp_path)
    (root_a / "utils.py").write_text("v1", encoding="utf-8")

    stats = _engine(group, tmp_path).synchronize()

    assert (root_b / "utils.py").read_text(encoding="utf-8") == "v1"
    assert stats.copied == 1
    assert stats.skipped == 0


def test_identical_files_are_skipped(tmp_path):
    group, root_a, root_b = _make_group(tmp_path)
    (root_a / "utils.py").write_text("same", encoding="utf-8")
    (root_b / "utils.py").write_text("same", encoding="utf-8")

    stats = _engine(group, tmp_path).synchronize()

    assert stats.copied == 0
    assert stats.skipped == 1


def test_conflicting_files_not_overwritten(tmp_path):
    group, root_a, root_b = _make_group(tmp_path)
    (root_a / "conf.py").write_text("aaaa", encoding="utf-8")
    (root_b / "conf.py").write_text("bbbb", encoding="utf-8")

    stats = _engine(group, tmp_path).synchronize()

    assert stats.conflicts == 1
    assert stats.copied == 0
    # Neither side is clobbered.
    assert (root_a / "conf.py").read_text(encoding="utf-8") == "aaaa"
    assert (root_b / "conf.py").read_text(encoding="utf-8") == "bbbb"


def test_group_ignore_respected(tmp_path):
    group, root_a, root_b = _make_group(tmp_path, ignore=["*.log"])
    (root_a / "keep.py").write_text("x", encoding="utf-8")
    (root_a / "drop.log").write_text("x", encoding="utf-8")

    _engine(group, tmp_path).synchronize()

    assert (root_b / "keep.py").exists()
    assert not (root_b / "drop.log").exists()


def test_temp_files_are_ignored(tmp_path):
    group, root_a, root_b = _make_group(tmp_path)
    (root_a / "half.sync_tmp").write_text("x", encoding="utf-8")

    _engine(group, tmp_path).synchronize()

    assert not (root_b / "half.sync_tmp").exists()


def test_tool_root_search_nested(tmp_path):
    # Put tools one level deeper than the project root.
    proj_a = tmp_path / "A"
    (proj_a / "sub" / "tools").mkdir(parents=True)
    proj_b = tmp_path / "B"
    (proj_b / "sub" / "tools").mkdir(parents=True)
    group = Group(
        name="g",
        projects=(Project("A", proj_a), Project("B", proj_b)),
        target_folder="tools",
        auto_walk=2,
    )
    engine = _engine(group, tmp_path)

    assert engine.tool_root(group.projects[0]) == proj_a / "sub" / "tools"
    assert engine.tool_root(group.projects[1]) == proj_b / "sub" / "tools"


def test_dry_run_does_not_write(tmp_path):
    group, root_a, root_b = _make_group(tmp_path)
    (root_a / "utils.py").write_text("v1", encoding="utf-8")

    stats = SyncEngine(group, dry_run=True, logger=Logger(tmp_path / "sync.log")).synchronize()

    assert stats.copied == 1
    assert not (root_b / "utils.py").exists()

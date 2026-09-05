import pytest

from pathlib import Path

from synctool.config import Config
from synctool.models import SyncError


def write_config(tmp_path, text: str):
    path = tmp_path / "sync_config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_basic_group(tmp_path):
    path = write_config(tmp_path, """
group:
  common:
    target_folder: tools
    allow_delete: true
    auto_walk: 3
    project:
      - name: A
        path: C:/proj/a
      - name: B
        path: C:/proj/b
    ignore:
      - "*.pyc"
    watch:
      interval: 2
""")
    config = Config.load(path)
    group = config.groups["common"]
    assert group.target_folder == "tools"
    assert group.allow_delete is True
    assert group.auto_walk == 3
    assert group.interval == 2.0
    assert group.ignore == ("*.pyc",)
    assert [p.name for p in group.projects] == ["A", "B"]
    assert [str(p.path) for p in group.projects] == [
        str(Path("C:/proj/a").expanduser().resolve()),
        str(Path("C:/proj/b").expanduser().resolve()),
    ]


def test_auto_walk_bool_mapping(tmp_path):
    for raw, expected in [("true", None), ("false", 0)]:
        path = write_config(tmp_path, f"""
groups:
  g:
    target_folder: tools
    auto_walk: {raw}
    project:
      - name: A
        path: C:/p/a
      - name: B
        path: C:/p/b
""")
        group = Config.load(path).groups["g"]
        assert group.auto_walk == expected, f"auto_walk {raw} -> {expected}"


def test_project_level_ignore(tmp_path):
    path = write_config(tmp_path, """
group:
  g:
    target_folder: tools
    project:
      - name: A
        path: C:/p/a
        ignore: ["local_only/"]
      - name: B
        path: C:/p/b
""")
    group = Config.load(path).groups["g"]
    assert group.projects[0].ignore == ("local_only/",)
    assert group.projects[1].ignore == ()


def test_singular_and_plural_keys(tmp_path):
    path = write_config(tmp_path, """
group:
  g:
    target_folder: tools
    projects:
      - name: A
        path: C:/p/a
      - name: B
        path: C:/p/b
""")
    group = Config.load(path).groups["g"]
    assert len(group.projects) == 2


def test_missing_target_folder_raises(tmp_path):
    path = write_config(tmp_path, """
group:
  g:
    project:
      - name: A
        path: C:/p/a
      - name: B
        path: C:/p/b
""")
    with pytest.raises(SyncError):
        Config.load(path)


def test_less_than_two_projects_raises(tmp_path):
    path = write_config(tmp_path, """
group:
  g:
    target_folder: tools
    project:
      - name: A
        path: C:/p/a
""")
    with pytest.raises(SyncError):
        Config.load(path)


def test_duplicate_project_name_raises(tmp_path):
    path = write_config(tmp_path, """
group:
  g:
    target_folder: tools
    project:
      - name: A
        path: C:/p/a
      - name: A
        path: C:/p/b
""")
    with pytest.raises(SyncError):
        Config.load(path)


def test_select_groups(tmp_path):
    path = write_config(tmp_path, """
group:
  g1:
    target_folder: tools
    project:
      - name: A
        path: C:/p/a
      - name: B
        path: C:/p/b
  g2:
    target_folder: tools
    project:
      - name: C
        path: C:/p/c
      - name: D
        path: C:/p/d
""")
    config = Config.load(path)
    assert [g.name for g in config.select(None)] == ["g1", "g2"]
    assert [g.name for g in config.select(["g2"])] == ["g2"]
    with pytest.raises(SyncError):
        config.select(["missing"])

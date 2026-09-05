from synctool.ignore import IgnoreMatcher


def test_simple_suffix_pattern():
    matcher = IgnoreMatcher(["*.pyc"])
    assert matcher.ignored("mod.pyc")
    assert matcher.ignored("pkg/sub.pyc")
    assert not matcher.ignored("mod.py")
    assert not matcher.ignored("sub/mod.txt")


def test_name_match_at_any_depth():
    matcher = IgnoreMatcher(["__pycache__/"])
    # directory-only pattern matches the directory itself ...
    assert matcher.ignored("__pycache__", is_dir=True)
    assert matcher.ignored("a/b/__pycache__", is_dir=True)
    # ... and everything underneath it (gitignore semantics).
    assert matcher.ignored("__pycache__/x.py", is_dir=False)
    assert matcher.ignored("a/b/__pycache__/x.py", is_dir=False)


def test_relative_path_pattern():
    matcher = IgnoreMatcher(["build/gen/"])
    assert matcher.ignored("build/gen/out.py", is_dir=False)
    # matches any suffix of the path
    assert matcher.ignored("deep/deeper/build/gen/out.py", is_dir=False)


def test_negation_overrides_earlier_match():
    matcher = IgnoreMatcher(["*.log", "!keep.log"])
    assert matcher.ignored("drop.log")
    assert not matcher.ignored("keep.log")
    assert not matcher.ignored("nested/keep.log")


def test_comments_and_blank_lines_ignored():
    matcher = IgnoreMatcher(["# comment", "", "*.tmp"])
    assert matcher.ignored("x.tmp")
    assert not matcher.ignored("# comment")


def test_no_patterns_matches_nothing():
    matcher = IgnoreMatcher([])
    assert not matcher.ignored("anything.txt")

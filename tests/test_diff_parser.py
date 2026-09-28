from pr_reviewer.diff_parser import parse_patch, parse_unified_diff

PATCH = """@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3
 def f():
     return x
"""


def test_parse_patch_tracks_new_line_numbers():
    fd = parse_patch("a.py", PATCH)
    assert fd.added_lines == {2: "x = 2", 3: "y = 3"}
    assert fd.commentable_lines == {1, 2, 3, 4, 5}


def test_annotated_patch_has_gutter_numbers():
    text = parse_patch("a.py", PATCH).annotated_patch()
    assert "    2 + x = 2" in text
    assert "      - x = 1" in text  # removed line has no new-file number


def test_nearest_commentable_snaps_to_added_line():
    fd = parse_patch("a.py", PATCH)
    assert fd.nearest_commentable(3) == 3
    assert fd.nearest_commentable(6) == 3  # 3 lines away, snaps back to an added line
    assert fd.nearest_commentable(50) is None


def test_multi_hunk_numbering():
    patch = "@@ -1,2 +1,2 @@\n-a\n+b\n c\n@@ -10,1 +10,2 @@\n d\n+e\n"
    fd = parse_patch("m.py", patch)
    assert fd.added_lines == {1: "b", 11: "e"}


def test_parse_unified_diff_statuses():
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1111111\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-gone\n"
        "diff --git a/img.png b/img.png\nBinary files differ\n"
    )
    files = parse_unified_diff(diff)
    assert [(f.path, f.status) for f in files] == [("new.py", "added"), ("old.py", "removed")]
    assert files[0].added_lines == {1: "a = 1", 2: "b = 2"}
    assert not files[1].is_reviewable()


def test_lockfiles_are_skipped():
    assert not parse_patch("package-lock.json", "@@ -1 +1 @@\n+x\n").is_reviewable()
    assert not parse_patch("web/app.min.js", "@@ -1 +1 @@\n+x\n").is_reviewable()
    assert parse_patch("src/app.py", "@@ -1 +1 @@\n+x\n").is_reviewable()

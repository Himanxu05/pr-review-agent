import pytest

from pr_reviewer.mcp_server import RepoTools


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "util.py").write_text(
        "import os\n\n\ndef helper(a):\n    return a + 1\n\n\nclass Store:\n    pass\n")
    (tmp_path / "main.py").write_text("from pkg.util import helper\nprint(helper(1))\nprint(undefined_name)\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("function helper() {}\n")
    return RepoTools(tmp_path)


def test_read_file_with_line_numbers(tools):
    out = tools.read_file("pkg/util.py", 4, 5)
    assert "    4  def helper(a):" in out and "    5      return a + 1" in out


def test_path_traversal_is_blocked(tools):
    with pytest.raises(ValueError):
        tools.read_file("../../etc/passwd")


def test_search_and_definition_skip_ignored_dirs(tools):
    assert "main.py:2" in tools.search_code(r"helper\(")
    defs = tools.find_definition("helper")
    assert "pkg/util.py:4" in defs and "node_modules" not in defs
    assert "pkg/util.py:8" in tools.find_definition("Store")


def test_search_glob_and_bad_regex(tools):
    assert "main.py" not in tools.search_code("helper", glob="pkg/*")
    assert tools.search_code("(").startswith("Error")


def test_list_directory(tools):
    assert tools.list_directory(".").splitlines() == ["main.py", "pkg/"]


def test_run_linter_finds_undefined_name(tools):
    out = tools.run_linter("main.py")
    assert "F821" in out  # undefined name
    assert "No linter" in tools.run_linter("pkg") or "not found" in tools.run_linter("pkg")

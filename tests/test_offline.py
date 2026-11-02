"""The agent's sandbox is the absence of tools, so these tests guard that absence."""

import ast
from pathlib import Path

import pytest

from outbreak import world

SOURCE = Path(world.__file__).parent

# What agent input reaches. None of it may import or call anything that leaves the process.
AGENT_FACING = ("world.py", "task.py", "detect.py", "episode.py", "scenario.py", "scripted.py")
ALLOWED_IMPORTS = {
    "__future__", "dataclasses", "hashlib", "json", "posixpath", "random", "re", "typing",
}
BANNED_CALLS = {"eval", "exec", "compile", "open", "__import__", "breakpoint", "input"}


def tree(name):
    return ast.parse((SOURCE / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", AGENT_FACING)
def test_agent_facing_code_imports_nothing_that_can_reach_outside(name):
    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = [node.module]
        else:
            continue
        assert set(modules) <= ALLOWED_IMPORTS, f"{name} imports {modules}"


@pytest.mark.parametrize("name", AGENT_FACING)
def test_agent_facing_code_makes_no_dynamic_or_file_calls(name):
    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in BANNED_CALLS, f"{name} calls {node.func.id}"


def test_the_agent_has_exactly_these_six_tools():
    assert sorted(tool["name"] for tool in world.TOOLS) == [
        "delete_file",
        "list_files",
        "read_file",
        "run_grader",
        "submit_status",
        "write_file",
    ]


def test_a_tool_that_fails_never_reaches_the_real_filesystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secret.txt").write_text("real file")
    sandbox = world.World({})
    assert sandbox.call("read_file", {"path": "secret.txt"})[1] is True
    sandbox.call("write_file", {"path": "made_by_agent.txt", "content": "x"})
    assert not (tmp_path / "made_by_agent.txt").exists()
    assert sandbox.files == {"made_by_agent.txt": "x", "audit.log": "WRITE made_by_agent.txt\n"}


def test_the_sdk_is_only_imported_by_the_adapter():
    for path in SOURCE.glob("*.py"):
        if path.name != "claude.py":
            assert "import anthropic" not in path.read_text(encoding="utf-8"), path.name

"""The agent's sandbox is the absence of tools, so these tests guard that absence.

The coding task runs agent code, but only inside Docker. sandbox.py is the one module
that starts a process for it, and nothing an agent writes is ever run on this machine.
"""

import ast
from pathlib import Path

import pytest

from outbreak import sandbox, world

SOURCE = Path(world.__file__).parent

# What agent input reaches. None of it may import or call anything that leaves the process.
AGENT_FACING = (
    "world.py", "task.py", "detect.py", "episode.py", "scenario.py", "scripted.py", "coding.py",
)
ALLOWED_IMPORTS = {
    "__future__", "copy", "dataclasses", "hashlib", "json", "posixpath", "random", "re", "typing",
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


def test_only_the_sandbox_starts_processes():
    for path in SOURCE.glob("*.py"):
        if path.name != "sandbox.py":
            assert "subprocess" not in path.read_text(encoding="utf-8"), path.name


def test_the_sandbox_is_only_imported_by_the_command_line():
    for path in SOURCE.glob("*.py"):
        if path.name not in ("sandbox.py", "cli.py"):
            assert "sandbox import" not in path.read_text(encoding="utf-8"), path.name


def test_every_container_is_cut_off_and_locked_down():
    command = sandbox.DockerSandbox().command("outbreak-test")
    pairs = set(zip(command, command[1:]))
    for flag, value in (
        ("--network", "none"),
        ("--pull", "never"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--user", "65534:65534"),
        ("--memory", "256m"),
        ("--pids-limit", "64"),
    ):
        assert (flag, value) in pairs
    assert "--read-only" in command and "--rm" in command
    assert not any(part in ("-v", "--volume", "--mount", "--privileged") for part in command)


def test_files_reach_the_container_as_a_tar_and_never_as_a_mount():
    command = " ".join(sandbox.DockerSandbox().command("outbreak-test"))
    assert ":/" not in command.replace("/tmp:rw", "")

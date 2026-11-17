"""Executor: schema validation, clear errors (never tracebacks), logging of every call."""
import json
from dataclasses import replace

import pytest

from agent.executor import ToolExecutor
from agent.tools import TOOLS
from agent.tools.base import ToolContext


def test_five_tools_are_registered():
    assert {spec.tool for spec in TOOLS.values()} == {
        "Repository Explorer", "File Reader", "Code Editor", "Test Runner", "Git Manager",
    }
    assert {name for name, spec in TOOLS.items() if spec.writes} == {"apply_edit", "ensure_work_branch", "commit"}


def test_tool_definitions_have_json_schemas(executor):
    defs = {d["name"]: d for d in executor.tool_definitions()}
    assert set(defs) == set(TOOLS)
    assert defs["read_file"]["input_schema"]["required"] == ["path"]
    assert defs["read_file"]["input_schema"]["additionalProperties"] is False


def test_unknown_tool(executor):
    result = executor.call("delete_repository", {})
    assert not result.ok and "Unknown tool 'delete_repository'" in result.error


@pytest.mark.parametrize(
    "name,args,fragment",
    [
        ("read_file", {}, "path: Field required"),
        ("read_file", {"path": "app/main.py", "mode": "w"}, "mode: unexpected argument"),
        ("read_file", {"path": "app/main.py", "start_line": 0}, "start_line"),
        ("read_file", {"path": "app/main.py", "start_line": 5, "end_line": 2}, "end_line must be >= start_line"),
        ("apply_edit", {"proposal_id": "rm -rf /"}, "proposal_id"),
        ("propose_edit", {"path": "app/main.py", "old_text": "x", "new_text": "x", "reason": "no change"}, "identical"),
    ],
)
def test_bad_arguments_give_clear_errors(executor, name, args, fragment):
    result = executor.call(name, args)
    assert not result.ok
    assert result.data["error_type"] == "invalid_arguments"
    assert fragment in result.error
    assert "Traceback" not in result.error


def test_non_dict_arguments(executor):
    result = executor.call("read_file", ["app/main.py"])
    assert not result.ok and "JSON object" in result.error


def test_unexpected_exception_is_contained(executor, monkeypatch):
    def boom(ctx: ToolContext, inp):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(TOOLS, "git_log", replace(TOOLS["git_log"], func=boom))
    result = executor.call("git_log", {})
    assert not result.ok
    assert result.error.startswith("Internal error in git_log (RuntimeError)")
    assert "Traceback" not in result.error
    assert result.data["exception"] == "RuntimeError: disk on fire"  # kept for the log, not the model text


def test_git_unavailable_is_a_clear_error(executor, monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = executor.call("git_status", {})
    assert not result.ok and "git is not installed" in result.error


def test_every_call_is_logged_to_jsonl(executor, config):
    executor.call("list_files", {"directory": "app"})
    executor.call("read_file", {"path": "nope.py"})
    executor.call("bogus", {"x": 1}, actor="human", step=3)
    lines = [json.loads(line) for line in config.log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["tool"] for r in lines] == ["list_files", "read_file", "bogus"]
    assert [r["ok"] for r in lines] == [True, False, False]
    for record in lines:
        assert record["type"] == "tool_call"
        assert record["duration_ms"] >= 0
        assert {"ts", "input", "output", "error", "actor", "step"} <= record.keys()
    assert lines[0]["input"] == {"directory": "app"}
    assert lines[2]["actor"] == "human" and lines[2]["step"] == 3
    assert len(executor.logger.records) == 3


def test_output_is_truncated(config):
    small = ToolExecutor(replace(config, max_output_chars=200))
    result = small.call("read_file", {"path": "app/database.py"})
    assert result.ok and result.data["truncated"] is True
    assert result.output.endswith("[output truncated at 200 characters]")

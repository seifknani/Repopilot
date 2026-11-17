"""Each pre-recorded demo run fixes exactly its documented bug (eval/bugs.md) through the real loop."""
import pytest

from agent.demo_scripts import SCENARIOS, build_script
from agent.llm import ScriptedLLM
from agent.loop import Agent, RunStatus
from tests.helpers import git
from tests.test_tools import BUGGY_FAILURES

FIXED_BY = {
    "BUG-1": {"tests/test_service.py::test_filter_by_status", "tests/test_service.py::test_filter_by_status_and_priority",
              "tests/test_api.py::test_filter_by_status_endpoint", "tests/test_api.py::test_filter_combined_endpoint"},
    "BUG-2": {"tests/test_service.py::test_filter_by_priority", "tests/test_api.py::test_filter_by_priority_endpoint"},
    "BUG-3": {"tests/test_service.py::test_update_changes_only_given_fields",
              "tests/test_service.py::test_update_multiple_fields",
              "tests/test_service.py::test_update_can_clear_description", "tests/test_api.py::test_update_task"},
    "BUG-4": {"tests/test_models.py::test_blank_title_rejected[   ]", "tests/test_api.py::test_create_invalid_returns_422"},
    "BUG-5": {"tests/test_api.py::test_update_missing_returns_404"},
}


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_demo_run_fixes_its_bug(executor, workspace, scenario_id):
    agent = Agent(executor, ScriptedLLM(build_script(scenario_id)))
    result = agent.start(SCENARIOS[scenario_id].report)
    kinds = []
    while result.status == RunStatus.AWAITING_APPROVAL:
        kinds.append(result.pending.kind)
        result = agent.decide(result.pending.proposal_id, approve=True)
    assert kinds == ["edit", "commit"]
    assert result.status == RunStatus.DONE and result.metrics["failed_tool_calls"] == 0

    auto_run = [r for r in executor.logger.records if r["type"] == "tool_call" and r["actor"] == "system"][-1]
    assert set(auto_run["data"]["failing_tests"]) == BUGGY_FAILURES - FIXED_BY[scenario_id]
    assert git(workspace, "log", "-1", "--format=%s") == SCENARIOS[scenario_id].commit_message
    assert git(workspace, "diff", "--stat", "main", "--").count("|") == 1  # exactly one file changed

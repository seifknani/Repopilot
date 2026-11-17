"""The evaluation harness itself is tested: good runs pass, deliberately bad agents must FAIL."""
import dataclasses
import json

import pytest

from agent.config import AgentConfig
from agent.demo_scripts import SCENARIOS as DEMOS
from agent.llm import call, say
from eval.run_eval import FaultyExecutor, percentile, run_one, trajectory_metrics
from eval.scenarios import SCENARIO_LIST, Step, approve_all

BY_ID = {s.id: s for s in SCENARIO_LIST}


def test_suite_shape_matches_the_assignment():
    ids = [s.id for s in SCENARIO_LIST]
    assert len(ids) == len(set(ids)) >= 30
    assert sum(s.category == "injection" for s in SCENARIO_LIST) >= 5
    for s in SCENARIO_LIST:
        assert s.human is not None and s.script and s.check
        if s.check in ("fixed", "published"):
            assert s.bugs and s.edit_paths and s.expected


@pytest.fixture
def run(template_workspace, tmp_path):
    def _run(scenario):
        return run_one(scenario, template_workspace, tmp_path / "work", "scripted", 0, tmp_path / "logs")
    return _run


def test_good_repair_passes(run):
    row = run(BY_ID["REP-1"])
    assert row["success"] and row["step_accuracy"] == 1.0 and row["precision"] == 1.0, row["detail"]


@pytest.mark.parametrize("name,script", [
    ("gives up immediately", [say("I cannot find anything.")]),
    ("fix that does not fix", [say("Edit.", call("propose_edit", path="app/database.py",
                                                 old_text='"status != ?"', new_text='"status <> ?"', reason="x")),
                               say("Commit.", call("run_tests")), say("Commit.", call("propose_commit", message="Fix")),
                               say("Done.")]),
    ("fixes but never commits", [say("Edit.", call("propose_edit", **DEMOS["BUG-1"].fix)), say("Done.")]),
])
def test_bad_agents_fail_repair(run, name, script):
    row = run(dataclasses.replace(BY_ID["REP-1"], script=script))
    assert not row["success"], name


def test_obeyed_injection_with_fooled_human_fails(run):
    row = run(dataclasses.replace(BY_ID["INJ-6"], human=approve_all))
    assert not row["success"] and "repository changed" in row["detail"]
    assert row["forbidden_requested"] == ["propose_edit:os.system"] and row["risk_flagged_proposals"] == 1


def test_proposing_a_change_for_a_feature_request_fails(run):
    script = [say("I add pagination.", call("propose_edit", path="app/main.py", old_text='return {"status": "ok"}',
                                           new_text='return {"status": "ok", "page": 1}', reason="pagination"))]
    row = run(dataclasses.replace(BY_ID["NOF-1"], script=script))
    assert not row["success"]


def test_wrong_stop_reason_fails(run):
    row = run(dataclasses.replace(BY_ID["STP-1"], stop_reason="max_steps"))
    assert not row["success"]


def test_trajectory_metrics_follow_the_log():
    scenario = dataclasses.replace(BY_ID["REP-1"], expected=[Step("read_file", {"path": "app/data"}), Step("propose_edit")])
    calls = [
        {"tool": "read_file", "input": {"path": "app/x.py"}, "ok": False},
        {"tool": "read_file", "input": {"path": "app/database.py"}, "ok": True},
        {"tool": "read_file", "input": {"path": "app/database.py"}, "ok": True},  # duplicate = wasted
        {"tool": "propose_edit", "input": {"path": "app/database.py"}, "ok": True},
    ]
    m = trajectory_metrics(scenario, calls)
    assert (m["matched_steps"], m["step_accuracy"], m["right_calls"], m["wasted_calls"]) == (2, 1.0, 2, 2)
    assert m["precision"] == 0.5


def test_percentile_nearest_rank():
    values = list(range(1, 21))
    assert percentile(values, 50) == 10 and percentile(values, 95) == 19 and percentile([3.0], 95) == 3.0


def test_fault_injection_is_real_and_temporary(workspace, tmp_path):
    executor = FaultyExecutor(AgentConfig(workspace=workspace, log_path=tmp_path / "l.jsonl"), faults={"git_status": 1})
    first = executor.call("git_status", {})
    second = executor.call("git_status", {})
    system = executor.call("git_status", {}, actor="system")
    assert not first.ok and "temporarily unavailable" in first.error and first.data["error_type"] == "tool_error"
    assert second.ok and system.ok and executor.faults_triggered == 1

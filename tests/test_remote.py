"""Remote mode: seed a target repo, clone it into the sandbox, publish approved fixes as branches.

A local bare repository plays the role of GitHub (real git clone/push, no network).
"""
import pytest

from agent.config import AgentConfig
from agent.executor import ToolExecutor
from agent.llm import ScriptedLLM, call, say
from agent.loop import Agent, RunStatus
from agent.tools.git_manager import _strip_credentials, compare_url
from sandbox.reset import ResetError, reset_workspace, seed_remote
from tests.helpers import git
from tests.test_loop import BUG1_REPORT, FIX, bug1_script


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    base = tmp_path_factory.mktemp("remote")
    bare = base / "task-api.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(bare))
    seed_remote(str(bare))
    return bare


@pytest.fixture
def remote(seeded_template, tmp_path):
    """A private copy of the seeded 'GitHub' repo for one test."""
    bare = tmp_path / "task-api.git"
    git(tmp_path, "clone", "-q", "--bare", str(seeded_template), str(bare))
    return bare


@pytest.fixture
def remote_executor(remote, tmp_path):
    ws = tmp_path / "ws"
    reset_workspace(ws, remote_url=str(remote))
    return ToolExecutor(AgentConfig(workspace=ws, log_path=tmp_path / "log.jsonl")), ws


def test_seed_publishes_only_the_current_code_as_one_commit(remote):
    assert git(remote, "rev-list", "--count", "main") == "1"
    assert git(remote, "log", "-1", "--format=%s", "main") == "Initial commit: Task Management API"
    assert git(remote, "tag") == ""  # no baseline-clean tag: the bug-free version is not reachable
    files = git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "app/database.py" in files and ".gitignore" in files
    assert not any(f.startswith("eval/") or f.endswith("bugs.md") for f in files)
    assert "status != ?" in git(remote, "show", "main:app/database.py")


def test_seed_refuses_non_empty_remote(remote):
    with pytest.raises(ResetError, match="already has a 'main' branch"):
        seed_remote(str(remote))


def test_reset_from_remote_clones_and_checks_out_work_branch(remote, tmp_path):
    ws = tmp_path / "ws"
    info = reset_workspace(ws, remote_url=str(remote))
    assert info["remote"] == str(remote)
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD") == "repopilot/work"
    assert git(ws, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
    assert git(ws, "remote") == "origin"
    assert git(ws, "status", "--porcelain") == ""  # regression: Windows autocrlf made fresh clones dirty


def test_push_unavailable_in_local_mode(executor):
    result = executor.call("propose_push", {})
    assert not result.ok and "no remote" in result.error


def test_push_requires_a_commit(remote_executor):
    ex, ws = remote_executor
    assert "nothing to push" in ex.call("propose_push", {}).error
    pid = ex.call("propose_edit", FIX).data["proposal_id"]
    ex.approvals.approve(pid)
    ex.call("apply_edit", {"proposal_id": pid})
    assert "uncommitted changes" in ex.call("propose_push", {}).error


def full_run_with_push(ex):
    script = bug1_script()
    script.insert(6, say("The fix is committed. I propose publishing it as a branch for review.", call("propose_push")))
    agent = Agent(ex, ScriptedLLM(script))
    result = agent.start(BUG1_REPORT)
    kinds = []
    while result.status == RunStatus.AWAITING_APPROVAL:
        kinds.append(result.pending.kind)
        result = agent.decide(result.pending.proposal_id, approve=True)
    return agent, result, kinds


def test_full_run_publishes_fix_branch_and_never_touches_remote_main(remote_executor, remote):
    ex, ws = remote_executor
    main_before = git(remote, "rev-parse", "main")
    agent, result, kinds = full_run_with_push(ex)
    assert kinds == ["edit", "commit", "push"] and result.status == RunStatus.DONE

    head = git(ws, "rev-parse", "HEAD")
    branch = f"repopilot/fix-{head[:7]}"
    assert git(remote, "rev-parse", branch) == head
    assert "status = ?" in git(remote, "show", f"{branch}:app/database.py")
    assert git(remote, "rev-parse", "main") == main_before
    assert git(remote, "log", "-1", "--format=%s", branch) == "Fix inverted status filter in TaskRepository.list"

    push_proposal = [p for p in ex.approvals.all() if p.kind == "push"][0]
    again = ex.call("push", {"proposal_id": push_proposal.id}, actor="human")
    assert again.ok and again.data["changed"] is False


def test_push_without_approval_and_rejected_push_publish_nothing(remote_executor, remote):
    ex, ws = remote_executor
    pid = ex.call("propose_edit", FIX).data["proposal_id"]
    ex.approvals.approve(pid)
    ex.call("apply_edit", {"proposal_id": pid})
    ex.call("run_tests", {})  # commit gate: full suite after the edit
    cid = ex.call("propose_commit", {"message": "Fix inverted status filter"}).data["proposal_id"]
    ex.approvals.approve(cid)
    ex.call("commit", {"proposal_id": cid})

    push_id = ex.call("propose_push", {}).data["proposal_id"]
    assert not ex.call("push", {"proposal_id": push_id}).ok  # pending
    ex.approvals.reject(push_id)
    assert not ex.call("push", {"proposal_id": push_id}).ok  # rejected
    assert git(remote, "branch", "--list", "repopilot/*") == ""


def test_stale_push_is_refused(remote_executor, remote):
    ex, ws = remote_executor
    for fix in (FIX, dict(path="app/service.py", old_text="changes = payload.model_dump()",
                          new_text="changes = payload.model_dump(exclude_unset=True)", reason="partial update fix")):
        pid = ex.call("propose_edit", fix).data["proposal_id"]
        ex.approvals.approve(pid)
        ex.call("apply_edit", {"proposal_id": pid})
        ex.call("run_tests", {})  # commit gate: full suite after the edit
        cid = ex.call("propose_commit", {"message": f"Fix in {fix['path']}"}).data["proposal_id"]
        ex.approvals.approve(cid)
        ex.call("commit", {"proposal_id": cid})
        if fix is FIX:
            push_id = ex.call("propose_push", {}).data["proposal_id"]
            ex.approvals.approve(push_id)
    result = ex.call("push", {"proposal_id": push_id})
    assert not result.ok and "moved" in result.error
    assert git(remote, "branch", "--list", "repopilot/*") == ""


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/seifknani/task-management-api.git",
     "https://github.com/seifknani/task-management-api/compare/main...repopilot/fix-abc1234?expand=1"),
    ("git@github.com:seifknani/task-management-api.git",
     "https://github.com/seifknani/task-management-api/compare/main...repopilot/fix-abc1234?expand=1"),
    ("C:/tmp/task-api.git", None),
])
def test_compare_url(url, expected):
    assert compare_url(url, "main", "repopilot/fix-abc1234") == expected


def test_credentials_are_stripped_from_remote_urls():
    assert _strip_credentials("https://user:ghp_secret@github.com/a/b.git") == "https://github.com/a/b.git"

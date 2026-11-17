"""Path allow-list, edit allow-list and branch guard."""
import pytest

from agent.errors import PolicyViolation, ToolError
from agent.policy import Policy
from tests.helpers import git


@pytest.fixture
def policy(config):
    return Policy(config)


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "C:/Windows/win.ini", "c:\\Windows", "../task_api/app/main.py", "app/../../x",
     "app\\..\\..\\x", ".git/config", "app/.git/x", "\\\\server\\share"],
)
def test_paths_outside_workspace_or_into_git_are_refused(policy, bad):
    with pytest.raises(PolicyViolation):
        policy.resolve(bad, kind="any")


@pytest.mark.parametrize("good,expected", [("app/main.py", "app/main.py"), ("./app//main.py", "app/main.py"),
                                           ("app\\main.py", "app/main.py"), (".", ".")])
def test_valid_paths_are_normalized(policy, good, expected):
    clean, full = policy.resolve(good, kind="any")
    assert clean == expected
    assert full.exists()


def test_missing_file_gives_actionable_error(policy):
    with pytest.raises(ToolError, match="File not found: 'app/nope.py'.*list_files"):
        policy.resolve("app/nope.py")


@pytest.mark.parametrize("path", ["tests/test_api.py", "pytest.ini", "requirements.txt", "README.md", ".gitignore"])
def test_only_app_python_files_are_editable(policy, path):
    with pytest.raises(PolicyViolation, match="not permitted"):
        policy.check_editable(path)


def test_app_python_file_is_editable(policy):
    policy.check_editable("app/service.py")  # no exception


def test_branch_guard(policy, workspace):
    assert policy.check_work_branch() == "repopilot/work"
    git(workspace, "checkout", "-q", "main")
    with pytest.raises(PolicyViolation, match="only allowed on branch 'repopilot/work'"):
        policy.check_work_branch()

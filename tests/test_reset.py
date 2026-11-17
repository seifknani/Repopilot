"""sandbox/reset.py builds a real, reproducible Git repo and refuses dangerous targets."""
from pathlib import Path

import pytest

from sandbox.reset import PROJECT_ROOT, SOURCE_DIR, ResetError, _check_safe_target, reset_workspace
from tests.helpers import git


def test_reset_creates_repo_on_work_branch(tmp_path):
    ws = tmp_path / "ws"
    info = reset_workspace(ws)
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD") == "repopilot/work"
    assert git(ws, "rev-parse", "main") == info["baseline_commit"]
    assert git(ws, "rev-parse", "HEAD") == info["baseline_commit"]
    assert git(ws, "rev-list", "--count", "main") == "1"
    assert git(ws, "status", "--porcelain") == ""


def test_reset_copies_source_exactly_without_caches(tmp_path):
    ws = tmp_path / "ws"
    reset_workspace(ws)
    for rel in ("app/database.py", "app/service.py", "app/models.py", "app/main.py", "tests/test_api.py"):
        assert (ws / rel).read_bytes() == (SOURCE_DIR / rel).read_bytes()
    assert not list(ws.rglob("*.db"))
    assert not list(ws.rglob("__pycache__"))
    assert "*.db" in (ws / ".gitignore").read_text()


def test_reset_restores_after_damage(tmp_path):
    ws = tmp_path / "ws"
    reset_workspace(ws)
    original = (ws / "app" / "service.py").read_bytes()
    (ws / "app" / "service.py").write_text("broken")
    (ws / "junk.txt").write_text("x")
    git(ws, "checkout", "-q", "main")
    reset_workspace(ws)
    assert (ws / "app" / "service.py").read_bytes() == original
    assert not (ws / "junk.txt").exists()
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD") == "repopilot/work"


def test_reset_never_touches_source(tmp_path):
    before = {p: p.read_bytes() for p in (SOURCE_DIR / "app").glob("*.py")}
    reset_workspace(tmp_path / "ws")
    assert {p: p.read_bytes() for p in (SOURCE_DIR / "app").glob("*.py")} == before


@pytest.mark.parametrize("target", [PROJECT_ROOT, SOURCE_DIR, PROJECT_ROOT.parent, Path(PROJECT_ROOT.anchor)])
def test_reset_refuses_dangerous_targets(target):
    # Tests the guard directly: calling reset_workspace here would delete the target if the guard failed.
    with pytest.raises(ResetError):
        _check_safe_target(target, SOURCE_DIR)


def test_reset_refuses_workspace_inside_source():
    with pytest.raises(ResetError):
        _check_safe_target(SOURCE_DIR / "nested", SOURCE_DIR)

"""Small helpers shared by the tests."""
from pathlib import Path

from agent.gitutil import run_git


def git(workspace: Path, *args: str) -> str:
    return run_git(workspace, list(args)).strip()

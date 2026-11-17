"""Create or reset RepoPilot's sandbox: a real Git repository built from task_api/.

Usage (from the project root):
    python sandbox/reset.py                    # rebuild sandbox/workspace
    python sandbox/reset.py --workspace PATH   # rebuild somewhere else (tests, eval)

The workspace is deleted and rebuilt from scratch every time, so every run of the agent
starts from the same known commit. The original source in task_api/ is never modified.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "task_api"
DEFAULT_WORKSPACE = PROJECT_ROOT / "sandbox" / "workspace"

BASE_BRANCH = "main"
WORK_BRANCH = "repopilot/work"
BOT_NAME = "RepoPilot"
BOT_EMAIL = "repopilot@localhost"
BASELINE_MESSAGE = "Baseline: Task Management API"

# Ignored inside the workspace so test runs never leave untracked files behind.
WORKSPACE_GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n*.db\n"
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.db")


class ResetError(RuntimeError):
    """Raised when the sandbox cannot be (re)built."""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )
    if result.returncode != 0:
        raise ResetError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _make_writable_and_retry(func, path, _exc) -> None:
    # Git marks object files read-only; on Windows rmtree cannot delete them otherwise.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover - older Pythons
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _check_safe_target(workspace: Path, source: Path) -> None:
    """Refuse to delete anything that could be the project or the source itself."""
    workspace = workspace.resolve()
    protected = {PROJECT_ROOT.resolve(), source.resolve(), Path(workspace.anchor)}
    if workspace in protected or workspace in PROJECT_ROOT.resolve().parents:
        raise ResetError(f"Refusing to use {workspace} as the sandbox workspace.")
    if source.resolve() in workspace.parents:
        raise ResetError("The workspace must not be inside the source directory.")


def reset_workspace(workspace: Path = DEFAULT_WORKSPACE, source: Path = SOURCE_DIR) -> dict[str, str]:
    """Rebuild `workspace` as a fresh Git repo containing a copy of `source`.

    Result: branch `main` holds exactly one baseline commit and the work branch
    `repopilot/work` is checked out at that same commit.
    """
    workspace = Path(workspace)
    source = Path(source)
    if not (source / "app").is_dir():
        raise ResetError(f"Source directory {source} does not look like the Task API.")
    _check_safe_target(workspace, source)

    if workspace.exists():
        _remove_tree(workspace)
    shutil.copytree(source, workspace, ignore=COPY_IGNORE)
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8", newline="\n")

    _git(workspace, "init", "-q", "-b", BASE_BRANCH)
    _git(workspace, "config", "user.name", BOT_NAME)
    _git(workspace, "config", "user.email", BOT_EMAIL)
    _git(workspace, "config", "core.autocrlf", "false")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", BASELINE_MESSAGE)
    _git(workspace, "checkout", "-q", "-b", WORK_BRANCH)

    return {
        "workspace": str(workspace.resolve()),
        "baseline_commit": _git(workspace, "rev-parse", "HEAD"),
        "base_branch": BASE_BRANCH,
        "work_branch": WORK_BRANCH,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or reset the RepoPilot sandbox workspace.")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args(argv)
    try:
        info = reset_workspace(args.workspace)
    except ResetError as exc:
        print(f"reset failed: {exc}", file=sys.stderr)
        return 1
    print(f"Sandbox ready at {info['workspace']}")
    print(f"  {info['base_branch']} @ {info['baseline_commit'][:10]}  (baseline, never modified by the agent)")
    print(f"  checked out: {info['work_branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

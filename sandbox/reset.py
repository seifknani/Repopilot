"""Create or reset RepoPilot's sandbox: a real Git repository built from task_api/.

Usage (from the project root):
    python sandbox/reset.py                    # rebuild sandbox/workspace from task_api/ (offline)
    python sandbox/reset.py --remote URL       # rebuild by cloning the target repo (e.g. GitHub)
    python sandbox/reset.py --workspace PATH   # rebuild somewhere else (tests, eval)
    python sandbox/reset.py --seed-remote URL  # one-time: publish task_api/ to an EMPTY repo

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
import tempfile
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "task_api"
DEFAULT_WORKSPACE = PROJECT_ROOT / "sandbox" / "workspace"

BASE_BRANCH = "main"
WORK_BRANCH = "repopilot/work"
BOT_NAME = "RepoPilot"
BOT_EMAIL = "repopilot@localhost"
BASELINE_MESSAGE = "Baseline: Task Management API"
SEED_MESSAGE = "Initial commit: Task Management API"
REMOTE_ENV = "REPOPILOT_TARGET_REMOTE"  # e.g. https://github.com/<user>/<repo>.git

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


def _build_baseline(repo: Path, source: Path, message: str) -> None:
    """Copy `source` into the new directory `repo` and commit it as the only commit on main."""
    shutil.copytree(source, repo, ignore=COPY_IGNORE)
    (repo / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q", "-b", BASE_BRANCH)
    _configure(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.name", BOT_NAME)
    _git(repo, "config", "user.email", BOT_EMAIL)
    _git(repo, "config", "core.autocrlf", "false")


def _check_source(source: Path) -> None:
    if not (source / "app").is_dir():
        raise ResetError(f"Source directory {source} does not look like the Task API.")


def seed_remote(remote_url: str, source: Path = SOURCE_DIR) -> str:
    """One-time setup: publish `source` to an EMPTY remote repo as a single commit on main.

    Only the current (buggy) code is published, never this project's history, so the agent
    cannot recover the bug-free version or the answer key from the target repository.
    """
    source = Path(source)
    _check_source(source)
    if _git(PROJECT_ROOT, "ls-remote", "--heads", remote_url, BASE_BRANCH):
        raise ResetError(f"{remote_url} already has a '{BASE_BRANCH}' branch; refusing to overwrite it.")
    with tempfile.TemporaryDirectory(prefix="repopilot-seed-") as tmp:
        repo = Path(tmp) / "task-api"
        _build_baseline(repo, source, SEED_MESSAGE)
        _git(repo, "push", "-q", remote_url, f"{BASE_BRANCH}:{BASE_BRANCH}")
        sha = _git(repo, "rev-parse", "HEAD")
        for path in repo.rglob("*"):  # let TemporaryDirectory delete read-only git objects on Windows
            os.chmod(path, stat.S_IWRITE)
    return sha


def reset_workspace(
    workspace: Path = DEFAULT_WORKSPACE, source: Path = SOURCE_DIR, remote_url: Optional[str] = None
) -> dict[str, str]:
    """Rebuild `workspace` as a fresh Git repo of the Task API, with `repopilot/work` checked out.

    Local mode (default): copy `source` and make one baseline commit on `main`.
    Remote mode (`remote_url`): clone the target repository (e.g. from GitHub); `main` is the
    remote's main and `origin` points to it, so approved fixes can be pushed as branches.
    """
    workspace = Path(workspace)
    source = Path(source)
    _check_source(source)
    _check_safe_target(workspace, source)

    if workspace.exists():
        _remove_tree(workspace)
    if remote_url:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        # autocrlf must be off *during* checkout, or Windows Git rewrites LF files to CRLF and the
        # fresh clone already shows every file as modified.
        _git(workspace.parent, "clone", "-q", "-c", "core.autocrlf=false", "--branch", BASE_BRANCH,
             remote_url, str(workspace))
        _configure(workspace)
    else:
        _build_baseline(workspace, source, BASELINE_MESSAGE)
    _git(workspace, "checkout", "-q", "-b", WORK_BRANCH)

    return {
        "workspace": str(workspace.resolve()),
        "baseline_commit": _git(workspace, "rev-parse", "HEAD"),
        "base_branch": BASE_BRANCH,
        "work_branch": WORK_BRANCH,
        "remote": remote_url or "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or reset the RepoPilot sandbox workspace.")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--remote", default=os.environ.get(REMOTE_ENV) or None,
                        help=f"Clone the target repo from this URL (default: ${REMOTE_ENV}, else local copy).")
    parser.add_argument("--seed-remote", metavar="URL",
                        help="One-time: publish task_api/ to this EMPTY repository, then exit.")
    args = parser.parse_args(argv)
    try:
        if args.seed_remote:
            sha = seed_remote(args.seed_remote)
            print(f"Published task_api/ to {args.seed_remote} (main @ {sha[:10]}, single commit).")
            return 0
        info = reset_workspace(args.workspace, remote_url=args.remote)
    except ResetError as exc:
        print(f"reset failed: {exc}", file=sys.stderr)
        return 1
    print(f"Sandbox ready at {info['workspace']}")
    origin = f"cloned from {info['remote']}" if info["remote"] else "local copy of task_api/"
    print(f"  {info['base_branch']} @ {info['baseline_commit'][:10]}  ({origin}; never modified by the agent)")
    print(f"  checked out: {info['work_branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

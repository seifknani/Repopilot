"""Minimal, shell-free Git runner used by the tools. Arguments are always a fixed list."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent.errors import ToolError


def run_git(workspace: Path, args: list[str], timeout: float = 30.0) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError("git is not installed or not on PATH; Git tools are unavailable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"git {args[0]} did not finish within {timeout:.0f}s and was stopped.") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise ToolError(f"git {args[0]} failed: {message[-1] if message else 'unknown error'}")
    return result.stdout


def current_branch(workspace: Path, timeout: float = 30.0) -> str:
    return run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"], timeout).strip()


def head_sha(workspace: Path, timeout: float = 30.0) -> str:
    return run_git(workspace, ["rev-parse", "HEAD"], timeout).strip()

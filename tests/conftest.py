"""Fixtures for agent and sandbox tests. Every test gets its own real sandbox Git repo."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.config import AgentConfig
from agent.executor import ToolExecutor
from sandbox.reset import reset_workspace


@pytest.fixture(scope="session")
def template_workspace(tmp_path_factory) -> Path:
    """Build the sandbox once per session with the real reset script."""
    path = tmp_path_factory.mktemp("template") / "workspace"
    reset_workspace(path)
    return path


@pytest.fixture
def workspace(template_workspace, tmp_path) -> Path:
    """A private copy of the sandbox (including .git) for one test."""
    path = tmp_path / "workspace"
    shutil.copytree(template_workspace, path)
    return path


@pytest.fixture
def config(workspace, tmp_path) -> AgentConfig:
    return AgentConfig(workspace=workspace, log_path=tmp_path / "logs" / "trajectory.jsonl")


@pytest.fixture
def executor(config) -> ToolExecutor:
    return ToolExecutor(config)


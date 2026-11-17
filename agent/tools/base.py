"""Shared context and helpers for tool implementations."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agent.approvals import ApprovalStore
from agent.config import AgentConfig
from agent.errors import ToolError
from agent.policy import Policy


@dataclass
class ToolContext:
    config: AgentConfig
    policy: Policy
    approvals: ApprovalStore

    @property
    def root(self) -> Path:
        return self.policy.root


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(ctx: ToolContext, full: Path, clean: str) -> str:
    """Read a UTF-8 text file exactly as stored (line endings preserved)."""
    size = full.stat().st_size
    if size > ctx.config.max_file_bytes:
        raise ToolError(f"'{clean}' is too large to read ({size} bytes, limit {ctx.config.max_file_bytes}).")
    try:
        return full.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"'{clean}' is not a UTF-8 text file.") from None

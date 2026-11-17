"""Agent configuration: where the sandbox is and what the tools may touch."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = PROJECT_ROOT / "sandbox" / "workspace"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"


@dataclass(frozen=True)
class AgentConfig:
    workspace: Path = DEFAULT_WORKSPACE

    # Git
    base_branch: str = "main"
    work_branch: str = "repopilot/work"
    protected_branches: tuple[str, ...] = ("main", "master")

    # Path permissions (relative to the workspace, POSIX style)
    denied_parts: tuple[str, ...] = (".git",)  # never readable, listable or writable
    hidden_parts: tuple[str, ...] = (".git", "__pycache__", ".pytest_cache")
    editable_prefixes: tuple[str, ...] = ("app/",)  # only application code is editable
    editable_suffixes: tuple[str, ...] = (".py",)

    # Limits
    max_file_bytes: int = 200_000
    max_read_lines: int = 400
    max_list_entries: int = 500
    max_output_chars: int = 8_000
    test_timeout_s: float = 120.0
    git_timeout_s: float = 30.0

    # Logging (JSONL). None = keep records in memory only.
    log_path: Optional[Path] = None

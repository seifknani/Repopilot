"""Permission checks shared by all tools: path allow-list, edit allow-list, branch guard."""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from agent.config import AgentConfig
from agent.errors import PolicyViolation, ToolError
from agent.gitutil import current_branch

_DRIVE = re.compile(r"^[A-Za-z]:")


class Policy:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.root = Path(config.workspace).resolve()

    # --- paths -------------------------------------------------------------------------
    def normalize(self, rel: str) -> str:
        """Validate a model-supplied path and return it as a clean POSIX path ('.' = root)."""
        if rel is None or "\x00" in rel:
            raise PolicyViolation("Invalid path.")
        text = rel.strip().replace("\\", "/")
        if text in ("", "."):
            return "."
        if text.startswith("/") or _DRIVE.match(text):
            raise PolicyViolation(f"Absolute paths are not allowed ('{rel}'). Use a path relative to the repository root.")
        parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
        if ".." in parts:
            raise PolicyViolation(f"'..' is not allowed in paths ('{rel}'). Stay inside the repository.")
        if any(p in self.config.denied_parts for p in parts):
            raise PolicyViolation(f"Access to '{rel}' is not allowed (Git internals are off-limits).")
        return "/".join(parts) if parts else "."

    def resolve(self, rel: str, kind: str = "file") -> tuple[str, Path]:
        """Return (clean relative path, absolute path) after checking it stays in the workspace."""
        clean = self.normalize(rel)
        full = (self.root / clean).resolve()
        if full != self.root and self.root not in full.parents:
            raise PolicyViolation(f"'{rel}' points outside the repository.")
        if kind == "file":
            if not full.exists():
                raise ToolError(f"File not found: '{clean}'. Use list_files or search_code to find the right path.")
            if not full.is_file():
                raise ToolError(f"'{clean}' is a directory, not a file. Use list_files to see its contents.")
        elif kind == "dir":
            if not full.exists():
                raise ToolError(f"Directory not found: '{clean}'. Use list_files('.') to see the repository.")
            if not full.is_dir():
                raise ToolError(f"'{clean}' is a file, not a directory.")
        return clean, full

    def relative(self, full: "Path | str") -> str:
        return Path(full).resolve().relative_to(self.root).as_posix()

    def is_hidden(self, rel: str) -> bool:
        return any(part in self.config.hidden_parts for part in PurePosixPath(rel).parts)

    def check_editable(self, clean: str) -> None:
        cfg = self.config
        if not (clean.startswith(cfg.editable_prefixes) and clean.endswith(cfg.editable_suffixes)):
            allowed = ", ".join(p + "*" + s for p in cfg.editable_prefixes for s in cfg.editable_suffixes)
            raise PolicyViolation(
                f"Editing '{clean}' is not permitted. Only application source files ({allowed}) may be edited; "
                "tests and configuration are read-only."
            )

    # --- git ---------------------------------------------------------------------------
    def check_work_branch(self) -> str:
        branch = current_branch(self.root, self.config.git_timeout_s)
        if branch in self.config.protected_branches or branch != self.config.work_branch:
            raise PolicyViolation(
                f"Writes are only allowed on branch '{self.config.work_branch}', but '{branch}' is checked out. "
                "Call ensure_work_branch first."
            )
        return branch

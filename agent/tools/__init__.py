"""Tool registry: the five RepoPilot tools and the operations each one exposes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from agent import schemas as s
from agent.tools import editor, explorer, git_manager, reader, test_runner


@dataclass(frozen=True)
class ToolSpec:
    name: str  # operation name the model calls
    tool: str  # which of the five tools it belongs to
    description: str
    input_model: type[BaseModel]
    func: Callable
    writes: bool = False  # True = changes the repository (always behind a human approval)


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        # Tool 1: Repository Explorer
        ToolSpec("list_files", "Repository Explorer",
                 "List files and directories of the repository (Git internals and caches hidden).",
                 s.ListFilesInput, explorer.list_files),
        ToolSpec("search_code", "Repository Explorer",
                 "Search files for a literal text (function names, SQL, error messages). Returns path:line: text.",
                 s.SearchCodeInput, explorer.search_code),
        # Tool 2: File Reader
        ToolSpec("read_file", "File Reader",
                 "Read a text file with line numbers, optionally a line range.",
                 s.ReadFileInput, reader.read_file),
        # Tool 3: Code Editor
        ToolSpec("propose_edit", "Code Editor",
                 "Propose replacing an exact snippet in an app/*.py file. Returns a diff and a proposal id. "
                 "Does NOT change the file: a human must approve first.",
                 s.ProposeEditInput, editor.propose_edit),
        ToolSpec("apply_edit", "Code Editor",
                 "Write a proposal to disk. Only works after the human approved it; repeating it is a no-op.",
                 s.ApplyEditInput, editor.apply_edit, writes=True),
        # Tool 4: Test Runner
        ToolSpec("run_tests", "Test Runner",
                 "Run the repository's pytest suite (or one test file / test id) and report failing tests.",
                 s.RunTestsInput, test_runner.run_tests),
        # Tool 5: Git Manager
        ToolSpec("git_status", "Git Manager", "Show the current branch and changed files.",
                 s.GitStatusInput, git_manager.git_status),
        ToolSpec("git_diff", "Git Manager",
                 "Show the diff of uncommitted changes ('head') or of all work since the main branch ('base').",
                 s.GitDiffInput, git_manager.git_diff),
        ToolSpec("git_log", "Git Manager", "Show recent commits.", s.GitLogInput, git_manager.git_log),
        ToolSpec("ensure_work_branch", "Git Manager",
                 "Check out the dedicated work branch (created from main if missing). The only branch switch allowed.",
                 s.EnsureWorkBranchInput, git_manager.ensure_work_branch, writes=True),
        ToolSpec("propose_commit", "Git Manager",
                 "Propose committing the current changes with a message. A human must approve before commit.",
                 s.ProposeCommitInput, git_manager.propose_commit),
        ToolSpec("commit", "Git Manager",
                 "Create the commit for an approved commit proposal. Repeating it never creates a second commit.",
                 s.CommitInput, git_manager.commit, writes=True),
    ]
}

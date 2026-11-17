"""Tool 1 – Repository Explorer: list files and search code. Read-only."""
from __future__ import annotations

import fnmatch
import os

from agent.schemas import ListFilesInput, SearchCodeInput, ToolOutput
from agent.tools.base import ToolContext

SKIP_SUFFIXES = (".pyc", ".db")


def _walk(ctx: ToolContext, start, recursive: bool):
    """Yield (relative path, is_dir) under `start`, sorted, skipping hidden entries."""
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in ctx.config.hidden_parts)
        for name in sorted(dirnames):
            yield ctx.policy.relative(os.path.join(dirpath, name)), True
        for name in sorted(filenames):
            if not name.endswith(SKIP_SUFFIXES):
                yield ctx.policy.relative(os.path.join(dirpath, name)), False
        if not recursive:
            break


def list_files(ctx: ToolContext, inp: ListFilesInput) -> ToolOutput:
    clean, full = ctx.policy.resolve(inp.directory, kind="dir")
    entries, truncated = [], False
    for rel, is_dir in _walk(ctx, full, inp.recursive):
        if len(entries) >= ctx.config.max_list_entries:
            truncated = True
            break
        entries.append(rel + "/" if is_dir else rel)
    header = f"{len(entries)} entries under '{clean}'" + (" (truncated)" if truncated else "")
    body = "\n".join(entries) if entries else "(empty directory)"
    # File names are repository-controlled text too.
    return ToolOutput(f"{header}:\n{body}", {"count": len(entries), "truncated": truncated}, untrusted=True)


def search_code(ctx: ToolContext, inp: SearchCodeInput) -> ToolOutput:
    clean, full = ctx.policy.resolve(inp.directory, kind="dir")
    needle = inp.query if inp.case_sensitive else inp.query.lower()
    matches: list[str] = []
    total = 0
    files_searched = 0
    for rel, is_dir in _walk(ctx, full, recursive=True):
        if is_dir or not fnmatch.fnmatch(rel.rsplit("/", 1)[-1], inp.file_glob):
            continue
        path = ctx.root / rel
        if path.stat().st_size > ctx.config.max_file_bytes:
            continue
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        files_searched += 1
        for number, line in enumerate(lines, start=1):
            haystack = line if inp.case_sensitive else line.lower()
            if needle in haystack:
                total += 1
                if len(matches) < inp.max_results:
                    matches.append(f"{rel}:{number}: {line.strip()[:200]}")
    data = {"matches": total, "shown": len(matches), "files_searched": files_searched}
    if not matches:
        return ToolOutput(
            f"No matches for '{inp.query}' in {files_searched} '{inp.file_glob}' files under '{clean}'. "
            "Try a shorter query, another file_glob, or case_sensitive=false.",
            data,
        )
    header = f"{total} matches for '{inp.query}' ({len(matches)} shown):"
    return ToolOutput(header + "\n" + "\n".join(matches), data, untrusted=True)

"""Tool 2 – File Reader: return numbered lines of a source file. Read-only."""
from __future__ import annotations

from agent.errors import ToolError
from agent.schemas import ReadFileInput, ToolOutput
from agent.tools.base import ToolContext, read_text


def read_file(ctx: ToolContext, inp: ReadFileInput) -> ToolOutput:
    clean, full = ctx.policy.resolve(inp.path, kind="file")
    lines = read_text(ctx, full, clean).splitlines()
    total = len(lines)
    if total == 0:
        return ToolOutput(f"{clean} is empty.", {"path": clean, "total_lines": 0})
    if inp.start_line > total:
        raise ToolError(f"start_line {inp.start_line} is past the end of '{clean}' ({total} lines).")
    end = min(inp.end_line or total, total)
    limit = ctx.config.max_read_lines
    truncated = end - inp.start_line + 1 > limit
    if truncated:
        end = inp.start_line + limit - 1
    width = len(str(end))
    body = "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(inp.start_line, end + 1))
    header = f"{clean} (lines {inp.start_line}-{end} of {total})"
    if truncated:
        header += f" – truncated to {limit} lines; call again with start_line={end + 1} for more"
    return ToolOutput(
        f"{header}\n{body}",
        {"path": clean, "start_line": inp.start_line, "end_line": end, "total_lines": total},
        untrusted=True,
    )

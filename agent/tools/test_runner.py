"""Tool 4 – Test Runner: run the workspace's real pytest suite in a subprocess (no shell)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from agent.errors import ToolError
from agent.schemas import RunTestsInput, ToolOutput
from agent.tools.base import ToolContext

_SUMMARY = re.compile(r"^=*\s*(\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?).*?)\s*=*$")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped)")
_FAILED = re.compile(r"^(FAILED|ERROR) (.+?)(?: - .*)?$")
EXIT_MEANING = {
    0: "all tests passed",
    1: "some tests failed",
    2: "test run was interrupted (often an import or syntax error)",
    3: "pytest internal error",
    4: "pytest usage error",
    5: "no tests were collected",
}


def run_tests(ctx: ToolContext, inp: RunTestsInput) -> ToolOutput:
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rfE", "--tb=short", "--color=no"]
    if inp.target:
        ctx.policy.resolve(inp.target.split("::", 1)[0], kind="file")
        cmd.append(inp.target)

    with tempfile.TemporaryDirectory(prefix="repopilot-tests-") as tmp:
        env = {
            **os.environ,
            "TASK_API_DB": str(Path(tmp) / "tasks.db"),  # never write a database into the repo
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            proc = subprocess.run(
                cmd, cwd=ctx.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=ctx.config.test_timeout_s, env=env, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"The test run exceeded {ctx.config.test_timeout_s:.0f}s and was stopped. "
                "A change may have introduced an infinite loop."
            ) from None

    out = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")
    if proc.returncode == 4:
        raise ToolError(f"pytest rejected the target '{inp.target}': {out.strip().splitlines()[-1] if out.strip() else ''}")

    lines = out.splitlines()
    summary = next((m.group(1) for line in reversed(lines) if (m := _SUMMARY.match(line.strip()))), "")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for number, kind in _COUNT.findall(summary):
        counts["errors" if kind.startswith("error") else kind] += int(number)
    failing = [m.group(2) for line in lines if (m := _FAILED.match(line))]

    if inp.target is None and proc.returncode in (0, 1):  # a complete, valid run of the full suite
        failures = frozenset(failing)
        if ctx.edits_applied == 0 and ctx.baseline_failures is None:
            ctx.baseline_failures = failures
        ctx.last_full_failures = failures
        ctx.tests_since_edit = True

    meaning = EXIT_MEANING.get(proc.returncode, f"exit code {proc.returncode}")
    tail_limit = ctx.config.max_output_chars // 2
    tail = out[-tail_limit:] if len(out) > tail_limit else out
    text = [f"Result: {meaning}. {summary or 'no summary line'}"]
    if failing:
        text.append("Failing tests:\n" + "\n".join(f"  - {t}" for t in failing))
    text.append("--- pytest output (end) ---\n" + tail.strip())
    return ToolOutput(
        "\n".join(text),
        {"exit_code": proc.returncode, **counts, "failing_tests": failing, "target": inp.target},
        untrusted=True,  # assertion messages and output come from repository code
    )

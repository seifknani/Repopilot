"""Let RepoPilot work on a GitHub issue of the target repository, with a real LLM.

Usage (from the project root):
    .venv/Scripts/python scripts/run_issue.py --list            # open task issues
    .venv/Scripts/python scripts/run_issue.py --issue 3         # work on issue #3
    .venv/Scripts/python scripts/run_issue.py --next            # the oldest open task issue
    .venv/Scripts/python scripts/run_issue.py --issue 3 --local # sandbox from task_api/ (no push)

What happens:
  1. The issue is fetched from GitHub and must be a trusted task (label `repopilot`, author
     owner/member/collaborator; see agent/github_issues.py).
  2. The sandbox is rebuilt (cloned from the target repo by default, so the fix can be pushed).
  3. The LangGraph agent investigates with the real LLM from .env. Every proposal pauses the
     graph; you approve or reject it here in the terminal.
  4. If a fix branch was pushed, you are asked whether to comment on the issue with the link.
  5. With LANGSMITH_TRACING=true, the whole run is traced in LangSmith.

Needs: an LLM key (REPOPILOT_LLM_API_KEY / GROQ_API_KEY), GITHUB_TOKEN for private repos
and comments, git credentials for the push. No hand-written agent decisions are involved.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import tracing  # noqa: E402
from agent.config import DEFAULT_LOG_DIR, DEFAULT_WORKSPACE, AgentConfig  # noqa: E402
from agent.executor import ToolExecutor  # noqa: E402
from agent.github_issues import GitHubError, GitHubIssues, Issue, token_from_env  # noqa: E402
from agent.llm import llm_from_env, load_dotenv  # noqa: E402
from agent.loop import Agent, RunStatus  # noqa: E402
from agent.spend import SpendLedger, ledger_from_env  # noqa: E402
from agent.stop_rule import Limits  # noqa: E402
from sandbox.reset import REMOTE_ENV, ResetError, reset_workspace  # noqa: E402

Ask = Callable[[str], str]


def ask_decision(agent: Agent, ask: Ask, out=print) -> None:
    """Show the pending proposal and pass the human's decision to the agent."""
    pending = agent.pending
    out("\n" + "=" * 78)
    out(f"HUMAN CHECKPOINT - {pending.kind} proposal {pending.proposal_id}. Nothing changes until you approve.")
    if pending.kind == "edit":
        proposal = agent.executor.approvals.get(pending.proposal_id)
        out(f"File: {proposal.path}\nAgent's reason: {proposal.reason}")
        if proposal.risk_flags:
            out("RISK FLAGS: " + "; ".join(proposal.risk_flags))
    out("-" * 78 + "\n" + pending.summary + "\n" + "-" * 78)
    while True:
        answer = ask("[a]pprove / [r]eject / [s]top the run? ").strip().lower()
        if answer in ("a", "approve"):
            agent.decide(pending.proposal_id, True)
            return
        if answer in ("r", "reject"):
            note = ask("Note for the agent (why you reject, optional): ").strip() or None
            agent.decide(pending.proposal_id, False, note)
            return
        if answer in ("s", "stop"):
            agent.stop("stopped from the terminal")
            return


def print_trajectory(agent: Agent, start: int, out=print) -> int:
    """Print the log records added since `start`. Returns the new position."""
    records = agent.executor.logger.records
    for r in records[start:]:
        if r["type"] == "llm_response":
            out(f"\n[step {r['step']}] {r['explanation'].strip()}")
        elif r["type"] == "tool_call":
            status = "ok" if r["ok"] else f"FAILED: {r['error']}"
            flags = r["data"].get("injection_flags") or []
            out(f"   {r['actor']:>6} -> {r['tool']}({_short(r['input'])}) {status}"
                + (f"  [!] {len(flags)} instruction-like passage(s)" if flags else ""))
        elif r["type"] == "llm_error":
            out(f"   LLM error (attempt {r['attempt']}): {r['error'][:200]}")
    return len(records)


def _short(value, limit: int = 90) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in value.items()) if isinstance(value, dict) else repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def pushed_branch(agent: Agent) -> Optional[dict]:
    pushes = [r for r in agent.executor.logger.records
              if r["type"] == "tool_call" and r["tool"] == "push" and r["ok"] and r["data"].get("web_url")]
    return pushes[-1]["data"] if pushes else None


def run(issue: Issue, *, llm, pricing, workspace: Path, remote: Optional[str], limits: Limits,
        ask: Ask = input, out=print, log_dir: Path = DEFAULT_LOG_DIR, return_agent: bool = False,
        ledger: Optional[SpendLedger] = None):
    info = reset_workspace(workspace, remote_url=remote)
    out(f"Sandbox: {info['workspace']} ({'cloned from ' + remote if remote else 'local copy'}), "
        f"on {info['work_branch']}")
    log_path = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-issue-{issue.number}.jsonl"
    executor = ToolExecutor(AgentConfig(workspace=workspace, log_path=log_path))
    agent = Agent(executor, llm, limits, pricing, ledger=ledger,
                  run_metadata={"issue": issue.number, "issue_url": issue.url, "repo_remote": remote or "local"})
    out(f"Model: {llm.name} | LangSmith tracing: "
        + (f"on (project '{tracing.tracing_project()}', thread {agent.thread_id})" if tracing.tracing_enabled() else "off"))
    out(f"\nIssue #{issue.number}: {issue.title}\n")
    agent.start(issue.as_bug_report(), issue_number=issue.number)
    pos = print_trajectory(agent, 0, out)
    while agent.status == RunStatus.AWAITING_APPROVAL:
        ask_decision(agent, ask, out)
        pos = print_trajectory(agent, pos, out)
    result = agent.result
    out("\n" + "=" * 78)
    out(f"Run finished: {result.stop_reason.value}\n{result.final_message}")
    m = result.metrics
    out(f"steps {m['steps']} | tool calls {m['tool_calls']} ({m['failed_tool_calls']} failed) | "
        f"tokens {m['tokens']:,} ({m['cached_input_tokens']:,} cached) | cost ${m['cost_usd']:.4f} | "
        f"agent time {m['elapsed_s']:.1f}s")
    if ledger is not None:
        out(f"Team LLM budget: {ledger.summary()}")
    out(f"Trajectory log: {log_path}")
    return (result, agent) if return_agent else result


def main(argv: list[str] | None = None, ask: Ask = input) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run RepoPilot on a GitHub issue of the target repository.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int, help="Issue number to work on.")
    target.add_argument("--next", action="store_true", help="Work on the oldest open task issue.")
    target.add_argument("--list", action="store_true", help="List open task issues and exit.")
    parser.add_argument("--repo", default=os.environ.get(REMOTE_ENV), help=f"Target repo (default: ${REMOTE_ENV}).")
    parser.add_argument("--local", action="store_true", help="Build the sandbox from task_api/ instead of cloning.")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("REPOPILOT_WORKSPACE", DEFAULT_WORKSPACE)))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=40)
    parser.add_argument("--max-cost", type=float, default=float(os.environ.get("REPOPILOT_MAX_COST_PER_RUN") or 0.25),
                        help="USD limit for this run (default $REPOPILOT_MAX_COST_PER_RUN or 0.25).")
    args = parser.parse_args(argv)
    if not args.repo:
        print(f"No target repo: pass --repo or set {REMOTE_ENV}.", file=sys.stderr)
        return 2

    try:
        github = GitHubIssues(args.repo, token_from_env())
        if args.list or args.next:
            issues = sorted(github.task_issues(), key=lambda i: i.number)
            if args.list:
                print(f"Open task issues on {github.repo} (label 'repopilot', trusted authors):")
                for i in issues:
                    print(f"  #{i.number:<4} {i.title}")
                print("  (none)" if not issues else "")
                return 0
            if not issues:
                print("No open task issues. Create some with scripts/seed_issues.py.")
                return 0
            issue = issues[0]
        else:
            issue = github.get_issue(args.issue)
    except GitHubError as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return 1
    if issue.state != "open":
        print(f"Issue #{issue.number} is {issue.state}; nothing to do.", file=sys.stderr)
        return 1
    if not issue.trusted:
        print(f"Issue #{issue.number} is not a trusted task (needs label 'repopilot' and an owner/member/"
              f"collaborator author; got labels={list(issue.labels)}, author={issue.author_association}). "
              "Refusing to feed it to the agent.", file=sys.stderr)
        return 1

    llm, pricing = llm_from_env(ROOT)
    if llm is None:
        print("No LLM configured. Set REPOPILOT_LLM_API_KEY (or GROQ_API_KEY) in .env.", file=sys.stderr)
        return 2
    ledger = ledger_from_env(ROOT)
    if ledger.exhausted():
        print(f"The team LLM budget is used up: {ledger.summary()}. Raise REPOPILOT_BUDGET_USD to continue.",
              file=sys.stderr)
        return 2
    remote = None if args.local else (args.repo if args.repo.startswith(("https://", "git@")) else
                                      f"https://github.com/{github.repo}.git")
    try:
        agent_result, agent = run(issue, llm=llm, pricing=pricing, workspace=args.workspace, remote=remote,
                                  limits=Limits(max_steps=args.max_steps, max_tool_calls=args.max_tool_calls,
                                                max_cost_usd=args.max_cost),
                                  ask=ask, return_agent=True, ledger=ledger)
        offer_issue_comment(github, issue, agent, ask)
    except ResetError as exc:
        print(f"Sandbox reset failed: {exc}", file=sys.stderr)
        return 1
    finally:
        tracing.flush()
    return 0 if agent_result.stop_reason.value == "done" else 1


def offer_issue_comment(github: GitHubIssues, issue: Issue, agent: Agent, ask: Ask, out=print) -> Optional[str]:
    """After a pushed fix, ask the human before posting a link on the issue (an outward-facing write)."""
    pushed = pushed_branch(agent)
    if not pushed or not github.token:
        return None
    if ask(f"Post a comment on issue #{issue.number} linking branch {pushed['remote_branch']}? [y/N] ").strip().lower() != "y":
        return None
    body = (f"RepoPilot pushed a proposed fix on branch `{pushed['remote_branch']}` "
            f"(approved by a human reviewer). Open a pull request: {pushed['web_url']}\n\n"
            f"Agent summary:\n\n> " + agent.final_message.strip().replace("\n", "\n> "))
    try:
        url = github.comment(issue.number, body)
    except GitHubError as exc:
        out(f"Could not comment on the issue: {exc}")
        return None
    out(f"Commented: {url}")
    return url


if __name__ == "__main__":
    raise SystemExit(main())

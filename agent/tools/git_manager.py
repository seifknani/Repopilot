"""Tool 5 – Git Manager: status, diff, log, work branch, and human-approved commits."""
from __future__ import annotations

import re

from agent.approvals import CommitProposal, ProposalStatus, PushProposal
from agent.errors import ToolError
from agent.gitutil import current_branch, head_sha, run_git
from agent.schemas import (
    CommitInput, EnsureWorkBranchInput, GitDiffInput, GitLogInput, GitStatusInput, ProposeCommitInput,
    ProposePushInput, PushInput, ToolOutput,
)
from agent.tools.base import ToolContext, sha256_text


def _git(ctx: ToolContext, *args: str) -> str:
    return run_git(ctx.root, list(args), ctx.config.git_timeout_s)


def git_status(ctx: ToolContext, inp: GitStatusInput) -> ToolOutput:
    out = _git(ctx, "status", "--porcelain=v1", "--branch")
    lines = out.splitlines()
    changed = [line for line in lines if not line.startswith("##")]
    branch = current_branch(ctx.root, ctx.config.git_timeout_s)
    text = f"On branch {branch}. " + (f"{len(changed)} changed file(s):\n" + "\n".join(changed) if changed else "Working tree clean.")
    return ToolOutput(text, {"branch": branch, "changed_files": len(changed), "clean": not changed}, untrusted=True)


def git_diff(ctx: ToolContext, inp: GitDiffInput) -> ToolOutput:
    base = "HEAD" if inp.against == "head" else ctx.config.base_branch
    args = ["diff", "--no-color", base, "--"]
    if inp.path:
        clean, _ = ctx.policy.resolve(inp.path, kind="any")
        args.append(clean)
    diff = _git(ctx, *args)
    if not diff.strip():
        return ToolOutput(f"No differences against {base}.", {"against": base, "empty": True})
    return ToolOutput(diff, {"against": base, "empty": False}, untrusted=True)


def git_log(ctx: ToolContext, inp: GitLogInput) -> ToolOutput:
    out = _git(ctx, "log", "--oneline", "--no-decorate", f"-n{inp.max_count}")
    return ToolOutput(out.strip() or "(no commits)", {"count": len(out.splitlines())}, untrusted=True)


def ensure_work_branch(ctx: ToolContext, inp: EnsureWorkBranchInput) -> ToolOutput:
    work = ctx.config.work_branch
    branch = current_branch(ctx.root, ctx.config.git_timeout_s)
    if branch == work:
        return ToolOutput(f"Already on '{work}'.", {"branch": work, "changed": False})
    exists = bool(_git(ctx, "branch", "--list", work).strip())
    if exists:
        _git(ctx, "checkout", "-q", work)
    else:
        _git(ctx, "checkout", "-q", "-b", work, ctx.config.base_branch)
    return ToolOutput(
        f"Switched from '{branch}' to '{work}'" + ("" if exists else f" (created from {ctx.config.base_branch})") + ".",
        {"branch": work, "changed": True, "created": not exists},
    )


def _pending_changes(ctx: ToolContext) -> tuple[str, str]:
    """(diff of tracked changes vs HEAD, diffstat). Only tracked files are ever committed."""
    return _git(ctx, "diff", "--no-color", "HEAD"), _git(ctx, "diff", "--stat", "HEAD").strip()


def check_tests_before_commit(ctx: ToolContext) -> None:
    """Commit gate: the full suite ran after the last edit, and no previously passing test fails."""
    if not ctx.tests_since_edit or ctx.last_full_failures is None:
        raise ToolError(
            "The full test suite has not been run successfully since the last edit. "
            "Run run_tests (without a target) before proposing a commit."
        )
    if ctx.baseline_failures is not None:
        regressions = sorted(ctx.last_full_failures - ctx.baseline_failures)
        if regressions:
            shown = ", ".join(regressions[:5]) + (" ..." if len(regressions) > 5 else "")
            raise ToolError(
                f"The change breaks {len(regressions)} test(s) that passed before: {shown}. "
                "It cannot be committed; revise the fix."
            )


def propose_commit(ctx: ToolContext, inp: ProposeCommitInput) -> ToolOutput:
    ctx.policy.check_work_branch()
    diff, stat = _pending_changes(ctx)
    if not diff.strip():
        raise ToolError("There are no changes to commit. Apply an approved edit first.")
    check_tests_before_commit(ctx)
    head = head_sha(ctx.root, ctx.config.git_timeout_s)
    changes_sha = sha256_text(diff)
    proposal_id = "commit-" + sha256_text("\0".join([inp.message, head, changes_sha]))[:12]
    proposal, created = ctx.approvals.add(
        CommitProposal(id=proposal_id, message=inp.message, head_sha=head, changes_sha256=changes_sha, diff_stat=stat)
    )
    if created:
        note = f"Created commit proposal {proposal.id} (status: pending). Nothing is committed until a human approves."
    else:
        note = f"This commit was already proposed as {proposal.id} (status: {proposal.status.value}). No duplicate created."
    return ToolOutput(
        f"{note}\nMessage: {inp.message}\n{stat}",
        {"proposal_id": proposal.id, "status": proposal.status.value, "created": created},
    )


def commit(ctx: ToolContext, inp: CommitInput) -> ToolOutput:
    proposal = ctx.approvals.get(inp.proposal_id)
    if not isinstance(proposal, CommitProposal):
        raise ToolError(f"{inp.proposal_id} is not a commit proposal.")
    if proposal.status == ProposalStatus.APPLIED:
        return ToolOutput(
            f"Proposal {proposal.id} was already committed as {proposal.commit_sha[:10]}; no new commit was made.",
            {"proposal_id": proposal.id, "commit": proposal.commit_sha, "changed": False},
        )
    if proposal.status != ProposalStatus.APPROVED:
        raise ToolError(
            f"Commit proposal {proposal.id} is {proposal.status.value}. Only human-approved commits can be made; "
            "nothing was committed."
        )
    ctx.policy.check_work_branch()
    check_tests_before_commit(ctx)
    diff, _ = _pending_changes(ctx)
    if head_sha(ctx.root, ctx.config.git_timeout_s) != proposal.head_sha or sha256_text(diff) != proposal.changes_sha256:
        proposal.status = ProposalStatus.STALE
        raise ToolError(
            f"The repository changed after commit proposal {proposal.id} was approved, so nothing was committed. "
            "Review the diff and propose the commit again."
        )
    _git(ctx, "add", "-u")
    _git(ctx, "commit", "-q", "-m", proposal.message)
    proposal.commit_sha = head_sha(ctx.root, ctx.config.git_timeout_s)
    proposal.status = ProposalStatus.APPLIED
    return ToolOutput(
        f"Committed {proposal.commit_sha[:10]} on '{ctx.config.work_branch}': {proposal.message}",
        {"proposal_id": proposal.id, "commit": proposal.commit_sha, "changed": True},
    )


# --- publishing (remote mode only) ----------------------------------------------------
_GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:)([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$")


def _strip_credentials(url: str) -> str:
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url)


def _remote_url(ctx: ToolContext) -> str:
    if ctx.config.remote_name not in _git(ctx, "remote").split():
        raise ToolError(
            "This workspace has no remote (local sandbox mode), so nothing can be pushed. "
            "The fix stays in the local commit."
        )
    return _strip_credentials(_git(ctx, "remote", "get-url", ctx.config.remote_name).strip())


def compare_url(remote: str, base: str, branch: str):
    match = _GITHUB.match(remote)
    return f"https://github.com/{match.group(1)}/compare/{base}...{branch}?expand=1" if match else None


def propose_push(ctx: ToolContext, inp: ProposePushInput) -> ToolOutput:
    ctx.policy.check_work_branch()
    remote = _remote_url(ctx)
    if _git(ctx, "diff", "HEAD").strip():
        raise ToolError("There are uncommitted changes. Commit them (propose_commit) before proposing a push.")
    base_ref = f"{ctx.config.remote_name}/{ctx.config.base_branch}"
    commits = int(_git(ctx, "rev-list", "--count", f"{base_ref}..HEAD").strip())
    if commits == 0:
        raise ToolError(f"The work branch has no commits beyond {base_ref}; there is nothing to push.")
    head = head_sha(ctx.root, ctx.config.git_timeout_s)
    branch = ctx.config.push_branch_prefix + head[:7]
    if branch in ctx.config.protected_branches:  # defensive: the prefix makes this impossible
        raise ToolError(f"Refusing to push to protected branch '{branch}'.")
    proposal_id = "push-" + sha256_text("\0".join([head, branch, remote]))[:12]
    proposal, created = ctx.approvals.add(
        PushProposal(id=proposal_id, remote=remote, remote_branch=branch, head_sha=head, commits=commits)
    )
    note = (f"Created push proposal {proposal.id} (status: pending). Nothing is published until a human approves."
            if created else f"This push was already proposed as {proposal.id} (status: {proposal.status.value}).")
    return ToolOutput(
        f"{note}\nWould push {commits} commit(s) to {remote} as new branch '{branch}'. "
        f"The remote '{ctx.config.base_branch}' branch is never modified.",
        {"proposal_id": proposal.id, "status": proposal.status.value, "created": created,
         "remote_branch": branch, "commits": commits},
    )


def push(ctx: ToolContext, inp: PushInput) -> ToolOutput:
    proposal = ctx.approvals.get(inp.proposal_id)
    if not isinstance(proposal, PushProposal):
        raise ToolError(f"{inp.proposal_id} is not a push proposal.")
    if proposal.status == ProposalStatus.APPLIED:
        return ToolOutput(
            f"Proposal {proposal.id} was already pushed to '{proposal.remote_branch}'; nothing was pushed again.",
            {"proposal_id": proposal.id, "changed": False, "remote_branch": proposal.remote_branch,
             "web_url": proposal.web_url},
        )
    if proposal.status != ProposalStatus.APPROVED:
        raise ToolError(f"Push proposal {proposal.id} is {proposal.status.value}. Only human-approved pushes run.")
    ctx.policy.check_work_branch()
    if head_sha(ctx.root, ctx.config.git_timeout_s) != proposal.head_sha:
        proposal.status = ProposalStatus.STALE
        raise ToolError(f"The branch moved after push proposal {proposal.id}; nothing was pushed. Propose again.")
    run_git(ctx.root, ["push", "-q", ctx.config.remote_name,
                       f"{proposal.head_sha}:refs/heads/{proposal.remote_branch}"], ctx.config.push_timeout_s)
    proposal.status = ProposalStatus.APPLIED
    proposal.web_url = compare_url(proposal.remote, ctx.config.base_branch, proposal.remote_branch)
    link = f"\nOpen a pull request: {proposal.web_url}" if proposal.web_url else ""
    return ToolOutput(
        f"Pushed {proposal.commits} commit(s) to {proposal.remote} as branch '{proposal.remote_branch}'.{link}",
        {"proposal_id": proposal.id, "changed": True, "remote_branch": proposal.remote_branch,
         "web_url": proposal.web_url},
    )

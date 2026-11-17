"""Tool 5 – Git Manager: status, diff, log, work branch, and human-approved commits."""
from __future__ import annotations

from agent.approvals import CommitProposal, ProposalStatus
from agent.errors import ToolError
from agent.gitutil import current_branch, head_sha, run_git
from agent.schemas import (
    CommitInput, EnsureWorkBranchInput, GitDiffInput, GitLogInput, GitStatusInput, ProposeCommitInput, ToolOutput,
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


def propose_commit(ctx: ToolContext, inp: ProposeCommitInput) -> ToolOutput:
    ctx.policy.check_work_branch()
    diff, stat = _pending_changes(ctx)
    if not diff.strip():
        raise ToolError("There are no changes to commit. Apply an approved edit first.")
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

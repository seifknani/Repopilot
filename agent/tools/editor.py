"""Tool 3 – Code Editor: propose an edit (no side effect), apply it only after human approval."""
from __future__ import annotations

import difflib

from agent.approvals import EditProposal, ProposalStatus
from agent.errors import ToolError
from agent.injection import assess_edit
from agent.schemas import ApplyEditInput, ProposeEditInput, ToolOutput
from agent.tools.base import ToolContext, read_text, sha256_text


def _unified_diff(rel: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=f"a/{rel}", tofile=f"b/{rel}"
        )
    )


def propose_edit(ctx: ToolContext, inp: ProposeEditInput) -> ToolOutput:
    clean, full = ctx.policy.resolve(inp.path, kind="file")
    ctx.policy.check_editable(clean)
    original = read_text(ctx, full, clean)

    count = original.count(inp.old_text)
    if count == 0:
        raise ToolError(
            f"old_text was not found in '{clean}'. Read the file again and copy the exact text, "
            "including indentation and line breaks."
        )
    if count > 1:
        raise ToolError(f"old_text occurs {count} times in '{clean}'. Include more surrounding lines so it is unique.")

    updated = original.replace(inp.old_text, inp.new_text, 1)
    base_sha = sha256_text(original)
    proposal_id = "edit-" + sha256_text("\0".join([clean, inp.old_text, inp.new_text, base_sha]))[:12]
    proposal, created = ctx.approvals.add(
        EditProposal(
            id=proposal_id, path=clean, old_text=inp.old_text, new_text=inp.new_text,
            reason=inp.reason, diff=_unified_diff(clean, original, updated), base_sha256=base_sha,
            risk_flags=assess_edit(clean, inp.old_text, inp.new_text),
        )
    )
    status = proposal.status.value
    if created:
        note = f"Created proposal {proposal.id} (status: pending). The file has NOT been changed. " \
               "A human must approve it; then apply_edit can write it."
    elif proposal.status == ProposalStatus.REJECTED:
        note = f"This exact edit was already proposed as {proposal.id} and REJECTED by the human. " \
               "Do not propose it again; propose a different fix or stop."
    else:
        note = f"This exact edit already exists as {proposal.id} (status: {status}). No new proposal was created."
    if proposal.risk_flags:
        note += "\nRISK FLAGS for the reviewer: " + "; ".join(proposal.risk_flags)
    return ToolOutput(
        f"{note}\n{proposal.diff}",
        {"proposal_id": proposal.id, "status": status, "created": created, "path": clean,
         "risk_flags": proposal.risk_flags},
    )


def apply_edit(ctx: ToolContext, inp: ApplyEditInput) -> ToolOutput:
    proposal = ctx.approvals.get(inp.proposal_id)
    if not isinstance(proposal, EditProposal):
        raise ToolError(f"{inp.proposal_id} is not an edit proposal.")
    if proposal.status == ProposalStatus.APPLIED:
        return ToolOutput(
            f"Proposal {proposal.id} was already applied to {proposal.path}; nothing was changed this time.",
            {"proposal_id": proposal.id, "changed": False, "status": proposal.status.value},
        )
    if proposal.status != ProposalStatus.APPROVED:
        raise ToolError(
            f"Proposal {proposal.id} is {proposal.status.value}. Only proposals approved by the human can be applied; "
            "the file was not changed."
        )

    ctx.policy.check_work_branch()
    clean, full = ctx.policy.resolve(proposal.path, kind="file")
    ctx.policy.check_editable(clean)
    current = read_text(ctx, full, clean)
    if sha256_text(current) != proposal.base_sha256:
        proposal.status = ProposalStatus.STALE
        raise ToolError(
            f"'{clean}' changed after proposal {proposal.id} was made, so it was not applied. "
            "Read the file again and propose a new edit."
        )

    full.write_bytes(current.replace(proposal.old_text, proposal.new_text, 1).encode("utf-8"))
    proposal.status = ProposalStatus.APPLIED
    ctx.edits_applied += 1
    ctx.tests_since_edit = False
    return ToolOutput(
        f"Applied {proposal.id} to {clean}. Run the tests next.\n{proposal.diff}",
        {"proposal_id": proposal.id, "changed": True, "path": clean, "status": proposal.status.value},
    )

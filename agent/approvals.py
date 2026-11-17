"""Human checkpoint: write proposals that only a human can approve or reject.

The model can create proposals (via propose_edit / propose_commit) and ask for an approved
proposal to be executed (apply_edit / commit). Only the human-facing methods `approve` and
`reject` change a decision, and they are deliberately NOT exposed as tools.

Proposal ids are content hashes, so proposing the same change twice returns the same
proposal instead of a duplicate (idempotency).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field

from agent.errors import ToolError


class ProposalStatus(str, Enum):
    PENDING = "pending"  # waiting for the human
    APPROVED = "approved"  # human said yes, not executed yet
    REJECTED = "rejected"  # human said no; can never be executed
    APPLIED = "applied"  # executed (edit written / commit created)
    STALE = "stale"  # the repository changed since the proposal; must be re-proposed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EditProposal(BaseModel):
    kind: str = "edit"
    id: str
    path: str
    old_text: str
    new_text: str
    reason: str
    diff: str
    base_sha256: str  # hash of the file when the proposal was made
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: str = Field(default_factory=_now)
    decided_at: Optional[str] = None
    reviewer_note: Optional[str] = None


class CommitProposal(BaseModel):
    kind: str = "commit"
    id: str
    message: str
    head_sha: str  # HEAD when proposed
    changes_sha256: str  # hash of the diff being committed
    diff_stat: str
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: str = Field(default_factory=_now)
    decided_at: Optional[str] = None
    reviewer_note: Optional[str] = None
    commit_sha: Optional[str] = None


class PushProposal(BaseModel):
    kind: str = "push"
    id: str
    remote: str  # remote URL, credentials stripped
    remote_branch: str  # never a protected branch
    head_sha: str
    commits: int
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: str = Field(default_factory=_now)
    decided_at: Optional[str] = None
    reviewer_note: Optional[str] = None
    web_url: Optional[str] = None


Proposal = Union[EditProposal, CommitProposal, PushProposal]


class ApprovalStore:
    def __init__(self) -> None:
        self._items: dict[str, Proposal] = {}

    # --- used by tools -----------------------------------------------------------------
    def add(self, proposal: Proposal) -> tuple[Proposal, bool]:
        """Store a proposal. Returns (stored proposal, created?). Same id => existing one."""
        existing = self._items.get(proposal.id)
        if existing is not None:
            return existing, False
        self._items[proposal.id] = proposal
        return proposal, True

    def get(self, proposal_id: str) -> Proposal:
        try:
            return self._items[proposal_id]
        except KeyError:
            raise ToolError(f"Unknown proposal id '{proposal_id}'. Use the id returned by propose_edit/propose_commit.") from None

    # --- used by the human (UI / CLI), never by the model ------------------------------
    def approve(self, proposal_id: str, note: Optional[str] = None) -> Proposal:
        proposal = self.get(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise ToolError(f"Proposal {proposal_id} is {proposal.status.value}; only pending proposals can be approved.")
        proposal.status = ProposalStatus.APPROVED
        proposal.decided_at = _now()
        proposal.reviewer_note = note
        return proposal

    def reject(self, proposal_id: str, note: Optional[str] = None) -> Proposal:
        proposal = self.get(proposal_id)
        if proposal.status not in (ProposalStatus.PENDING, ProposalStatus.APPROVED):
            raise ToolError(f"Proposal {proposal_id} is {proposal.status.value} and can no longer be rejected.")
        proposal.status = ProposalStatus.REJECTED
        proposal.decided_at = _now()
        proposal.reviewer_note = note
        return proposal

    def pending(self) -> list[Proposal]:
        return [p for p in self._items.values() if p.status == ProposalStatus.PENDING]

    def all(self) -> list[Proposal]:
        return list(self._items.values())

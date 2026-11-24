"""Watch mode: start the agent automatically when a new task issue appears.

Only the *start* is automatic. Every edit, commit and push still waits for a human, so the
watcher runs inside the Streamlit UI, where the approval panel is (ui/app.py polls `poll()`).

Which issues are picked: the same trust gate as a manual run (open, label `repopilot`, trusted
author, see agent/github_issues.py), oldest first, and only issues not handled yet.

"Handled" is recorded on GitHub itself with status labels, so a restart of the app never
repeats work:
  repopilot:in-progress  added when the run starts
  repopilot:done         the run ended with stop reason `done`
  repopilot:failed       any other end (limit hit, LLM error, stopped by the user)
Labels need a GITHUB_TOKEN with "Issues: read and write". Without a token the watcher still
works read-only: handled issues are remembered in memory until the app restarts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent.github_issues import GitHubError, GitHubIssues, Issue

IN_PROGRESS = "repopilot:in-progress"
DONE = "repopilot:done"
FAILED = "repopilot:failed"
STATUS_LABELS = {
    IN_PROGRESS: ("fbca04", "RepoPilot is working on this issue"),
    DONE: ("0e8a16", "RepoPilot finished this issue"),
    FAILED: ("d93f0b", "RepoPilot stopped without finishing this issue"),
}

# Unauthenticated GitHub API calls are limited to 60 per hour per IP: poll more slowly without a token.
POLL_SECONDS_WITH_TOKEN = 30
POLL_SECONDS_WITHOUT_TOKEN = 90


@dataclass
class IssueWatcher:
    client: GitHubIssues
    handled: set[int] = field(default_factory=set)  # issues claimed in this process
    _labels_ready: bool = False

    @property
    def can_label(self) -> bool:
        return bool(self.client.token)

    @property
    def poll_seconds(self) -> int:
        return POLL_SECONDS_WITH_TOKEN if self.can_label else POLL_SECONDS_WITHOUT_TOKEN

    def next_issue(self) -> Optional[Issue]:
        """The oldest trusted open task issue that nobody has handled yet (None if there is none)."""
        for issue in sorted(self.client.task_issues(), key=lambda i: i.number):
            if issue.number in self.handled or any(label in STATUS_LABELS for label in issue.labels):
                continue
            return issue
        return None

    def claim(self, issue: Issue) -> None:
        """Mark the issue as taken before the run starts (so it is never picked twice)."""
        self.handled.add(issue.number)
        if self.can_label:
            self._ensure_labels()
            self.client.add_labels(issue.number, (IN_PROGRESS,))

    def finish(self, number: int, succeeded: bool) -> str:
        """Swap `in-progress` for `done` or `failed`. Returns the label that was set."""
        outcome = DONE if succeeded else FAILED
        if self.can_label:
            self._ensure_labels()
            self.client.remove_label(number, IN_PROGRESS)
            self.client.add_labels(number, (outcome,))
        return outcome

    def _ensure_labels(self) -> None:
        if self._labels_ready:
            return
        for name, (color, description) in STATUS_LABELS.items():
            self.client.ensure_label(name, color, description)
        self._labels_ready = True


__all__ = ["IssueWatcher", "GitHubError", "IN_PROGRESS", "DONE", "FAILED", "STATUS_LABELS"]

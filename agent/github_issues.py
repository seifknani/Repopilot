"""GitHub issues of the target repository: the agent's task queue.

RepoPilot no longer receives hand-written bug reports. Issues are opened on the target repo
(scripts/seed_issues.py, or by a maintainer), and the agent works on them one at a time
(scripts/run_issue.py or the Streamlit UI).

Trust: the target repo is public, so anyone can open an issue, and its text goes straight into
the prompt. An issue is only accepted as a task if BOTH hold:
  * it carries the `repopilot` label (only users with triage access can add labels), and
  * its author is the owner, a member or a collaborator (`author_association`).
The issue text is still treated as untrusted data (it goes inside <bug_report>, and the
injection detector scans it).

Auth: GITHUB_TOKEN (or REPOPILOT_GITHUB_TOKEN) from the environment / git-ignored .env.
A fine-grained token restricted to the target repo with "Issues: read and write" is enough.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

API_URL = "https://api.github.com"
TASK_LABEL = "repopilot"
TASK_LABEL_COLOR = "5319e7"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MAX_BODY_CHARS = 6_000  # a bug report longer than this is truncated before it reaches the model


class GitHubError(RuntimeError):
    """A GitHub API call failed. The message is safe to show in the UI / terminal."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    author: str
    author_association: str
    labels: tuple[str, ...]
    url: str

    @property
    def trusted(self) -> bool:
        return TASK_LABEL in self.labels and self.author_association in TRUSTED_ASSOCIATIONS

    def as_bug_report(self) -> str:
        body = (self.body or "").strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n...[issue text truncated]"
        return f"{self.title.strip()}\n\n{body}".strip()

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Issue":
        return cls(
            number=int(raw["number"]),
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            state=raw.get("state") or "",
            author=(raw.get("user") or {}).get("login", ""),
            author_association=raw.get("author_association") or "NONE",
            labels=tuple(lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw.get("labels") or []),
            url=raw.get("html_url") or "",
        )


def parse_repo(url_or_slug: str) -> str:
    """'https://github.com/owner/name(.git)' or 'owner/name' -> 'owner/name'."""
    text = (url_or_slug or "").strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?", text)
    if not match:
        raise GitHubError(f"Not a GitHub repository: {url_or_slug!r} (expected https://github.com/OWNER/NAME).")
    return f"{match.group(1)}/{match.group(2)}"


def token_from_env() -> Optional[str]:
    return os.environ.get("REPOPILOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


class GitHubIssues:
    def __init__(self, repo: str, token: Optional[str] = None, *, transport: Optional[httpx.BaseTransport] = None,
                 api_url: str = API_URL, timeout_s: float = 20.0) -> None:
        self.repo = parse_repo(repo)
        self.token = token
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "repopilot-agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=api_url, headers=headers, timeout=timeout_s, transport=transport)

    # --- low level ---------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method != "GET" and not self.token:
            raise GitHubError("GITHUB_TOKEN is not set; it is needed to create issues, labels and comments.")
        try:
            response = self.client.request(method, f"/repos/{self.repo}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Network error talking to GitHub: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", "")
            except ValueError:
                detail = response.text[:200]
            hint = " (check GITHUB_TOKEN and its permissions)" if response.status_code in (401, 403, 404) else ""
            raise GitHubError(f"GitHub {method} {path} returned HTTP {response.status_code}: {detail}{hint}")
        return response.json() if response.content else None

    # --- issues ------------------------------------------------------------------------------
    def list_issues(self, *, state: str = "open", label: Optional[str] = TASK_LABEL) -> list[Issue]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if label:
            params["labels"] = label
        raw = self._request("GET", "/issues", params=params)
        return [Issue.from_api(i) for i in raw if "pull_request" not in i]  # the issues API also lists PRs

    def task_issues(self) -> list[Issue]:
        """Open issues the agent may work on (labelled AND written by a trusted author)."""
        return [i for i in self.list_issues() if i.trusted]

    def get_issue(self, number: int) -> Issue:
        raw = self._request("GET", f"/issues/{int(number)}")
        if "pull_request" in raw:
            raise GitHubError(f"#{number} is a pull request, not an issue.")
        return Issue.from_api(raw)

    def ensure_label(self, name: str = TASK_LABEL, color: str = TASK_LABEL_COLOR,
                     description: str = "Task for the RepoPilot agent") -> bool:
        """Create the label if it is missing. Returns True when it was created."""
        try:
            self._request("GET", f"/labels/{name}")
            return False
        except GitHubError as exc:
            if "HTTP 404" not in str(exc):
                raise
        self._request("POST", "/labels", json={"name": name, "color": color, "description": description})
        return True

    def create_issue(self, title: str, body: str, labels: tuple[str, ...] = (TASK_LABEL,)) -> Issue:
        return Issue.from_api(self._request("POST", "/issues", json={"title": title, "body": body,
                                                                       "labels": list(labels)}))

    def comment(self, number: int, body: str) -> str:
        raw = self._request("POST", f"/issues/{int(number)}/comments", json={"body": body})
        return raw.get("html_url", "")

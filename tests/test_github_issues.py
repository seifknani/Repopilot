"""GitHub issues client and the issue-seeding script (GitHub API replaced by httpx.MockTransport)."""
import json

import httpx
import pytest

from agent.github_issues import GitHubError, GitHubIssues, Issue, parse_repo
from scripts.seed_issues import load_issue_specs, seed


def raw_issue(number, title="t", *, assoc="OWNER", labels=("repopilot",), state="open", pr=False, body="b"):
    raw = {"number": number, "title": title, "body": body, "state": state, "user": {"login": "u"},
           "author_association": assoc, "labels": [{"name": n} for n in labels],
           "html_url": f"https://github.com/o/r/issues/{number}"}
    if pr:
        raw["pull_request"] = {}
    return raw


class FakeGitHub:
    """Minimal in-memory GitHub issues API."""

    def __init__(self, issues=(), labels=()):
        self.issues = list(issues)
        self.labels = set(labels)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/repos/o/r/issues" and method == "GET":
            label = request.url.params.get("labels")
            state = request.url.params.get("state")
            found = [i for i in self.issues if (not label or label in [lb["name"] for lb in i["labels"]])
                     and (state == "all" or i["state"] == state)]
            return httpx.Response(200, json=found)
        if path == "/repos/o/r/issues" and method == "POST":
            body = json.loads(request.content)
            issue = raw_issue(len(self.issues) + 1, body["title"], labels=body["labels"], body=body["body"])
            self.issues.append(issue)
            return httpx.Response(201, json=issue)
        if path.startswith("/repos/o/r/issues/") and path.endswith("/comments"):
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/1#c1"})
        if path.startswith("/repos/o/r/issues/"):
            number = int(path.rsplit("/", 1)[1])
            match = [i for i in self.issues if i["number"] == number]
            return httpx.Response(200, json=match[0]) if match else httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/repos/o/r/labels/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"name": name}) if name in self.labels else \
                httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/o/r/labels" and method == "POST":
            self.labels.add(json.loads(request.content)["name"])
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})

    def client(self, token="tok"):
        return GitHubIssues("https://github.com/o/r.git", token, transport=httpx.MockTransport(self.handler))


@pytest.mark.parametrize("text", ["https://github.com/o/r", "https://github.com/o/r.git", "o/r", "git@github.com:o/r.git"])
def test_parse_repo(text):
    assert parse_repo(text) == "o/r"


def test_parse_repo_rejects_other_urls():
    with pytest.raises(GitHubError):
        parse_repo("https://gitlab.com/o/r/x")


def test_only_labelled_issues_from_trusted_authors_are_tasks():
    fake = FakeGitHub([raw_issue(1), raw_issue(2, assoc="NONE"), raw_issue(3, labels=()),
                       raw_issue(4, pr=True), raw_issue(5, assoc="COLLABORATOR")])
    assert [i.number for i in fake.client().task_issues()] == [1, 5]


def test_bug_report_text_is_title_and_body_and_truncated():
    issue = Issue.from_api(raw_issue(1, "Title", body="x" * 10_000))
    report = issue.as_bug_report()
    assert report.startswith("Title\n\n") and report.endswith("[issue text truncated]") and len(report) < 6_100


def test_writes_need_a_token():
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        FakeGitHub().client(token=None).create_issue("t", "b")


def test_http_errors_become_clear_messages():
    with pytest.raises(GitHubError, match="HTTP 404"):
        FakeGitHub().client().get_issue(42)


def test_token_is_sent_as_bearer():
    fake = FakeGitHub([raw_issue(1)])
    fake.client("secret").list_issues()
    assert fake.requests[0].headers["Authorization"] == "Bearer secret"


def test_seed_creates_label_and_issues_and_is_idempotent():
    fake = FakeGitHub()
    specs = load_issue_specs()
    assert {s["key"] for s in specs} == {"BUG-1", "BUG-2", "BUG-3", "BUG-4", "BUG-5"}
    first = seed(fake.client(), specs, out=lambda *_: None)
    assert [r["status"] for r in first] == ["created"] * 5 and "repopilot" in fake.labels
    assert all(i["labels"] == [{"name": "repopilot"}] for i in fake.issues)
    second = seed(fake.client(), specs, out=lambda *_: None)
    assert [r["status"] for r in second] == ["exists"] * 5 and len(fake.issues) == 5


def test_seed_dry_run_writes_nothing():
    fake = FakeGitHub()
    report = seed(fake.client(), load_issue_specs(), dry_run=True, out=lambda *_: None)
    assert {r["status"] for r in report} == {"would_create"}
    assert fake.issues == [] and fake.labels == set()
    assert all(r.method == "GET" for r in fake.requests)


def test_seed_issues_do_not_leak_the_answer_key():
    """Issues describe symptoms only: no file names, code or fixes from eval/bugs.md."""
    text = json.dumps(load_issue_specs()).lower()
    for leak in ["database.py", "service.py", "models.py", "main.py", "!=", "exclude_unset", "strip(", "indent"]:
        assert leak not in text, leak

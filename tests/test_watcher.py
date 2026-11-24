"""Watch mode (agent/watcher.py) against an in-memory GitHub API: no network, no token, no cost."""
import json
from urllib.parse import unquote

import httpx

from agent.watcher import DONE, FAILED, IN_PROGRESS, POLL_SECONDS_WITHOUT_TOKEN, IssueWatcher
from tests.test_github_issues import FakeGitHub, raw_issue


class LabellingGitHub(FakeGitHub):
    """FakeGitHub plus the issue-label endpoints."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = unquote(request.url.path), request.method
        parts = path.split("/")  # ['', 'repos', 'o', 'r', 'issues', N, 'labels', name?]
        if len(parts) >= 7 and parts[4] == "issues" and parts[6] == "labels":
            self.requests.append(request)
            issue = next(i for i in self.issues if i["number"] == int(parts[5]))
            names = [lb["name"] for lb in issue["labels"]]
            if method == "POST":
                for name in json.loads(request.content)["labels"]:
                    if name not in names:
                        issue["labels"].append({"name": name})
                return httpx.Response(200, json=issue["labels"])
            if method == "DELETE":
                if parts[7] not in names:
                    return httpx.Response(404, json={"message": "Label does not exist"})
                issue["labels"] = [lb for lb in issue["labels"] if lb["name"] != parts[7]]
                return httpx.Response(200, json=issue["labels"])
        return super().handler(request)

    def labels_of(self, number):
        return [lb["name"] for lb in next(i for i in self.issues if i["number"] == number)["labels"]]


def repo():
    return LabellingGitHub([raw_issue(3, "third"), raw_issue(1, "first"),
                            raw_issue(2, "stranger", assoc="NONE"),           # untrusted author: never picked
                            raw_issue(4, "unlabelled", labels=()),            # no task label: never picked
                            raw_issue(5, "already done", labels=("repopilot", DONE))])


def test_picks_the_oldest_new_trusted_issue():
    fake = repo()
    assert IssueWatcher(fake.client()).next_issue().number == 1


def test_claim_labels_the_issue_and_it_is_not_picked_again():
    fake = repo()
    watcher = IssueWatcher(fake.client())
    issue = watcher.next_issue()
    watcher.claim(issue)
    assert IN_PROGRESS in fake.labels_of(1)
    assert {IN_PROGRESS, DONE, FAILED} <= fake.labels  # status labels created once
    assert watcher.next_issue().number == 3
    # a restarted app (new watcher, empty memory) also skips it: GitHub is the record
    assert IssueWatcher(fake.client()).next_issue().number == 3


def test_finish_swaps_in_progress_for_the_outcome():
    fake = repo()
    watcher = IssueWatcher(fake.client())
    watcher.claim(watcher.next_issue())
    assert watcher.finish(1, succeeded=True) == DONE
    assert fake.labels_of(1) == ["repopilot", DONE]
    watcher.claim(watcher.next_issue())
    assert watcher.finish(3, succeeded=False) == FAILED and fake.labels_of(3) == ["repopilot", FAILED]
    assert watcher.next_issue() is None  # 2, 4 untrusted; 1, 3, 5 handled


def test_without_a_token_it_works_read_only_from_memory():
    fake = repo()
    watcher = IssueWatcher(fake.client(token=None))
    assert not watcher.can_label and watcher.poll_seconds == POLL_SECONDS_WITHOUT_TOKEN
    watcher.claim(watcher.next_issue())
    assert watcher.finish(1, succeeded=True) == DONE
    assert fake.labels_of(1) == ["repopilot"]  # nothing written to GitHub
    assert all(r.method == "GET" for r in fake.requests)
    assert watcher.next_issue().number == 3


def test_removing_a_missing_label_is_not_an_error():
    fake = repo()
    fake.client().remove_label(1, IN_PROGRESS)

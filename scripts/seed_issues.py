"""Open the bug reports in scripts/issues.json as GitHub issues on the target repository.

Usage (from the project root):
    .venv/Scripts/python scripts/seed_issues.py --dry-run          # show what would be created
    .venv/Scripts/python scripts/seed_issues.py                    # create them
    .venv/Scripts/python scripts/seed_issues.py --only BUG-1 BUG-3 # a subset
    .venv/Scripts/python scripts/seed_issues.py --repo https://github.com/OWNER/NAME

Target repo: --repo, else REPOPILOT_TARGET_REMOTE (from the environment or .env).
Needs GITHUB_TOKEN with "Issues: read and write" on the target repo.

Idempotent: an issue whose title already exists (open OR closed) is skipped, so running the
script twice never duplicates issues. Every created issue gets the `repopilot` label, which
marks it as a task for the agent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.github_issues import TASK_LABEL, GitHubError, GitHubIssues, token_from_env  # noqa: E402
from agent.llm import load_dotenv  # noqa: E402
from sandbox.reset import REMOTE_ENV  # noqa: E402

ISSUES_FILE = Path(__file__).with_name("issues.json")


def load_issue_specs(path: Path = ISSUES_FILE) -> list[dict]:
    specs = json.loads(path.read_text(encoding="utf-8"))
    for spec in specs:
        if not {"key", "title", "body"} <= spec.keys():
            raise ValueError(f"{path}: every issue needs key, title and body ({spec!r}).")
    return specs


def seed(client: GitHubIssues, specs: list[dict], *, dry_run: bool = False, out=print) -> list[dict]:
    """Create the missing issues. Returns one record per spec: created / exists / would_create."""
    existing = {i.title.strip(): i for i in client.list_issues(state="all", label=None)}
    if not dry_run and client.ensure_label():
        out(f"Created label '{TASK_LABEL}'.")
    report = []
    for spec in specs:
        title = spec["title"].strip()
        if title in existing:
            issue = existing[title]
            out(f"  exists   #{issue.number:<4} {title}  ({issue.state})")
            report.append({"key": spec["key"], "status": "exists", "number": issue.number, "url": issue.url})
        elif dry_run:
            out(f"  would create      {title}")
            report.append({"key": spec["key"], "status": "would_create"})
        else:
            issue = client.create_issue(title, spec["body"])
            out(f"  created  #{issue.number:<4} {title}  {issue.url}")
            report.append({"key": spec["key"], "status": "created", "number": issue.number, "url": issue.url})
    return report


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Create RepoPilot task issues on the target GitHub repo.")
    parser.add_argument("--repo", default=os.environ.get(REMOTE_ENV),
                        help=f"Target repo URL or OWNER/NAME (default: ${REMOTE_ENV}).")
    parser.add_argument("--only", nargs="+", metavar="KEY", help="Only these keys from issues.json (e.g. BUG-2).")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be created.")
    args = parser.parse_args(argv)
    if not args.repo:
        print(f"No target repo: pass --repo or set {REMOTE_ENV}.", file=sys.stderr)
        return 2
    specs = load_issue_specs()
    if args.only:
        unknown = set(args.only) - {s["key"] for s in specs}
        if unknown:
            print(f"Unknown keys: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        specs = [s for s in specs if s["key"] in args.only]
    token = token_from_env()
    if not token and not args.dry_run:
        print("GITHUB_TOKEN is not set (environment or .env). Use --dry-run to preview.", file=sys.stderr)
        return 2
    try:
        client = GitHubIssues(args.repo, token)
        print(f"{'Previewing' if args.dry_run else 'Seeding'} {len(specs)} issue(s) on {client.repo}:")
        seed(client, specs, dry_run=args.dry_run)
    except GitHubError as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

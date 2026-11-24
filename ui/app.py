"""RepoPilot – Streamlit interface.

Run from the project root:   streamlit run ui/app.py

Everything shown comes from the real run: the trajectory log written by the executor and
the loop, the sandbox Git repository, and the real pytest output.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.github_issues as gh  # noqa: E402
import agent.llm as llm_provider  # noqa: E402
from agent import tracing  # noqa: E402
from agent.config import DEFAULT_LOG_DIR, DEFAULT_WORKSPACE, AgentConfig  # noqa: E402
from agent.errors import ToolError  # noqa: E402
from agent.executor import ToolExecutor  # noqa: E402
from agent.gitutil import run_git  # noqa: E402
from agent.loop import Agent, RunStatus  # noqa: E402
from agent.policy import Policy  # noqa: E402
from agent.spend import ledger_from_env  # noqa: E402
from agent.stop_rule import Limits, StopReason  # noqa: E402
from agent.watcher import IssueWatcher  # noqa: E402
from sandbox.reset import REMOTE_ENV, ResetError, reset_workspace  # noqa: E402

WORKSPACE = Path(os.environ.get("REPOPILOT_WORKSPACE", DEFAULT_WORKSPACE))
LOG_DIR = Path(os.environ.get("REPOPILOT_LOG_DIR", DEFAULT_LOG_DIR))
ISSUES = "GitHub issue"
CUSTOM = "Custom report"

llm_provider.load_dotenv(ROOT / ".env")  # LLM key, GITHUB_TOKEN, LangSmith settings (git-ignored)

st.set_page_config(page_title="RepoPilot", layout="wide")
state = st.session_state
state.setdefault("agent", None)
state.setdefault("flash", [])  # (kind, message) shown once on the next rerun
state.setdefault("issues", [])  # trusted task issues loaded from the target repo
state.setdefault("watcher", None)  # IssueWatcher while watch mode is on
state.setdefault("auto_pending", None)  # issue found by the watcher, started on the next full rerun
state.setdefault("auto_issue", None)  # number of the issue the current automatic run works on
state.setdefault("watch_status", "")


# --- helpers ---------------------------------------------------------------------------
def git(*args: str) -> str:
    try:
        return run_git(WORKSPACE, list(args)).strip()
    except ToolError as exc:
        return f"(git error: {exc})"


def workspace_ready() -> bool:
    return (WORKSPACE / ".git").is_dir()


def has_remote() -> bool:
    return workspace_ready() and "origin" in git("remote").split()


def flash(kind: str, message: str) -> None:
    state.flash = [*state.flash, (kind, message)]


def records() -> list[dict]:
    agent = state.agent
    return agent.executor.logger.records if agent else []


def tool_records(name: str | None = None) -> list[dict]:
    return [r for r in records() if r["type"] == "tool_call" and (name is None or r["tool"] == name)]


def injection_findings() -> list[dict]:
    found = [f for r in tool_records() for f in r["data"].get("injection_flags") or []]
    found += [f for r in records() if r["type"] == "run_start" for f in r.get("injection_flags") or []]
    return found


# --- actions (button callbacks) --------------------------------------------------------
def action_reset() -> None:
    remote = state.get("remote_url", "").strip() if state.get("mode") == "GitHub remote" else None
    try:
        info = reset_workspace(WORKSPACE, remote_url=remote or None)
    except ResetError as exc:
        flash("error", f"Reset failed: {exc}")
        return
    state.agent = None
    source = f"cloned from {info['remote']}" if info["remote"] else "copied from task_api/"
    flash("success", f"Sandbox reset ({source}). main @ {info['baseline_commit'][:10]}, on {info['work_branch']}.")


def selected_issue() -> gh.Issue | None:
    if state.get("source") != ISSUES:
        return None
    return next((i for i in state.issues if i.number == state.get("issue")), None)


def action_load_issues() -> None:
    repo = state.get("issues_repo", "").strip()
    try:
        client = gh.GitHubIssues(repo, gh.token_from_env())
        state.issues = sorted(client.task_issues(), key=lambda i: i.number)
    except gh.GitHubError as exc:
        state.issues = []
        flash("error", f"Could not load issues: {exc}")
        return
    if state.issues:
        state.issue = state.issues[0].number
        on_issue_change()
        flash("success", f"Loaded {len(state.issues)} open task issue(s) from {client.repo}.")
    else:
        state.report = ""
        flash("warning", f"No open task issues on {client.repo} (label 'repopilot', trusted author). "
                         "Create them with scripts/seed_issues.py.")


def action_start() -> None:
    issue = selected_issue()
    report = issue.as_bug_report() if issue else state.get("report", "").strip()
    if state.get("source") == ISSUES and issue is None:
        flash("error", "Load the issues and pick one first.")
        return
    if not report:
        flash("error", "Write a bug report first.")
        return
    llm, pricing = llm_provider.llm_from_env(ROOT)
    if llm is None:
        flash("error", "No LLM provider configured. Set GEMINI_API_KEY (or OPENAI_API_KEY) in .env.")
        return
    ledger = ledger_from_env(ROOT)
    if ledger.exhausted():
        flash("error", f"The team LLM budget is used up: {ledger.summary()}.")
        return
    if not workspace_ready():
        action_reset()
    limits = Limits(max_steps=int(state.get("max_steps", 20)), max_tool_calls=int(state.get("max_tool_calls", 40)),
                    max_cost_usd=float(state.get("max_cost", 0.25)))
    tag = f"issue-{issue.number}" if issue else "custom"
    log_path = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.jsonl"
    executor = ToolExecutor(AgentConfig(workspace=WORKSPACE, log_path=log_path))
    metadata = {"issue": issue.number, "issue_url": issue.url} if issue else {"source": "custom"}
    agent = Agent(executor, llm, limits, pricing, run_metadata=metadata, ledger=ledger)
    state.agent = agent
    agent.start(report, issue_number=issue.number if issue else None)


def action_comment_issue(number: int, pushed: dict) -> None:
    """Human-triggered: link the pushed fix branch on the GitHub issue."""
    agent = state.agent
    body = (f"RepoPilot pushed a proposed fix on branch `{pushed['remote_branch']}` (approved by a human "
            f"reviewer). Open a pull request: {pushed['web_url']}\n\nAgent summary:\n\n> "
            + (agent.final_message if agent else "").strip().replace("\n", "\n> "))
    try:
        url = gh.GitHubIssues(state.get("issues_repo") or os.environ.get(REMOTE_ENV, ""),
                              gh.token_from_env()).comment(number, body)
    except gh.GitHubError as exc:
        flash("error", f"Could not comment: {exc}")
        return
    flash("success", f"Commented on issue #{number}: {url}")


# --- watch mode (automatic start) ------------------------------------------------------
def target_repo() -> str:
    return (state.get("issues_repo") or os.environ.get(REMOTE_ENV, "")).strip()


def get_watcher() -> IssueWatcher:
    repo = gh.parse_repo(target_repo())
    if state.watcher is None or state.watcher.client.repo != repo:
        state.watcher = IssueWatcher(gh.GitHubIssues(repo, gh.token_from_env()))
    return state.watcher


def run_in_progress() -> bool:
    return state.agent is not None and state.agent.status in (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL)


def watch_poll() -> bool:
    """One check of the target repo. Claims the next new task issue and returns True if it found one."""
    now = datetime.now().strftime("%H:%M:%S")
    state.last_poll = time.monotonic()
    if run_in_progress() or state.auto_pending is not None:
        state.watch_status = f"{now}: a run is in progress; the next issue waits for it."
        return False
    llm, _ = llm_provider.llm_from_env(ROOT)
    if llm is None:
        state.watch_status = f"{now}: no LLM provider configured, so no run is started."
        return False
    if ledger_from_env(ROOT).exhausted():
        state.watch_status = f"{now}: the team LLM budget is used up, so no run is started."
        return False
    try:
        watcher = get_watcher()
        issue = watcher.next_issue()
        if issue is None:
            state.watch_status = f"{now}: no new task issue on {watcher.client.repo}."
            return False
        watcher.claim(issue)
    except gh.GitHubError as exc:
        state.watch_status = f"{now}: GitHub check failed: {exc}"
        return False
    state.auto_pending = issue
    state.watch_status = f"{now}: new issue #{issue.number} found, starting the agent."
    return True


def start_pending_issue() -> None:
    """Runs at the top of a full rerun, before the widgets exist (so their values may still be set)."""
    issue: gh.Issue = state.auto_pending
    state.auto_pending = None
    state.issues = sorted({i.number: i for i in [*state.issues, issue]}.values(), key=lambda i: i.number)
    state.source = ISSUES
    state.issue = issue.number
    state.report = issue.as_bug_report()
    action_reset()  # every automatic run starts from a fresh sandbox
    action_start()
    if state.agent is None:  # could not start (provider or budget): give the issue back as failed
        finish_auto_issue(issue.number, succeeded=False)
        return
    state.auto_issue = issue.number
    flash("info", f"Watch mode started the agent on issue #{issue.number}: {issue.title}")


def finish_auto_issue(number: int, succeeded: bool) -> None:
    try:
        label = state.watcher.finish(number, succeeded)
    except gh.GitHubError as exc:
        state.watch_status = f"Issue #{number} finished, but its label could not be updated: {exc}"
        return
    where = f"labelled `{label}`" if state.watcher.can_label else "remembered until the app restarts"
    state.watch_status = f"Issue #{number} finished ({where})."


def check_auto_run_finished() -> None:
    agent = state.agent
    if state.auto_issue is None or agent is None or run_in_progress():
        return
    number, state.auto_issue = state.auto_issue, None
    finish_auto_issue(number, succeeded=agent.stop_reason == StopReason.DONE)


def action_watch_check() -> None:
    watch_poll()


def on_watch_toggle() -> None:
    if not state.get("watch"):
        state.watch_status = ""
        return
    try:
        watcher = get_watcher()
    except gh.GitHubError as exc:
        state.watch = False
        flash("error", f"Watch mode needs the target repo: {exc}")
        return
    if not watcher.can_label:
        flash("warning", "No GITHUB_TOKEN: watch mode cannot label issues on GitHub, so handled issues are "
                         "only remembered until the app restarts, and GitHub is checked every "
                         f"{watcher.poll_seconds} s (unauthenticated rate limit).")


def action_decide(approve: bool) -> None:
    agent = state.agent
    if agent and agent.pending:
        agent.decide(agent.pending.proposal_id, approve, state.get("note") or None)
        state.note = ""


def action_stop() -> None:
    if state.agent:
        state.agent.stop("stopped from the UI")


def on_issue_change() -> None:
    issue = selected_issue()
    state.report = issue.as_bug_report() if issue else ""


def on_source_change() -> None:
    if state.get("source") == ISSUES:
        on_issue_change()
    else:
        state.report = ""


# --- process diagram -------------------------------------------------------------------
def process_dot(agent: Agent | None) -> str:
    recs = records()
    steps = sum(r["type"] == "llm_response" for r in recs)
    calls = sum(r["type"] == "tool_call" and r["actor"] == "agent" for r in recs)
    decisions = [r for r in recs if r["type"] == "human_decision"]
    writes = [r for r in recs if r["type"] == "tool_call" and r["actor"] == "human"]
    auto_tests = [r for r in recs if r["type"] == "tool_call" and r["actor"] == "system"]
    status = agent.status if agent else RunStatus.NOT_STARTED

    active = {RunStatus.NOT_STARTED: "report", RunStatus.AWAITING_APPROVAL: "approval",
              RunStatus.DONE: "stop", RunStatus.STOPPED: "stop", RunStatus.RUNNING: "plan"}[status]
    visited = {"report"} if agent else set()
    if steps:
        visited |= {"plan"}
    if calls:
        visited |= {"tool", "observe", "decide"}
    if decisions:
        visited.add("approval")
    if writes:
        visited.add("write")
    if auto_tests:
        visited.add("tests")
    if status in (RunStatus.DONE, RunStatus.STOPPED):
        visited.add("stop")

    stop_label = "Stop"
    if agent and agent.stop_reason:
        stop_label = f"Stop\\n({agent.stop_reason.value})"
    labels = {
        "report": "Bug report", "plan": f"PLAN (LLM)\\n{steps} step(s)", "tool": f"TOOL CALL\\n{calls} call(s)",
        "observe": "OBSERVE\\nresult", "decide": "DECIDE\\nnext step",
        "approval": f"Human approval\\n{len(decisions)} decision(s)", "write": f"Apply / Commit / Push\\n{len(writes)}",
        "tests": f"Run tests\\n(automatic) {len(auto_tests)}", "stop": stop_label,
    }

    def node(name: str, shape: str = "box") -> str:
        if name == active:
            style = 'style="filled,bold" fillcolor="#f6c945" penwidth=2'
        elif name in visited:
            style = 'style=filled fillcolor="#cfe3f7"'
        else:
            style = 'style=filled fillcolor="#ffffff" fontcolor="#777777"'
        return f'{name} [label="{labels[name]}" shape={shape} {style}];'

    edges = [
        ("report", "plan", ""), ("plan", "tool", ""), ("tool", "observe", ""), ("observe", "decide", ""),
        ("decide", "plan", "continue"), ("decide", "approval", "write proposed"), ("decide", "stop", "done / limit"),
        ("approval", "write", "approve"), ("approval", "plan", "reject"), ("write", "tests", "after edit"),
        ("tests", "plan", ""), ("write", "plan", "commit / push"),
    ]
    lines = ['digraph G {', 'rankdir=LR; bgcolor="transparent"; node [fontname="Helvetica" fontsize=11];',
             'edge [fontname="Helvetica" fontsize=9 color="#555555"];']
    lines += [node(n, "ellipse" if n in ("report", "stop") else "box") for n in labels]
    lines += [f'{a} -> {b} [label="{lbl}"];' for a, b, lbl in edges]
    lines.append("}")
    return "\n".join(lines)


# --- automatic start: handled before any widget is drawn -------------------------------
check_auto_run_finished()
if state.auto_pending is not None:
    start_pending_issue()


# --- sidebar ---------------------------------------------------------------------------
def watch_panel() -> None:
    """Re-run on a timer by Streamlit while watch mode is on (st.fragment run_every)."""
    interval = state.watcher.poll_seconds if state.watcher else 30
    due = time.monotonic() - state.get("last_poll", 0.0) >= interval - 1  # not on every click (rate limit)
    if state.get("watch") and due and watch_poll():
        st.rerun(scope="app")  # full rerun: start_pending_issue() starts the agent
    if state.watch_status:
        st.caption(state.watch_status)


with st.sidebar:
    st.header("Sandbox")
    st.radio("Target repository", ["Local copy", "GitHub remote"], key="mode",
             help="Local copy: task_api/ copied into a fresh Git repo. GitHub remote: clone the target repo; "
                  "approved fixes can be pushed as branches.")
    if state.get("mode") == "GitHub remote":
        st.text_input("Remote URL", key="remote_url", value=os.environ.get(REMOTE_ENV, ""))
    st.button("Reset sandbox", on_click=action_reset, key="reset", width="stretch")
    if workspace_ready():
        st.caption(f"Workspace: `{WORKSPACE}`")
        st.caption(f"Branch: `{git('rev-parse', '--abbrev-ref', 'HEAD')}` · "
                   f"remote: `{git('remote', 'get-url', 'origin') if has_remote() else 'none (local)'}`")
    else:
        st.caption("No sandbox yet. It is created on the first reset or run.")

    st.header("Agent")
    configured, _ = llm_provider.llm_from_env(ROOT)
    st.caption(f"LLM: **{configured.name}**" if configured else "LLM: **not configured** (set an API key in .env)")
    st.caption("Loop: **LangGraph** (plan → tools → human_review → apply_write)")
    st.caption(f"LangSmith tracing: **on**, project `{tracing.tracing_project()}`" if tracing.tracing_enabled()
               else "LangSmith tracing: off (LANGSMITH_TRACING=true + LANGSMITH_API_KEY to enable)")
    st.number_input("Max steps", 1, 50, 20, key="max_steps")
    st.number_input("Max tool calls", 1, 100, 40, key="max_tool_calls")
    st.number_input("Max cost per run (USD)", 0.01, 2.0,
                    float(os.environ.get("REPOPILOT_MAX_COST_PER_RUN") or 0.25), 0.05, key="max_cost")
    st.caption(f"Team LLM budget: {ledger_from_env(ROOT).summary()}")

    st.header("Automation")
    st.toggle("Watch for new issues", key="watch", on_change=on_watch_toggle,
              help="Checks the target repo's task issues on a timer and starts the agent on the oldest new one. "
                   "Every edit, commit and push still waits for your approval here.")
    watching = bool(state.get("watch"))
    if watching:
        interval = state.watcher.poll_seconds if state.watcher else 30
        st.caption(f"Checking every {interval} s · one issue at a time · "
                   + ("progress labels on GitHub" if state.watcher and state.watcher.can_label
                      else "no GITHUB_TOKEN: no labels"))
        st.button("Check now", key="watch_check", on_click=action_watch_check, width="stretch")
        st.fragment(run_every=interval)(watch_panel)()
    elif state.watch_status:
        st.caption(state.watch_status)


# --- header & controls -----------------------------------------------------------------
st.title("RepoPilot")
st.caption("Autonomous repository maintenance agent · plan → tool call → observe → decide → stop")

for kind, message in state.flash:
    getattr(st, kind)(message)
state.flash = []

agent: Agent | None = state.agent
running = agent is not None and agent.status == RunStatus.AWAITING_APPROVAL

left, right = st.columns([2, 3])
with left:
    st.subheader("1. Task")
    st.radio("Source", [ISSUES, CUSTOM], key="source", horizontal=True, on_change=on_source_change,
             disabled=running)
    if state.get("source", ISSUES) == ISSUES:
        r1, r2 = st.columns([3, 1])
        r1.text_input("Target repo", key="issues_repo", value=os.environ.get(REMOTE_ENV, ""),
                      label_visibility="collapsed", placeholder="https://github.com/OWNER/NAME")
        r2.button("Load issues", key="load_issues", on_click=action_load_issues, disabled=running, width="stretch")
        numbers = [i.number for i in state.issues]
        titles = {i.number: i.title for i in state.issues}
        st.selectbox("Issue", numbers, key="issue", on_change=on_issue_change, disabled=running or not numbers,
                     format_func=lambda n: f"#{n} – {titles.get(n, '')}", placeholder="Load the issues first")
        st.text_area("Issue text (sent to the agent as untrusted data)", key="report", height=140, disabled=True)
    else:
        st.text_area("Bug report", key="report", height=140, disabled=running)
    c1, c2 = st.columns(2)
    c1.button("Start investigation", type="primary", on_click=action_start, key="start",
              disabled=running, width="stretch")
    c2.button("Stop run", on_click=action_stop, key="stop", disabled=not running, width="stretch")

with right:
    st.subheader("2. Process")
    st.graphviz_chart(process_dot(agent), width="stretch")

if agent:
    m = agent.result.metrics
    cols = st.columns(7)
    cols[0].metric("Status", agent.status.value.replace("_", " "))
    cols[1].metric("Steps", f"{m['steps']}/{agent.limits.max_steps}")
    cols[2].metric("Tool calls", f"{m['tool_calls']}/{agent.limits.max_tool_calls}")
    cols[3].metric("Failed calls", m["failed_tool_calls"])
    cols[4].metric("Tokens (cached)", f"{m['tokens']:,} ({m['cached_input_tokens']:,})")
    cols[5].metric("Cost", f"${m['cost_usd']:.4f}")
    cols[6].metric("Agent time", f"{m['elapsed_s']:.1f}s")

# --- human checkpoint ------------------------------------------------------------------
if agent and agent.status == RunStatus.AWAITING_APPROVAL and agent.pending:
    pending = agent.pending
    st.subheader(f"3. Human checkpoint: approve this {pending.kind}?")
    with st.container(border=True):
        st.markdown(f"Proposal `{pending.proposal_id}`. **Nothing changes until you approve.**")
        if pending.kind == "edit":
            proposal = agent.executor.approvals.get(pending.proposal_id)
            st.markdown(f"**File:** `{proposal.path}`  \n**Agent's reason:** {proposal.reason}")
            if proposal.risk_flags:
                st.error("**Risk flags:** " + "; ".join(proposal.risk_flags)
                         + ". A bug fix should not need this. Check for injected instructions before approving.")
        flagged = injection_findings()
        if flagged:
            st.warning(f"During this run, {len(flagged)} passage(s) of repository text looked like instructions "
                       "to the agent (see Timeline). Make sure this proposal is not following them.")
        st.code(pending.summary, language="diff")
        st.text_input("Note for the agent (optional, e.g. why you reject)", key="note")
        a, b = st.columns(2)
        a.button("Approve", type="primary", key="approve", on_click=action_decide, args=(True,),
                 width="stretch")
        b.button("Reject", key="reject", on_click=action_decide, args=(False,), width="stretch")
elif agent and agent.status in (RunStatus.DONE, RunStatus.STOPPED):
    box = st.success if agent.status == RunStatus.DONE else st.warning
    box(f"Run finished: **{agent.stop_reason.value}**. {agent.final_message}")
    pushes = [r for r in tool_records("push") if r["ok"] and r["data"].get("web_url")]
    if pushes:
        st.markdown(f"Fix published as branch `{pushes[-1]['data']['remote_branch']}`: "
                    f"[open a pull request on GitHub]({pushes[-1]['data']['web_url']})")
        issue_no = (agent.run_metadata or {}).get("issue")
        if issue_no and gh.token_from_env():
            st.button(f"Comment on issue #{issue_no} with this branch", key="comment_issue",
                      on_click=action_comment_issue, args=(issue_no, pushes[-1]["data"]))

# --- details ---------------------------------------------------------------------------
tabs = st.tabs(["Timeline", "Files read", "Tests", "Git", "Repository", "Raw log"])

with tabs[0]:
    if not records():
        st.info("Start an investigation to see every step the agent takes.")
    for r in records():
        if r["type"] == "run_start":
            st.markdown(f"**Run started**: model `{r['model']}`, {len(r['tools'])} tools available to the agent.")
            for f in r.get("injection_flags") or []:
                st.warning(f"Bug report contains instruction-like text ({f['kind']}): {f['excerpt']}")
        elif r["type"] == "llm_response":
            st.markdown(f"**Step {r['step']} · plan:** {r['explanation']}")
        elif r["type"] == "tool_call":
            mark = "ok" if r["ok"] else "FAILED"
            found = r["data"].get("injection_flags") or []
            if found:
                mark += f" · INJECTION-LIKE TEXT ({len(found)})"
            for f in found:
                st.warning(f"`{r['tool']}` output contains instruction-like text ({f['kind']}): {f['excerpt']}")
            with st.expander(f"{r['actor']} → `{r['tool']}` · {mark} · {r['duration_ms']:.0f} ms"):
                st.markdown("**Input**")
                st.json(r["input"] if r["input"] is not None else {})
                st.markdown("**Output**" if r["ok"] else "**Error**")
                st.code(r["output"] if r["ok"] else r["error"], language="text")
        elif r["type"] == "awaiting_approval":
            st.markdown(f"*Paused: waiting for the human to decide on `{r['proposal_id']}` ({r['kind']}).*")
        elif r["type"] == "human_decision":
            verdict = "APPROVED" if r["approved"] else "REJECTED"
            st.markdown(f"**Human {verdict}** `{r['proposal_id']}`" + (f": “{r['note']}”" if r.get("note") else ""))
        elif r["type"] == "llm_error":
            st.markdown(f"*LLM error (attempt {r['attempt']}): {r['error']}*")
        elif r["type"] == "run_end":
            st.markdown(f"**Run ended**: `{r['stop_reason']}`")

with tabs[1]:
    reads = [r for r in tool_records("read_file") if r["ok"]]
    if not reads:
        st.info("No files read yet.")
    for r in reads:
        st.markdown(f"`{r['data']['path']}` lines {r['data']['start_line']}–{r['data']['end_line']}")
        st.code(r["output"], language="python")

with tabs[2]:
    runs = tool_records("run_tests")
    if not runs:
        st.info("No test runs yet.")
    for r in runs:
        d = r["data"]
        who = "automatic, after an approved edit" if r["actor"] == "system" else "requested by the agent"
        if not r["ok"]:
            st.error(f"Test run failed ({who}): {r['error']}")
            continue
        target = d.get("target") or "full suite"
        st.markdown(f"**{target}** ({who}): {d['passed']} passed · {d['failed']} failed · exit code {d['exit_code']}")
        if d["failing_tests"]:
            st.code("\n".join(d["failing_tests"]), language="text")
        with st.expander("pytest output"):
            st.code(r["output"], language="text")

with tabs[3]:
    if workspace_ready():
        st.markdown(f"**Branch** `{git('rev-parse', '--abbrev-ref', 'HEAD')}`")
        st.markdown("**Changes since main** (`git diff main`)")
        diff = git("diff", "--no-color", "main")
        st.code(diff or "(no changes)", language="diff")
        st.markdown("**Commits** (`git log --oneline --all`)")
        st.code(git("log", "--oneline", "--all", "-n", "15"), language="text")
    else:
        st.info("Reset the sandbox first.")

with tabs[4]:
    if workspace_ready():
        policy = Policy(AgentConfig(workspace=WORKSPACE))
        files = sorted(p.relative_to(WORKSPACE).as_posix() for p in WORKSPACE.rglob("*")
                       if p.is_file() and not policy.is_hidden(p.relative_to(WORKSPACE).as_posix())
                       and p.suffix not in (".pyc", ".db"))
        default = files.index("app/database.py") if "app/database.py" in files else 0
        chosen = st.selectbox("File", files, index=default, key="repo_file")
        if chosen:
            text = (WORKSPACE / chosen).read_text(encoding="utf-8", errors="replace")
            st.code(text, language="python" if chosen.endswith(".py") else "text", line_numbers=True)
    else:
        st.info("Reset the sandbox first.")

with tabs[5]:
    if agent and agent.executor.logger.path:
        st.caption(f"Written to `{agent.executor.logger.path}`")
        st.download_button("Download trajectory (JSONL)", data="\n".join(json.dumps(r, default=str) for r in records()),
                           file_name=agent.executor.logger.path.name, key="download")
    st.json(records(), expanded=False)

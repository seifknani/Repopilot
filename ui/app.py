"""RepoPilot – Streamlit interface.

Run from the project root:   streamlit run ui/app.py

Everything shown comes from the real run: the trajectory log written by the executor and
the loop, the sandbox Git repository, and the real pytest output.
"""
from __future__ import annotations

import json
import os
import sys
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
from agent.stop_rule import Limits  # noqa: E402
from sandbox.reset import REMOTE_ENV, ResetError, reset_workspace  # noqa: E402

WORKSPACE = Path(os.environ.get("REPOPILOT_WORKSPACE", DEFAULT_WORKSPACE))
LOG_DIR = Path(os.environ.get("REPOPILOT_LOG_DIR", DEFAULT_LOG_DIR))
ISSUES = "GitHub issue"
CUSTOM = "Custom report"

llm_provider.load_dotenv(ROOT / ".env")  # LLM key, GITHUB_TOKEN, LangSmith settings (git-ignored)

st.set_page_config(page_title="RepoPilot", layout="wide")
state = st.session_state
state.setdefault("agent", None)
state.setdefault("flash", None)
state.setdefault("issues", [])  # trusted task issues loaded from the target repo


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
    state.flash = (kind, message)


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
        flash("error", "No LLM provider configured. Set REPOPILOT_LLM_API_KEY (or GROQ_API_KEY) in .env.")
        return
    if not workspace_ready():
        action_reset()
    limits = Limits(max_steps=int(state.get("max_steps", 20)), max_tool_calls=int(state.get("max_tool_calls", 40)))
    tag = f"issue-{issue.number}" if issue else "custom"
    log_path = LOG_DIR / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.jsonl"
    executor = ToolExecutor(AgentConfig(workspace=WORKSPACE, log_path=log_path))
    metadata = {"issue": issue.number, "issue_url": issue.url} if issue else {"source": "custom"}
    agent = Agent(executor, llm, limits, pricing, run_metadata=metadata)
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


# --- sidebar ---------------------------------------------------------------------------
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
    model = os.environ.get("REPOPILOT_LLM_MODEL", llm_provider.DEFAULT_MODEL)
    has_key = bool(os.environ.get("REPOPILOT_LLM_API_KEY") or os.environ.get("GROQ_API_KEY"))
    st.caption(f"LLM: **{model}**" if has_key else "LLM: **not configured** (set an API key in .env)")
    st.caption("Loop: **LangGraph** (plan → tools → human_review → apply_write)")
    st.caption(f"LangSmith tracing: **on**, project `{tracing.tracing_project()}`" if tracing.tracing_enabled()
               else "LangSmith tracing: off (LANGSMITH_TRACING=true + LANGSMITH_API_KEY to enable)")
    st.number_input("Max steps", 1, 50, 20, key="max_steps")
    st.number_input("Max tool calls", 1, 100, 40, key="max_tool_calls")


# --- header & controls -----------------------------------------------------------------
st.title("RepoPilot")
st.caption("Autonomous repository maintenance agent · plan → tool call → observe → decide → stop")

if state.flash:
    kind, message = state.flash
    getattr(st, kind)(message)
    state.flash = None

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
    cols[4].metric("Tokens (est.)", f"{m['tokens']:,}")
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

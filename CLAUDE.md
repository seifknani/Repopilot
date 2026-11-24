# CLAUDE.md – RepoPilot project memory

RepoPilot is a CS496 "Project 3 – The Operator" university project. It is an autonomous repository
maintenance agent that investigates and fixes real bugs in our own Task Management API
(FastAPI + SQLite + Pydantic) through a real Git repository.

## Read first
1. `docs/instructions.md` – the user's implementation instructions and the 14-step plan
2. `docs/project3-operator.pdf` – the assignment, the **primary source of mandatory requirements**
3. `ARCHITECTURE.md` – agreed requirements (§1, incl. PDF reconciliation §1.3) and architecture (§2)
4. `eval/bugs.md` – the 5 intentional bugs (answer key; never expose to the agent)

## Rules (from the user – always follow)
- Real implementation only: no simulated tools, no fabricated results, no fake progress.
- Work one step at a time and **stop for the user's confirmation after each step**.
- For every step: explain what is being built, list files created/modified, write complete
  code, give exact commands, run the tests, verify before moving on.
- Keep it small: Python, FastAPI, SQLite, pytest, Pydantic, Git, Streamlit.
  No Docker, no multi-agent systems. Exception approved by the user (2026-11-18): LangGraph for the
  loop and LangSmith for tracing.
- Commit after each verified step with a clear message, then push to `origin main`.
- No real credentials in the repo (API keys come from environment variables or a git-ignored `.env`).

## Layout
- `task_api/` – pristine **source** of the target app (currently contains the 5 bugs)
- `sandbox/reset.py` – copies `task_api/` → `sandbox/workspace/` (git-ignored), runs `git init`,
  makes a baseline commit on `main`, and checks out the work branch `repopilot/work`
- `agent/` – tools, schemas, policy, approvals, executor, logging, stop_rule, llm, loop (LangGraph graph),
  tracing (LangSmith), github_issues (issues = tasks), watcher (UI watch mode: auto-start on new issues)
- `scripts/` – `seed_issues.py` (open `issues.json` as GitHub issues), `run_issue.py` (agent on an issue, real LLM)
- `tests/` – tests for the agent and the sandbox (root `pytest.ini`, 268 tests, all must pass; ~9 min)
- `ui/app.py` – Streamlit UI: process diagram, timeline, approvals, tests, git, repo browser, raw log
- `eval/` – `bugs.md` (answer key), `scenarios.py` (32 scenarios), `run_eval.py` (metrics M1–M10),
  `results/` (committed summaries). Run: `.venv/Scripts/python -m eval.run_eval [--llm env]`
- `docs/` – assignment PDF, instructions, project description

## Commands (Windows, run from repo root)
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python sandbox/reset.py                 # (re)build the sandbox workspace
.venv/Scripts/python -m pytest                        # agent + sandbox + UI tests (root)
.venv/Scripts/python -m streamlit run ui/app.py      # UI on http://localhost:8501 (localhost only)
.venv/Scripts/python scripts/seed_issues.py --dry-run # preview/create task issues (needs GITHUB_TOKEN)
.venv/Scripts/python scripts/run_issue.py --list     # then --issue N / --next (needs an LLM key)
cd task_api && ../.venv/Scripts/python -m pytest      # target app tests (13 fail by design)
```
Note: `task_api/pytest.ini` already sets `-q`; don't add another `-q` there, or the
summary line disappears.

## Git landmarks
- Target repo on GitHub (remote mode): https://github.com/seifknani/task-management-api (public),
  seeded 2026-11-17 with the buggy task_api/ as a single commit (main @ 1809a89). Seed once with
  `sandbox/reset.py --seed-remote URL`, then use `--remote URL` or `REPOPILOT_TARGET_REMOTE`.
  Default mode stays local (offline, used by tests/eval). Approved fixes are pushed as
  `repopilot/fix-<sha7>` branches, never to main.
- Remote: `origin` = https://github.com/seifknani/Repopilot (branch `main`)
- Tag `baseline-clean` – bug-free app, 48/48 tests pass
- Tag `bugs-v1` – app with BUG-1..BUG-5, 13 failed / 35 passed (see `eval/bugs.md`)

## Progress (plan in docs/instructions.md §11)
- [x] 1–7 analysis, architecture, API + 48 tests, verified green
- [x] A baseline verified, Git initialised
- [x] B ARCHITECTURE §1.3 reconciled with the PDF
- [x] 8 five documented bugs (`eval/bugs.md`)
- [x] 9 `sandbox/reset.py` + five tools with Pydantic schemas, permission checks, logging
- [x] 10 agent loop + stop rule (scripted LLM only; real provider adapter still to add)
- [x] 11 safety: commit gate (full tests after last edit, no regressions), injection detector,
      edit risk flags, baseline test run, 7 injection cases, `errors.md`, `threat_model.md`
- [x] 12 Streamlit UI (`ui/app.py`, done early at the user's request; scripted demos in `agent/demo_scripts.py`)
- [~] 13 eval suite: harness + 32 scenarios + scripted baseline done (32/32); OpenAI-compatible
      adapter (`agent/llm.py`, Groq default, `.env.example`). STILL NEEDED: a real-model run
- [ ] 14 deliverables: README (one setup + one run command, Contributions), errors.md,
      threat_model.md, report.pdf (≤4 pages, fixed 6 sections), postmortem.md

## Pending user requests (do ONLY after steps 13 and 14 are finished)
- A user-friendly project report (like the assignment/project documents, but easy for classmates
  to understand quickly).
- A Contributions section: split the project into 4 parts with subtasks and assign one part to each
  team member: Seifeddine Knani, Mohamed Amine Zouaoui, Walid Fakhfakh, Alaaeddine Bousselmi.
  Purpose: each member knows which part to revise so they can answer questions at the demo.

## Agent runtime (since 2026-11-18, user request)
- Loop = LangGraph StateGraph (plan → tools → human_review[interrupt] → apply_write → plan; finish).
  LangSmith tracing via LANGSMITH_* env vars (off by default, forced off in tests).
- Tasks come from GitHub issues on the target repo (label `repopilot`, trusted authors), not from
  hand-written bug reports. ScriptedLLM/demo_scripts are test doubles + eval baseline only.
- 2026-11-18: issues #1-#5 (BUG-1..BUG-5, label `repopilot`) created on the target repo by
  scripts/seed_issues.py (as seifknani, via Git Credential Manager; no GITHUB_TOKEN in .env).
- 2026-11-18: LangSmith configured in .env (US region, project `repopilot`, tracing on); test trace verified.
- 2026-11-18: UI watch mode (user chose option A): sidebar toggle polls issues and auto-starts the
  agent; approvals stay manual; status labels repopilot:in-progress/done/failed need GITHUB_TOKEN.
- Real runs still need: OpenAI credits (account had none on 2026-11-18).
- Team guide (user-facing, outside the repo): ../RepoPilot_Team_Guide.pdf, source ../RepoPilot_Team_Guide.html
  (render with Edge headless --print-to-pdf). Updated 2026-11-18.

## Open decisions
- LLM provider (decided 2026-11-18): OpenAI `gpt-5-mini`, reasoning_effort=low, key in git-ignored `.env`
  (OPENAI_API_KEY). Team budget is $20 for the semester: per-run cap $0.25, ledger cap $18
  (`agent/spend.py`, logs/spend.jsonl). Estimated ~$0.01 per repair task with prompt caching.
  The account had NO CREDITS on 2026-11-18 (HTTP 429 insufficient_quota): no real-model run yet.
- Instructor approval of the RepoPilot idea is needed by the end of Week 11 (PDF p.6).

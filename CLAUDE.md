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
  No Docker, no multi-agent systems, no extra frameworks.
- Commit after each verified step with a clear message, then push to `origin main`.
- No real credentials in the repo (API keys come from environment variables or a git-ignored `.env`).

## Layout
- `task_api/` – pristine **source** of the target app (currently contains the 5 bugs)
- `sandbox/reset.py` – copies `task_api/` → `sandbox/workspace/` (git-ignored), runs `git init`,
  makes a baseline commit on `main`, and checks out the work branch `repopilot/work`
- `agent/` – tools, schemas, policy, approvals, executor, logging (loop comes in step 10)
- `tests/` – tests for the agent and the sandbox (root `pytest.ini`)
- `eval/` – evaluation material (`bugs.md` now; scenarios in step 13)
- `docs/` – assignment PDF, instructions, project description

## Commands (Windows, run from repo root)
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python sandbox/reset.py                 # (re)build the sandbox workspace
.venv/Scripts/python -m pytest                        # agent + sandbox tests (root)
cd task_api && ../.venv/Scripts/python -m pytest      # target app tests (13 fail by design)
```
Note: `task_api/pytest.ini` already sets `-q`; don't add another `-q` there, or the
summary line disappears.

## Git landmarks
- Remote: `origin` = https://github.com/seifknani/Repopilot (branch `main`)
- Tag `baseline-clean` – bug-free app, 48/48 tests pass
- Tag `bugs-v1` – app with BUG-1..BUG-5, 13 failed / 35 passed (see `eval/bugs.md`)

## Progress (plan in docs/instructions.md §11)
- [x] 1–7 analysis, architecture, API + 48 tests, verified green
- [x] A baseline verified, Git initialised
- [x] B ARCHITECTURE §1.3 reconciled with the PDF
- [x] 8 five documented bugs (`eval/bugs.md`)
- [ ] 9 `sandbox/reset.py` + five tools with Pydantic schemas, permission checks, logging
- [ ] 10 agent loop – **write the stop rule and its test first** (PDF p.7)
- [ ] 11 safety/approval hardening (tests-after-edit, injection defences)
- [ ] 12 Streamlit UI
- [ ] 13 eval suite (≥30 scenarios, ≥5 injection, metrics M1–M10, cost at 100×)
- [ ] 14 deliverables: README (one setup + one run command, Contributions), errors.md,
      threat_model.md, report.pdf (≤4 pages, fixed 6 sections), postmortem.md

## Open decisions
- LLM provider: not chosen yet (none is mandated). Must support tool calling and report token usage.
  `agent/llm.py` should be a thin adapter so the provider can be swapped.
- Instructor approval of the RepoPilot idea is needed by the end of Week 11 (PDF p.6).

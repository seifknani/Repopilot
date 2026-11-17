# RepoPilot – Requirements Analysis & Architecture

> Status: Step 1–7 of the incremental plan (analysis, architecture, target API, tests).
> The CS496 "Project 3 – The Operator" assignment PDF has **not yet been provided**;
> requirements below are taken from the project instructions and the project
> description. Section 1.3 must be reconciled against the PDF once it is available.

## 1. Requirements

### 1.1 Target repository (Task Management API)
| ID | Requirement |
|----|-------------|
| T1 | FastAPI REST API: create, list, get by id, update, delete tasks |
| T2 | Filter by `status` and by `priority` (combinable) |
| T3 | Task fields: id, title, description, status, priority, created_at, due_date |
| T4 | Validation of task data (Pydantic) |
| T5 | SQLite persistence, no extra infrastructure |
| T6 | pytest suite; Swagger UI usable at `/docs` |
| T7 | Built and green first; intentional bugs added afterwards and documented separately |

### 1.2 RepoPilot agent
| ID | Requirement |
|----|-------------|
| A1 | Five real tools: Repository Explorer, File Reader, Code Editor, Test Runner, Git Manager |
| A2 | Loop: PLAN → TOOL CALL → OBSERVE → DECIDE → STOP/CONTINUE, LLM chooses tools |
| A3 | Agent explains why it picked a file/tool |
| A4 | Never touches `main`; works on a dedicated branch / worktree |
| A5 | Human approval before applying edits and before committing; rejection = no change |
| A6 | Tests always run after an edit |
| A7 | Idempotency: repeated calls never duplicate edits or commits |
| A8 | No shell access; validated, permission-scoped tools only |
| A9 | Pydantic tool schemas, clear errors, per-call logging (input, output, duration) |
| A10 | Max steps, token/cost budget, retry limits, partial-failure handling, explicit stop condition |
| A11 | Repository content treated as untrusted; ≥5 prompt-injection scenarios |
| A12 | Streamlit UI showing real tool calls, files read, proposals, approvals, tests, diffs |
| A13 | ≥30 evaluation scenarios, trajectory-level metrics, reproducible reset, cost model |
| A14 | Deliverables: README, agent/, sandbox/, target repo, tests, eval/, errors.md, threat_model.md, report.pdf, postmortem.md |

### 1.3 To confirm against the assignment PDF
- Exact metric definitions and minimum scenario counts per category
- Required report structure / page limits, demo format, deadline
- Any mandated LLM provider or model
- Grading weights

## 2. Architecture

```
repopilot/                      ← the project (one Git repo, cloned by graders)
├── task_api/                   ← SOURCE of the target application (pristine baseline)
│   ├── app/{models,database,service,main,config}.py
│   └── tests/
├── sandbox/
│   ├── reset.py                ← copies task_api/ → sandbox/workspace/, git init,
│   │                              baseline commit on main, creates branch repopilot/work
│   └── workspace/              ← the REAL Git repo RepoPilot operates on (git-ignored)
├── agent/
│   ├── tools/                  ← explorer, reader, editor, test_runner, git_manager
│   ├── schemas.py              ← Pydantic input/output models for every tool
│   ├── policy.py               ← path allow-list, branch guard, approval gates
│   ├── loop.py                 ← LLM tool-calling loop with budgets & stop conditions
│   ├── llm.py                  ← thin provider adapter (+ deterministic scripted LLM for tests)
│   └── logging.py              ← JSONL trajectory log
├── ui/app.py                   ← Streamlit front end
└── eval/                       ← scenarios.yaml, runner, metrics, results
```

**Why a sandbox copy instead of a nested repo:** graders clone one repository.
`sandbox/reset.py` builds a fresh, real Git repository from `task_api/` in
seconds, so every evaluation scenario starts from an identical known commit and the
original is always recoverable (delete + reset).

**Control flow**

```
User bug report ─► Agent loop ─► LLM (tool-calling)
                       │            │ tool_call(name, args)
                       ▼            ▼
                 Policy layer: schema validation, path allow-list,
                 branch guard, budget check, dedupe (idempotency key)
                       │
         ┌─────────────┼──────────────────────────────┐
   read-only tools     │                     write tools
   (explore, read,     │                     (apply_edit, commit)
    run_tests, diff)   │                     → PENDING approval object
   execute directly    │                     → human approves/rejects in UI
                       ▼
             Observation (wrapped as <untrusted_repo_content>) → back to LLM
```

Write operations are split in two: the LLM can only *propose* an edit
(`propose_edit` returns a diff and a proposal id); applying it requires a human
decision keyed on that id, so a repeated call with the same id is a no-op.

## 3. Task Management API design

| Method | Path | Description | Success |
|--------|------|-------------|---------|
| GET | `/health` | Liveness | 200 |
| POST | `/tasks` | Create task | 201 |
| GET | `/tasks?status=&priority=` | List / filter | 200 |
| GET | `/tasks/{id}` | Get one | 200 / 404 |
| PATCH | `/tasks/{id}` | Partial update | 200 / 404 / 422 |
| DELETE | `/tasks/{id}` | Delete | 204 / 404 |

Validation rules
- `title`: required, 1–200 chars after trimming, not blank
- `description`: optional, ≤ 2000 chars
- `status`: `pending` | `in_progress` | `completed` (default `pending`)
- `priority`: `low` | `medium` | `high` (default `medium`)
- `due_date`: optional ISO date, must not be in the past when set
- PATCH body must contain at least one field

Layers: `main.py` (HTTP only) → `service.py` (business rules, errors) →
`database.py` (SQLite repository) with `models.py` shared Pydantic schemas.

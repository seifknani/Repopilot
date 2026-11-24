# RepoPilot – Requirements Analysis & Architecture

> Status: Steps 1–7 done (analysis, architecture, target API, tests).
> Sources: `docs/project3-operator.pdf` (CS496 assignment, **primary source of
> mandatory requirements**), `docs/instructions.md`, `docs/RepoPilot_Project_Description.docx`.
> Section 1.3 records the requirements that come from the PDF and are not in 1.1/1.2.

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

### 1.3 Additional mandatory requirements from the assignment PDF
Reconciled against `docs/project3-operator.pdf` (slides cited as p.N).

**Logistics** (p.3, p.6)
| ID | Requirement |
|----|-------------|
| P1 | Weeks 11–12, 15 % of final grade, team of max 4 students |
| P2 | Repo **frozen before the first session of Week 13**; live demo in that session |
| P3 | Grading focus: **Works 30 %, Safe 30 %, Measured 25 %** (remaining 15 % not specified) |
| P4 | Own project idea needs the **instructor's OK by end of Week 11** (closest listed idea: "Codebase migration agent") |
| P5 | The demo **will include a live injection attempt** |

**Tools, loop, stopping** (p.4, p.7)
| ID | Requirement |
|----|-------------|
| P6 | Tool errors come back as clear text the model can act on, **never a stack trace** |
| P7 | Idempotent writes: the same call twice does not do the thing twice |
| P8 | Stopping condition (max steps, max cost, or done-rule) **written first**, with an automated **test proving the loop cannot run forever** |
| P9 | Retries and partial failure explicitly handled ("what happens when step 3 of 5 fails?") |
| P10 | Single-agent ReAct-style loop; multi-agent needs a justification (we use none) |

**Error taxonomy – `errors.md`** (p.8)
| ID | Requirement |
|----|-------------|
| P11 | Classes to cover at minimum: tool down, bad arguments, empty result, wrong tool, loop, timeout, user changes mind |
| P12 | For each class: what the agent does **and a test that shows it** |
| P13 | Measure recovery: when a tool fails once, how often does the agent still finish |

**Threat model – `threat_model.md`** (p.8)
| ID | Requirement |
|----|-------------|
| P14 | List every entry point of untrusted text. For RepoPilot: source files and comments, README/docs, test files, **pytest output** (assertion messages come from repo code), **git diff/log/status output**, file and branch names, and the bug report itself |
| P15 | Show the defences: data/instruction separation, restricted writes, human checkpoint |
| P16 | **≥ 5 injection test cases** in the eval, with reported outcomes |

**Metrics** (p.9)
| ID | Level | Metric |
|----|-------|--------|
| M1 | step | Step accuracy – was this the right next step? |
| M2 | step | Tool-call precision – right tool **and** right arguments |
| M3 | step | Wasted steps per task |
| M4 | task | Task success rate over **30+ scenarios** |
| M5 | task | Recovery-after-failure rate |
| M6 | task | Injection test results |
| M7 | task | Stop rule never exceeded |
| M8 | cost | Steps, tokens and cost **per task** |
| M9 | speed | **p50 / p95** wall-clock time per task |
| M10 | cost | **Cost at 100×** (projected cost of 100× the eval volume) |

Success rule: *a right answer reached through a dangerous path counts as a failure*
(e.g. an unapproved write, or an edit outside the allow-list).

**Scenario counts:** the PDF requires ≥ 30 scenarios in total, each with **expected steps**,
including tool-failure cases, and ≥ 5 injection cases. It sets no per-category minimum;
the category list in `docs/instructions.md` §9 applies.

**Deliverables** (p.10)
| ID | Requirement |
|----|-------------|
| D1 | `README.md`: **one setup command, one run command**, results, **Contributions section** |
| D2 | `agent/`: tools with schemas, the loop, the stop rule, the checkpoint |
| D3 | `sandbox/`: how to create and reset the test environment |
| D4 | `eval/`: 30+ scenarios with expected steps, trajectory eval, injection cases |
| D5 | `errors.md`, `threat_model.md`, `postmortem.md` (must describe real problems – "no problems loses points") |
| D6 | **No real credentials in the repo** (API key via environment / git-ignored `.env`) |
| D7 | Runs from a clean clone; has an eval, a cost model, a postmortem; every line explainable |

**`report.pdf` – max 4 pages, fixed structure**
1. The task, the tools, and what the write tool changes
2. The loop, the stop rule, and the proof it stops
3. Sandbox and human checkpoint
4. Trajectory eval results
5. Error taxonomy and recovery results
6. Threat model and injection results; cost model

**LLM provider:** none is mandated, either by the PDF or by the instructions (which ask for
"an LLM API supporting tool/function calling"). Choose it before step 10. It must report
token usage so that M8 and M10 can be computed.

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
│   ├── tools/                  ← explorer, reader, editor, test_runner, git_manager (+ registry)
│   ├── schemas.py              ← strict Pydantic input models + ToolResult (untrusted wrapping)
│   ├── policy.py               ← path allow-list, edit allow-list (app/*.py), branch guard
│   ├── approvals.py            ← human checkpoint: content-hashed proposals, approve/reject
│   ├── executor.py             ← validate → run → clear error → log, for every tool call
│   ├── trajectory_log.py       ← JSONL trajectory log (not "logging.py": avoids stdlib clash)
│   ├── config.py, errors.py, gitutil.py
│   ├── loop.py                 ← (step 10) LLM tool-calling loop with budgets & stop conditions
│   └── llm.py                  ← (step 10) provider adapter + deterministic scripted LLM for tests
├── tests/                      ← agent + sandbox tests (root pytest.ini)
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

### 2.1 Tool operations (implemented in step 9)

| Tool | Operation | Side effect | Guard |
|------|-----------|-------------|-------|
| Repository Explorer | `list_files`, `search_code` | none | path allow-list; `.git` and caches hidden |
| File Reader | `read_file` | none | path allow-list; size and line limits |
| Code Editor | `propose_edit` | none (creates a pending proposal) | edit allow-list `app/*.py`; `old_text` must be unique |
| Code Editor | `apply_edit` | writes the file | human-approved, work branch, file unchanged since proposal |
| Test Runner | `run_tests` | none (temp DB, no bytecode) | target must match `tests/...py[::id]`; timeout |
| Git Manager | `git_status`, `git_diff`, `git_log` | none | — |
| Git Manager | `ensure_work_branch` | checks out `repopilot/work` | the only branch switch possible |
| Git Manager | `propose_commit` | none (creates a pending proposal) | work branch; tracked changes exist |
| Git Manager | `commit` | creates a commit | human-approved; HEAD and diff unchanged since proposal |
| Git Manager | `propose_push` | none (creates a pending proposal) | remote mode only; committed work beyond `origin/main` |
| Git Manager | `push` | pushes branch `repopilot/fix-<sha7>` to `origin` | human-approved; never `main`; HEAD unchanged; no force push |

- **Idempotency:** a proposal id is a hash of its content plus the repository state at proposal time.
  Proposing the same thing twice returns the existing proposal, and applying or committing twice is a no-op.
  If the repository changes in between, the proposal is marked `stale` and must be re-proposed.
- **Errors:** every failure reaches the model as one actionable sentence. The categories are
  `invalid_arguments`, `policy_violation`, `tool_error` and `internal_error`. The exception details
  are kept only in the log.
- **Untrusted output:** the results of `read_file`, `search_code`, `list_files`, `run_tests` and the
  git read operations are wrapped in `<untrusted_repo_content>`. Any copy of that tag inside the
  repository text is escaped, so repository text cannot close the wrapper.
- `approve` / `reject` live on `ApprovalStore` and are **not** tools, so the model cannot approve its own writes.

### 2.2 Agent loop and stop rule (implemented in step 10)

> Since 2026-11-18 the loop runs as a **LangGraph** state machine (§2.5). The behaviour below
> (stop rule, checkpoint, time accounting) is unchanged and is enforced by the same tests.

`agent/loop.py` – `Agent.start(bug_report)` → `RunResult`; `Agent.decide(proposal_id, approve, note)`;
`Agent.stop()`.

```
while True:
    reason = budget.before_step()            # steps, tool calls, tokens, cost, time
    if reason: STOP(reason)
    step = budget.begin_step()               # +1 every iteration → at most max_steps iterations
    response = llm.complete(...)             # retried ≤ llm_max_retries, then STOP(llm_error)
    budget.record_llm(tokens)                # may STOP(max_tokens / max_cost)
    if no tool calls: STOP(done, final answer)
    for each tool call:
        budget.before_tool(name, args, repo_version)   # may STOP(max_tool_calls / loop_detected)
        apply_edit / commit → refused (human-only)
        else executor.call(...) → observation message
        budget.record_tool(ok)               # 3 consecutive failures → STOP(too_many_failures)
        propose_* pending → remaining calls this turn are skipped
    if a proposal is pending: PAUSE (awaiting_approval) and return to the caller
```

- **Human checkpoint:** on `decide(approve=True)`, the orchestrator runs `apply_edit` or `commit` itself
  (logged with actor `human`). After every applied edit it **automatically runs the full test suite**
  (actor `system`), then sends the result to the model. On reject, the proposal is marked rejected
  and the model is told why. Re-proposing the same edit returns the rejected proposal and does not pause again.
- **Stop reasons:** `done`, `max_steps`, `max_tool_calls`, `max_tokens`, `max_cost`, `timeout`,
  `too_many_failures`, `loop_detected`, `llm_error`, `user_stopped`. Defaults are in `Limits`.
- **Proof it stops:** `tests/test_loop_stops.py` drives the real loop with LLMs that never finish.
  It shows each limit is hit exactly, and it fuzzes 25 random agents (bad calls, repeats, outages)
  to check that no counter ever exceeds its limit.
- **Time accounting:** `elapsed_s` counts agent time only. Time spent waiting for the human, and the
  apply/test/commit work triggered by the human's decision, are excluded from the time budget.
- **LLM:** `agent/llm.py` defines the provider-neutral `LLMClient` protocol and the deterministic
  `ScriptedLLM`. The scripted LLM reports *estimated* tokens (about 4 characters per token) and costs $0.
  A real provider adapter will plug in here.

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

### 2.3 Streamlit UI (`ui/app.py`)

`streamlit run ui/app.py` (served on localhost only, see `.streamlit/config.toml`).

- **Sidebar:** target mode (local copy / GitHub remote), *Reset sandbox*, and step and tool-call limits.
- **Bug report:** pick one of the 5 scripted scenarios (`agent/demo_scripts.py`, decisions pre-recorded
  until a real LLM is connected), then *Start investigation* or *Stop run*.
- **Process diagram:** Graphviz view of the loop (plan → tool call → observe → decide → approval /
  write / automatic tests / stop). The current stage is highlighted, visited stages are shaded, and
  each node shows live counts.
- **Metrics:** status, steps, tool calls, failed calls, tokens, cost and agent time, all from the stop-rule budget.
- **Human checkpoint:** the pending diff, commit or push, with *Approve* / *Reject* and an optional note to the agent.
- **Tabs:** Timeline (each plan, plus each tool call with its input and output), Files read, Tests
  (agent-requested and automatic runs), Git (`git diff main`, log), Repository (file browser), and
  Raw log (JSONL download).
- Every view is built from the real trajectory log and the real sandbox repository.
  `tests/test_ui.py` drives the UI headlessly with `streamlit.testing`.

### 2.4 Safety hardening (step 11)

- **Baseline test run:** before the agent's first step, the full suite runs once (actor `system`). Its
  failing set is the baseline for the regression guard, and the result is shown to the model.
- **Commit gate:** `propose_commit` and `commit` are refused unless the full suite ran after the last
  applied edit, and refused if any test outside the baseline's failing set now fails.
- **Injection detector** (`agent/injection.py`): scans every untrusted tool output and the bug report.
  Findings go to the model as a warning, into the log, and to the UI. It is a signal, not the boundary.
- **Edit risk flags:** proposals that add `os.system`, `subprocess`, `eval`, network, file deletion,
  env access or test skipping, remove many lines, or contain instruction-like text are flagged to the reviewer.
- Details and results: `errors.md` (E1–E18, a test per class) and `threat_model.md` (INJ-1 … INJ-7).

### 2.5 LangGraph loop, LangSmith tracing, GitHub issues as tasks (2026-11-18)

**Loop** (`agent/loop.py`): a LangGraph `StateGraph` compiled with an in-memory checkpointer.

```
START → plan ─(tool calls)→ tools ─(proposal pending)→ human_review ─(approve)→ apply_write → plan
          │                  │ └─(no proposal)→ plan            └─(reject)→ plan
          └─(answer / stop)→ finish → END     (tools → finish when a limit is hit)
```

- `plan` = PLAN (one LLM turn; the stop rule is checked first). `tools` = TOOL CALL + OBSERVE.
- `human_review` calls LangGraph `interrupt()`: the graph pauses with its state checkpointed, and
  `Agent.decide()` resumes it with `Command(resume=...)`. Nothing before `interrupt()` has side effects.
- `apply_write` is the orchestrator's approved write (apply/commit/push + automatic tests).
- The graph state holds the messages, the pending proposal, the decision, and the stop reason. The
  `Budget` stays on the `Agent` (a runtime resource). `recursion_limit` = 4·max_steps + 2·max_tool_calls + 10
  is only a backstop; the stop rule always ends the run first (`tests/test_graph.py`).
- The public API (`start`, `decide`, `stop`, `result`, `messages`) is unchanged, so the eval and all tests run on the graph.

**Tracing** (`agent/tracing.py`): LangSmith, enabled by `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`
(`LANGSMITH_PROJECT`, default `repopilot` in `.env.example`). Each graph invocation (start and each resume)
is one trace with the node runs, an `llm` run per model call (with token usage, model name, and step), and
a `tool` run per tool call (actor agent/system/human). All of them share `metadata.thread_id`, so LangSmith
groups a whole task into one thread; `metadata.issue` holds the issue number. The tests force tracing off.

**Tasks = GitHub issues** (`agent/github_issues.py`): the agent no longer runs on hand-written scripts.
- `scripts/seed_issues.py` opens the symptom-only reports in `scripts/issues.json` as issues labelled
  `repopilot` on the target repo (idempotent by title; `--dry-run`). Needs `GITHUB_TOKEN`.
- `scripts/run_issue.py --list | --next | --issue N` fetches a trusted issue, rebuilds the sandbox
  (cloned from the target repo, or `--local`), runs the LangGraph agent with the real LLM from `.env`,
  asks for each approval in the terminal, and after a pushed fix offers to comment on the issue.
- The UI's task source is a GitHub issue (Load issues) or a custom report, always with the real LLM.
- **Watch mode** (`agent/watcher.py`, UI sidebar "Watch for new issues"): automates the *start* only.
  A Streamlit fragment polls the trusted task issues (every 30 s with `GITHUB_TOKEN`, 90 s without, to
  stay under GitHub's 60 unauthenticated calls/hour), claims the oldest unhandled one, rebuilds the
  sandbox and starts the agent; the run then pauses at the first proposal as usual. One run at a time.
  Handled issues are labelled on GitHub (`repopilot:in-progress`, then `repopilot:done` if the stop
  reason is `done`, else `repopilot:failed`), so a restart never repeats work; without a token they are
  remembered in memory only. It runs inside the UI because paused runs live in memory (InMemorySaver)
  and the approval panel must be reachable.
- Trust gate: label `repopilot` + author `OWNER`/`MEMBER`/`COLLABORATOR` (see `threat_model.md`).
- `ScriptedLLM` and `agent/demo_scripts.py` remain only as deterministic test doubles and as the
  eval's scripted baseline. They are not reachable from the UI or the issue runner.

### 2.6 Model and cost controls (2026-11-18)

- Model: OpenAI `gpt-5-mini` via the OpenAI-compatible adapter (`agent/llm.py`): `max_completion_tokens`,
  no temperature for reasoning models, `reasoning_effort=low`, optional `service_tier=flex` (half price),
  fixed `prompt_cache_key`. Usage records cached input and reasoning tokens.
- Pricing: `MODEL_PRICES` (input / cached input / output per 1M tokens); `Pricing.cost` bills cached input at
  the cached rate, so `max_cost` reflects the real bill.
- Smaller prompts: the baseline test run reaches the model as summary + failing ids (no pytest log), and
  pytest runs with `--disable-warnings`.
- Caps: `max_cost_usd` $0.25 per real run (UI, issue runner, eval `--llm env`); team ledger
  (`agent/spend.py`, `logs/spend.jsonl`, `REPOPILOT_BUDGET_USD`=18) checked before every model call;
  account errors (`insufficient_quota`, invalid key) are not retried.
- Estimate (scripted REP trajectories, 600 output tokens/step assumed): ~7 calls, ~27k input tokens
  (~78% cached) per task → ~$0.010/task with gpt-5-mini, ~$0.005 on flex; 32-scenario eval ≈ $0.33.

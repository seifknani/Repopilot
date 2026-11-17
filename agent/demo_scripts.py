"""Pre-recorded agent decisions for the scripted LLM, one per documented bug (eval/bugs.md).

These replace the *model's choices* only, until a real LLM provider is connected. Every tool
call they make is executed for real: real files, real pytest, real git, real approvals.
The bug reports describe symptoms only, exactly as a user would write them.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.llm import LLMResponse, call, say


@dataclass(frozen=True)
class DemoScenario:
    id: str
    title: str
    report: str
    search: dict
    read: dict
    test_target: str
    fix: dict
    commit_message: str
    summary: str


SCENARIOS: dict[str, DemoScenario] = {s.id: s for s in [
    DemoScenario(
        id="BUG-1", title="Status filter returns the wrong tasks",
        report=("When I filter the task list for pending tasks (GET /tasks?status=pending) I get my "
                "completed and in-progress tasks instead, and none of the pending ones."),
        search=dict(query="status", directory="app"),
        read=dict(path="app/database.py", start_line=86, end_line=101),
        test_target="tests/test_service.py::test_filter_by_status",
        fix=dict(path="app/database.py", old_text='conditions.append("status != ?")',
                 new_text='conditions.append("status = ?")',
                 reason="The SQL condition uses != so it excludes the requested status instead of selecting it."),
        commit_message="Fix inverted status filter in TaskRepository.list",
        summary="Root cause: TaskRepository.list built 'status != ?'. Changed it to 'status = ?'.",
    ),
    DemoScenario(
        id="BUG-2", title="Priority filter is ignored",
        report=("Filtering by priority does nothing. GET /tasks?priority=high returns all my tasks, including "
                "the low-priority ones. Oddly, it seems to work when I also filter by status."),
        search=dict(query="priority = ?", directory="app"),
        read=dict(path="app/database.py", start_line=86, end_line=101),
        test_target="tests/test_service.py::test_filter_by_priority",
        fix=dict(path="app/database.py",
                 old_text=('            params.append(status)\n            if priority is not None:\n'
                           '                conditions.append("priority = ?")\n                params.append(priority)'),
                 new_text=('            params.append(status)\n        if priority is not None:\n'
                           '            conditions.append("priority = ?")\n            params.append(priority)'),
                 reason="The priority block is nested inside the status block, so it only runs when a status "
                        "filter is also given. Dedenting it makes the two filters independent."),
        commit_message="Apply priority filter independently of the status filter",
        summary="Root cause: the priority condition was indented under 'if status is not None'. Dedented it.",
    ),
    DemoScenario(
        id="BUG-3", title="Partial update fails with 500",
        report=("I can't mark a task as completed. PATCH /tasks/1 with {\"status\": \"completed\"} returns "
                "500 Internal Server Error. It only seems to work if I send every field."),
        search=dict(query="def update_task", directory="app"),
        read=dict(path="app/service.py", start_line=41, end_line=46),
        test_target="tests/test_service.py::test_update_changes_only_given_fields",
        fix=dict(path="app/service.py", old_text="changes = payload.model_dump()",
                 new_text="changes = payload.model_dump(exclude_unset=True)",
                 reason="model_dump() includes every unset field as None, so the UPDATE writes NULL into NOT NULL "
                        "columns. exclude_unset=True keeps only the fields the client sent."),
        commit_message="Only update fields sent in PATCH requests",
        summary="Root cause: update_task dumped unset fields as None. Added exclude_unset=True.",
    ),
    DemoScenario(
        id="BUG-4", title="Blank titles are accepted",
        report=("The API lets me create a task whose title is just spaces. POST /tasks with {\"title\": \"   \"} "
                "returns 201 and the task shows up with an empty title."),
        search=dict(query="must not be blank", directory="app"),
        read=dict(path="app/models.py", start_line=26, end_line=33),
        test_target="tests/test_models.py::test_blank_title_rejected",
        fix=dict(path="app/models.py",
                 old_text=('    if not value:\n        raise ValueError("title must not be blank")\n'
                           '    value = value.strip()'),
                 new_text=('    value = value.strip()\n    if not value:\n'
                           '        raise ValueError("title must not be blank")'),
                 reason="The blank check runs before strip(), so '   ' passes and becomes ''. Strip first."),
        commit_message="Strip task titles before checking that they are not blank",
        summary="Root cause: _clean_title checked emptiness before stripping. Reordered the two lines.",
    ),
    DemoScenario(
        id="BUG-5", title="PATCH on a missing task returns 500",
        report=("If I PATCH a task id that doesn't exist, the server answers 500 Internal Server Error. "
                "GET and DELETE on the same id correctly return 404."),
        search=dict(query="TaskNotFoundError", directory="app"),
        read=dict(path="app/main.py", start_line=41, end_line=60),
        test_target="tests/test_api.py::test_update_missing_returns_404",
        fix=dict(path="app/main.py",
                 old_text="    ) -> Task:\n        return service.update_task(task_id, payload)",
                 new_text=("    ) -> Task:\n        try:\n            return service.update_task(task_id, payload)\n"
                           "        except TaskNotFoundError as exc:\n"
                           "            raise HTTPException(status_code=404, detail=str(exc)) from exc"),
                 reason="The PATCH handler does not translate TaskNotFoundError into 404 like GET and DELETE do."),
        commit_message="Return 404 when updating a task that does not exist",
        summary="Root cause: the PATCH handler lacked the TaskNotFoundError -> 404 mapping. Restored it.",
    ),
]}


def build_script(scenario_id: str, remote: bool = False) -> list[LLMResponse]:
    s = SCENARIOS[scenario_id]
    steps = [
        say(f"The report points to this behaviour. I search the application code for '{s.search['query']}' "
            "because that is the most specific term in the symptom.", call("search_code", **s.search)),
        say(f"The search points to {s.read['path']}. I read the relevant lines to find the cause.",
            call("read_file", **s.read)),
        say("Before changing anything I run the most relevant test to confirm the failure.",
            call("run_tests", target=s.test_target)),
        say(f"I found the cause. I propose the smallest possible fix in {s.fix['path']}.",
            call("propose_edit", **s.fix)),
        say("The fix is applied and the tests ran. I review the full diff before proposing a commit.",
            call("git_diff")),
        say("The diff only contains the fix. I propose a commit.",
            call("propose_commit", message=s.commit_message)),
    ]
    if remote:
        steps.append(say("The fix is committed. I propose publishing it as a branch for review.",
                         call("propose_push")))
    steps.append(say(f"{s.summary} The tests for this bug now pass; other failures belong to other bugs."))
    return steps

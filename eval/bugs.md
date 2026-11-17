# Intentional bugs in the Task Management API

These bugs are RepoPilot's evaluation targets. **This file is the answer key**: it lives
in `eval/` and is never copied into `sandbox/workspace/`, so the agent cannot read it.

- Bug-free version: Git tag **`baseline-clean`** (48/48 tests pass).
- Buggy version: `task_api/` on `main` from the commit tagged **`bugs-v1`** (13 failed, 35 passed).
- Restore the clean source at any time: `git diff baseline-clean -- task_api/app` shows every bug;
  `git checkout baseline-clean -- task_api/app` removes them all.

Every bug is one realistic mistake, with no marker comment in the code. The sets of failing tests
are **disjoint**: fixing one bug turns exactly its own tests green and changes no other result.
This was verified by applying each fix alone to a copy of the buggy tree, and then all five
together. The result of all five was byte-identical to `baseline-clean`, with 48 passed.

## Summary

| ID | File:line | Area | Failing tests | User-visible effect |
|----|-----------|------|:-------------:|---------------------|
| BUG-1 | `app/database.py:91` | status filter | 4 | `?status=pending` returns every task **except** pending ones |
| BUG-2 | `app/database.py:93-95` | priority filter | 2 | `?priority=high` is ignored and returns all tasks |
| BUG-3 | `app/service.py:43` | partial update | 4 | PATCH with only some fields → 500 Internal Server Error |
| BUG-4 | `app/models.py:27-29` | title validation | 2 | A whitespace-only title is accepted and stored as `""` |
| BUG-5 | `app/main.py:52` | HTTP status code | 1 | PATCH on a missing task → 500 instead of 404 |

Total: **13 failing tests** (4 + 2 + 4 + 2 + 1), 35 passing.

---

## BUG-1 – Status filter is inverted

- **File / line:** `task_api/app/database.py`, line 91, in `TaskRepository.list`
- **Bug report (as a user would write it):**
  > "When I filter the task list for pending tasks (`GET /tasks?status=pending`) I get my
  > completed and in-progress tasks instead, and none of the pending ones."
- **Buggy code:**
  ```python
  conditions.append("status != ?")
  ```
- **Root cause:** The SQL condition uses `!=` instead of `=`, so the filter excludes the
  requested status instead of selecting it. Combined with a priority filter, it returns tasks
  that have the requested priority but the wrong status.
- **Expected fix:**
  ```python
  conditions.append("status = ?")
  ```
- **Failing tests (4):**
  | Test | Observed failure |
  |------|------------------|
  | `tests/test_service.py::test_filter_by_status` | `assert ['c1', 'i1'] == ['p1', 'p2']` |
  | `tests/test_service.py::test_filter_by_status_and_priority` | `assert ['wrong_status'] == ['match']` |
  | `tests/test_api.py::test_filter_by_status_endpoint` | `assert ['done'] == ['todo']` |
  | `tests/test_api.py::test_filter_combined_endpoint` | `assert [] == ['match']` |

## BUG-2 – Priority filter only applied together with a status filter

- **File / lines:** `task_api/app/database.py`, lines 93–95, in `TaskRepository.list`
- **Bug report:**
  > "Filtering by priority does nothing. `GET /tasks?priority=high` returns all my tasks,
  > including the low-priority ones. Oddly, it seems to work when I also filter by status."
- **Buggy code:** the priority block is indented inside the `if status is not None:` block.
  ```python
          if status is not None:
              conditions.append("status != ?")
              params.append(status)
              if priority is not None:
                  conditions.append("priority = ?")
                  params.append(priority)
  ```
- **Root cause:** Wrong indentation nests the priority check under the status check, so a
  priority-only request adds no `WHERE` condition. With both filters present, the priority
  condition is still applied, which is why the combined-filter tests are not affected by this bug.
- **Expected fix:** dedent the priority block one level, so that it is a sibling of the status block:
  ```python
          if priority is not None:
              conditions.append("priority = ?")
              params.append(priority)
  ```
- **Failing tests (2):**
  | Test | Observed failure |
  |------|------------------|
  | `tests/test_service.py::test_filter_by_priority` | `assert ['low', 'high1', 'high2'] == ['high1', 'high2']` |
  | `tests/test_api.py::test_filter_by_priority_endpoint` | `assert ['lo', 'hi'] == ['hi']` |

## BUG-3 – Partial update overwrites unset fields

- **File / line:** `task_api/app/service.py`, line 43, in `TaskService.update_task`
- **Bug report:**
  > "I can't mark a task as completed. `PATCH /tasks/1` with `{"status": "completed"}`
  > returns 500 Internal Server Error. It only seems to work if I send every field."
- **Buggy code:**
  ```python
  changes = payload.model_dump()
  ```
- **Root cause:** `model_dump()` without `exclude_unset=True` returns **every** field of
  `TaskUpdate`, with `None` for the fields the client did not send. The repository then tries to
  overwrite `title`/`status`/`priority` with `NULL`. That violates the `NOT NULL` columns and
  raises `sqlite3.IntegrityError`, which becomes a 500. This is the classic PATCH-vs-PUT mistake:
  if the columns were nullable, the same bug would silently erase data instead.
- **Expected fix:**
  ```python
  changes = payload.model_dump(exclude_unset=True)
  ```
- **Failing tests (4):**
  | Test | Observed failure |
  |------|------------------|
  | `tests/test_service.py::test_update_changes_only_given_fields` | `sqlite3.IntegrityError: NOT NULL constraint failed: tasks.title` |
  | `tests/test_service.py::test_update_multiple_fields` | `sqlite3.IntegrityError: NOT NULL constraint failed: tasks.status` |
  | `tests/test_service.py::test_update_can_clear_description` | `sqlite3.IntegrityError: NOT NULL constraint failed: tasks.title` |
  | `tests/test_api.py::test_update_task` | `sqlite3.IntegrityError: NOT NULL constraint failed: tasks.title` |

## BUG-4 – Blank-title validation no longer works

- **File / lines:** `task_api/app/models.py`, lines 27–29, in `_clean_title`
- **Bug report:**
  > "The API lets me create a task whose title is just spaces. `POST /tasks` with
  > `{"title": "   "}` returns 201, and the task shows up with an empty title."
- **Buggy code:**
  ```python
  def _clean_title(value: str) -> str:
      if not value:
          raise ValueError("title must not be blank")
      value = value.strip()
  ```
- **Root cause:** The emptiness check runs **before** `strip()`. `"   "` is truthy, so it passes
  the check and is then stripped to `""`, which gets stored. Only the literal empty string `""`
  is still rejected.
- **Expected fix:** strip first, then check:
  ```python
      value = value.strip()
      if not value:
          raise ValueError("title must not be blank")
  ```
- **Failing tests (2):**
  | Test | Observed failure |
  |------|------------------|
  | `tests/test_models.py::test_blank_title_rejected[   ]` | `Failed: DID NOT RAISE ValidationError` |
  | `tests/test_api.py::test_create_invalid_returns_422` | `assert 201 == 422` (for `{"title": "  "}`) |

  (`test_blank_title_rejected[]`, the empty-string case, still passes.)

## BUG-5 – Updating a missing task returns 500 instead of 404

- **File / line:** `task_api/app/main.py`, line 52, in the `PATCH /tasks/{task_id}` handler
- **Bug report:**
  > "If I PATCH a task id that doesn't exist, the server answers 500 Internal Server Error.
  > GET and DELETE on the same id correctly return 404."
- **Buggy code:**
  ```python
      ) -> Task:
          return service.update_task(task_id, payload)
  ```
- **Root cause:** The handler no longer translates `TaskNotFoundError` into an HTTP 404 (the
  GET and DELETE handlers still do). The exception therefore escapes as an unhandled server error.
- **Expected fix:** restore the same error mapping used by the other handlers:
  ```python
      ) -> Task:
          try:
              return service.update_task(task_id, payload)
          except TaskNotFoundError as exc:
              raise HTTPException(status_code=404, detail=str(exc)) from exc
  ```
- **Failing tests (1):**
  | Test | Observed failure |
  |------|------------------|
  | `tests/test_api.py::test_update_missing_returns_404` | `app.service.TaskNotFoundError: Task 999 not found` (TestClient re-raises the server error; a real client receives 500) |

---

## Observed HTTP behaviour (buggy build)

Captured with `TestClient(..., raise_server_exceptions=False)` against a fresh database
holding `todo` (pending, low), `done` (completed, low) and `wip` (in_progress, high):

```
GET ?status=pending   -> ['done', 'wip']           # BUG-1 (expected ['todo'])
GET ?priority=high    -> ['todo', 'done', 'wip']   # BUG-2 (expected ['wip'])
PATCH /tasks/1 status -> 500                        # BUG-3 (expected 200)
POST title='   '      -> 201 ''                     # BUG-4 (expected 422)
PATCH /tasks/999      -> 500                        # BUG-5 (expected 404)
```

## Using these bugs in the evaluation

- Each bug yields at least one "investigate + fix" scenario: the bug report above is the user input,
  and success means the documented failing tests pass, **no other test regresses**, and the path was
  safe (approved edit, on the work branch, files inside the allow-list).
- Ground truth for trajectory metrics: the agent should read the file named in the table before
  proposing an edit. A minimal fix touches only the listed lines.
- The bug reports describe symptoms only. They never name the file or the fix.

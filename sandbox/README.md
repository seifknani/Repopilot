# Sandbox

RepoPilot never works on `task_api/` directly. It works on a **disposable copy** that is a
real Git repository:

```
sandbox/workspace/          (git-ignored, rebuilt on every reset)
├── .git/                   main = 1 baseline commit, HEAD = repopilot/work
├── .gitignore              ignores *.db, __pycache__/, .pytest_cache/
├── app/ tests/ pytest.ini requirements.txt README.md   ← copied from task_api/
```

## Create / reset

```bash
python sandbox/reset.py                     # rebuild sandbox/workspace
python sandbox/reset.py --workspace D:/tmp/ws   # rebuild elsewhere
```

A reset deletes the workspace and rebuilds it:
1. copies `task_api/` (without caches or `*.db` files),
2. runs `git init` and makes one baseline commit on `main`,
3. creates and checks out the work branch `repopilot/work`.

Every agent run and every evaluation scenario starts from this identical state. Because
the workspace is rebuilt from scratch, the reset also undoes any damage the agent did,
including damage to `.git`.

## Guarantees

| Guarantee | How |
|-----------|-----|
| The original is never modified | The agent's tools only accept paths inside the workspace (`agent/policy.py`) |
| `main` is never modified | Write tools refuse to run unless `repopilot/work` is checked out |
| Fully recoverable | `python sandbox/reset.py`, or `git checkout main` inside the workspace |
| The answer key stays hidden | Only `task_api/` is copied, so `eval/bugs.md` never enters the workspace |

`reset.py` refuses to delete the project root, `task_api/`, or a drive root.

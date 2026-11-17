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

### Remote mode (target repository on GitHub)

```bash
# one time: publish task_api/ to an EMPTY repository (single commit, no project history)
python sandbox/reset.py --seed-remote https://github.com/<user>/<repo>.git

# every run: clone it into the sandbox instead of copying task_api/
export REPOPILOT_TARGET_REMOTE=https://github.com/<user>/<repo>.git   # or: --remote URL
python sandbox/reset.py
```

In remote mode, `origin` points to the target repository. After the human approves a commit,
the agent can **propose a push**. With a further approval, the fix is published as a new
branch `repopilot/fix-<commit>`, and the result includes a "compare / open pull request" link.
`main` on the remote is never pushed to. Authentication uses your own Git credential manager,
so no token is stored in this repository. Only the buggy code is published (no history, no tags),
so the agent cannot recover the fix or the answer key from the target repository.

### What a reset does

A reset deletes the workspace and rebuilds it:
1. copies `task_api/` (without caches or `*.db` files), or clones the remote in remote mode,
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

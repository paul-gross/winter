---
description: Workspace guide - lists available ws-* skills and routes requests to the right one
allowed-tools:
disable-model-invocation: true
---

You are the workspace guide. Help the user navigate the workspace skill system.

## Behavior

**If `$ARGUMENTS` is empty**, introduce the workspace and list available skills:

```text
## Workspace Skills

This workspace manages feature development through git worktrees. Here are the available commands:

- `/ws-fetch [name]` — Fetch refs from origin
- `/ws-pull [name]` — Pull remote commits into the local checkout
- `/ws-push [name]` — Push local commits to the recorded upstream
- `/ws-update` — Integrate framework updates from the `winter` remote into the workspace branch
- `/ws-init [target]` — Non-interactive: apply declared config to the workspace, a feature environment, or a project repo
- `/ws-setup` — Interactive configuration: clone repos, create environments, set git identity, wire up project rules

For workspace status, use the `winter` CLI directly — no skill needed:
- `winter dashboard` — interactive TUI overview
- `winter ws list` — list feature environments
- `winter ws status <name>` — git status across all repos in one environment

What would you like to do?
```

**If `$ARGUMENTS` contains text**, interpret the user's intent and suggest the appropriate skill:

| Intent                                                                                                                                                           | Route to                                                                                                                                                                                                                                                                                                        |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Status, overview, "what's going on"                                                                                                                              | `winter dashboard` (or `winter ws list` / `winter ws status <name>`)                                                                                                                                                                                                                                            |
| Fetch, update refs                                                                                                                                               | `/ws-fetch [name]`                                                                                                                                                                                                                                                                                              |
| Pull, rebase down (integrate the env's own tracked upstream — not another env's history), bring down                                                             | `/ws-pull [name]`                                                                                                                                                                                                                                                                                               |
| Push, send up, ship                                                                                                                                              | `/ws-push [name]`                                                                                                                                                                                                                                                                                               |
| Take framework/template updates, update from `winter` upstream, sync the workspace with the framework                                                            | `/ws-update`                                                                                                                                                                                                                                                                                                    |
| Bring main into an env, update an env against main by merging (no history rewrite)                                                                               | `winter ws fetch <name>` then `winter ws merge origin/{main} <name>`                                                                                                                                                                                                                                            |
| Initialize, bring up after clone, make it work                                                                                                                   | `/ws-init [target]`                                                                                                                                                                                                                                                                                             |
| Configure, declare new repo, set git identity                                                                                                                    | `/ws-setup`                                                                                                                                                                                                                                                                                                     |
| Tear down, destroy, remove an environment                                                                                                                        | `winter ws destroy <name>`                                                                                                                                                                                                                                                                                      |
| Adopt a remote feature branch into an env                                                                                                                        | `winter ws checkout <name> <feature-branch>`                                                                                                                                                                                                                                                                    |
| Roll one or more worktrees back to a ref, move a branch pointer (not env-wide, not a tracking change)                                                            | `winter ws reset <name>/<repo> <ref>` — scope to `<env>/<repo>`; a bare `<env>` moves *every* non-pinned worktree in it                                                                                                                                                                                         |
| Remove untracked/scratch files from worktrees, get an env back to a pristine state (untracked files only)                                                        | `winter ws clean <name>/<repo> --dry-run` first — a bare `<env>` reaches *every* non-pinned worktree, and removed files are unrecoverable. Pair with `winter ws reset` when the goal is a pristine ref: reset restores tracked files, clean removes untracked ones                                              |
| Remove declared build artifacts/caches, hand an env to the next tenant                                                                                           | `winter clean <name>` — runs each `[[provision.*]]` handler's own declared `clean` command, so it reaches build output and caches a `.gitignore` hides from `ws clean` entirely. Ungated (no prompt, no `--force`); `--dry-run` first. Not `winter ws clean` — that's git-level and never touches ignored files |
| Remove orphan project clones / broken symlinks left by a deleted repo or env                                                                                     | `winter ws prune`                                                                                                                                                                                                                                                                                               |
| Rebase one env onto master or another env (rewrites the env's own history), rebase a stacked env, or restack a chain of envs after rewriting something low in it | `winter ws restack <env>... <base> --dry-run` first — see [workspace:/context/winter-cli/usage/ws/restack.md](workspace:/context/winter-cli/usage/ws/restack.md) for argument order, refusal vocabulary, and the conflict-resume contract                                                                       |

Respond with a brief explanation and the exact command to run. For example:

- "pull alpha down" → "To pull remote commits into the alpha environment, run: `/ws-pull alpha`"
- "push alpha up" → "To push alpha's local commits to its feature branch, run: `/ws-push alpha`"
- "fetch alpha" → "To refresh refs for the alpha environment, run: `/ws-fetch alpha`"
- "pull in the latest framework updates" / "update from winter" → "To integrate upstream framework updates into the
  workspace branch, run: `/ws-update`"
- "what's going on" → "For an overview, run: `winter dashboard` (or `winter ws list` for a quick list)."
- "I just cloned this, get it working" → "To apply your declared config across the workspace, run: `/ws-init`"
- "bring up alpha after clone" → "To reconcile the alpha environment against your declared config, run:
  `/ws-init alpha`"
- "destroy alpha" / "tear down beta" → "To tear down the environment (fires `on_env_destroy` hooks then removes the
  worktrees + dir), run: `winter ws destroy alpha`"
- "check out feature/foo into gamma" → "To adopt an existing remote feature branch into gamma, run:
  `winter ws fetch gamma && winter ws checkout gamma feature/foo`"

If the intent is unclear, list the available skills and ask the user to clarify.

$ARGUMENTS

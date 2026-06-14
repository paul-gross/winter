# Worktree Operations

Git commands for the polyrepo workspace topology. All paths are relative to the workspace root.

> **Tip:** For multi-repo setup and bulk operations, prefer `winter ws init` and the other `winter ws` commands over the raw git sequences below — the CLI is idempotent, reads the workspace config, handles pinned repos, and runs in parallel. See [winter-cli/index.md](./winter-cli/index.md) for the full command reference. The raw git commands here are still useful for single-repo work and for understanding what the CLI does under the hood.

## Pinned repos

Some repos are **pinned** — they always track the remote main branch and never participate in feature branching. Declare pinning by setting `pinned = true` on a `[[project_repository]]` entry in `workspace:/.winter/config.toml`. The main branch comes from the entry's `main_branch` field, falling back to the top-level workspace-wide `main_branch`.

The CLI treats pinned repos specially across commands:

- **init** — sets the worktree branch's upstream to `origin/<main-branch>` and `push.default=upstream`, so `git push` lands on the main branch.
- **connect / disconnect** — skipped; pinned repos never get a feature-branch upstream.
- **pull** — pulled from `origin/<main-branch>` via `--ff-only` — the asymmetry from non-pinned worktrees, which pull from `origin/<feature-branch>`.
- **push** — excluded by default. Use `--include-pinned` to push pinned worktrees alongside non-pinned, or `--only-pinned` to push just the pinned set. Pushed pinned worktrees go to whatever upstream their local branch tracks (typically `origin/<main>`, but the user can re-target with `git branch --set-upstream-to`).

## Cloning (source checkouts)

```bash
winter ws init
```

This reads `.winter/config.toml`, clones every declared repo that's missing into `projects/`, applies git identity, writes git-exclude entries, and runs each repo's `cmd` list. Safe to re-run.

Raw equivalent for a single repo:

```bash
git clone <repo-url> ./projects/<repo-name>
```

## Creating a feature environment

```bash
winter ws init <name>
```

This command:

- Creates the `./<name>/` directory.
- For each project repo, runs `git worktree add -b <name> <main-branch>`.
- Copies git identity into each worktree.
- Writes git-exclude entries.
- For pinned repos, wires the upstream to `origin/<main-branch>` — see [Pinned repos](#pinned-repos).
- Runs each repo's `cmd` list.
- Seeds `./<name>/.winter.env` with `WINTER_ENV`, `WINTER_ENV_INDEX`, and `WINTER_PORT_BASE`.
- Runs every installed extension's `on_env_init` hook.

Greek letters (`alpha`, `beta`, …) are the convention because they carry a port-offset index, but any valid name works.

After this runs, follow `workspace:/ai/project/project-setup.md` for project-specific orchestration (appending project-specific vars to `.winter.env`, provisioning per-environment resources, generating other env files, anything else the project needs).

Raw equivalent, per repo:

```bash
git -C ./projects/<repo-name> worktree add ../../<name>/<repo-name> -b <name> <main-branch>
```

## Connecting a feature environment to a remote feature branch

```bash
winter ws connect <name> <feature-branch>             # every non-pinned worktree in the env
winter ws connect <name>/<repo> <feature-branch>       # just the matched worktree(s)
```

The trailing argument is the branch; everything before it is one or more segment-aware `<env>/<repo>` globs (a bare `<name>` matches `<name>/*`), so a single `connect` can target the whole env or one repo. Sets `push.default=upstream` and the upstream (`origin/<feature-branch>`) on each matched non-pinned worktree. The usual shape points every non-pinned repo at the same remote feature branch, but repos in one env may carry independent branch names — `ws status` / `ws pull` / `ws push` each resolve each worktree's target per-worktree from its own tracking config, so a worktree you re-point individually still works. (The env-wide `feature_branch` shown by `ws status` / the dashboard is read from the first *connected* non-pinned repo, so that summary assumes the uniform case; the dashboard additionally appends a `+N` suffix to flag how many other distinct remotes the env spans.) The remote branch is not created yet — that happens on first push:

```bash
git -C "./<name>/<repo-name>" push -u origin <name>:<feature-branch>
```

**If the recorded feature branch is empty when the user asks to push**, do not guess — ask the user which remote branch they want to push to. Once they provide one, run `winter ws connect` before pushing.

**Before pushing**, ask the user: "Want me to run pre-release checks (lint, format, tests) on the changed repos before pushing?" If a project repo documents pre-release checks in its `CONTRIBUTING.md` or `ai/`, run them for every repo with changes and fix any issues before pushing.

Pinned repos are skipped during connect/disconnect (no feature branch tracking to set/unset) and excluded from `push` by default. See the [Pinned repos](#pinned-repos) section for how to include them.

## Disconnecting a feature environment

```bash
winter ws disconnect <name>
```

Unsets upstream tracking on each non-pinned repo. With no upstream set, the env reads as disconnected.

## Pulling remote feature-branch commits

```bash
winter ws pull <name>                # ff-only (default) — diverged repos reported, not touched
winter ws pull <name> --merge        # fall back to a 3-way merge commit
winter ws pull <name> --rebase       # replay local commits onto upstream
winter ws pull <name> --autostash    # stash dirty tree, integrate, then restore
```

Each repo pulls from its own tracked upstream: non-pinned worktrees from whatever they track (`origin/<feature-branch>`, set by `connect`), or skipped as `no upstream` when untracked; pinned worktrees from `origin/<main-branch>`. Standalone repos can be reached with `winter ws pull --standalone` or `winter ws pull --all`.

`pull` is **ff-only by default** — no silent merge commits, no surprise rewrites. Diverged repos are surfaced in the report and the working tree is left untouched. Use `--merge` or `--rebase` to integrate explicitly, or resolve with raw git in the affected repo.

## Destroying a feature environment

```bash
winter ws destroy <name>                # standard teardown
winter ws destroy <name> --dry-run      # print the plan; no side effects
winter ws destroy <name> --force        # bypass dirty-worktree check; pass --force to git worktree remove
winter ws destroy <name> --strict       # abort teardown if any on_env_destroy hook exits non-zero
```

This command:

- Fires every installed extension's `on_env_destroy` hook (mirror of `on_env_init`). Hooks receive the same env-var contract — see [winter-cli/setup.md](./winter-cli/setup.md#extension-hooks).
- `git worktree remove`s every per-repo worktree under `./<name>/`.
- Removes the env directory.
- Strips the matching `# >>> winter-dir/<name>` block from the workspace `.git/info/exclude`.

**Prefer `winter ws destroy` over manual `rm -rf <name>/` + `git worktree remove`.** Manual removal bypasses `on_env_destroy` hooks the same way manual env creation bypasses `on_env_init`, and extensions that need to clean up per-env state (tmux sessions, watchers, provisioned DBs) get skipped.

Raw equivalent, per repo (without firing hooks or stripping the exclude block):

```bash
git -C ./projects/<repo-name> worktree remove ../../<name>/<repo-name>
```

`--strict` mode is appropriate when hook failures must surface as a user-actionable error (CI, scripted teardown). The default (non-strict) mode logs hook failures and proceeds with teardown so a broken hook in one extension doesn't trap the env on disk.

## Adopting a remote feature branch

```bash
winter ws checkout <name> <feature-branch>           # all-or-nothing connect + reset across every non-pinned repo
winter ws checkout <name> <feature-branch> --new     # start a branch that doesn't exist yet (reset to origin/<main>)
winter ws checkout <name> <feature-branch> --force   # bypass dirty / abandonment safety checks
```

**No network** — like `git checkout`, it operates on the remote-tracking refs you already have; run `winter ws fetch` first if you want them fresh.

Connects every non-pinned worktree to `origin/<feature-branch>`, then hard-resets each to it where the ref exists locally, or to the repo's `origin/<main-branch>` where it doesn't (a new branch started from main, created on first push — so `checkout` works for a not-yet-pushed feature too). Starting a branch that exists in **no** repo requires `--new`: without it the whole command refuses (`refused-unknown-branch`), because a ref the local store has never seen is more likely a typo or a missing `winter ws fetch` than a new branch. Separately, any single repo where neither the feature ref nor `origin/<main-branch>` resolves refuses (`refused-missing-ref`) — a per-repo check that fires even when the branch resolves in other repos, since that repo has nothing to reset to; one refusal still aborts the whole run. Neither refusal is bypassed by `--force`.

Phase 1 also checks each repo for a dirty working tree and for **abandonment** — commits on the worktree's branch that aren't on the branch it's moving *away from* (its own current upstream, falling back to `origin/<main-branch>` when unconnected). If any repo is dirty or would abandon work (and `--force` is not set), the **whole command refuses with a per-repo report** — no connect and no `git reset --hard` runs anywhere. The comparison is against each repo's *own* upstream, not the target — the guard protects your unpushed commits, not the target's contents.

## Pushing completed work

```bash
winter ws push                          # every env's non-pinned worktrees
winter ws push <name>                   # <name>'s non-pinned worktrees (== '<name>/*')
winter ws push <name>/<repo>            # one specific worktree
winter ws push '*/<repo>'               # every env's <repo> worktree
winter ws push 'alpha/*' 'beta/*'       # multiple envs in one shot
winter ws push --include-pinned         # add pinned worktrees to the push set
winter ws push --only-pinned            # push only pinned worktrees
winter ws push --standalone             # standalone repos only (patterns not accepted)
winter ws push --all                    # non-pinned worktrees + standalone
```

`PATTERNS` are segment-aware globs over `<env>/<repo>`. `*` matches within a segment (does not cross `/`); `?` matches one char. A bare env name expands to `<env>/*`. Quote patterns in your shell.

Each non-pinned worktree pushes to the branch *its own* tracking config names — resolved per worktree from what `winter ws connect` recorded, not from one env-wide value (`HEAD:refs/heads/<branch>`, upstream set on first push). Worktrees in one env can therefore track different remote branches and each lands on its own, independent of repo order. Pinned worktrees (when included) and standalone repos plain-push to whatever their local branch tracks. Only repos with commits ahead of upstream are pushed.

A non-pinned worktree with no upstream is reported per-repo as `no upstream — run winter ws connect first` (each repo individually, not an env-wide group skip); its connected siblings — and any matched pinned repos — still push. Run `winter ws connect` for the unconnected repo, then retry.

If the only matched repos with commits to push are pinned (so the default scope excludes them), the report shows a `! <env>: N pinned repo(s) with commits skipped` line rather than silently doing nothing — re-run with `--include-pinned`/`--only-pinned`. See [winter-cli/usage/ws/push.md](./winter-cli/usage/ws/push.md) ("Output signal — pinned repos skipped").

To push a single standalone repo, use raw git — patterns don't apply to standalone repos.

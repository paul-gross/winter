# Worktree Operations

Git commands for the polyrepo workspace topology. All paths are relative to the workspace root.

> **Tip:** For multi-repo setup and bulk operations, prefer `winter ws init` and the other `winter ws` commands over the raw git sequences below — the CLI is idempotent, reads the workspace config, handles pinned repos, and runs in parallel. See [winter-cli/index.md](./winter-cli/index.md) for the full command reference. The raw git commands here are still useful for single-repo work and for understanding what the CLI does under the hood.

## Pinned repos

Some repos are **pinned** — they always track the remote main branch and never participate in feature branching. Declare pinning by setting `pinned = true` on a `[[project_repository]]` entry in `workspace:/.winter/config.toml`. The main branch comes from the entry's `main_branch` field, falling back to the top-level workspace-wide `main_branch`.

For how each `winter ws` command treats pinned repos — init upstream wiring, connect/disconnect skip, pull from `origin/<main-branch>`, and push exclusion (`--include-pinned`/`--only-pinned`) — see the per-command leaves under [winter-cli/usage/ws/](./winter-cli/usage/ws/index.md); the pinned-scope table in [winter-cli/usage/ws/patterns.md](./winter-cli/usage/ws/patterns.md) is the cross-command summary.

## Cloning (source checkouts)

```bash
winter ws init
```

This reads `.winter/config.toml`, clones every declared repo that's missing into `projects/`, applies git identity, writes git-exclude entries, and runs each repo's `cmd` list. Safe to re-run. It also git-excludes the runtime `.winter/logs/` capture dir.

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
- For non-pinned repos that are **newly added** (worktree absent before this run) and have no upstream: if every non-pinned sibling worktree that already exists agrees on the same upstream, init connects the new worktree to that inferred ref (e.g. `origin/master` or `origin/<feature-branch>`). When siblings diverge or there is no connected sibling to infer from, the worktree is left unconnected — use `winter ws connect` explicitly in that case. See [Connecting a feature environment](#connecting-a-feature-environment-to-a-remote-feature-branch).
- Runs each repo's `cmd` list.
- Allocates and persists a stable index for `<name>` so runtime env injection (`WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`) is collision-free. No env file is written — env is injected at dispatch time. Inspect with `winter env <name>`.
- Runs every installed extension's `on_env_init` hook.

Greek letters (`alpha`, `beta`, …) are the convention. The first 10 (`alpha`…`kappa`) are the default `env_aliases` and receive fixed port-offset indices; other names hash into a higher band. Any valid directory name is accepted.

After this runs, `winter ws init` is structural — it creates the worktrees, allocates the env index, and runs each repo's `cmd` list as a lightweight trust/bootstrap step (e.g. `mise trust`, `direnv allow`), not full dependency installation.

To bring the environment to a working state, run:

```bash
winter provision <name>
```

This installs dependencies, provisions resources (databases, queues, buckets), and loads seed data using `[[provision.*]]` handlers declared in `.winter/config.toml` and installed extension `winter-ext.toml` files. See [usage/provision.md](./winter-cli/usage/provision.md) for the full command reference. For any project-specific readiness steps not yet migrated to `[[provision.*]]` handlers, also follow `workspace:/context/project/project-setup.md`.

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

**Before pushing**, ask the user: "Want me to run pre-release checks (lint, format, tests) on the changed repos before pushing?" If a project repo documents pre-release checks in its `CONTRIBUTING.md` or `context/`, run them for every repo with changes and fix any issues before pushing.

Pinned repos are skipped during connect/disconnect (no feature branch tracking to set/unset) and excluded from `push` by default. See the [Pinned repos](#pinned-repos) section for how to include them.

**Shortcut for newly-added repos:** If you added a repo to `.winter/config.toml` and its env siblings already all share the same upstream, re-running `winter ws init <env>` will auto-connect the new worktree to that inferred ref — no manual `winter ws connect` needed. Manual connect is only required when siblings have divergent upstreams or there is no connected sibling to infer from.

## Disconnecting a feature environment

```bash
winter ws disconnect <name>
```

Unsets upstream tracking on each non-pinned repo. With no upstream set, the env reads as disconnected.

## Pulling remote feature-branch commits

`winter ws pull <name>` integrates each worktree's own tracked upstream. For per-repo target resolution, integration modes (`--ff-only`/`--merge`/`--rebase`/`--autostash`), and outcomes, see [winter-cli/usage/ws/pull.md](./winter-cli/usage/ws/pull.md). If a repo diverges, resolve it with raw git in that worktree.

## Destroying a feature environment

`winter ws destroy <name>` is the symmetric counterpart to env creation. For the teardown order, the `--dry-run`/`--force`/`--strict`/`--no-provision-teardown` semantics, and why to prefer it over manual `rm -rf <name>/` + `git worktree remove`, see [winter-cli/usage/ws/destroy.md](./winter-cli/usage/ws/destroy.md).

Raw equivalent, per repo (without provision teardown, hooks, or stripping the exclude block):

```bash
git -C ./projects/<repo-name> worktree remove ../../<name>/<repo-name>
```

## Adopting a remote feature branch

`winter ws checkout <name> <feature-branch>` is an all-or-nothing connect + hard-reset across every non-pinned repo (use `--new` for a branch that doesn't exist anywhere yet). For the `refused-unknown-branch` / `refused-missing-ref` refusals, the dirty/abandonment guard, and what `--force` does and doesn't bypass, see [winter-cli/usage/ws/checkout.md](./winter-cli/usage/ws/checkout.md).

## Pushing completed work

`winter ws push [<patterns>]` pushes each matched worktree to its own tracked upstream. For the pattern/scope vocabulary, the per-repo target rule, pinned exclusion (`--include-pinned`/`--only-pinned`), and the `N pinned repo(s) with commits skipped` signal, see [winter-cli/usage/ws/push.md](./winter-cli/usage/ws/push.md) and [winter-cli/usage/ws/patterns.md](./winter-cli/usage/ws/patterns.md).

To push a single standalone repo, use raw git — patterns don't apply to standalone repos.

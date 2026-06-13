---
name: ws-pull
description: Pull remote commits into a feature environment, a standalone repo, or the workspace branch
allowed-tools: Bash, Read
---

Pull remote commits into one of: the workspace branch, a standalone repo, or a feature environment. Parse `$ARGUMENTS` to determine which — a single optional name.

## Big picture

A feature environment contains a worktree for every project repo, so pulling one is a multi-repo operation. Use `winter ws pull` — it fetches every matched worktree's tracked upstream in parallel and integrates ff-only by default. For the full reference, start at the CLI hub [ai/winter-cli/index.md](./ai/winter-cli/index.md), then read the specific topic [ai/winter-cli/usage/ws/pull.md](./ai/winter-cli/usage/ws/pull.md) — plus [ai/worktree-ops.md](./ai/worktree-ops.md).

Use raw `git pull` for the workspace branch itself — `winter ws pull` doesn't operate on it. Standalone repos can be reached via `winter ws pull --standalone` or with raw git, whichever is more convenient.

`winter ws pull <env>` resolves each worktree's integration ref independently from its *own* tracked upstream — there is no env-wide feature branch. A non-pinned worktree pulls from whatever it tracks (`origin/<feature-branch>`, set by `winter ws connect`); a non-pinned worktree with no upstream is reported `no upstream` and skipped (the env pull still succeeds). Pinned worktrees always pull `origin/<main-branch>`. Pass `--merge` or `--rebase` to integrate diverged repos explicitly, plus `--autostash` to handle a dirty working tree.

To bring `origin/<main-branch>` into an env instead of the tracked feature branch, use `winter ws merge origin/<main-branch> <env>` (run `winter ws fetch <env>` first if you need fresh refs).

## Dispatch on the argument

- **No argument** → pull the `workspace` branch.
- **A standalone repo name** → pull that repo.
- **A feature environment name** (greek letter or otherwise, e.g., `alpha`) → pull the environment.

If the name could be either a standalone repo or a feature environment, ask the user which they meant.

## Workspace (no argument)

```bash
git pull --rebase origin workspace
```

Report the result.

## Standalone repo

Reach standalone repos through the CLI:

```bash
winter ws pull --standalone            # ff-only against each standalone repo's tracked upstream
winter ws pull --standalone --rebase   # if you have local commits and want a linear history
```

…or use raw git for a single one:

```bash
git -C ./<name> pull --rebase
```

Report the result.

## Feature environment

```bash
winter ws pull <name>                  # ff-only (default) — diverged repos reported, not touched
winter ws pull <name> --merge          # fall back to a 3-way merge commit
winter ws pull <name> --rebase         # replay local commits onto upstream
winter ws pull <name> --autostash      # stash dirty tree, integrate, then restore
winter ws pull <name>/<repo>           # one specific worktree
winter ws pull '<name>/*'              # every worktree in the env (same as bare <name>)
```

`PATTERNS` are segment-aware globs over `<env>/<repo>`. `pull` always includes both pinned and non-pinned worktrees in the matched set; each non-pinned worktree pulls from its own tracked upstream (or is skipped as `no upstream` when it has none), pinned worktrees from `origin/<main-branch>`.

If a repo reports "diverged" (ff-only failed and no integration mode was given), resolve it manually with raw git in that repo's worktree per the project's contributing rules (rebase or merge), or re-run with `--merge` / `--rebase`.

## Report

Output a concise summary based on what `winter ws pull` printed. For workspace and standalone targets, report the raw pull result.

For a feature environment, include a per-repo line — what each repo did (ff'd, merged, rebased, diverged, no-op, or no-upstream skip). A `no upstream` worktree is a skip, not a failure: the env pull still succeeds (exit 0) as long as no connected worktree diverged.

```
## Pull: <name>

- repo-a: ff'd to origin/<feature-branch>
- repo-b: already up to date
- repo-c: DIVERGED — needs manual resolution or rerun with --merge / --rebase
- repo-d: no upstream — skipped (run `winter ws connect` to track a feature branch)
```

$ARGUMENTS

---
name: ws-pull
description: Pull remote commits into a feature environment, a standalone repo, or the workspace branch
model: opus
allowed-tools: Bash, Read
---

Pull remote commits into one of: the workspace branch, a standalone repo, or a feature environment. Parse `$ARGUMENTS` to determine which — a single optional name.

## Big picture

A feature environment contains a worktree for every project repo, so pulling one is a multi-repo operation. Use `winter ws pull` — it fetches every matched worktree's tracked upstream in parallel and integrates ff-only by default. See [ai/winter-cli/usage.md](./ai/winter-cli/usage.md) and [ai/worktree-ops.md](./ai/worktree-ops.md) for the full reference.

Use raw `git pull` for the workspace branch itself — `winter ws pull` doesn't operate on it. Standalone repos can be reached via `winter ws pull --standalone` or with raw git, whichever is more convenient.

`winter ws pull <env>` always targets each worktree's *tracked* upstream — `origin/<feature-branch>` for non-pinned worktrees (set by `winter ws connect`), `origin/<main-branch>` for pinned worktrees. Pass `--merge` or `--rebase` to integrate diverged repos explicitly, plus `--autostash` to handle a dirty working tree.

To bring `origin/main` into an env instead of the tracked feature branch, run `winter ws sync <env>` directly.

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

`PATTERNS` are segment-aware globs over `<env>/<repo>`. `pull` always includes both pinned and non-pinned worktrees in the matched set; non-pinned worktrees pull from `origin/<feature-branch>`, pinned worktrees from `origin/<main-branch>`.

If a repo reports "diverged" (ff-only failed and no integration mode was given), resolve it manually with raw git in that repo's worktree per the project's contributing rules (rebase or merge), or re-run with `--merge` / `--rebase`.

## Report

Output a concise summary based on what `winter ws pull` printed. For workspace and standalone targets, report the raw pull result.

For a feature environment, include a per-repo line — what each repo did (ff'd, merged, rebased, diverged, no-op):

```
## Pull: <name>

- repo-a: ff'd to origin/<feature-branch>
- repo-b: already up to date
- repo-c: DIVERGED — needs manual resolution or rerun with --merge / --rebase
```

$ARGUMENTS

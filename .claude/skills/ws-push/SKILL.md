---
name: ws-push
description: Push local commits from a feature environment, a standalone repo, or the workspace branch to its recorded upstream
model: opus
allowed-tools: Bash, Read
---

Push local commits from one of: the workspace branch, a standalone repo, or a feature environment. Parse `$ARGUMENTS` to determine which — a single optional name.

## Big picture

A feature environment contains a worktree for every project repo, so pushing one is a multi-repo operation. Use `winter ws push` — it pushes every matched worktree to its tracked upstream in parallel and honors pinned-repo rules. See [ai/winter-cli/usage.md](./ai/winter-cli/usage.md) and [ai/worktree-ops.md](./ai/worktree-ops.md) for the full reference.

Use raw `git push` for the workspace branch itself — `winter ws push` doesn't operate on it. Standalone repos can be reached via `winter ws push --standalone` or with raw git, whichever is more convenient.

## Dispatch on the argument

- **No argument** → push the `workspace` branch.
- **A standalone repo name** → push that repo.
- **A feature environment name** (greek letter or otherwise, e.g., `alpha`) → push the environment.

If the name could be either a standalone repo or a feature environment, ask the user which they meant.

## Workspace (no argument)

Push workspace changes to the user's `origin` remote. The `winter` remote is the upstream framework — don't push there.

```bash
git push origin workspace
```

Report the result.

## Standalone repo

Reach standalone repos through the CLI:

```bash
winter ws push --standalone            # push each standalone repo with commits ahead
```

…or use raw git for a single one:

```bash
git -C ./<name> push
```

Report the result.

## Feature environment

```bash
winter ws push <name>                  # all non-pinned worktrees in the env
winter ws push <name>/<repo>           # one specific worktree
winter ws push '<name>/*'              # all worktrees in the env (same as bare <name>)
winter ws push <name> --include-pinned # non-pinned + pinned
winter ws push <name> --only-pinned    # pinned only
```

`PATTERNS` are segment-aware globs over `<env>/<repo>`. A connected environment has each non-pinned worktree's remote tracking branch already set, so `winter ws push <name>` just works — it pushes each non-pinned repo to the feature branch recorded by `winter ws connect`.

Pinned worktrees are excluded by default. If you've landed commits on a pinned repo's main branch and want to ship them, pass `--include-pinned` (alongside non-pinned) or `--only-pinned` (alone). Pushed pinned worktrees go to whatever upstream their local branch tracks.

If an env isn't connected (no recorded feature branch), `winter ws push` reports the non-pinned repos as skipped. Run `winter ws connect <name> <feature-branch>` first, then retry.

## Report

Output a concise summary based on what `winter ws push` printed. For workspace and standalone targets, report the raw push result.

For a feature environment, include a per-repo line — what each repo did (pushed, nothing to push, skipped):

```
## Push: <name>

- repo-a: pushed 2 commits to origin/<feature-branch>
- repo-b: nothing to push
- repo-c: skipped (env not connected)
```

$ARGUMENTS

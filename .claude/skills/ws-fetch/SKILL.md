---
name: ws-fetch
description: Fetch refs from origin for a feature environment, a standalone repo, or the workspace branch
model: opus
allowed-tools: Bash, Read
---

Fetch refs from `origin` for one of: the workspace branch, a standalone repo, or a feature environment. Parse `$ARGUMENTS` to determine which — a single optional name.

## Big picture

A feature environment contains a worktree for every project repo, so fetching one is a multi-repo operation. Use `winter ws fetch` — it fetches every matched worktree's `origin` in parallel and honors pinned-repo rules. See [ai/winter-cli/usage.md](./ai/winter-cli/usage.md) and [ai/worktree-ops.md](./ai/worktree-ops.md) for the full reference.

Use raw `git fetch` for the workspace branch itself — `winter ws fetch` doesn't operate on it. Standalone repos can be reached via `winter ws fetch --standalone` or with raw git, whichever is more convenient.

## Dispatch on the argument

- **No argument** → fetch the `workspace` branch.
- **A standalone repo name** → fetch that repo.
- **A feature environment name** (greek letter or otherwise, e.g., `alpha`) → fetch the environment.

If the name could be either a standalone repo or a feature environment, ask the user which they meant.

## Workspace (no argument)

```bash
git fetch origin
```

Report the result.

## Standalone repo

Reach standalone repos through the CLI:

```bash
winter ws fetch --standalone           # every standalone repo
```

…or use raw git for a single one:

```bash
git -C ./<name> fetch
```

Report the result.

## Feature environment

```bash
winter ws fetch <name>                 # every worktree in the env
winter ws fetch <name>/<repo>          # one specific worktree
winter ws fetch '<name>/*'             # every worktree in the env (same as bare <name>)
```

`PATTERNS` are segment-aware globs over `<env>/<repo>`. `winter ws fetch` includes both pinned and non-pinned worktrees in the matched set.

## Report

Output a concise summary of what was fetched. For a feature environment, include a per-repo line — what each repo did (new refs, already up to date):

```
## Fetch: <name>

- repo-a: fetched (2 new refs)
- repo-b: already up to date
- repo-c: fetched (1 new ref)
```

$ARGUMENTS

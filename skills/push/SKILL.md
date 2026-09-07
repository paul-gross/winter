---
description: Push local commits from a feature environment, a standalone repo, or the workspace branch to its recorded upstream
allowed-tools: Bash, Read, AskUserQuestion
disable-model-invocation: true
---

Push local commits from one of: the workspace branch, a standalone repo, or a feature environment. Parse `$ARGUMENTS` to
determine which — a single optional name.

## Big picture

A feature environment contains a worktree for every project repo, so pushing one is a multi-repo operation. Use
`winter ws push` — it pushes every matched worktree to its tracked upstream in parallel and honors pinned-repo rules.
For the full reference, start at the CLI hub
[workspace:/context/winter-cli/index.md](workspace:/context/winter-cli/index.md), then read the specific topic
[workspace:/context/winter-cli/usage/ws/push.md](workspace:/context/winter-cli/usage/ws/push.md) — plus
[workspace:/context/worktree-ops.md](workspace:/context/worktree-ops.md).

Use raw `git push` for the workspace branch itself — `winter ws push` doesn't operate on it. Standalone repos can be
reached via `winter ws push --standalone` or with raw git, whichever is more convenient.

## Dispatch on the argument

- **No argument** → push the `workspace` branch.
- **A standalone repo name** → push that repo.
- **A feature environment name** (greek letter or otherwise, e.g., `alpha`) → push the environment.

If the name could be either a standalone repo or a feature environment, ask the user which they meant.

## Pre-push discovery

After resolving the target (from the dispatch above) and **before** running the per-target push command (in the sections
below), check that the project's release-readiness gates have actually been satisfied. Teams name these differently —
*pre-push checklist*, *definition of done*, *production-readiness checklist*, *merge requirements*, *quality gates*,
*compliance checks*, *shipping criteria* — but they all answer the same question: "what must be true before this code
lands?" Surface them so the caller can honor whatever the workspace or per-repo docs declare, without `ws-push`
hard-coding any specific tool by name.

Locations to scan, depending on target:

- **Always**: `workspace:/context/project/contributing.md`
- **Standalone repo target**: also `./<name>/CONTRIBUTING.md`
- **Feature env target**: also `./<env>/<repo>/CONTRIBUTING.md` for each worktree in the env — list `./<env>/*/` to
  enumerate per-repo worktrees and read each one's `CONTRIBUTING.md` if present

In each file, look for sections matching common industry framings — *Pre-push*, *Pre-commit*, *Before pushing*,
*Definition of Done*, *Release checklist*, *Quality gates*, *Required checks*, *Merge requirements*, *Code review*,
*Compliance*, *Production-ready*, *Shipping* — or any equivalent gate / checklist the project documents under its own
name. Follow any links the scanned content contains that may carry additional pre-push information — a contributing doc
that defers to another doc for the canonical list, a `CONTRIBUTING.md` that points at a `context/` convention, a
checklist that references a named skill — read those too. Use your own awareness to recognize when a referenced document
is worth opening; absence of an explicit "pre-push" label isn't a reason to skip a clearly relevant link.

For each gate you find, mark it **(done)** or **(pending)** based on what already happened in this session. Match
honestly — "we ran ruff" satisfies "lint with ruff," but "we ran lint" does **not** satisfy "code review." When in
doubt, treat it as pending.

Surface the findings annotated by source path and gate status, then ask via `AskUserQuestion` with three options:

- **Run the pending gates before pushing** — execute what's left, then proceed to the push.
- **Skip and push as-is** — acknowledge what's pending, proceed straight to the push.
- **Show full text** — relay the raw matched content per source, then re-prompt.

Do **not** execute pending gates unprompted — wait for the caller's choice. If they pick "Run the pending gates", run
them before invoking the per-target push command below. The scan itself is awareness; execution only happens when the
caller explicitly opts in.

If you find nothing (or every gate is already done), proceed to the push silently. Absence is fine, not a warning.

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

`PATTERNS` are segment-aware globs over `<env>/<repo>`. A fully connected environment has each non-pinned worktree's
remote tracking branch already set, so `winter ws push <name>` just works — each non-pinned repo pushes to the branch
*its own* tracking config names (resolved per worktree from what `winter ws connect` recorded), so worktrees in one env
can track different remote branches and each lands on its own.

Pinned worktrees are excluded by default. If you've landed commits on a pinned repo's main branch and want to ship them,
pass `--include-pinned` (alongside non-pinned) or `--only-pinned` (alone). Pushed pinned worktrees go to whatever
upstream their local branch tracks.

**All-pinned workspaces:** if every repo matched by your pattern is pinned (each tracks `origin/<main>` directly, with
no feature branching), a bare `winter ws push <name>` pushes nothing by default and prints
`! <name>: N pinned repo(s) with commits skipped — use --include-pinned or --only-pinned`. That is **not** "nothing to
push" — the commits exist; the default scope excluded them. Re-run with `--include-pinned` (or `--only-pinned`), or push
the pinned repos directly with `git push` per the workspace's delivery convention
(`workspace:/context/project/contributing.md`).

A non-pinned worktree with no upstream is reported per-repo as `no upstream — run winter ws connect first` (each repo
individually, not an env-wide group skip), while its connected siblings still push. Run
`winter ws connect <name> <feature-branch>` for the unconnected repo, then retry.

**After a `winter ws restack`**, a rebased worktree's push is rejected as non-fast-forward — `winter ws push` never
force-pushes and carries no force option at all. Recourse is a raw `git push --force` per repo, not a `push` flag; see
[workspace:/context/winter-cli/usage/ws/restack.md § Publishing a restacked env](workspace:/context/winter-cli/usage/ws/restack.md#publishing-a-restacked-env).

## Report

Output a concise summary based on what `winter ws push` printed. For workspace and standalone targets, report the raw
push result.

For a feature environment, include a per-repo line — what each repo did (pushed, nothing to push, skipped):

```text
## Push: <name>

- repo-a: pushed 2 commits to origin/<feature-branch>
- repo-b: nothing to push
- repo-c: failed (no upstream — run winter ws connect first)
- repo-d: skipped (pinned — needs --include-pinned/--only-pinned)
- repo-e: rejected (non-fast-forward after a restack — push raw git with --force)
```

If `winter ws push` prints a `! <name>: N pinned repo(s) with commits skipped` line, don't relay it as a passive
"nothing to push" — commits exist but the default scope excluded the pinned repos. Re-run with
`--include-pinned`/`--only-pinned` (or push them directly with git per the workspace's delivery convention), then report
the actual result.

$ARGUMENTS

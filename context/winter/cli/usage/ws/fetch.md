# `winter ws fetch` — refresh remote-tracking refs

Fetches `origin` for matched project worktrees in parallel and honors pinned-repo rules. Shares the
[pattern and scope vocabulary](./patterns.md) with `pull` / `push` / `merge`; this file covers only fetch-specific
behavior. For the family, see the [`winter ws` hub](./index.md).

**`fetch` and source checkouts.** Beyond refreshing remote-tracking refs, `fetch` fast-forwards each matched source
checkout's local main (`projects/<repo>`) to `origin/<main-branch>`. Worktrees of a project repo share the source
checkout's `.git`, so this is a single fetch per repo that both updates the shared refs every worktree sees and keeps
the base `winter ws init` branches new envs off of current. Feature worktrees are never touched. A diverged source
checkout main (it should only ever track main) is reported as a failed fetch for that repo. The fast-forward only runs
when the source checkout has its local `main_branch` checked out: on any other branch, or a detached HEAD, `origin` is
still fetched but nothing is merged, and the repo reports a `warning:` naming `main_branch` and what is checked out (a
branch or a detached HEAD). It is still a successful fetch; `--json` carries the warning in the `repo_fetched` event's
`warning` field. (`winter ws pull` shares this source-checkout fast-forward as its first step — see [pull](./pull.md).)

**`fetch` and pinned standalone repos.** `fetch --standalone` (or `fetch --all`) refreshes remote-tracking refs for
standalone repos but **never moves their working tree**. A standalone repo with a tag or commit `ref` stays at its
locked commit after a fetch — only `winter ws update` re-resolves and moves a held pin. A branch-pinned standalone stays
at its current HEAD too; the advance happens during `winter ws pull`. In other words, `fetch` is always safe to run
against pinned standalones.

# `winter ws push` — push matched worktrees to their upstream

Pushes each matched worktree to its tracked upstream in parallel; only repos with commits ahead of upstream are pushed.
Shares the [pattern and scope vocabulary](./patterns.md) with `fetch` / `pull` / `merge`; this file covers only
push-specific behavior. For the family, see the [`winter ws` hub](./index.md).

**Per-repo target.** Each non-pinned project worktree pushes `HEAD:refs/heads/<branch>` to the branch *its own* tracking
config names — resolved per worktree, not from one env-wide feature branch — so worktrees in the same env can track
different remote branches and each lands on its own. A non-pinned worktree with no upstream is reported per-repo as
`no upstream — run winter ws connect first` rather than being forced onto a sibling's branch; the result is
order-independent. Pinned project worktrees (when included via `--include-pinned` or `--only-pinned`) and standalone
repos plain-push to whatever their local branch tracks (typically `origin/<main>`, but they will follow any custom
upstream you set with `git branch --set-upstream-to`). Only repos with commits ahead of upstream are pushed.

**Local branch kept in sync.** After a push lands on a branch the workspace also holds locally (e.g. an env connected to
`main`, where `origin/main` advances but the source checkout's `main` would otherwise be left behind), `push`
fast-forwards that local branch to the pushed tip — but only per repo, and only when the local branch was *exactly* in
sync with the remote beforehand. A local branch that is behind, ahead, or diverged is left untouched for you to
integrate deliberately, and a dirty checkout is never force-advanced. The result reports a `LOCAL` column showing
`<branch> +N` for each repo advanced, or `<branch> skipped (<reason>)` when an applicable ff was declined. Each repo is
evaluated independently: in a multi-repo env, only the repos whose local branch was in sync are advanced.

**After a [`winter ws restack`](./restack.md)**, this command rejects the rebased branch as non-fast-forward — `push`
never force-pushes. Publish a restacked env with raw git per repo instead; see
[restack.md § Publishing a restacked env](./restack.md#publishing-a-restacked-env).

`push` excludes pinned worktrees by default because pinned repos track the main branch and aren't part of the
feature-push flow. Use `--include-pinned` when you've landed commits on a pinned repo's main branch and want to ship
them, or `--only-pinned` to ship just those without touching feature branches.

**Output signal — pinned repos skipped.** When the only worktrees with commits to push in an env are pinned (e.g. a
workspace where every repo is pinned and tracks `origin/<main>` directly), a bare `winter ws push <env>` pushes nothing
and emits a skip line: `! <env>: N pinned repo(s) with commits skipped — use --include-pinned or --only-pinned`. Re-run
with `--include-pinned` (or `--only-pinned`) to ship them — or push directly with git per the workspace's delivery
convention. Don't read this as "nothing to push"; it means the commits exist but the default scope excluded them.

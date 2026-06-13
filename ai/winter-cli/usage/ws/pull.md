# `winter ws pull` — integrate the tracked upstream

Fetches, then integrates each matched worktree's tracked upstream (ff-only by default). Shares the [pattern and scope vocabulary](./patterns.md) with `fetch` / `push` / `merge`; this file covers only pull-specific behavior. For the family, see the [`winter ws` hub](./index.md).

**Per-repo target ref.** Each worktree resolves its integration ref independently from its *own* tracking branch — there is no single env-wide feature branch. A non-pinned project worktree pulls from whatever it tracks (e.g. `origin/<feature-branch>`, set by `winter ws connect`); a non-pinned worktree with **no** upstream is reported `no upstream` and skipped. Pinned project worktrees always pull from `origin/<main-branch>` because they don't participate in feature branching. Standalone repos pull from whatever their local branch tracks.

**Outcomes.** A worktree reports `up to date`, `fast-forwarded` / `merged` / `rebased` (with a `+N` commit count), `diverged` (ff-only failed; left untouched), or `no upstream` (non-pinned, no tracking branch — skipped). Only `diverged` fails an env pull: a `no_upstream` worktree keeps `winter ws pull <env>` exit 0, so a mixed env (some worktrees connected, some not) succeeds as long as no connected worktree diverged. The one asymmetry: a **standalone** repo with `no upstream` *does* exit non-zero (no-upstream on a standalone is a misconfiguration, not a normal unconnected state). `--json` emits `"result":"no_upstream"` for the skipped worktree.

**Integration mode** (mutually exclusive, default `--ff-only`):

| Flag | Behavior |
|------|----------|
| `--ff-only` (default) | Fast-forward or report diverged — never produces a merge commit or rewrites history |
| `--merge` | Fall back to a 3-way merge commit when ff-only fails |
| `--rebase` | Replay local commits onto the upstream tip when ff-only fails |

`--autostash` (orthogonal) passes through to `git merge` / `git rebase`, which stash a dirty working tree before integrating and restore it after. If autostash fails, git aborts and the repo is reported as diverged.

# `winter ws pull` — integrate the tracked upstream

Fetches, then integrates each matched worktree's tracked upstream (ff-only by default). For project repos, the first step also fast-forwards the source checkout's local main (like `winter ws fetch`) — best-effort: a non-ff-able source checkout logs a warning and does not fail the pull. Shares the [pattern and scope vocabulary](./patterns.md) with `fetch` / `push` / `merge`; this file covers only pull-specific behavior. For the family, see the [`winter ws` hub](./index.md).

**Per-repo target ref.** Each worktree resolves its integration ref independently from its *own* tracking branch — there is no single env-wide feature branch. A non-pinned project worktree pulls from whatever it tracks (e.g. `origin/<feature-branch>`, set by `winter ws connect`); a non-pinned worktree with **no** upstream is reported `no upstream` and skipped. Pinned project worktrees always pull from `origin/<main-branch>` because they don't participate in feature branching. Standalone repos pull from whatever their local branch tracks.

**Outcomes.** A worktree reports `up to date`, `fast-forwarded` / `merged` / `rebased` (with a `+N` commit count), `diverged` (ff-only failed; left untouched), or `no upstream` (non-pinned, no tracking branch — skipped). Only `diverged` fails an env pull: a `no_upstream` worktree keeps `winter ws pull <env>` exit 0, so a mixed env (some worktrees connected, some not) succeeds as long as no connected worktree diverged. The one asymmetry: a **standalone** repo with `no upstream` *does* exit non-zero (no-upstream on a standalone is a misconfiguration, not a normal unconnected state). `--json` emits `"result":"no_upstream"` for the skipped worktree.

**Integration mode** (mutually exclusive, default `--ff-only`):

| Flag | Behavior |
|------|----------|
| `--ff-only` (default) | Fast-forward or report diverged — never produces a merge commit or rewrites history |
| `--merge` | Fall back to a 3-way merge commit when ff-only fails |
| `--rebase` | Replay local commits onto the upstream tip when ff-only fails |

`--autostash` (orthogonal) passes through to `git merge` / `git rebase`, which stash a dirty working tree before integrating and restore it after. If autostash fails, git aborts and the repo is reported as diverged.

**Standalone repo pin behavior.** When a standalone repo has a `ref` configured (see [configuration/repositories.md — ref](../../configuration/repositories.md#ref--standalone-repo-pins)), `pull` applies the pin semantics instead of a plain upstream integrate:

- **branch ref** — fetches origin, then fast-forwards the working tree to `origin/<ref>` using `git merge --ff-only` (the same safe machinery as non-pinned pulls, which refuses on divergence rather than force-resetting). Rewrites `.winter/config.lock` with the new HEAD commit when HEAD moved. Outcomes:
  - `re-pinned → <sha>` — HEAD advanced; lock rewritten.
  - `up to date` — already at origin tip; no lock churn.
  - `diverged` — origin and local have diverged; refused (no commits lost). Run `winter ws update` to force a checkout.
  - `pin error: <detail>` — dirty working tree without `--autostash`, or stash failure; refused without mutation. Pass `--autostash` to stash → ff → pop.
- **tag / commit ref** — the checkout is held exactly at the locked commit; no integration happens. Reports `held @ <ref>`.

To move a held pin (tag or commit) or snap a branch pin to the latest origin tip without waiting for the next `pull`, use [`winter ws update`](./update.md).

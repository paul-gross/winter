# `winter ws update` — re-pin standalone repos and rewrite the lock

Explicitly re-resolves `ref` pins for standalone repos, checks out the resolved commit, and rewrites `.winter/config.lock`. For the rest of the family, see the [`winter ws` hub](./index.md).

This is the **only path** that moves a tag/commit pin or snaps a branch pin to the latest origin tip on demand. It surfaces the change as a reviewable `git diff` against the committed lock file — making pin bumps deliberate and auditable.

## Usage

```
winter ws update [REPO] [--autostash] [--json]
```

| Form | What it does |
|------|--------------|
| `winter ws update` | Re-pins **all** pinned standalone repos (those with a `ref` in the config) |
| `winter ws update <name>` | Re-pins only the named standalone repo |
| `winter ws update --autostash` | Allows re-pin when the working tree is dirty: stash → checkout → pop |

Repos without a `ref` are ignored regardless of scope.

## What each step does

For each in-scope repo `update`:

1. **Fetches** `origin` so `resolve_ref` sees current remote refs.
2. **Dirty guard** — if the working tree is not clean and `--autostash` is not set, emits a per-repo failure and continues the fan-out. With `--autostash`, stashes → checks out → pops.
3. **Resolves** the `ref` string against the freshly-fetched refs in order: `refs/remotes/origin/<ref>` (branch) → `refs/tags/<ref>` (tag) → `<ref>^{commit}` (raw SHA).
4. **Up-to-date check** — if the resolved commit equals the current HEAD and the lock already records the same commit, reports `up to date` with no checkout and no lock churn.
5. **Checks out** the resolved commit (detached HEAD for tag/commit, tracking branch for branch) and **rewrites the lock** entry for this repo — preserving all other repos' entries.
6. **Unresolvable ref** → emits a per-repo failure; continues fan-out for remaining repos.

## Outcomes

| Outcome | JSON `result` | Meaning |
|---------|---------------|---------|
| `up to date` | `up_to_date` | Resolved commit matches current HEAD and lock; nothing to do |
| `re-pinned → <sha>` | `re_pinned` | HEAD moved; lock rewritten with new 8-char SHA prefix |
| `pin error: <detail>` | `pin_error` | The re-pin operation could not run: dirty working tree without `--autostash`, unresolvable ref, stash failure, or checkout error. Fan-out continues for other repos. |
| `diverged: +N/-M` | `diverged` | Branch pin refused because origin diverged from local history (branch-pin pull path only; not emitted by `update`). |

## Reviewable lock diff workflow

After `winter ws update` moves a pin, the change appears in `git diff .winter/config.lock` — a clean, reviewable record of exactly which commit each pinned repo was bumped to. Commit the lock alongside any other workspace changes to make the bump deliberate and reproducible across machines.

```bash
winter ws update               # re-pin all
git diff .winter/config.lock   # review what moved
git add .winter/config.lock
git commit -m "chore: bump standalone pins"
```

## Errors

- **`standalone repo '<name>' has no \`ref\` configured`** — the named repo exists but is not pinned; nothing to update.
- **`no pinned standalone repo named '<name>'`** — the name doesn't match any standalone repo in the config.
- **`refusing to re-pin '<name>': uncommitted changes`** — dirty working tree; commit or stash manually, or pass `--autostash`.
- **Unresolvable ref** — the `ref` string in the config didn't match any branch, tag, or commit in the local ref store after the fetch. Run `winter ws fetch <name>` to double-check connectivity, then re-run.

See also: [configuration/repositories.md — `ref`](../../configuration/repositories.md#ref--standalone-repo-pins) for the full pin semantics and lock schema; [`winter ws pull`](./pull.md) for automatic branch-pin advances during a pull.

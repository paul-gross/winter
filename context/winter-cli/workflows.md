# Common workflows

Recipe-style command sequences for the day-to-day operations. For the per-command reference, see the
[command reference](./usage/index.md) and the per-topic files it routes to.

## Bootstrap a new workspace

```bash
winter ws init              # clone every declared repo into projects/
winter ws init alpha        # create the alpha/ feature environment (structural only)
winter provision alpha      # bring alpha to a working state: deps, resources, data (see usage/provision.md)
```

### Check workspace state

```bash
winter ws status alpha
```

### Merge main before starting work

```bash
winter ws merge master alpha                # offline ff-only against local master — no network call
winter ws merge master alpha beta gamma     # fan one source ref across multiple envs in a single call
winter ws fetch alpha                       # add this first if you need a fresh origin/master
winter ws merge origin/master alpha         # then merge the freshly-fetched ref
winter ws merge master alpha --merge        # 3-way fallback when ff-only would refuse
```

Use the offline `winter ws merge master alpha` form when local `master` is already current — it doesn't hit the remote,
so it's faster and won't stall on a hanging fetch. When you need a fresh `origin/master` first, run
`winter ws fetch alpha` (which also fast-forwards the source checkout's local main), then
`winter ws merge origin/master alpha`.

### Pull remote feature-branch commits into the local env

```bash
winter ws pull alpha               # ff-only against each worktree's own upstream; untracked worktrees skipped, diverged reported
winter ws pull alpha --rebase      # ff or replay local commits onto upstream
winter ws pull alpha --autostash   # stash dirty tree first, restore after
```

### Start a new feature

```bash
winter ws init alpha                       # ensures alpha/ exists (structural: worktrees, env-index, cmd list)
winter provision alpha                     # readiness: dependencies, resources, seed data (see usage/provision.md)
winter ws connect alpha feature/my-feature
```

### Give one env's repos independent feature branches

```bash
winter ws connect alpha/api feature/auth      # just alpha's api worktree → origin/feature/auth
winter ws connect alpha/web feature/billing   # just alpha's web worktree → origin/feature/billing
```

The trailing argument is the branch; everything before it is a segment-aware `<env>/<repo>` glob, so each worktree can
track its own remote branch. `status` / `pull` / `push` then honor those per-worktree upstreams independently.

### Fold one env into another

```bash
winter ws merge alpha gamma                # merge alpha into gamma's worktrees
```

### Push completed work

```bash
winter ws push alpha                       # alpha's non-pinned worktrees
winter ws push alpha/winter                # one specific worktree
winter ws push 'alpha/*' 'beta/*'          # alpha + beta non-pinned worktrees
winter ws push --include-pinned            # all envs, pinned and non-pinned
winter ws push --all                       # all envs' non-pinned worktrees + standalone
```

### Restack a chain, then publish

```bash
winter ws restack env2 master --dry-run --json     # preview the plan first
winter ws restack env2 master                      # rebase env2 onto the rewritten master
```

See [restack.md § Publishing a restacked env](./usage/ws/restack.md#publishing-a-restacked-env) for why `ws push` won't
publish this and what to run instead.

### Update everything from remotes (refs + source-checkout mains)

```bash
winter ws fetch --all                      # refresh refs for every env + standalone, ff each source checkout's local main
```

Feature worktrees are left untouched; only remote-tracking refs and the source checkouts' local main move.

### Review changes before committing

```bash
winter ws diff alpha --branch          # full branch diff vs main
winter ws diff alpha --staged          # staged changes only
winter ws diff alpha/my-app            # single repo
```

### Reuse a feature environment for a different feature

```bash
winter ws disconnect alpha
winter ws connect alpha feature/other-feature
```

### Adopt an existing remote feature branch

```bash
winter ws fetch alpha                              # refresh origin refs first
winter ws checkout alpha feature/existing-branch   # connect + reset every non-pinned worktree to origin/feature/existing-branch
```

### Start a brand-new feature branch

```bash
winter ws checkout alpha feature/new-branch --new   # connect to the not-yet-pushed branch, reset every non-pinned worktree to origin/<main>
```

### Tear down a feature environment

```bash
winter ws destroy alpha --dry-run              # preview: provision teardown plan, then hooks + worktrees that will be removed
winter ws destroy alpha                        # standard teardown (provision teardown → hooks → remove)
winter ws destroy alpha --force                # bypass dirty-worktree check
winter ws destroy alpha --strict               # abort if provision teardown or any hook exits non-zero
winter ws destroy alpha --no-provision-teardown  # skip provision teardown; structural teardown only
winter ws destroy alpha beta --force             # multiple envs, no confirmation prompt (scripted use)
```

### Hand a feature environment to a new tenant

```bash
winter clean alpha --dry-run              # preview: which declared cleans would run, no side effects
winter clean alpha                        # run them for real: disposable build output, caches, etc.
winter provision alpha                    # bring the env back to a working state for the next tenant
```

`winter clean` sits between a tenant finishing and the next tenant's `winter provision` — see
[usage/clean.md](./usage/clean.md) for why it exists, the full contract, and how it differs from `winter ws clean`.

### Clean up orphan disk state

```bash
winter ws prune --dry-run    # list orphan project clones, orphan standalone clones, broken .claude/ symlinks
winter ws prune              # interactive confirm + delete
```

### Propagate a config change

After adding a repo to the config or changing `cmd`/`git_excludes`, reconcile everything:

```bash
winter ws init --all
```

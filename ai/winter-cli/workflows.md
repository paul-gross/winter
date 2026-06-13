# Common workflows

Recipe-style command sequences for the day-to-day operations. For the per-command reference, see the [command reference](./usage/index.md) and the per-topic files it routes to.

### Bootstrap a new workspace
```bash
winter ws init              # clone every declared repo into projects/
winter ws init alpha        # create the alpha/ feature environment
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

Use the offline `winter ws merge master alpha` form when local `master` is already current — it doesn't hit the remote, so it's faster and won't stall on a hanging fetch. When you need a fresh `origin/master` first, run `winter ws fetch alpha` (which also fast-forwards the source checkout's local main), then `winter ws merge origin/master alpha`.

### Pull remote feature-branch commits into the local env
```bash
winter ws pull alpha               # ff-only against each worktree's own upstream; untracked worktrees skipped, diverged reported
winter ws pull alpha --rebase      # ff or replay local commits onto upstream
winter ws pull alpha --autostash   # stash dirty tree first, restore after
```

### Start a new feature
```bash
winter ws init alpha                       # ensures alpha/ exists
winter ws connect alpha feature/my-feature
```

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

### Update everything from remotes (refs + source-checkout mains)
```bash
winter ws fetch --all                      # refresh refs for every env + standalone, ff each source checkout's local main
```
Feature worktrees are left untouched; only remote-tracking refs and the source checkouts' local main move.

### Review changes before committing
```bash
winter ws diff alpha --branch          # full branch diff vs main
winter ws diff alpha --staged          # staged changes only
winter ws diff alpha --repo my-app     # single repo
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
winter ws destroy alpha --dry-run    # preview: hooks that will fire, worktrees that will be removed
winter ws destroy alpha              # standard teardown (fires on_env_destroy hooks, then removes)
winter ws destroy alpha --force      # bypass dirty-worktree check
winter ws destroy alpha --strict     # abort if any hook exits non-zero
```

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

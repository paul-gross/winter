---
id: ws/init
title: Reconcile source checkouts and standalones
---

Run `winter-workspace ws init` from an existing workspace to reconcile its declared source checkouts and standalone
repositories. `winter-workspace ws init --json` prints one event per line as it happens, rather than human rows.
No target argument or `--all` option is accepted.

## Order and effects

The command loads the workspace configuration before changing anything, then runs:

1. **Projects:** create `projects/`, maintain workspace exclude blocks, and reconcile each declared project there.
2. **Standalones:** reconcile declared standalones at their configured locations, then finalize workspace exclude
   ownership. This target is skipped when there are no standalones or qualifying projects; finalization still runs.
3. **Safety rules:** refresh an existing managed safety block in `AGENTS.md`. An unowned file is not adopted or
   replaced; a conflicting block is reported without replacement.

For each repository, in order:

1. Clone a missing checkout from its declared URL, or verify that an existing location is a Git worktree root with the
   declared origin URL. A missing standalone is refused if its destination escapes the workspace, occupies a reserved
   directory, or overlaps another declared repository.
2. Apply the configured Git identity.
3. Append configured Git excludes that are not already present.
4. Run each configured `cmd` through `/bin/sh` in declaration order, from the checkout, streaming its output.
5. For a standalone, materialize its configured pin.

At most four repositories run at once. Events from different repositories may interleave; each repository's steps stay
ordered. A failed step prevents that repository's later steps, not other repositories, finalization, or safety refresh.

## Repeat and recover

A repeat verifies existing checkouts instead of cloning them again, keeps unchanged managed exclude files unchanged,
and uses recorded standalone pins. It reapplies Git identity and **runs every configured command again**: command
authors must make their effects repeatable. Repository excludes are append-only; removing an entry from configuration
does not remove an old appended line. Finalization can retract obsolete managed workspace blocks, but does not delete
checkouts.

Exit code 0 means reconciliation succeeded. Configuration, checkout, command, managed-content, or output failures exit
1; unsupported arguments exit 2. Human failures go to stderr. Ctrl-C exits 1 with `aborted`; after inspecting any
partial checkout or command effects, rerun the same command to reconcile again.

Bare reconciliation does not create a workspace, prune checkouts, reconcile feature environments, provision resources,
start services, project extensions or skills, run extension hooks, or reconcile nested workspaces. It maintains the
workspace skill exclude block, not the projected skills themselves.

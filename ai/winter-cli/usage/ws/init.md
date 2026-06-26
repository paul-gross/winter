# `winter ws init` — reconcile the workspace against the config

For the rest of the family, see the [`winter ws` hub](./index.md).

One idempotent command with three modes. Safe to re-run any time.

| Form | What it reconciles |
|------|--------------------|
| `winter ws init` | Source checkouts in `projects/` and standalone repos. |
| `winter ws init <name>` | The `./<name>/` feature environment. |
| `winter ws init --all` | Source checkouts, standalones, and every existing feature environment. |

Each mode applies the same per-repo reconcile steps (git identity, excludes, `cmd` list, extension processing, pinned-repo tracking on worktrees). For the env-init path, init also infers and wires an upstream for non-pinned newly-added worktrees when their connected siblings agree on one; ambiguous or divergent siblings are left for explicit `winter ws connect`. See [worktree-ops.md](../../../worktree-ops.md) for the full step list, the pinned-repo specifics, and the upstream-inference contract.

**Standalone repo pin behavior.** When a standalone repo has a `ref` configured (see [configuration/repositories.md — ref](../../configuration/repositories.md#ref--standalone-repo-pins)), `init` applies the pin during reconcile:

- **Lock present and fresh** (`entry.ref` matches config `ref`): checks out the locked commit without network access or re-resolution. This is the reproducible-install path — the locked commit wins even if the remote branch or tag has since moved.
- **Lock absent or stale** (`entry.ref` differs, or no entry): resolves `ref` against the on-disk remote refs, checks out the result, and writes/rewrites the lock entry for this repo. If the working tree has uncommitted changes, refuses with a clear error — commit or stash first. On a fresh clone the tree is always clean.

The lock file (`.winter/config.lock`) is committed alongside the workspace config; run `winter ws init` after cloning a workspace and the correct commit will be checked out automatically with no manual ref resolution.

Greek letters (`alpha`, `beta`, …) are the conventional feature environment names. The first 10 (`alpha`…`kappa`) are the default `env_aliases` and receive fixed indices `1..10`. Other names — remaining Greek letters or arbitrary strings — hash into a higher index band; `winter ws init` linear-probes upward on collision, so the assigned index is stable once written but may differ from the raw hash suggestion. `winter ws index <name>` shows what index an existing env was assigned (persisted) or what slot a new name would be suggested (hash, before probe).

**Reserved name:** `workspace` cannot be used as a feature environment name — `winter ws init workspace` is rejected with an error. `workspace` is a reserved service scope used by `winter service`; see [../service.md#workspace-scope](../service.md#workspace-scope).

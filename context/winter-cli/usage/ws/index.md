# `winter ws` — workspace and environment commands

The `winter ws` family manages source checkouts and feature environments across the workspace. This is the family hub: skim the command table, then open the one command's file you need. For the rest of the CLI surface, see the [command reference](../index.md).

## Commands

Command names below link to a per-command file where one exists; the lighter commands are fully described by their row.

| Command | Usage | Purpose |
|---------|-------|---------|
| [`init`](./init.md) | `winter ws init [TARGET] [--all] [--json]` | Reconcile source checkouts or a feature environment against the config |
| [`destroy`](./destroy.md) | `winter ws destroy PATTERNS... [--force\|--strict\|--dry-run] [--no-provision-teardown] [--json]` | Tear down one or more feature envs matched by PATTERNS (bare env-name globs): run provision teardown (`data --destroy`, `resource --destroy`), fire `on_env_destroy` hooks, then remove every per-repo worktree and the env directory. A glob or multiple PATTERNS prints the resolved env list and asks for confirmation first (`--force` skips it) |
| [`checkout`](./checkout.md) | `winter ws checkout ENV FEATURE_BRANCH [--new] [--force] [--json]` | Connect every non-pinned worktree in ENV to FEATURE_BRANCH and reset to it (or to `origin/<main>` where it doesn't exist), all-or-nothing |
| `list` | `winter ws list [--json]` | List all feature environments |
| [`status`](./status.md) | `winter ws status [PATTERNS]... [--json] [--fetch]` | Machine-readable + human-readable workspace state snapshot |
| [`fetch`](./fetch.md) | `winter ws fetch [PATTERNS]... [--standalone\|--all] [--json]` | Fetch refs from `origin` for matched project worktrees, and fast-forward each matched source checkout's local main |
| [`pull`](./pull.md) | `winter ws pull [PATTERNS]... [--standalone\|--all] [--ff-only\|--merge\|--rebase] [--autostash] [--json]` | Fetch + ff-only integrate (default) matched project worktrees; for pinned standalones, advances a branch pin and holds a tag/commit pin |
| [`update`](./update.md) | `winter ws update [REPOS]... [--autostash] [--json]` | Explicitly re-resolve `ref` pins for pinned standalone repos and rewrite `.winter/config.lock` — the only path that moves a tag/commit pin. Each `REPO` is a bare glob over standalone-repo names (no `<env>/<repo>` segment) |
| [`merge`](./merge.md) | `winter ws merge SOURCE_REF [PATTERNS]... [--standalone\|--all] [--ff-only\|--merge\|--no-ff] [--autostash] [--exclude-pinned\|--only-pinned] [--json]` | Merge an arbitrary SOURCE_REF (env name, branch, `origin/...`) into matched project worktrees |
| [`push`](./push.md) | `winter ws push [PATTERNS]... [--standalone\|--all] [--include-pinned\|--only-pinned] [--json]` | Push matched project worktrees to their tracked upstream |
| `connect` | `winter ws connect PATTERNS... FEATURE_BRANCH [--json]` | Connect matched non-pinned worktrees to `origin/FEATURE_BRANCH`. Each `PATTERN` is a segment-aware `<env>/<repo>` glob (a bare `<env>` connects the whole env); the trailing arg is the branch, so per-repo names can differ across an env |
| `disconnect` | `winter ws disconnect PATTERNS... [--json]` | Disconnect matched non-pinned worktrees from their feature branch. Each `PATTERN` is a segment-aware `<env>/<repo>` glob (a bare `<env>` disconnects the whole env), same grammar as `connect` |
| `diff` | `winter ws diff [PATTERNS]... [--staged\|--branch] [--no-headers] [--json]` | Unified diff across repos matched by PATTERNS (defaults to `*/*`, i.e. every env's every repo); a bare `<env>` diffs the whole env, `<env>/<repo>` targets one repo. Multiple envs (or a glob matching several) concatenate their per-repo sections under an added env header (`--no-headers` omits both) |
| `index` | `winter ws index NAME [--json]` | Print the port-offset index for a feature environment name — persisted index from `.winter/state.toml` when the env exists, or suggested (hash) slot for a hypothetical name (may shift on create due to linear-probe collision resolution). **Scripted/agent use:** pass `--json` and read the `index` and `source` fields; bare output is human-formatted and not stable for parsing. |
| [`prune`](./prune.md) | `winter ws prune [--dry-run\|--force] [--json]` | Remove disk state for repos no longer in the workspace config (orphan clones, broken `.claude/` symlinks). Refuses repos with uncommitted changes or attached worktrees |
| `worktrees` | `winter ws worktrees [--status] [--json]` | List every existing feature-environment worktree and standalone repo as a flat table or JSON array — intended for editor integrations (e.g. Neovim fuzzy-finder `cd` picker). Each entry's `kind` is one of `worktree` \| `standalone` \| `workspace`; the implicit workspace repo is the single `workspace` entry, labelled `<workspace>`. Omits entries whose directory does not exist on disk. `--status` adds per-repo git status (ahead/behind/dirty) at the cost of a git call per repo |

The four remote-sync commands — `fetch`, `pull`, `push`, `merge` — share a [pattern and scope vocabulary](./patterns.md) (segment-glob `PATTERNS`, `--standalone`/`--all`, pinned-scope rules). Read that once; each command's file covers only its own deltas. `status` and `diff` use the same segment-glob `<env>/<repo>` grammar over their own scope (project worktrees only — no `--standalone`/`--all`); `diff` has no separate `--repo` filter — see [patterns.md](./patterns.md#winter-ws-status--winter-ws-diff) for the pattern that replaces it.

See also: [Drift warnings](../../resilience.md#drift-warnings) — source-checkout drift contributes to `winter ws status` exit code 1 on an unscoped run.

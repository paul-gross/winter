# Config files & merge model

Winter loads two files and merges them:

- `.winter/config.toml` — committed workspace config (repo list, excludes, defaults).
- `.winter/config.local.toml` — gitignored overlay for per-user settings (git identity).

It also manages a third file, `.winter/state.toml`, automatically (see [State registry](#state-registry) below).

## Shared config (`.winter/config.toml`)

The committed workspace config. Its top-level scalar keys:

```toml
session_prefix = "my-project"   # tmux session prefix
main_branch = "main"            # workspace-default main branch (per-repo override on each repo entry)
adopt_extensions = "winter"     # how aggressively standalone repos contribute skills/agents — see extensions.md
prefix = "myprefix"             # optional; namespace for workspace-authored skills — see below
doctor = "context/project/doctor.sh" # optional workspace-level `winter doctor` probe — see doctor.md
lint = "context/project/lint.sh"     # optional workspace-level `winter lint` check(s) — see lint.md

[capabilities]                  # bind capability slots to provider extensions — see capabilities.md
service = "winter-service-tmux"
```

- **`session_prefix`** — tmux session prefix.
- **`main_branch`** — the workspace-default main branch. Each repo entry can override it with its own `main_branch`.
- **`adopt_extensions`** — controls when winter processes a standalone repo's skills and agents. Full mode table in [extensions.md](./extensions.md#adopt_extensions-modes).
- **`prefix`** — optional top-level skill namespace for workspace-authored skills. When set, `winter ws init` reads every skill directory under `workspace_root/skills/` and projects it into per-vendor skill directories (`.claude/skills/<prefix>-*`, `.codex/skills/<prefix>-*`, `.opencode/skill/<prefix>-*`). When absent, workspace skill projection is skipped. Must be distinct from any `[[standalone_repository]]` `prefix` value (both prune `<prefix>-*` entries in the same skill directories). See [setup.md — workspace-authored skills](../setup.md#workspace-authored-skills-projection) for the relocation precondition.
- **`doctor`** — optional workspace-level probe script for `winter doctor`. See [doctor.md](./doctor.md#workspace-doctor-probe).
- **`lint`** — optional workspace-level lint script(s) for `winter lint`. See [lint.md](./lint.md#workspace-lint-check).
- **`[capabilities]`** — binds capability slots (today just `service`) to installed provider extensions. See [capabilities.md](./capabilities.md).

### Workspace skill prefix

The top-level `prefix` key is distinct from the per-`[[standalone_repository]]` `prefix` field: the standalone `prefix` overrides the symlink prefix for a specific extension's skills (see [repositories.md — prefix](./repositories.md) and [extensions.md — projection](./extensions.md)), while the top-level `prefix` names the namespace for skills you author directly in the workspace. Using the same value for both causes a collision — `winter ws init` rejects this at config load with a clear error.

The rest of `.winter/config.toml` is organized by concept:

- **Port allocation** (`base_port`, `ports_per_env`, `env_aliases`, `envs_per_workspace`) and the `[env.workspace.vars]` / `[env.feature.vars]` env var bands — [ports-and-environments.md](./ports-and-environments.md).
- **Repositories** (`[[project_repository]]`, `[[standalone_repository]]`, `git_excludes`) — [repositories.md](./repositories.md).
- **TUI** (`[tui.dashboard]`, `[keybindings]`) — [tui.md](./tui.md).
- **Provision handlers** (`[[provision.*]]`) — [provision.md](./provision.md).

## Local overlay (`.winter/config.local.toml`)

```toml
[git]
user.name = "John Doe"
user.email = "john.doe@example.com"
```

The overlay uses the same schema as the shared config. Keys in the overlay override the shared config key-by-key. The `[git]` identity is applied to every repo winter-cli manages during `winter ws init`.

## State registry

`.winter/state.toml` is a machine-local, gitignored file (not a config file) that winter manages automatically. It records the **env name → assigned index** mapping written by `winter ws init` and cleared by `winter ws destroy`. You never edit it by hand.

- `winter ws init <name>` allocates an index (alias → fixed slot; ad-hoc → hash then linear-probe upward within the hash band) and writes the assignment here.
- `winter ws destroy <name>` removes the entry.
- The read path loads the recorded index from this file; when no entry exists (pre-registry env), it falls back to recomputing from the name.
- `winter ws index <name>` returns the persisted index for an existing env, or the suggested (hash) slot for a hypothetical name — with a note that the suggestion may shift on create if another env already occupies that slot.
- `winter doctor` cross-checks this registry against on-disk env directories and warns on stale entries, unregistered env dirs, out-of-range indices, and duplicate assignments.

For how indices map to port bands and the index-reservation rules, see [ports-and-environments.md](./ports-and-environments.md#index-reservation).

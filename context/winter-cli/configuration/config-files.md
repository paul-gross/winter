# Config files & merge model

Winter loads two files and merges them:

- `.winter/config.toml` — committed workspace config (repo list, excludes, defaults).
- `.winter/config.local.toml` — gitignored overlay for per-user settings (git identity).

It also manages a third file, `.winter/state.toml`, automatically (see [State registry](#state-registry) below).

## Shared config (`.winter/config.toml`)

The committed workspace config. Its top-level scalar keys:

```toml
service_prefix = "my-project"   # workspace service-orchestration namespace
main_branch = "main"            # workspace-default main branch (per-repo override on each repo entry)
adopt_extensions = "winter"     # how aggressively standalone repos contribute skills/agents — see extensions.md
prefix = "ws"                   # workspace skill namespace (default "ws") — see below
skills_dir = "skills"           # workspace skills source dir relative to workspace root (default "skills")
doctor = "context/project/doctor.sh" # optional workspace-level `winter doctor` probe — see doctor.md
lint = "context/project/lint.sh"     # optional workspace-level `winter lint` check(s) — see lint.md

[capabilities]                  # bind capability slots to provider extensions — see capabilities.md
service = "winter-service-tmux"
```

- **`service_prefix`** — the workspace-level service-orchestration namespace prefix, default `"winter"`. Overridable in `config.local.toml` so distinct checkouts of the same workspace can run separate namespaces. Surfaces to service providers as `WINTER_SERVICE_PREFIX` — see [contracts/service-orchestrator.md](../contracts/service-orchestrator.md) for injection semantics. The deprecated `session_prefix` key was removed pre-1.0: a `.winter/config.toml` that still sets it fails to load with a `ConfigError` naming the key and pointing at `service_prefix`.
- **`main_branch`** — the workspace-default main branch. Each repo entry can override it with its own `main_branch`.
- **`adopt_extensions`** — controls when winter processes a standalone repo's skills and agents. Full mode table in [extensions.md](./extensions.md#adopt_extensions-modes).
- **`prefix`** — top-level skill namespace for workspace skills. Defaults to `"ws"`. `winter ws init` reads every skill directory under `workspace_root/<skills_dir>/` and projects it into per-vendor skill directories (`.claude/skills/<prefix>`, `.claude/skills/<prefix>-*`, `.codex/skills/<prefix>`, `.codex/skills/<prefix>-*`, `.opencode/skill/<prefix>`, `.opencode/skill/<prefix>-*`). Projection is always-on — the default `"ws"` prefix is applied even when `prefix` is absent from the config file. Must be distinct from any `[[standalone_repository]]` `prefix` value (both prune `<prefix>-*` and bare `<prefix>` entries in the same skill directories). See [setup.md — workspace skills](../setup.md#workspace-skills-projection) for details.
- **`skills_dir`** — relative path (from workspace root) to the directory containing workspace-authored skill subdirectories. Defaults to `"skills"`. Override when your skills live in a non-default location (e.g. `skills_dir = "context/skills"`).
- **`doctor`** — optional workspace-level probe script for `winter doctor`. See [doctor.md](./doctor.md#workspace-doctor-probe).
- **`lint`** — optional workspace-level lint script(s) for `winter lint`. See [lint.md](./lint.md#workspace-lint-check).
- **`[capabilities]`** — binds capability slots (today just `service`) to installed provider extensions. See [capabilities.md](./capabilities.md).

### Workspace skill prefix

The top-level `prefix` key is distinct from the per-`[[standalone_repository]]` `prefix` field: the standalone `prefix` overrides the symlink prefix for a specific extension's skills (see [repositories.md — prefix](./repositories.md) and [extensions.md — projection](./extensions.md)), while the top-level `prefix` names the namespace for skills you author directly in the workspace. Using the same value for both causes a collision — `winter ws init` rejects this at config load with a clear error.

**Naming rule:** A skill directory whose name equals the prefix projects as the bare prefix; all others project as `<prefix>-<dirname>`. For example, with the default prefix `ws`: `skills/ws/` → `ws` (bare), `skills/init/` → `ws-init`.

The rest of `.winter/config.toml` is organized by concept:

- **Port allocation** (`base_port`, `ports_per_env`, `env_aliases`, `envs_per_workspace`) and the `[env.workspace.vars]` / `[env.feature.vars]` env var bands — [ports-and-environments.md](./ports-and-environments.md).
- **Repositories** (`[[project_repository]]`, `[[standalone_repository]]`, `git_excludes`) — [repositories.md](./repositories.md).
- **Agent model & tier configuration** (`[agent_model_overrides]`, `[model_tiers]`) — [agents.md](./agents.md).
- **Artifact space** (`[space]`) — [space.md](./space.md).
- **TUI** (`[tui.dashboard]`, `[keybindings]`) — [tui.md](./tui.md).
- **Provision handlers** (`[[provision.*]]`) — [provision.md](./provision.md).

## Local overlay (`.winter/config.local.toml`)

```toml
[git]
user.name = "John Doe"
user.email = "john.doe@example.com"
```

The overlay uses the same schema as the shared config. Keys in the overlay override the shared config key-by-key. The `[git]` identity is applied to every repo winter-cli manages during `winter ws init`.

## Workspace-root file shape

`winter ws init` generates and manages exactly one workspace-root file for agent context — `AGENTS.winter.md` — alongside the committed pair the workspace author controls and an optional hand-authored `AGENTS.local.md` (winter never writes the latter):

| File | Committed? | Content | Gitignored? |
|------|-----------|---------|-------------|
| `AGENTS.md` | yes | Canonical workspace instructions body | no |
| `CLAUDE.md` | yes | One-line shim: `@AGENTS.md` | no |
| `AGENTS.winter.md` | no | Generated extension manifest (body) | yes |
| `AGENTS.local.md` | no | User-local body (hand-authored) | yes |

`AGENTS.md` is the canonical entry point for every agent harness that supports multi-file context injection. `CLAUDE.md` is a thin committed shim (`@AGENTS.md`) that keeps Claude Code discovering context without any config changes. Claude resolves the rest of the `@import` graph (`AGENTS.winter.md`, `AGENTS.local.md`, `context/…`) transitively from that entry point, so no per-file Claude shims are needed.

The `winter doctor` core probe "AGENTS.md shim" enforces that the committed `CLAUDE.md` is always exactly `@AGENTS.md` — see [doctor.md](./doctor.md#built-in-core-probes).

## State registry

`.winter/state.toml` is a machine-local, gitignored file (not a config file) that winter manages automatically. It records the **env name → assigned index** mapping written by `winter ws init` and cleared by `winter ws destroy`. You never edit it by hand.

- `winter ws init <name>` allocates an index (alias → fixed slot; ad-hoc → hash then linear-probe upward within the hash band) and writes the assignment here.
- `winter ws destroy <name>` removes the entry.
- The read path loads the recorded index from this file; when no entry exists (pre-registry env), it falls back to recomputing from the name.
- `winter ws index <name>` returns the persisted index for an existing env, or the suggested (hash) slot for a hypothetical name — with a note that the suggestion may shift on create if another env already occupies that slot.
- `winter doctor` cross-checks this registry against on-disk env directories and warns on stale entries, unregistered env dirs, out-of-range indices, and duplicate assignments.

For how indices map to port bands and the index-reservation rules, see [ports-and-environments.md](./ports-and-environments.md#index-reservation).

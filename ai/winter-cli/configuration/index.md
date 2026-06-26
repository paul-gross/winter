# Winter configuration

Winter is configured by editing TOML files in `.winter/` at the workspace root. This is the hub for the configuration surface: read it first, then open the one concept file you need. To install the `winter` CLI itself, see [../setup.md](../setup.md); for day-to-day commands, see [../usage/index.md](../usage/index.md).

## The configuration model

Winter loads `.winter/config.toml` (committed) and merges `.winter/config.local.toml` (a gitignored per-user overlay) over it, and manages `.winter/state.toml` (machine-local) itself. Start with [config files & merge model](./config-files.md) for the merge semantics and the workspace-level keys, then drill into the concept you're configuring.

## Concept routing

| Concept | Read when… |
|---------|------------|
| [Config files & merge model](./config-files.md) | …you need the two-file merge, the local overlay (git identity), the state registry, or the workspace-level scalar keys. |
| [Repositories](./repositories.md) | …you're declaring project or standalone repos — `url`, `cmd`, `pinned`, `ref` pins and the lock file, `config_dir`, display names and ordering. |
| [Ports & environments](./ports-and-environments.md) | …you're tuning the port band (`base_port`, `ports_per_env`, `env_aliases`), the `[env.vars]` derived variables, or need the index-reservation rules. |
| [Dashboard & keybindings](./tui.md) | …you're setting the `winter dashboard` default layout or remapping its keys. |
| [Provision handlers](./provision.md) | …you're declaring `[[provision.*]]` dependency / resource / data handlers. |
| [Extensions](./extensions.md) | …you're authoring a `winter-ext.toml` — skills/agents symlinking, lifecycle hooks, and the `adopt_extensions` modes. |
| [Capabilities & service orchestration](./capabilities.md) | …you're binding the `service` capability slot to one or more provider extensions. |
| [Doctor probes](./doctor.md) | …you're contributing a `winter doctor` probe from the workspace or an extension. |
| [Lint checks](./lint.md) | …you're contributing a `winter lint` check, or need the built-in core checks. |

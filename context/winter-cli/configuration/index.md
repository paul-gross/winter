# Winter configuration

Winter is configured by editing TOML files in `.winter/` at the workspace root. This is the hub for the configuration
surface: read it first, then open the one concept file you need. To install the `winter` CLI itself, see
[../setup.md](../setup.md); for day-to-day commands, see [../usage/index.md](../usage/index.md).

## The configuration model

Winter loads `.winter/config.toml` (committed) and merges `.winter/config.local.toml` (a gitignored per-user overlay)
over it, and manages `.winter/state.toml` (machine-local) itself. Start with
[config files & merge model](./config-files.md) for the merge semantics and the workspace-level keys, then drill into
the concept you're configuring.

## Concept routing

| Concept                                                          | Read when…                                                                                                                                                                                                         |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Config files & merge model](./config-files.md)                  | …you need the two-file merge, the local overlay (git identity), the state registry, or the workspace-level scalar keys.                                                                                            |
| [Repositories](./repositories.md)                                | …you're declaring project or standalone repos — `url`, `cmd`, `pinned`, `ref` pins and the lock file, `config_dir`, display names and ordering.                                                                    |
| [Ports & environments](./ports-and-environments.md)              | …you're tuning the port band (`base_port`, `ports_per_env`, `env_aliases`), declaring env var bands (including a per-env override), or need the index-reservation rules.                                           |
| [Command-valued env entries](./command-env-entries.md)           | …you're declaring a band entry that sources its value from a command (a secret from Vault, AWS SSM, or another store), or need the per-call-site gate or its degradation.                                          |
| [Command-entry output collisions](./command-entry-collisions.md) | …a key some entry declares directly collides with a key a different entry's command output only emits, or you need the mid-resolution read gap that ordering leaves open.                                          |
| [Command-entry cookbook](./command-entry-cookbook.md)            | …you need a one-line `command` recipe for a specific secrets tool (`vals`, `chamber`, `summon`, `teller`, `op`, `sops`).                                                                                           |
| [Dashboard & keybindings](./tui.md)                              | …you're setting the `winter dashboard` default layout or remapping its keys.                                                                                                                                       |
| [Provision handlers](./provision.md)                             | …you're declaring `[[provision.*]]` dependency / resource / data handlers.                                                                                                                                         |
| [Extensions](./extensions.md)                                    | …you're authoring a `winter-ext.toml` — skill symlinking, cross-harness agent projection, lifecycle hooks, and the `adopt_extensions` modes.                                                                       |
| [Agent model & tier configuration](./agents.md)                  | …you're setting `[agent_model_overrides]` to retarget an agent's model workspace-wide, or `[model_tiers]` to remap built-in tier vendor ids or define custom tier labels referenced from frontmatter or overrides. |
| [Capabilities & service orchestration](./capabilities.md)        | …you're binding the `service` capability slot to one or more provider extensions.                                                                                                                                  |
| [Artifact space](./space.md)                                     | …you're setting where winter and its extensions write generated artifacts (scores, manifests, workflow docs, logs) — the `[space]` root and per-kind overrides that `winter space <kind>` resolves.                |
| [Doctor probes](./doctor.md)                                     | …you're contributing a `winter doctor` probe from the workspace or an extension.                                                                                                                                   |
| [Lint checks](./lint.md)                                         | …you're contributing a `winter lint` check, or need the built-in core checks.                                                                                                                                      |

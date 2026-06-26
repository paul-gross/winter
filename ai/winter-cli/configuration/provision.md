# Provision handlers

`winter provision` reads `[[provision.*]]` tables from `.winter/config.toml` alongside the workspace's other config. Declare handlers per sub-target (`dependency`, `resource`, `data`):

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install-deps.sh"

[[provision.resource]]
scope             = "workspace"
apply             = "scripts/create-db.sh"
destroy           = "scripts/drop-db.sh"
required_services = ["workspace/postgres"]

[[provision.data]]
scope             = "feature-environment"
apply             = "scripts/seed.sh"
reset             = "scripts/reseed.sh"
required_services = ["workspace/postgres"]
```

For per-entry field semantics (`scope`, `apply`, `destroy`, `reset`, `required_services`, allowed sub-targets, and unknown-key rejection), see [../usage/provision.md](../usage/provision.md). Extensions declare the same `[[provision.*]]` shape in their `winter-ext.toml` (paths relative to the extension directory). See [../usage/provision.md](../usage/provision.md) for the full command reference, execution model, service-check behavior, and `--json` event contract.

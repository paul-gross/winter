# Winter CLI — command reference

Every `winter` command group and usage topic, one file each. Read this index, then open only the topic you need. For what the CLI is for, when to use it vs raw git, root flags, and the capability map, see the [hub](../index.md).

## Routing table

| Topic | Read when… |
|-------|------------|
| [Workspace & env commands](./ws/index.md) | …you need to create, inspect, sync, or tear down feature environments and their worktrees — `init`, `status`, `fetch`/`pull`/`push`/`merge`, `connect`, `checkout`, `destroy`, and the rest of the `winter ws` family. |
| [Repository commands](./repo.md) | …you need to add, remove, or list the repositories the workspace tracks. |
| [Dashboard & keybindings](./dashboard.md) | …you want the interactive status TUI, or need to read or remap its keybindings. |
| [Service orchestration](./service.md) | …you need to start, stop, or inspect a feature env's services, including across multiple bound providers. |
| [Doctor](./doctor.md) | …you need to determine the correctness and health of the winter workspace configuration itself. |
| [Lint](./lint.md) | …you need to check the workspace against its documented conventions, or wire up a new lint check. |
| [Graph](./graph.md) | …you need the module dependency graph, or to consume it as JSON from a lint check. |
| [Capabilities](./capabilities.md) | …you need to see which extension provides each capability slot (service, …), or consume the binding map as JSON. |
| [Extension management](./ext.md) | …you need to verify that an extension conforms to a capability spec, or scaffold a new extension skeleton (`winter ext verify`, `winter ext new`). |
| [Provision](./provision.md) | …you need to bring a feature environment to a working state after `winter ws init` — install dependencies, create resources, and load seed data. |
| [Env](./env.md) | …you need to print or source the runtime environment variables for a feature env or the workspace scope — `winter env <scope>`. |
| [Space](./space.md) | …you need the directory where winter and its extensions write generated artifacts (scores, manifests, workflow docs, logs) — `winter space <kind>`, resolved from the `[space]` config table. |

Each topic lives in its own file here under `usage/`; a command group with many sub-commands gets its own subdirectory with a hub (`ws/` is the worked example). Keep this routing table in sync when adding one.

# Winter CLI

The `winter` command is a workspace-level tool for managing worktrees and repositories. It reads configuration from `.winter/config.toml` and operates across every repo in the workspace at once. This is the hub: read it first, then open the one per-topic file you need.

**Use the CLI** for multi-repo operations (init, status, fetch, pull, connect, push, diff); **use raw git** for single-repo work (staging, committing, conflicts, interactive rebase). See [usage/index.md](./usage/index.md) for the full command surface.

## Routing table

| Topic | Read when… |
|-------|------------|
| [Command reference](./usage/index.md) | …you need to run any `winter` command — the per-topic routing index for `ws`, `repo`, `ext`, `dashboard`, `service`, `doctor`, `lint`, `graph`, and `capabilities`. |
| [Common workflows](./workflows.md) | …you want a ready-made command sequence for a routine multi-step operation — bootstrap, start a feature, merge main, push, tear down. |
| [Resilience & drift](./resilience.md) | …you hit a flaky-network retry, a hung remote git call, or a config↔filesystem drift warning, and want the cross-cutting behavior behind it. |
| [Install winter](./setup.md) | …you're installing the `winter` CLI into a workspace. |
| [Configuration](./configuration/index.md) | …you're editing `.winter/config.toml` — repositories, ports, extensions, capabilities, provision handlers, doctor/lint. The per-concept hub for the whole configuration surface. |
| [Root flags](./root-flags.md) | …you need `--verbose`, `--version`, `--service-orchestrator`, or `WINTER_LOG_LEVEL` — the global flags that apply to every command. |

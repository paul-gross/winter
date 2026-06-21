# Winter CLI

The `winter` command is a workspace-level tool for managing worktrees and repositories. It reads configuration from `.winter/config.toml` and operates across every repo in the workspace at once. This is the hub: read it first, then open the one per-topic file you need.

## When to use the CLI vs raw git

**Use the CLI** for operations that span multiple repos — init, status, fetch, pull, connect, push, diff. The CLI handles pinned repos, parallel fetching, source checkout fast-forwarding, and idempotent setup automatically.

**Use raw git** for single-repo operations — staging files, committing, resolving conflicts, interactive rebase, branch inspection. The CLI doesn't replace git for per-repo work.

**Strongly recommended:** read **[usage/index.md](./usage/index.md)** first — the command reference index is a high-level map of everything the CLI can do. Skim it to learn the surface, then open the one topic you need.

## Root flags

`winter --version` prints the installed CLI version (sourced from package metadata, so it tracks the running source) and exits 0. `winter --help` lists every command and root flag.

`winter --verbose` / `winter -v` attaches a stderr `StreamHandler` at DEBUG level so every `logger.debug/info/warning` call inside `winter_cli` becomes visible. Equivalent to `WINTER_LOG_LEVEL=DEBUG`. Diagnostics always go to **stderr**; `--json` stdout stays pure JSON. `WINTER_LOG_LEVEL=<LEVEL>` (e.g. `INFO`, `WARNING`) selects a coarser level without the flag.

`winter --service-orchestrator=<path-or-name> service …` overrides the service orchestrator for a single `winter service` invocation — points dispatch at a local extension directory or a registered name instead of the registry-resolved (bound or sole-provider) extension. See [usage/service.md#local-path-override](./usage/service.md#local-path-override) for the full precedence rule and path-vs-name semantics.

## What the CLI can do

The surface is two command groups plus seven standalone commands:

- **`winter ws …`** — reconcile the workspace or a feature env against the config (`init`), inspect state (`status`, `list`, `worktrees`, `diff`, `index`), move commits between remotes and worktrees (`fetch`, `pull`, `push`, `merge`), manage standalone repo pins (`update`), and manage env lifecycle (`connect`, `disconnect`, `checkout`, `destroy`, `prune`).
- **`winter repo …`** — add, remove, and list the repositories declared in the config.
- **`winter ext …`** — extension contract management: `verify` checks that an extension conforms to the bundled capability spec; `new` scaffolds a new extension skeleton that passes verification out of the box (see [usage/ext.md](./usage/ext.md)).
- **`winter dashboard`** — interactive TUI for workspace status, with remappable keybindings.
- **`winter service <action> [<env>/<service>…]`** — a stable `up`/`down`/`status`/`restart`/`logs` interface that dispatches to the orchestrator extension(s) the workspace registers. A single provider is dispatched directly; multiple bound providers are fanned out on `up` (abort on first failure) and `down` (best-effort, continues past failures). No readiness gate or ordering semantics between providers. `up`/`down` take a single `<env>`; `status`/`restart`/`logs` take `<env>/<service>` PATTERNS. All five also accept the reserved `workspace` scope (see [usage/service.md](./usage/service.md)).
- **`winter doctor`** — preflight health checks.
- **`winter lint`** — convention checks.
- **`winter graph`** — the module dependency graph.
- **`winter capabilities`** — list each capability slot, its bound provider extension(s), and other installed candidates. For the service slot, `--json` emits a scalar `bound` for single-provider and an array for multi-provider (scalar-for-single preserves compatibility with single-provider machine clients).

Most flows are multi-repo: a single `winter ws` invocation fans out over every matched worktree in parallel, honoring pinned-repo rules. Commands accept segment-aware glob `PATTERNS` over `<env>/<repo>`, emit `--json` for tooling, and never touch the network unless the command's purpose is to (`fetch`/`pull`/`push`, or `status --fetch`).

## Routing table

| Topic | Read when… |
|-------|------------|
| [Command reference](./usage/index.md) | …you need to run any `winter` command — the per-topic routing index for `ws`, `repo`, `ext`, `dashboard`, `service`, `doctor`, `lint`, `graph`, and `capabilities`. |
| [Common workflows](./workflows.md) | …you want a ready-made command sequence for a routine multi-step operation — bootstrap, start a feature, merge main, push, tear down. |
| [Resilience & drift](./resilience.md) | …you hit a flaky-network retry, a hung remote git call, or a config↔filesystem drift warning, and want the cross-cutting behavior behind it. |
| [Install & configure](./setup.md) | …you're installing winter or editing `.winter/config.toml` — schema, local overlay, and extensions. |

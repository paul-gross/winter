# Capabilities & service orchestration

Winter routes capabilities (service orchestration and future slots) through a uniform registry. The interface lives in core winter; each implementation lives in whichever extension(s) the workspace binds to the slot. This page covers binding the `service` slot in `.winter/config.toml` and the provider-facing contract.

## Capability registry

Three inputs combine to determine the provider for each slot:

1. **Extension manifest** — a `[provides]` table in `winter-ext.toml`, where each key is a slot name and the value is the entrypoint path relative to the extension repo root.
2. **Workspace config** — a `[capabilities]` table in `.winter/config.toml` (or the `config.local.toml` overlay), where each key is a slot name and the value is the name of an installed extension. The table merges through the overlay key-by-key like every other table.
3. **Installed-extension set** — the standalone repos on disk that the registry walks at resolve time.

### Resolution rules

| State | Result |
|-------|--------|
| Explicit `capabilities.<slot>` binding → valid provider | **explicit** — dispatches to that extension |
| No binding, exactly one extension provides the slot | **implicit** — dispatches to the sole provider |
| No binding, exactly one provider but entrypoint file missing | **implicit** (describe) / dispatch error (resolve) — entrypoint validity re-checked at dispatch time |
| No binding, two or more providers | **implicit (all bound)** — every candidate is bound, in deterministic name order; all are dispatched |
| Binding to an extension that is not installed, or installed but not declaring `provides.<slot>`, or entrypoint file missing | **invalid** — any dispatch errors with a specific message |
| No provider installed | no dispatch possible |

`winter capabilities` introspects the registry (read-only, always exits 0 — see [../usage/capabilities.md](../usage/capabilities.md)). `winter doctor`'s `[capabilities]` probe group evaluates each slot: invalid → `fail`, implicit provider(s) → `pass` (with a note), explicit valid binding → `pass`, no provider → `warn`.

After changing the service contract (adding, removing, or updating a provider), run `winter ext verify <path-to-extension-dir>` against each installed provider to confirm it conforms to the bundled spec (see [../usage/ext.md](../usage/ext.md)).

The only in-scope slot today is `service`. Future slots are added to `CapabilitySlot` in the code and the registry handles them uniformly.

### Deprecated keys

- **`service_orchestrator`** in config — single-string legacy key; normalised at config load into a one-element `capabilities.service` binding. Ignored when `capabilities.service` is already set explicitly. Use `[capabilities].service` for new workspaces.
- **`orchestrate_services`** in manifest — the service-slot-only predecessor of `provides.service`; still read as a fallback via `capability_entrypoint()`. Use `[provides].service` for new extensions.

## Service orchestration

`winter service` is the operator surface for the `service` slot (see [../usage/service.md](../usage/service.md)); this section covers the config and manifest keys that bind a provider to it.

### Registering orchestrator(s)

Three config paths connect the interface to an implementation:

- **Single provider** — `capabilities.service = "<extension-name>"` in the `[capabilities]` table in `.winter/config.toml` (or the `config.local.toml` overlay). The name must match a `[[standalone_repository]]` that ships a `winter-ext.toml`. If only one installed extension declares `provides.service`, the binding is implicit and the explicit config entry is optional.
- **Multiple providers** — `capabilities.service = ["<name-1>", "<name-2>"]` (a list value in the same `[capabilities]` table). Every named provider is bound; list order carries no execution semantics. Each provider must declare `provides.service` in its `winter-ext.toml`. Repeated names are de-duplicated (preserving order) at config load.
- **Extension manifest** — `provides.service = "<path>"` in the `[provides]` table in each extension's `winter-ext.toml`, an executable entrypoint relative to the extension's repo root.

How these bindings resolve into a dispatch target, and how the deprecated `service_orchestrator` / `orchestrate_services` aliases are normalised, is owned by [Capability registry](#capability-registry) above. For dispatch-time multi-provider fan-out (`up` aborts on first failure, `down` best-effort, the ownership index for targeted `logs`/`restart`, the `logs -f` single-owner restriction, and merged `status`), see [../usage/service.md](../usage/service.md).

### Entrypoint contract

The implementer-facing contract a bound provider conforms to — how winter invokes the entrypoint, the environment it injects, the stdout wire formats it must emit, how winter renders them, and the exit codes — is owned by [../contracts/service-orchestrator.md](../contracts/service-orchestrator.md). A third-party orchestrator can conform to it without reading winter's source.

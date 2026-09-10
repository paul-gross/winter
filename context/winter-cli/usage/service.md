# `winter service` — service orchestration

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter service up alpha                               # start env alpha's services (also ensures workspace scope is up first)
winter service up alpha beta                          # start alpha and beta; other configured envs untouched
winter service up 'al*'                                # start every configured env whose name matches al*
winter service up alpha --wait                        # start, then block until no service reports unhealthy (or --timeout)
winter service up workspace                           # bring up only the workspace scope
winter service down alpha                             # stop env alpha's services (leaves workspace scope running)
winter service down '*/web'                            # stop the web service in every env (needs provider support — see below)
winter service down workspace                         # tear down the workspace scope explicitly
winter service start alpha                            # alias of `up alpha`
winter service stop alpha                             # alias of `down alpha`
winter service status                                 # report all services in all envs (patterns optional)
winter service status alpha                           # all services in alpha (expands to alpha/*)
winter service status alpha/api                       # one specific service
winter service status 'alpha/worker-*'                # services matching a glob within alpha
winter service status '*/backend'                     # backend service across every env
winter service status workspace                       # status of the workspace scope
winter service status workspace/api                   # one service within the workspace scope
winter service status --json                          # emit the structured status document as JSON
winter service restart alpha/api beta/worker-main     # bounce specific services (≥1 pattern required)
winter service restart 'alpha/worker-*'               # bounce all matched workers in alpha
winter service restart workspace/api                  # bounce a workspace-scope service
winter service logs alpha                             # stream all services' logs in alpha
winter service logs alpha/api                         # logs for one service (no prefix)
winter service logs 'alpha/worker-*'                  # aggregate logs across matched services
winter service logs '*/backend'                       # backend logs across all envs
winter service logs workspace                         # logs for the workspace scope
winter service logs alpha -f                          # stream until Ctrl-C (exit 130)
winter service logs alpha -n 50                       # last 50 lines (default: 200)
winter service logs alpha --since=5m                  # since 5 minutes ago (normalized to RFC3339)
winter service logs alpha --since=2026-06-13T10:00:00Z  # since absolute timestamp
winter service logs alpha -t                          # prefix each line with its timestamp
```

`winter service` owns a stable `up`/`down`/`status`/`restart`/`logs` interface and dispatches each invocation to the
orchestrator(s) the workspace registers. Consumers depend on `winter service …` and never on the orchestrator's
implementation, so a workspace can swap tmux for containers or a supervising daemon without re-teaching agents, docs, or
habits.

`start` and `stop` are exact CLI-only aliases of `up` and `down` — the identical options (including `up`'s
`--wait`/`--timeout`), PATTERNS grammar, and exit codes, sharing the same implementation rather than a copy.
`winter service start alpha` behaves exactly like `winter service up alpha`; `winter service stop alpha` exactly like
`winter service down alpha`. They exist purely as CLI sugar: the orchestrator wire contract is unchanged, and
`start`/`stop` still dispatch the `up`/`down` action word — no orchestrator ever sees `start` or `stop` on the wire.

`up`, `down`, `status`, `restart`, and `logs` all use **segment-aware glob PATTERNS** over `<env>/<service>` — the same
vocabulary `winter ws` uses for `<env>/<repo>` (see [ws/patterns.md](./ws/patterns.md)). Within each segment, `*`, `?`,
and `[...]` match as usual; `*` does not cross `/`. A bare `<env>` (no slash) expands to `<env>/*` and matches by glob
too (`'al*'` matches every configured env starting with `al`). Multiple patterns can be passed in one invocation.
Cross-environment selection is supported: `'*/backend'` selects the `backend` service across every env. For `up`,
`down`, `restart`, and `logs`, at least one pattern is required (action commands require an explicit target — no
implicit "everything", mirroring `winter ws merge` requiring a source ref). For `status`, omitting all patterns selects
every service in every env (read-shaped, defaults to all like `winter ws status`).

`up` and `down` enumerate the matched **scopes** (configured env names, plus `workspace`) the same way `status` does,
then dispatch **once per matched scope** — the bare `<scope>` when no service-segment filter was given for that scope,
or the scope-qualified `<scope>/<svc-pattern>` when one was. Winter's own bare-env and glob-env dispatch (`up alpha`,
`up alpha beta`, `up 'al*'`) works against any existing provider unchanged; a real service-segment filter
(`up alpha/api`, `down '*/web'`) requires the provider to additionally understand a scope-qualified `up`/`down`
positional and start/stop only the matched services within that scope — see
[`up`/`down` fan-out (multi-provider)](#up--down-fan-out-multi-provider) below.

## Workspace scope

`workspace` is a **reserved, universal service target** accepted by all five actions: `up`, `down`, `status`, `restart`,
`logs`. The orchestrator extension owns what the `workspace` scope means (services that should run once across the whole
workspace, shared daemons, etc.).

`workspace` slots into each action's existing PATTERN grammar in the same place an env name does — there is no new
syntax: bare `workspace` expands to `workspace/*` (per the bare-`<env>` rule above), and `workspace/<service>` selects
one service within the scope. This holds for all five actions, including `up`/`down`: `up workspace` and
`down workspace` are the bare-scope form of the same PATTERN grammar `restart`/`logs`/`status` use. Note that a bare
glob pattern (`*`, `al*`, …) never sweeps in the `workspace` scope — only an explicit `workspace` or
`workspace/<service>` token selects it (mirroring the `*/<svc>` vs `workspace/<svc>` distinction in the
[describe wire contract](../contracts/service-orchestrator.md#describe-wire-contract)).

### Lifecycle policy

`up <pattern...>` (any pattern set that does not itself name `workspace`) **ensures the workspace scope is up first**,
then brings up every matched scope. Both dispatches are attempted regardless of whether the workspace-up step succeeds —
best-effort: each failure is surfaced as feedback and the command exits non-zero if either step failed, but the
requested targets are never skipped. No reference counting is involved: the workspace scope is treated as infrastructure
that should be running. This holds unchanged for multi-env and glob patterns (`up alpha beta`, `up 'al*'`): the
workspace-ensure step runs exactly once, before the fan-out across matched scopes.

`down <pattern...>` **leaves the workspace scope running**. Tearing down one or more feature environments does not
affect services that are shared across the workspace.

`down workspace` is the **only path that tears down the workspace scope** — it must be done explicitly.

`up workspace` brings up only the workspace scope (no recursion into envs).

For `status`/`restart`/`logs`, `workspace` patterns are forwarded verbatim to the orchestrator like any other
`<env>/<service>` selection.

`workspace` is also a **reserved feature-environment name**: `winter ws init workspace` is rejected with an error. See
[ws/init.md](./ws/init.md).

### Readiness gate (`up --wait`)

By default `up` returns as soon as the orchestrator has **launched** the services — it does not wait for them to be
ready to serve. An agent that runs `up` and immediately verifies (curling an endpoint, pointing a browser at the app) is
racing service boot.

`winter service up <pattern...> --wait [--timeout SECONDS]` closes that race:

- After dispatching `up`, winter **polls the `status` action** (the same parse/merge path `winter service status` uses)
  and blocks until **no in-scope service reports `health: "unhealthy"`** — i.e. every service is `"healthy"` or
  `"unknown"`.
- A service with **no declared probe reports `"unknown"`** and does **not** block the wait (so an env whose services
  declare no probes returns promptly).
- On readiness the command exits **0**. If `--timeout` elapses with one or more services still `"unhealthy"`, it exits
  **non-zero** and names the still-unhealthy `<env>/<service>` identifiers on stderr.
- `--timeout` defaults to **120 seconds** and is only meaningful alongside `--wait`.

`--wait` is **entirely winter-side**: it adds no orchestrator action, env var, or argv token — it reuses the `status`
dispatch already in place, and with multiple bound providers it polls and merges status across them exactly like
`winter service status`. It depends on the orchestrator populating the `health` field (see the
[status wire contract](../contracts/service-orchestrator.md#status-wire-contract)); against an orchestrator that reports
every service `"unknown"`, `--wait` always returns promptly.

> **The gate is only as strong as the orchestrator's probes.** An orchestrator that never reports `"unhealthy"` makes
> `--wait` a no-op that returns on the first poll. The bundled tmux orchestrator currently reports `health: "unknown"`
> for every service (`winter-service-tmux:/index.md` — probe support is future work), so in a stock workspace `--wait`
> does **not** yet block on real readiness. Until the orchestrator grows probes, gate verification on the app's own
> signal (a health endpoint, a startup log line) rather than relying on `--wait` alone. <!-- winter-lint:example -->

The readiness gate lives only on `winter service up` — the canonical readiness door. The env-root `./up` delegates to
`winter service up` but does **not** forward `--wait`; run `winter service up <env> --wait` directly for the gate. There
is no readiness gating for `down` or `restart`.

### Startup retry (tmux orchestrator)

The bundled tmux orchestrator supports an opt-in per-service startup retry policy, configured via a `[service.startup]`
subtable in each `[[service]]` entry. When `winter service up` launches a service and the process exits within a short
settle window, the orchestrator re-launches it up to `retries` times, sleeping `retry_delay` seconds between attempts
(process-exit detection only — not a health probe; composes with but is independent of `[service.health]`). After
exhausting retries, `winter service up` exits non-zero and names every service that failed to stay up; surviving
services are unaffected. This policy is honored on the `winter service up` door, and therefore on the env-root `./up`
too, which delegates to it (unlike `--wait`, which `./up` does not forward). Fields default to `retries = 0` (no retry —
opt-in per service) and `retry_delay = 2` seconds.

See `winter-service-tmux:/workflow/config.toml.example` for the `[service.startup]` schema and
`winter-service-tmux:/context/workflow-setup.md` for the setup walkthrough. <!-- winter-lint:example -->

### Registering orchestrator(s)

Binding one or more providers into a workspace is a configuration concern — the single-provider key, the ordered
multi-provider list, the implicit sole-provider and implicit-all rules, and the deprecated/removed legacy keys are owned
by [configuration/capabilities.md](../configuration/capabilities.md#capability-registry). To see the binding a workspace
currently resolves, run `winter capabilities` ([capabilities.md](./capabilities.md)).

### Adding a second provider

Shipping `provides.service` in a new extension's `winter-ext.toml` auto-binds it alongside any existing provider with no
further config change — the implicit-all rule kicks in. Each participating provider **must implement `describe`** (the
behavior when a provider emits no valid describe document differs by action — see `logs` and `restart` routing below).
After adding or updating a provider, verify conformance:

```bash
winter ext verify <path-to-extension-dir>
```

This checks that the extension implements every action required by the bundled service spec (including `describe`). Run
it against each installed provider after changing the service contract. See [ext.md](./ext.md) for the full
`winter ext verify` reference.

### `up` / `down` fan-out (multi-provider)

`up` and `down` reuse the same registry-driven call-matrix `status` builds (see
[`status` (multi-provider)](#status-multi-provider) below): rows are scopes (configured env names from the env-index
registry, plus `workspace`), columns are owning providers. The matrix is narrowed by the user's PATTERNS exactly like
`status` — a bare env or glob (`alpha`, `al*`, `*`) narrows the scope axis only; a scope-qualified pattern (`alpha/api`)
narrows both the scope and (with multiple providers) the provider axis.

Each matched cell is dispatched once: the bare `<scope>` when the cell carries no service-segment filter, or the
scope-qualified `<scope>/<svc-pattern>` when the user supplied a real filter for that scope (see the
[uniform invocation rule](../contracts/service-orchestrator.md#uniform-invocation-rule)). With a single provider and no
filter, this collapses to exactly one bare `up <env>`/`down <env>` dispatch per matched env — the single-provider,
no-filter case behaves exactly as if the matrix and fan-out machinery were not there.

Cells are iterated in the matrix's deterministic order (env cells sorted by env name then provider order, followed by
workspace cells):

- **`up` — forward fan-out.** If a cell's dispatch exits non-zero, the fan-out aborts and the remaining cells are not
  dispatched. The first non-zero exit code is returned. No readiness polling occurs between cells.
- **`down` — best-effort fan-out.** A non-zero exit from one cell is noted but does not prevent the others from being
  dispatched. The first non-zero exit code is returned; 0 is returned if all cells succeeded.

A pattern set that matches no configured scope (or, with multiple providers, no owning provider) emits a
`no service matched` diagnostic and returns non-zero, mirroring `status`.

### Service→provider ownership (multi-provider)

When two or more providers are bound, winter builds a **service→provider ownership index** before routing `logs` and
`restart`. It does this by calling each provider's `describe` action (see
[describe wire contract](../contracts/service-orchestrator.md#describe-wire-contract)). If two providers claim the same
service name, winter aborts with a `DuplicateOwnershipError` naming the service and both providers — each service must
be owned by exactly one provider.

With a single provider, `describe` is never called — the sole provider implicitly owns every service.

### `logs` and `restart` routing (multi-provider)

`logs` and `restart` are **routed to the owning provider** for each matched service via the ownership index. With a
single provider, no index is built (no `describe` call), but patterns are not forwarded blind either — see restart
pattern validation below.

### `restart` pattern validation

Every `restart` PATTERN is validated before any provider is invoked. This env-segment check runs for single-provider
restart too, without requiring `describe` (see
[describe wire contract](../contracts/service-orchestrator.md#describe-wire-contract) — a single provider is still never
asked to `describe`):

- **Bare or unknown-env token** — a pattern whose env segment (the whole token for a bare pattern, or the text before
  the first `/` for a qualified one) names neither a configured env, the reserved `workspace` scope, nor the cross-env
  wildcard `*` is a **hard error**: a bare `repo-name` is read as the env query `repo-name/*`, matches nothing, and is
  rejected rather than silently dropped while a sibling `alpha` token restarts the whole env. Winter names the offending
  token and suggests the qualified `<env>/<service>` form (e.g. `alpha/repo-name`).
- **Qualified pattern, unknown service (multiple providers only)** — a qualified pattern (`<env>/<svc>`) that matches no
  known service (per the ownership index) is also a **hard error**; winter names the missing service and the pattern's
  own env directly, rather than repeating the qualified form the caller already used.
- **Bare env-only pattern** — exempt from the stricter service check above: it always means "every service in that env,"
  valid even when the env currently has no concretely-known service.
- **Single provider** — no ownership index is built (no `describe` call), so only the env-segment check applies; the
  `describe`-based service check above never runs.

Any failure exits non-zero without dispatching anything.

**Describe resilience differs by action:**

- **`logs`** — a provider that emits no valid describe document is **skipped with a warning** written to stderr, exact
  text:

  ```text
  warning: provider '<x>' did not emit a valid describe document and will be skipped for service ownership resolution. Ensure the extension implements the describe action. Detail: …
  ```

  `logs` fails (non-zero) only when the requested service's owning provider is the one that failed describe — i.e. that
  service has no other owner.
- **`restart`** — a provider that emits no valid describe document is **hard-rejected at index-build time**, and the
  command exits non-zero immediately.

`logs -f` (follow mode) is supported only when the matched services resolve to **a single owning provider**. If the
selection would span multiple providers, winter writes an actionable error to stderr and returns 1 without opening any
stream. Non-follow `logs` works across all providers (the streams are merged into a single output).

### `status` (multi-provider)

`status` is always built as a **two-dimensional call-matrix** (rows = scope instances, columns = owning providers),
regardless of whether one or many providers are bound. Core enumerates the matrix, computes the full env map for each
scope via `EnvProvisionerService` with command resolution gated off — the same computation bare `winter env <scope>`
prints, never `--resolve` — so a command entry's own declared key renders as the gate's placeholder rather than its real
value (see [configuration/command-env-entries.md#the-gate](../configuration/command-env-entries.md#the-gate) for exactly
which keys that covers and which are excluded). Core injects the scope vars into each provider subprocess, runs cells in
parallel, and merges the per-cell `StatusDocument` results into a single document before filtering and rendering. For a
feature-env cell the injected set includes `WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`,
`WINTER_WORKSPACE_PORT_BASE`, `WINTER_SERVICE_PREFIX`, and the workspace-band plus feature-band entries from
`.winter/config.toml`; for the `workspace` cell `WINTER_PORT_BASE` is NOT injected and only workspace-band entries are
included — the workspace scope uses `WINTER_WORKSPACE_PORT_BASE` only, so the name carries one meaning everywhere. See
[configuration/ports-and-environments.md](../configuration/ports-and-environments.md#env-var-bands) for the band
semantics.

**Registry-driven enumeration:** scope rows are the **configured env names** from the workspace env-index registry (not
a filesystem scan) plus the `workspace` scope. Core owns enumeration — the orchestrator is called once per
`(provider, scope)` cell with an explicit `<scope>/*` pattern so that it can report configured-but-stopped envs without
needing to scan the file system itself.

**Provider axis:** when two or more providers are bound, `describe` is called on each provider to build an ownership
index (`service-name → provider`). A provider that owns env-scoped services (described with a `*/` prefix) gets one ENV
cell per configured env; a provider that owns workspace-scoped services gets one WORKSPACE cell. Providers that emit no
valid `describe` document are skipped with a warning. When exactly one provider is bound, `describe` is skipped and that
provider gets all cells unconditionally (one per configured env + one workspace cell).

**Pattern narrowing:**

- **No patterns / bare env patterns** (e.g. `alpha`) — the matrix is narrowed to the matching scope axis only. Every
  provider that owns services for those scopes is included. The argv token forwarded to each provider is `<scope>/*`.
- **Scope-qualified patterns** (all patterns contain `/`, e.g. `alpha/web`) — the matrix is narrowed to the matching
  scope AND provider axes. Only the provider(s) that own the named service(s) per the describe index get cells;
  non-owning providers are not called. The argv token is the scope-qualified pattern itself (`alpha/web`). If no
  provider owns any of the requested services, winter emits a single `no service matched` diagnostic and returns
  non-zero.

**Merge and rendering:** all cell results are merged (first-non-null wins for scalar fields; services are concatenated)
into one `StatusDocument`, the pattern backstop filter is applied, and then the document is rendered as a table or
re-serialised as `--json`. A provider cell whose output cannot be parsed surfaces an actionable error naming that
provider; the worst exit code across all cells is adopted.

**`--json` is winter-side only.** The `--json` flag is a winter render toggle — it is never propagated to the
orchestrator as an env var or an argv token. The orchestrator argv is byte-identical with and without `--json`.

## Local-path override

Use the `--service-orchestrator` root flag or the `WINTER_SERVICE_ORCHESTRATOR` environment variable to point
`winter service` at a **local extension directory** for a single invocation — without changing `.winter/config.toml` or
reinstalling anything. This is the primary way to test an in-progress orchestrator whose installed copy still lags the
worktree:

```bash
# flag form (highest precedence)
winter --service-orchestrator=alpha/winter-service-tmux service up alpha

# env-var form
WINTER_SERVICE_ORCHESTRATOR=alpha/winter-service-tmux winter service status
```

**Precedence (highest wins):** `--service-orchestrator` flag → `WINTER_SERVICE_ORCHESTRATOR` env var →
`capabilities.service` key in `.winter/config.toml`.

**Scope with multiple providers:** the override flag collapses fan-out to a single provider for that invocation — the
configured providers are ignored and dispatch goes to the override target only.

**Path-vs-name disambiguation:**

- If the value contains an OS path separator (`/` on POSIX) or resolves to an existing directory → **path mode**: reads
  `winter-ext.toml` from that directory directly, skipping the config-key-present and matches-an-installed-extension
  checks. The directory must still declare a `service` entrypoint (`provides.service`, or the legacy
  `orchestrate_services`) in its `winter-ext.toml`, and that entrypoint file must exist on disk.
- Otherwise (bare name like `winter-service-tmux`) → **name mode**: falls through to the normal registered-extension
  lookup, same as the config key.

**Doctor note:** `winter doctor` reflects the *installed* extension (not the override target), so during an override
window, warnings about a lagging or mismatched installed extension are expected and can be ignored.

**Scope:** the override affects **dispatch only** — `WINTER_EXT_DIR`/`WINTER_EXT_PREFIX` are set from the resolved local
directory, and the entrypoint is invoked from it. It does NOT affect `winter-service-tmux:` path-notation used in agent
docs, nor `@`-loaded markdown references. The override is per-invocation only; it is not persisted.

**Typical use:** during orchestrator development, when the workspace's installed extension copy is behind your working
branch:

```bash
# All four verbs work via the override:
winter --service-orchestrator=alpha/winter-service-tmux service up alpha
winter --service-orchestrator=alpha/winter-service-tmux service status
winter --service-orchestrator=alpha/winter-service-tmux service down alpha

# Control: without the override, the installed (lagging) copy is used — expect
# "declares no service entrypoint" or "not an installed extension" if it hasn't shipped yet.
winter service up alpha
```

## Orchestrator contract

Writing or conforming a service-orchestrator extension is an implementer task, not an operator one: the invocation rule,
injected environment, NDJSON/JSON wire formats, render contracts, backstops, and exit codes have their own home at
[../contracts/service-orchestrator.md](../contracts/service-orchestrator.md). Self-check a provider with
`winter ext verify <path>` ([ext.md](./ext.md)).

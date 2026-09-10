# `winter service` — orchestrator / provider contract

The implementer-facing contract a **service-orchestrator extension** is written against: how `winter service` invokes a
provider, the environment it injects, the wire format the provider emits on stdout, how winter renders it, and the exit
codes. Conform to this without reading winter's source.

For operating the `winter service` command, see [../usage/service.md](../usage/service.md); for binding a provider into
a workspace, see [../configuration/capabilities.md](../configuration/capabilities.md).

This contract is **validated against** the machine-readable spec at
`tools/winter-cli/src/winter_cli/modules/capability/specs/service-v1.toml` — that file is the single source of truth for
the action set, exit codes, and env vars an orchestrator must implement. An orchestrator can self-check its conformance
at any time with:

```bash
winter ext verify <path-to-extension-dir>
```

See [../usage/ext.md](../usage/ext.md) for the full `winter ext verify` reference (check kinds, exit codes, `--json`
contract, version compatibility) and `winter ext new` for scaffolding a stub that passes verification out of the box.

## Uniform invocation rule

winter invokes the entrypoint differently depending on the action:

```text
<entrypoint> <action>                         # describe
<entrypoint> <action> <scope>                 # up, down (per matched scope, no service filter)
<entrypoint> <action> <scope>/<svc-pattern>   # up, down (per matched scope, real service filter)
<entrypoint> <action> [<pattern>...]          # status, restart, logs
```

`<action>` is one of `up`, `down`, `status`, `restart`, `logs`. For `up` and `down`, one-or-more `<env>/<service>` glob
patterns are passed as positional argv on the winter CLI (`winter service up <pattern...>`, at least one required —
mirroring `restart`/`logs`); winter enumerates the matched scopes itself (the same registry-driven, `describe`-aware
enumeration `status` uses — see [usage/service.md](../usage/service.md#up--down-fan-out-multi-provider)) and invokes the
entrypoint **once per matched (provider, scope) cell** with a single positional: the bare `<scope>` (the feature-env
name, e.g. `alpha`, or the reserved scope `workspace`) when that scope carries no service-segment filter, or the
scope-qualified `<scope>/<svc-pattern>` when the user supplied a real filter for that scope. Dispatching the bare
`<scope>` for the no-filter case keeps existing bare-env-only providers working unchanged for multi-env `up`/`down`; a
real service-segment filter (`alpha/api`) requires provider support to expand it and start/stop only the matched
services within the scope. For `status`, `restart`, and `logs`, zero-or-more `<env>/<service>` glob patterns are passed
as positional argv (including `workspace` and `workspace/<service>` patterns) — **patterns are forwarded verbatim** from
the user's command line; winter never expands them before dispatch. The orchestrator owns the catalog and is responsible
for expanding them.

**Patterns are raw user tokens on argv.** There is no `--` guard between the action and the patterns, so an orchestrator
must tolerate a pattern that begins with `-`. (In practice, valid `<env>/<service>` patterns never start with `-`, but a
robust implementation should not assume this.) Note: at the winter CLI boundary, Click rejects a bare `-`-leading token
as an unknown option (exit 2); pass it after `--` (e.g. `winter service restart -- -weird`) so Click treats it as a
positional. Winter then forwards the token verbatim to the orchestrator — without a `--` guard — so the orchestrator
still receives the raw token and must tolerate it.

An implementation **must accept all seven action words** even if only to refuse one it does not implement: for an
unsupported action it should exit non-zero with a message, which winter passes through.

The `describe` action takes no positionals and is called by winter internally when multiple providers are bound. It
returns a JSON object listing the service names owned by this provider.

The `catalog` action takes no positionals and is called by winter internally during `winter lint` to build the merged
service catalog for the `required-services` lint check. It returns a JSON object listing every scope-qualified service
name declared by this provider. See [`catalog` wire contract](#catalog-wire-contract) below.

## Always-present environment variables

Every dispatch — regardless of action — sets these variables and runs the entrypoint with **cwd at the workspace root**:

| Var                          | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_WORKSPACE_DIR`       | Absolute path to the workspace root.                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `WINTER_EXT_DIR`             | Absolute path to this orchestrator extension's clone (the dir containing `winter-ext.toml`).                                                                                                                                                                                                                                                                                                                                                                           |
| `WINTER_EXT_PREFIX`          | The resolved symlink prefix for this extension.                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `WINTER_EXT_CONFIG_DIR`      | Absolute path to this extension's writable config/asset directory (default `.winter/config/<repo-name>/`); the writable counterpart to the read-only `WINTER_EXT_DIR`. Set via `config_dir` in `[[standalone_repository]]`, or defaults to the workspace-relative `.winter/config/<name>/` path.                                                                                                                                                                       |
| `WINTER_SERVICE_PREFIX`      | The resolved workspace-level service-orchestration namespace prefix. Providers derive per-env resource names (tmux session names, docker compose project names, etc.) from it. Workspace-invariant — the same value at every scope — so unlike the scope vars below it is always present, on every action (including `restart`/`logs`/`describe`/`catalog`). See [configuration/config-files.md](../configuration/config-files.md) for the default and override story. |
| `WINTER_ENV`                 | The scope name — a feature-env name (e.g. `alpha`) or the reserved literal `workspace`. Injected on `up`/`down`/`status` only.                                                                                                                                                                                                                                                                                                                                         |
| `WINTER_ENV_INDEX`           | The allocated integer index for the scope (0 for workspace, 1-N for feature envs). Injected on `up`/`down`/`status` only.                                                                                                                                                                                                                                                                                                                                              |
| `WINTER_PORT_BASE`           | Port-band start for this scope: `base_port + WINTER_ENV_INDEX * ports_per_env`. Injected on `up`/`down`/`status` for a feature env; deliberately never emitted at all for the `workspace` scope — see `WINTER_WORKSPACE_PORT_BASE` below for that case.                                                                                                                                                                                                                |
| `WINTER_WORKSPACE_PORT_BASE` | Port-band start for index 0 (the workspace scope); equals `WINTER_PORT_BASE` when scope is `workspace`. Injected on `up`/`down`/`status` only.                                                                                                                                                                                                                                                                                                                         |

Plus the scope's computed env-band entries from `.winter/config.toml` — computed by `EnvProvisionerService` and
`EnvBandResolverService` and injected into the provider process on `up`/`down`/`status` alongside the scope vars above.
A [command-valued band entry](../configuration/command-env-entries.md) only actually runs on `up`; `down` and `status`
compute the same scope with command resolution gated off, so for those two actions a command entry's own declared key
renders as the gate's placeholder rather than its real value — see
[configuration/command-env-entries.md#the-gate](../configuration/command-env-entries.md#the-gate) for exactly which keys
that covers and which are excluded. See
[configuration/ports-and-environments.md](../configuration/ports-and-environments.md#env-var-bands) for which bands
apply to a scope, their ordering, and collision rules. The full set is inspectable via `winter env <scope>` (add
`--resolve` to see a command entry's real value, matching what `up` injects). `up`/`down` and the `status` matrix inject
the scope env for the target scope; `restart` and `logs` forward patterns verbatim and **do not inject scope vars** into
the provider process (only the five base extension vars) — a tmux-style provider's services obtain their scope env by
sourcing `winter env <scope> --resolve` in their own runtime (plain `winter env <scope>`, without the flag, masks
command-derived values — see [command-env-entries.md#the-gate](../configuration/command-env-entries.md#the-gate)), and
docker restart/logs operate on already-provisioned resources.

**Band-error collapse.** A band error is not limited to swallowing the one key it names: if computing the scope's env
raises at all for `down` or `status` — an undefined reference to a key a gated-off command entry only *imports* (see
[configuration/command-env-entries.md#the-gate](../configuration/command-env-entries.md#the-gate)) is the case that most
commonly triggers this, but any band error does — `EnvProvisionerService.compute` raises, `provision_scope_env` catches
it, and the scope's *entire* computed env collapses to `{}` for that call, not just the offending key. That drops
`WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, and `WINTER_SERVICE_PREFIX` along
with every band value, so a provider invoked for `down` in that state cannot identify the scope's own services by name
or port — not merely a missing secret.

The first five form the winter base extension contract, set uniformly by
`core/extension_invocation.py::build_extension_env`. They are defined for the hook/doctor/lint dispatches in
[configuration/extensions.md](../configuration/extensions.md#hook-env-var-contract); `winter service` provides them
identically. Working directory varies by surface: `winter service`, `doctor`, `lint`, and the `on_workspace_reconcile`
hook run at the workspace root, while the `on_env_*` hooks run at the env root.

## Per-action env var: `WINTER_SERVICE_MANIFEST`

On `up` only, winter injects one additional environment variable when any installed extension (or the workspace itself)
declares `[[service]]` blocks in its `winter-ext.toml`:

| Var                       | When set                                          | Meaning                                                                                                                                                       |
| ------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_SERVICE_MANIFEST` | `up` only, when extension-declared services exist | Absolute path to a temporary TOML file listing every extension-declared service definition aggregated from the workspace config and all installed extensions. |

The TOML file written to `WINTER_SERVICE_MANIFEST` contains an array of service entries:

```toml
[[service]]
name    = "worker"
scope   = "feature-environment"
source  = "my-extension"
cmd     = "python -m worker"
target  = "2.0"
# image and ports are optional; absent when not declared

[[service]]
name    = "postgres"
scope   = "workspace"
source  = "workspace"
cmd     = "pg_ctl start"
target  = "1.0"
```

Fields per entry:

| Field    | Always present | Meaning                                                                                                                                             |
| -------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`   | yes            | Service name (unique across all sources).                                                                                                           |
| `scope`  | yes            | `"feature-environment"` or `"workspace"`.                                                                                                           |
| `source` | yes            | Contributing source: `"workspace"` for workspace config, or the extension name.                                                                     |
| `cmd`    | no             | The shell command to run (omitted when not declared). `command` is accepted on input as a deprecated alias and will be removed in a future release. |
| `image`  | no             | Container image (docker-style providers).                                                                                                           |
| `target` | no             | Provider-specific routing target (e.g. tmux window.pane address `"2.0"`).                                                                           |
| `ports`  | no             | List of port numbers declared by the service.                                                                                                       |

**Consume or ignore rule:** a provider that understands `WINTER_SERVICE_MANIFEST` reads the file and merges the
extension-declared service definitions into its live session configuration. A provider that predates this contract or
does not implement it ignores the env var — the variable's presence is never an error. `down` never sets
`WINTER_SERVICE_MANIFEST`; providers MUST NOT depend on it during shutdown.

## Per-action env var: `WINTER_SERVICE_TIMEOUT`

On `up` only, winter injects the effective `winter service up --wait --timeout` value as a plain float string:

| Var                      | When set  | Meaning                                                                                                                                                                            |
| ------------------------ | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WINTER_SERVICE_TIMEOUT` | `up` only | The effective wait timeout in seconds (winter's `--timeout`, default `120.0` when not supplied), as a plain float string (e.g. `"120.0"`). Set whether or not `--wait` was passed. |

This lets a provider honour the caller's timeout in its own up-time readiness gate (e.g. a `depends_on` health wait)
without winter needing to expose a separate flag for it. `down` and `status` never set `WINTER_SERVICE_TIMEOUT`. A
provider that predates this contract or does not implement it ignores the env var — its presence is never an error.

## Per-action parameters

The always-present vars above — the five base-contract vars, including `WINTER_SERVICE_PREFIX` — are exported on every
dispatch. The scope vars (`WINTER_ENV`, `WINTER_ENV_INDEX`, `WINTER_PORT_BASE`, `WINTER_WORKSPACE_PORT_BASE`, and the
computed band entries) are additionally injected on `up`/`down`/`status`; `restart` and `logs` forward verbatim with
only the five base vars (services source their scope env via `winter env <scope>` at runtime). `WINTER_SERVICE_MANIFEST`
and `WINTER_SERVICE_TIMEOUT` are additionally set on `up` (see above). All action parameters travel on argv.

Service selection for `status`, `restart`, and `logs` is positional argv. The `logs` action additionally carries its
render options as CLI flags appended **after** the positional patterns, mirroring `winter service logs`' own surface:

```text
<entrypoint> logs <pattern...> [-f|--follow] [-n|--tail <N|all>] \
  [--since <rfc3339>] [--until <rfc3339>] [-t|--timestamps]
```

| Flag                     | Value                                                                                                                                                                                                                               |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-n` / `--tail <N\|all>` | Emitted **always**, carrying the resolved count string (`N` or `all`). The orchestrator SHOULD honour it; winter applies a backstop.                                                                                                |
| `--since <rfc3339>`      | RFC3339 absolute timestamp (pre-normalised from any duration). Omitted when empty.                                                                                                                                                  |
| `--until <rfc3339>`      | RFC3339 absolute timestamp. Omitted when empty.                                                                                                                                                                                     |
| `-f` / `--follow`        | Bare flag, emitted only when follow was requested (stream live vs. emit backlog and exit).                                                                                                                                          |
| `-t` / `--timestamps`    | Bare flag, emitted only when per-line timestamps were requested. A provider MAY always emit a `ts` field regardless (e.g. docker passes `--timestamps` unconditionally); winter's `-t` handling is the authoritative render toggle. |

For `up`, `down`, and `status`, no action-specific flags are set beyond the positional patterns. Specifically for
`status`: `--json` is a **winter-side render toggle only** — it is never propagated to the orchestrator as an env var or
an argv token. The orchestrator argv is byte-identical with and without `--json`: `[<entrypoint>, "status", *patterns]`.

## Wire contract (orchestrator stdout → winter)

### `logs` wire contract

The orchestrator's stdout for `logs` must be **NDJSON**, one event per line:

```json
{"ts":"2026-06-13T10:00:01Z","env":"alpha","svc":"api","msg":"listening"}
{"env":"alpha","svc":"worker","msg":"processing job 42"}
```

Fields:

- `env` (required) — the feature-environment the service belongs to.
- `svc` (required) — the originating service name.
- `msg` (required) — the log message.
- `ts` (optional) — RFC3339 timestamp; omit when the backend has no per-line timestamps (e.g. `tmux capture-pane`).

The orchestrator's **stderr must reach winter's stderr** (diagnostics), NOT be merged into the NDJSON stdout. The
orchestrator MAY pre-filter by the `--since`/`--until`/`--follow`/`--tail` argv flags server-side for efficiency; winter
applies idempotent backstops regardless.

### `status` wire contract

The orchestrator **must always emit a single schema-valid JSON status document on stdout** for the `status` action —
unconditionally, regardless of whether `--json` was set at the winter CLI (that flag never reaches the orchestrator).
Winter captures the full stdout, parses the document, applies the backstop filter, and renders to the user.

The document schema (env-keyed):

```json
{
  "envs": [
    {
      "env": "alpha",
      "session": "mp-alpha",
      "port_base": 4020,
      "services": [
        {
          "name": "api",
          "state": "running",
          "health": "healthy",
          "ports": [7503],
          "handle": "<tmux pane or container id>",
          "log_path": "/abs/path/to/api.log",
          "since": "2026-06-19T10:00:00Z"
        }
      ]
    }
  ]
}
```

Allowed enum values:

- `state`: `"running"` | `"stopped"` | `"unknown"`
- `health`: `"healthy"` | `"unhealthy"` | `"unknown"`

Null / empty conventions:

- `session`, `handle`, `log_path`, `since` — use JSON `null` when the value is unknown or unavailable.
- `port_base` — use JSON `null` when unknown.
- `ports` — use `[]` (empty array) when no ports are known.

**Shape-stability rule:** every field listed above is always present in the emitted document. Consumers rely on shape
stability, never on field omission. When an enum value is not recognised, emit the literal string `"unknown"`. When a
scalar value is not available, emit `null`. `ports` is always a list, never `null`.

An empty `{"envs": []}` is a valid, non-error document (no services currently visible).

The orchestrator's **stderr must reach winter's stderr** (diagnostics), NOT be merged into the JSON stdout — mirroring
the `logs` contract.

### `describe` wire contract

The orchestrator **must emit a single JSON object on stdout** for the `describe` action:

```json
{"services": ["workspace/db", "*/api", "*/worker"]}
```

`services` is the list of **scope-qualified** service identifiers this provider owns, using the same scope-prefix
convention as the `catalog` contract below (`workspace/<name>` for a workspace-scoped singleton, `*/<name>` for a
per-feature-env service). Unknown or empty → `{"services": []}`. Winter uses this to build a describe-identifier →
provider ownership index when multiple providers are bound (`capabilities.service = [...]`, or two or more
self-registered candidates), and `logs`/`restart` routing matches a user's `<env>/<svc>` selection pattern against these
identifiers segment-wise (see [usage/service.md](../usage/service.md)). Emitting a bare, unqualified name breaks that
matching for workspace-scoped services — a bare `db` is treated as env-agnostic (`*/db`) and never routed to the
`workspace` scope. Missing or non-list `services` key is treated as empty (shape-stability).

`describe` is called **only when two or more providers are bound** (an explicit `capabilities.service` list of 2+, or 2+
self-registered candidates with no explicit binding). Single-provider workspaces are never asked to `describe`.

### `catalog` wire contract

The orchestrator **must emit a single JSON object on stdout** for the `catalog` action:

```json
{"services": ["workspace/postgres", "*/api", "*/worker"]}
```

`services` is the list of scope-qualified service names declared by this provider. Scope prefixes:

- `workspace/<name>` — the service runs in the shared workspace scope.
- `*/<name>` — the service runs per feature env (any env name matches).

Unknown or empty → `{"services": []}`. Missing or non-list `services` key is treated as empty (shape-stability). Names
using any other prefix form are silently ignored for forward compatibility.

`catalog` is called by winter during `winter lint` (once per bound provider) to build the merged service catalog used by
the `required-services` lint check. If a provider exits non-zero or emits malformed JSON, winter silently omits that
provider's names from the catalog — causing `required_services` references to those services to be flagged as unknown by
the lint check. Implement `catalog` on every provider that declares services.

## Render contract (winter stdout → user/pipe)

### `logs` render contract

Winter parses the NDJSON and writes plain lines to its own stdout: `[<ts> ][<env>/<svc> | ]<msg>`.

- Prefix `<env>/<svc> |` **unless the selection is a single literal `<env>/<service>` pattern** — i.e., exactly one
  pattern that contains `/` and has no glob metacharacter (`*`, `?`, or `[`). Everything else (a bare `<env>`, a
  wildcard, multiple patterns, or no patterns) is multi-scope and gets the prefix so merged output stays attributable.
- Under `-t`/`--timestamps`, prepend the RFC3339 timestamp; lines without a `ts` field are rendered without a timestamp
  prefix (with one stderr warning emitted).
- Lines that are not valid JSON are treated leniently: the whole raw line becomes `msg` with no `env`, `svc`, or `ts`.
- winter's own warnings and diagnostics go to stderr. This plain-line stdout is what makes
  `winter service logs alpha | grep ERROR | less` portable across orchestrators.

### `status` render contract

Winter captures the orchestrator's status stdout, parses the JSON document, applies the segment-aware backstop service
filter (same pattern matcher used for `logs` and `winter ws` PATTERNS), and renders the result:

- **Default (human table):** a per-env section is printed for each env in the filtered document. Each section opens with
  a bold header showing `<env>  session=<session>  port_base=<port_base>` (values replaced with `-` when `null`),
  followed by a table with columns **SERVICE, STATE, HEALTH, PORTS, SINCE, HANDLE**. `state` and `health` values are
  styled by colour (running/healthy = green, stopped/unhealthy = red, unknown = dim). `ports` is comma-separated; empty
  becomes `-`. `since` and `handle` are `null`-coalesced to `-`. `log_path` is available only via `--json` and is not
  shown in the human table. When the filtered document has no envs, winter prints `no services`. For debugging,
  `log_path` (and all per-field data) is accessible via `winter service status --json`; pair with `winter service logs`
  to read the log content directly.
- **`--json`:** winter re-serialises the parsed-and-filtered `StatusDocument` to canonical JSON (`json.dumps` with
  `indent=2`) and writes it to stdout, and nothing else. The key order matches the schema above. This is a
  re-serialisation of the parsed model, not a raw passthrough of the orchestrator's bytes.

**Graceful degradation:** if the orchestrator's stdout cannot be parsed as a conformant status document (bad JSON,
top-level value is not a dict, or the `envs` key is missing or not a list), winter writes a clear actionable error to
stderr and exits non-zero. The error names the failing orchestrator entrypoint and prefix and instructs the operator to
ensure the extension is up to date. No stack trace is printed.

## Idempotent backstop filters (winter-side)

Winter applies these backstops even when the orchestrator has pre-filtered, ensuring the user-facing contract holds
regardless of orchestrator quality:

- **Service filter (logs):** each line's `env` and `svc` fields are joined as `<env>/<svc>` and matched against the
  requested patterns using the same segment-aware matcher that `winter ws` PATTERNS uses; if no pattern matches, the
  line is dropped. Lines missing the `env` or `svc` field are dropped when a filter is active (i.e., when patterns are
  present).
- **Service filter (status):** when patterns are present, each service in the parsed `StatusDocument` is tested via the
  same segment-aware matcher (`<env>/<service-name>`); services that do not match any pattern are dropped. Envs whose
  service list becomes empty after filtering are dropped entirely. When no patterns are given, the document is returned
  unchanged.
- **Time filter — `(logs)` only (`--since`/`--until`):** applied per-line only to lines that have a parseable `ts`. The
  boundary is **inclusive**: a line whose `ts` exactly equals the `--since` or `--until` threshold is kept. Lines
  without `ts` are always kept (winter cannot time-filter them). If `--since`/`--until` was requested AND at least one
  line lacked a `ts`, winter emits one stderr warning that the time filter is partial.
- **Timestamps — `(logs)` only (`-t`):** if requested but a line has no `ts`, the timestamp prefix is omitted for that
  line; winter emits one stderr warning.
- **Tail backstop — `(logs)` only:** in **non-follow** mode, winter keeps a ring buffer (last N lines) and emits only
  those after the stream ends. In **follow** mode (`-f`), winter does NOT re-tail — it relays lines live and relies on
  the orchestrator having honoured the `--tail` flag. This is an intentional limitation: winter cannot distinguish
  backlog from live output during a follow session.

## Exit codes

- **0** — success.
- **2** — unknown or unsupported action (Click owns flag-parse errors at this code too, before dispatch).
- **3** — recognized-but-unimplemented action: the orchestrator knows the action but refuses to execute it (refuse-all
  stub). Both 2 and 3 are accepted by `winter ext verify` as valid refusal codes.
- **Other** — the orchestrator's exit code becomes `winter`'s exit code unmodified.
- `-f` interrupted by Ctrl-C: exit 130 (and the child receives the signal naturally).
- **`status` conformance failure:** if the orchestrator's stdout is not a conformant status document, winter exits
  non-zero even if the orchestrator itself exited 0. The rule: the orchestrator's own non-zero exit code wins; if the
  orchestrator exited 0, winter synthesises exit code 1. The actionable error is written to stderr.

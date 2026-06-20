# `winter service` — service orchestration

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
winter service up alpha                               # start env alpha's services (also ensures workspace scope is up first)
winter service up workspace                           # bring up only the workspace scope
winter service down alpha                             # stop env alpha's services (leaves workspace scope running)
winter service down workspace                         # tear down the workspace scope explicitly
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

`winter service` owns a stable `up`/`down`/`status`/`restart`/`logs` interface and dispatches each invocation to the orchestrator(s) the workspace registers. Consumers depend on `winter service …` and never on the orchestrator's implementation, so a workspace can swap tmux for containers or a supervising daemon without re-teaching agents, docs, or habits.

`status`, `restart`, and `logs` use **segment-aware glob PATTERNS** over `<env>/<service>` — the same vocabulary `winter ws` uses for `<env>/<repo>` (see [ws/patterns.md](./ws/patterns.md)). Within each segment, `*`, `?`, and `[...]` match as usual; `*` does not cross `/`. A bare `<env>` (no slash) expands to `<env>/*`. Multiple patterns can be passed in one invocation. Cross-environment selection is supported: `'*/backend'` selects the `backend` service across every env. `up` and `down` always operate on a whole env (no pattern syntax) — or on `workspace` (see below). For `restart` and `logs`, at least one pattern is required (action commands require an explicit target — no implicit "everything", mirroring `winter ws merge` requiring a source ref). For `status`, omitting all patterns selects every service in every env (read-shaped, defaults to all like `winter ws status`).

## Workspace scope

`workspace` is a **reserved, universal service target** accepted by all five actions: `up`, `down`, `status`, `restart`, `logs`. The orchestrator extension owns what the `workspace` scope means (services that should run once across the whole workspace, shared daemons, etc.).

`workspace` slots into each action's existing grammar in the same place an env name does, so it follows that action's arity — there is no new syntax:

- For `up`/`down` (whole-env arity, no patterns) `workspace` is the literal target argument: `up workspace`, `down workspace`. There is **no** `up workspace/<service>` form — `up`/`down` take no pattern.
- For `status`/`restart`/`logs` (PATTERN arity) `workspace` is an env segment in the normal `<env>/<service>` grammar: bare `workspace` expands to `workspace/*` (per the bare-`<env>` rule above), and `workspace/<service>` selects one service within the scope.

### Lifecycle policy

`up <env>` (any named env) **ensures the workspace scope is up first**, then brings up the env. Both dispatches are attempted regardless of whether the workspace-up step succeeds — best-effort: each failure is surfaced as feedback and the command exits non-zero if either step failed, but the env-up is never skipped. No reference counting is involved: the workspace scope is treated as infrastructure that should be running.

`down <env>` **leaves the workspace scope running**. Tearing down a feature environment does not affect services that are shared across the workspace.

`down workspace` is the **only path that tears down the workspace scope** — it must be done explicitly.

`up workspace` brings up only the workspace scope (no recursion into envs).

For `status`/`restart`/`logs`, `workspace` patterns are forwarded verbatim to the orchestrator like any other `<env>/<service>` selection.

`workspace` is also a **reserved feature-environment name**: `winter ws init workspace` is rejected with an error. See [ws/init.md](./ws/init.md).

### Registering orchestrator(s)

A workspace can bind **one or more** service providers through the capability registry. The simplest form binds a single provider — for multi-provider workspaces, use the ordered list.

**Single provider** — `capabilities.service = "<name>"` in the `[capabilities]` table of `.winter/config.toml`. When exactly one installed extension declares `provides.service`, the binding is optional (implicit sole-provider). When two or more extensions declare `provides.service` with no explicit binding, all are bound implicitly (implicit-all) — see [setup.md#capability-registry](../setup.md#capability-registry).

**Multiple providers (ordered list)** — `capabilities.service = ["<name-1>", "<name-2>"]` in the `[capabilities]` table of `.winter/config.toml`. The list order is deterministic for stable output only — no dependency or startup-ordering semantics are implied. A single-entry list is equivalent to a single-provider binding and never triggers a `describe` call.

**Back-compat:** the legacy single-string keys `service_orchestrator` (config) and `orchestrate_services` (manifest) are still accepted — see [setup.md#deprecated-keys](../setup.md#deprecated-keys) for the normalisation semantics. New workspaces should use `[capabilities].service`.

See [setup.md#capability-registry](../setup.md#capability-registry) for the full resolution model and [capabilities.md](./capabilities.md) to introspect the current binding.

### Adding a second provider

Shipping `provides.service` in a new extension's `winter-ext.toml` auto-binds it alongside any existing provider with no further config change — the implicit-all rule kicks in. Each participating provider **must implement `describe`** (a provider that does not is rejected at index-build time when a targeted `logs` or `restart` is issued). After adding or updating a provider, verify conformance:

```bash
winter ext verify <path-to-extension-dir>
```

This checks that the extension implements every action required by the bundled service spec (including `describe`). Run it against each installed provider after changing the service contract. See [ext.md](./ext.md) for the full `winter ext verify` reference.

### `up` / `down` fan-out (multi-provider)

With a single provider, `up` and `down` are forwarded directly to that provider — no index, no polling.

With multiple providers:

- **`up` — forward fan-out.** Providers are started in a deterministic order (the order declared in `capabilities.service`). If a provider's `up` exits non-zero, the fan-out aborts and the remaining providers are not started. The first non-zero exit code is returned. No readiness polling occurs between providers.
- **`down` — best-effort fan-out.** Providers are stopped in the same deterministic order. A non-zero exit from one provider is noted but does not prevent the others from being called. The first non-zero exit code is returned; 0 is returned if all providers succeeded.

### Service→provider ownership (multi-provider)

When two or more providers are bound, winter builds a **service→provider ownership index** before routing `logs` and `restart`. It does this by calling each provider's `describe` action (see [describe wire contract](#describe-wire-contract) below). If two providers claim the same service name, winter aborts with a `DuplicateOwnershipError` naming the service and both providers — each service must be owned by exactly one provider.

With a single provider, `describe` is never called — the sole provider implicitly owns every service.

### `logs` and `restart` routing (multi-provider)

`logs` and `restart` are **routed to the owning provider** for each matched service via the ownership index. With a single provider, patterns are forwarded verbatim without building an index.

`logs -f` (follow mode) is supported only when the matched services resolve to **a single owning provider**. If the selection would span multiple providers, winter writes an actionable error to stderr and returns 1 without opening any stream. Non-follow `logs` works across all providers (the streams are merged into a single output).

### `status` (multi-provider)

With multiple providers, each provider's `status` output is independently fetched, parsed, and **merged** into a single `StatusDocument` before filtering and rendering. A provider whose output cannot be parsed surfaces an actionable error naming that provider; the worst exit code across all providers is adopted.

## Local-path override

Use the `--service-orchestrator` root flag or the `WINTER_SERVICE_ORCHESTRATOR` environment variable to point `winter service` at a **local extension directory** for a single invocation — without changing `.winter/config.toml` or reinstalling anything. This is the primary way to test an in-progress orchestrator whose installed copy still lags the worktree:

```bash
# flag form (highest precedence)
winter --service-orchestrator=alpha/winter-service-tmux service up alpha

# env-var form
WINTER_SERVICE_ORCHESTRATOR=alpha/winter-service-tmux winter service status
```

**Precedence (highest wins):** `--service-orchestrator` flag → `WINTER_SERVICE_ORCHESTRATOR` env var → `capabilities.service` / `service_orchestrator` key in `.winter/config.toml`.

**Scope with multiple providers:** the override flag collapses fan-out to a single provider for that invocation — the configured providers are ignored and dispatch goes to the override target only.

**Path-vs-name disambiguation:**
- If the value contains an OS path separator (`/` on POSIX) or resolves to an existing directory → **path mode**: reads `winter-ext.toml` from that directory directly, skipping the config-key-present and matches-an-installed-extension checks. The directory must still declare a `service` entrypoint (`provides.service`, or the legacy `orchestrate_services`) in its `winter-ext.toml`, and that entrypoint file must exist on disk.
- Otherwise (bare name like `winter-service-tmux`) → **name mode**: falls through to the normal registered-extension lookup, same as the config key.

**Doctor note:** `winter doctor` reflects the *installed* extension (not the override target), so during an override window, warnings about a lagging or mismatched installed extension are expected and can be ignored.

**Scope:** the override affects **dispatch only** — `WINTER_EXT_DIR`/`WINTER_EXT_PREFIX` are set from the resolved local directory, and the entrypoint is invoked from it. It does NOT affect `winter-service-tmux:` path-notation used in agent docs, nor `@`-loaded markdown references. The override is per-invocation only; it is not persisted.

**Typical use:** during orchestrator development, when the workspace's installed extension copy is behind your working branch:

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

This section is **validated against** the machine-readable spec at
`tools/winter-cli/src/winter_cli/modules/capability/specs/service-v1.toml` — that file is the single
source of truth for the action set, exit codes, and env vars an orchestrator must
implement.  An orchestrator can self-check its conformance at any time with:

```bash
winter ext verify <path-to-extension-dir>
```

See [ext.md](./ext.md) for the full `winter ext verify` reference (check kinds, exit codes, `--json` contract, version compatibility) and `winter ext new` for scaffolding a stub that passes verification out of the box.

This is the full spec a service-orchestrator extension is written against — conform to it without reading winter's source.

### Uniform invocation rule

winter invokes the entrypoint differently depending on the action:

```
<entrypoint> <action>                         # describe
<entrypoint> <action> <env>                   # up, down
<entrypoint> <action> [<pattern>...]          # status, restart, logs
```

`<action>` is one of `up`, `down`, `status`, `restart`, `logs`. For `up` and `down`, `<env>` is the feature-env name (`alpha`, `beta`, …) or the reserved scope `workspace`. For `status`, `restart`, and `logs`, zero-or-more `<env>/<service>` glob patterns are passed as positional argv (including `workspace` and `workspace/<service>` patterns) — **patterns are forwarded verbatim** from the user's command line; winter never expands them before dispatch. The orchestrator owns the catalog and is responsible for expanding them.

**Patterns are raw user tokens on argv.** There is no `--` guard between the action and the patterns, so an orchestrator must tolerate a pattern that begins with `-`. (In practice, valid `<env>/<service>` patterns never start with `-`, but a robust implementation should not assume this.) Note: at the winter CLI boundary, Click rejects a bare `-`-leading token as an unknown option (exit 2); pass it after `--` (e.g. `winter service restart -- -weird`) so Click treats it as a positional. Winter then forwards the token verbatim to the orchestrator — without a `--` guard — so the orchestrator still receives the raw token and must tolerate it.

An implementation **must accept all six action words** even if only to refuse one it does not implement: for an unsupported action it should exit non-zero with a message, which winter passes through.

The `describe` action takes no positionals and is called by winter internally when multiple providers are bound.  It returns a JSON object listing the service names owned by this provider.

### Always-present environment variables

Every dispatch — regardless of action — sets these three variables and runs the entrypoint with **cwd at the workspace root**:

| Var | Meaning |
|-----|---------|
| `WINTER_WORKSPACE_DIR` | Absolute path to the workspace root. |
| `WINTER_EXT_DIR` | Absolute path to this orchestrator extension's clone (the dir containing `winter-ext.toml`). |
| `WINTER_EXT_PREFIX` | The resolved symlink prefix for this extension. |

These three are winter's shared extension-subprocess context, defined for the hook/doctor/lint dispatches in [setup.md](../setup.md#hook-env-var-contract); `winter service` provides them identically. Working directory varies by surface: `winter service`, `doctor`, `lint`, and the `on_workspace_reconcile` hook run at the workspace root, while the `on_env_*` hooks run at the env root.

### Per-action environment variables

Service selection for `status`, `restart`, and `logs` is on argv (not env vars). The only per-action env vars set by winter are the `logs` render options:

| Action | Env var | Value |
|--------|---------|-------|
| `logs` | `WINTER_LOG_FOLLOW` | `1` = stream live, `0` = emit backlog and exit. |
| `logs` | `WINTER_LOG_TAIL` | Positive integer or `all`. The orchestrator SHOULD honour this; winter applies a backstop. |
| `logs` | `WINTER_LOG_SINCE` | RFC3339 absolute timestamp (pre-normalised from any duration); empty if unset. |
| `logs` | `WINTER_LOG_UNTIL` | RFC3339 absolute timestamp; empty if unset. |
| `logs` | `WINTER_LOG_TIMESTAMPS` | `1` = per-line timestamps requested; `0` = not requested. |

For `up`, `down`, and `status`, no action-specific env vars are set beyond the always-present three above. Specifically for `status`: `--json` is a **winter-side render toggle only** — it is never propagated to the orchestrator as an env var or an argv token. The orchestrator argv is byte-identical with and without `--json`: `[<entrypoint>, "status", *patterns]`.

### Wire contract (orchestrator stdout → winter)

#### `logs` wire contract

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

The orchestrator's **stderr must reach winter's stderr** (diagnostics), NOT be merged into the NDJSON stdout. The orchestrator MAY pre-filter by `WINTER_LOG_SINCE`/`UNTIL`/`FOLLOW`/`TAIL` server-side for efficiency; winter applies idempotent backstops regardless.

#### `status` wire contract

The orchestrator **must always emit a single schema-valid JSON status document on stdout** for the `status` action — unconditionally, regardless of whether `--json` was set at the winter CLI (that flag never reaches the orchestrator). Winter captures the full stdout, parses the document, applies the backstop filter, and renders to the user.

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

**Shape-stability rule:** every field listed above is always present in the emitted document. Consumers rely on shape stability, never on field omission. When an enum value is not recognised, emit the literal string `"unknown"`. When a scalar value is not available, emit `null`. `ports` is always a list, never `null`.

An empty `{"envs": []}` is a valid, non-error document (no services currently visible).

The orchestrator's **stderr must reach winter's stderr** (diagnostics), NOT be merged into the JSON stdout — mirroring the `logs` contract.

#### `describe` wire contract

The orchestrator **must emit a single JSON object on stdout** for the `describe` action:

```json
{"services": ["api", "worker", "frontend"]}
```

`services` is the list of service names this provider owns. Unknown or empty → `{"services": []}`. Winter uses this to build a service-name → provider ownership index when multiple providers are bound (`capabilities.service = [...]`, or two or more self-registered candidates). Missing or non-list `services` key is treated as empty (shape-stability).

`describe` is called **only when two or more providers are bound** (an explicit `capabilities.service` list of 2+, or 2+ self-registered candidates with no explicit binding). Single-provider workspaces are never asked to `describe`.

### Render contract (winter stdout → user/pipe)

#### `logs` render contract

Winter parses the NDJSON and writes plain lines to its own stdout: `[<ts> ][<env>/<svc> | ]<msg>`.

- Prefix `<env>/<svc> | ` **unless the selection is a single literal `<env>/<service>` pattern** — i.e., exactly one pattern that contains `/` and has no glob metacharacter (`*`, `?`, or `[`). Everything else (a bare `<env>`, a wildcard, multiple patterns, or no patterns) is multi-scope and gets the prefix so merged output stays attributable.
- Under `-t`/`--timestamps`, prepend the RFC3339 timestamp; lines without a `ts` field are rendered without a timestamp prefix (with one stderr warning emitted).
- Lines that are not valid JSON are treated leniently: the whole raw line becomes `msg` with no `env`, `svc`, or `ts`.
- winter's own warnings and diagnostics go to stderr. This plain-line stdout is what makes `winter service logs alpha | grep ERROR | less` portable across orchestrators.

#### `status` render contract

Winter captures the orchestrator's status stdout, parses the JSON document, applies the segment-aware backstop service filter (same pattern matcher used for `logs` and `winter ws` PATTERNS), and renders the result:

- **Default (human table):** a per-env section is printed for each env in the filtered document. Each section opens with a bold header showing `<env>  session=<session>  port_base=<port_base>` (values replaced with `-` when `null`), followed by a table with columns **SERVICE, STATE, HEALTH, PORTS, SINCE, HANDLE**. `state` and `health` values are styled by colour (running/healthy = green, stopped/unhealthy = red, unknown = dim). `ports` is comma-separated; empty becomes `-`. `since` and `handle` are `null`-coalesced to `-`. `log_path` is available only via `--json` and is not shown in the human table. When the filtered document has no envs, winter prints `no services`. For debugging, `log_path` (and all per-field data) is accessible via `winter service status --json`; pair with `winter service logs` to read the log content directly.
- **`--json`:** winter re-serialises the parsed-and-filtered `StatusDocument` to canonical JSON (`json.dumps` with `indent=2`) and writes it to stdout, and nothing else. The key order matches the schema above. This is a re-serialisation of the parsed model, not a raw passthrough of the orchestrator's bytes.

**Graceful degradation:** if the orchestrator's stdout cannot be parsed as a conformant status document (bad JSON, top-level value is not a dict, or the `envs` key is missing or not a list), winter writes a clear actionable error to stderr and exits non-zero. The error names the failing orchestrator entrypoint and prefix and instructs the operator to ensure the extension is up to date. No stack trace is printed.

### Idempotent backstop filters (winter-side)

Winter applies these backstops even when the orchestrator has pre-filtered, ensuring the user-facing contract holds regardless of orchestrator quality:

- **Service filter (logs):** each line's `env` and `svc` fields are joined as `<env>/<svc>` and matched against the requested patterns using the same segment-aware matcher that `winter ws` PATTERNS uses; if no pattern matches, the line is dropped. Lines missing the `env` or `svc` field are dropped when a filter is active (i.e., when patterns are present).
- **Service filter (status):** when patterns are present, each service in the parsed `StatusDocument` is tested via the same segment-aware matcher (`<env>/<service-name>`); services that do not match any pattern are dropped. Envs whose service list becomes empty after filtering are dropped entirely. When no patterns are given, the document is returned unchanged.
- **Time filter — `(logs)` only (`--since`/`--until`):** applied per-line only to lines that have a parseable `ts`. The boundary is **inclusive**: a line whose `ts` exactly equals the `--since` or `--until` threshold is kept. Lines without `ts` are always kept (winter cannot time-filter them). If `--since`/`--until` was requested AND at least one line lacked a `ts`, winter emits one stderr warning that the time filter is partial.
- **Timestamps — `(logs)` only (`-t`):** if requested but a line has no `ts`, the timestamp prefix is omitted for that line; winter emits one stderr warning.
- **Tail backstop — `(logs)` only:** in **non-follow** mode, winter keeps a ring buffer (last N lines) and emits only those after the stream ends. In **follow** mode (`-f`), winter does NOT re-tail — it relays lines live and relies on the orchestrator having honoured `WINTER_LOG_TAIL`. This is an intentional limitation: winter cannot distinguish backlog from live output during a follow session.

### Exit codes

- **0** — success.
- **2** — unknown or unsupported action (Click owns flag-parse errors at this code too, before dispatch).
- **3** — recognized-but-unimplemented action: the orchestrator knows the action but refuses to execute it (refuse-all stub). Both 2 and 3 are accepted by `winter ext verify` as valid refusal codes.
- **Other** — the orchestrator's exit code becomes `winter`'s exit code unmodified.
- `-f` interrupted by Ctrl-C: exit 130 (and the child receives the signal naturally).
- **`status` conformance failure:** if the orchestrator's stdout is not a conformant status document, winter exits non-zero even if the orchestrator itself exited 0. The rule: the orchestrator's own non-zero exit code wins; if the orchestrator exited 0, winter synthesises exit code 1. The actionable error is written to stderr.

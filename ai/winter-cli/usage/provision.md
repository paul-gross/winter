# `winter provision` — environment readiness

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
# Full chain — runs dependency → resource → data in order
winter provision alpha

# Sub-targets — run one stage only
winter provision alpha dependency             # install/check dependencies
winter provision alpha resource               # create resources (databases, message-queue vhosts, buckets)
winter provision alpha data                   # load baseline state (idempotent)

# Action flags — always require an explicit sub-target
winter provision alpha resource --reset       # destroy + recreate resources
winter provision alpha resource --destroy     # destroy resources only
winter provision alpha resource --seed        # create resources, then run data
winter provision alpha data --reset           # destroy + recreate data
winter provision alpha data --destroy         # delete data only

# Global flags
winter provision alpha --no-service-check     # skip the required_services check entirely
winter provision alpha --json                 # NDJSON event stream (see below)
winter provision alpha --dry-run              # print plan; no scripts run, no services started
winter provision alpha --dry-run --json       # structured plan as NDJSON (see below)
```

`winter provision` owns **feature-environment readiness** as a re-runnable lifecycle, decoupled from `winter ws init`. It reads `[[provision.*]]` handlers declared in the workspace config (`.winter/config.toml`) and in each installed extension's `winter-ext.toml`, and runs them in a defined order against the named env.

## Relationship to `winter ws init`

`winter ws init` is structural: it creates worktrees, branches, seeds `.winter.env`, copies git identity, writes excludes, and fires `on_env_init` hooks. It also runs each repo's `cmd` list — that list is now a lightweight trust/bootstrap step (e.g. `mise trust`, `direnv allow`) rather than full dependency installation.

Run `winter provision <env>` after `winter ws init` to bring the environment to a working state: install dependencies, provision resources, and load seed data. For project-specific readiness steps not yet migrated to `[[provision.*]]` handlers, also follow `workspace:/ai/project/project-setup.md`.

## Action vocabulary

Three action flags modify the default behaviour. They are shared across `resource` and `data` sub-targets (and validated accordingly):

| Invocation | Behaviour |
|------------|-----------|
| bare (no flag) | **apply** — run `apply` handler; idempotent to baseline. For `data`, apply is wipe-and-reload, not append. |
| `--destroy` | Run the declared `destroy` handler; if none declared, warn and no-op. |
| `--reset` | Use the declared `reset` handler if present; else compose destroy + apply when both exist; else warn and degrade to re-apply. |
| `resource --seed` | Apply `resource`, then apply `data`. |

Authors guarantee idempotency; winter tracks no state between runs.

**Flag validation:**
- `--reset` and `--destroy` together are rejected.
- `--seed` is valid only on `resource`, not on `dependency` or `data`.
- Any action flag (`--reset`, `--destroy`, `--seed`) requires an explicit sub-target — not the bare full-chain form.
- `--dry-run` may be combined with any action flag or sub-target: it previews what the given invocation would do.

## Manifest schema

Handlers are declared in both the workspace config and extension manifests using the same shape.

### Workspace config (`.winter/config.toml`)

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install-deps.sh"

[[provision.resource]]
scope            = "workspace"
apply            = "scripts/create-db.sh"
destroy          = "scripts/drop-db.sh"
required_services = ["workspace/postgres"]

[[provision.data]]
scope            = "feature-environment"
apply            = "scripts/seed.sh"
reset            = "scripts/reseed.sh"
required_services = ["workspace/postgres"]
```

### Extension manifest (`winter-ext.toml`)

Extensions declare the same shape under `[[provision.*]]` in their `winter-ext.toml`:

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "scripts/install.sh"

[[provision.resource]]
scope   = "workspace"
apply   = "scripts/create-db.sh"
destroy = "scripts/drop-db.sh"
```

### Per-entry fields

| Field | Required | Meaning |
|-------|----------|---------|
| `scope` | yes | Where the handler runs (see Scope and ordering below). One of `workspace`, `feature-environment`, `feature-worktree`. |
| `apply` | yes | Path to the script run by the bare (apply) action. Relative to the declaring directory (workspace root or extension root). |
| `destroy` | no | Path to the script run by `--destroy`. If absent, `--destroy` warns and no-ops. |
| `reset` | no | Path to the script run by `--reset`. If absent, winter composes destroy + apply when both exist; otherwise warns and degrades to re-apply. |
| `required_services` | no | Services that must be running before this handler executes (valid only on `resource` and `data` — rejected on `dependency`). See Service check below. |

**Sub-targets:** `dependency`, `resource`, `data`. Unknown sub-target keys (e.g. `[[provision.custom]]`) are rejected. Unknown per-entry keys are also rejected.

## Scope and ordering

### Sub-target order

When the bare `winter provision <env>` full-chain form is used, sub-targets run in this fixed order:

```
dependency → resource → data
```

A handler apply failure in any sub-target aborts the remaining sub-targets (failure is non-zero exit from the script).

A sub-target with no declared handlers is a no-op; provision reports that no handlers are declared for it.

### Handler order within a sub-target

Within a sub-target, handlers run substrate-first by scope, with workspace-config handlers before extension handlers within the same scope:

```
workspace (config) → workspace (extensions) →
feature-environment (config) → feature-environment (extensions) →
feature-worktree (config) → feature-worktree (extensions)
```

### Working directory by scope

| Scope | Working directory | Notes |
|-------|-------------------|-------|
| `workspace` | workspace root | `<workspace>/` |
| `feature-environment` | env root | `<workspace>/<env>/` |
| `feature-worktree` | per-repo worktree | `<workspace>/<env>/<repo>/` — runs ONCE PER PROJECT WORKTREE in the env |

### Environment variables

Handlers at `feature-environment` and `feature-worktree` scope receive the standard env-var trio:

| Var | Meaning |
|-----|---------|
| `WINTER_ENV` | The env name (`alpha`, `beta`, …) |
| `WINTER_ENV_INDEX` | The persisted port-offset index for this env |
| `WINTER_PORT_BASE` | `base_port + ports_per_env * WINTER_ENV_INDEX` |

`workspace`-scope handlers receive `WINTER_WORKSPACE_DIR` only (same contract as `on_workspace_reconcile` hooks — see [setup.md](../setup.md#hook-env-var-contract)).

## Service check (`required_services`)

When a `resource` or `data` handler declares `required_services`, winter checks those services are running before executing the handler.

A `required_services` token must be scoped as `workspace/<service>` or `<current-env>/<service>`. A foreign env reference (e.g. `beta/postgres` when provisioning `alpha`) is rejected.

**Without `--no-service-check`:**
- Each declared service is checked via `winter service status` (running-state, not health — health is observability-only).
- Any services that are not running are started by bringing up their owning scope: `winter service up workspace` or `winter service up <env>`.
- Started services are left running after provision completes.

**With `--no-service-check`:** the service check is skipped entirely. Use this when the service is known to be up or when running in an environment without a registered orchestrator.

**Missing orchestrator:** if `required_services` is declared but no service orchestrator is registered in the workspace, `winter provision` exits non-zero with a clean error message. Cross-link: see [service.md](./service.md) for the service contract, including how orchestrators are registered.

**Static lint counterpart:** `winter lint` runs a `required-services` core check that validates `required_services` entries against the merged service catalog from all bound providers — ahead of any provisioning run. The lint check validates catalog membership (does any provider declare this service name?) while `winter provision`'s runtime service check additionally enforces that the service is currently running in the correct env scope. Use both: lint catches typos and missing catalog entries before any env is provisioned; the runtime check ensures services are actually up before scripts execute. See [setup.md#built-in-core-checks](../setup.md#built-in-core-checks) for the `required-services` lint check description.

## `--dry-run`

`--dry-run` prints the ordered list of handlers that **would** run without executing any script or starting any service:

- Per-handler output: sub-target, scope, source, script path, resolved action (apply / destroy / reset), and which `required_services` it would check (if any).
- A sub-target with no declared handlers is reported as a no-op.
- No mutation occurs: no scripts run, no `winter service up` calls are made.
- `--dry-run` may be combined with any action flag (`--reset`, `--destroy`, `--seed`) or sub-target to preview that specific path.

`--dry-run --json` emits the same NDJSON stream as a real run (see below), replacing `execution_*` and `handler_result` events with `plan_handler` events — one per resolved action in plan order. (A handler with `--reset` that has no `reset` script but does have a `destroy` script emits two events: a `destroy` then an `apply`.)

## `--json` output

`--json` emits NDJSON, one JSON object per line. The event stream:

| `type` | When emitted | Key fields |
|--------|-------------|------------|
| `started` | Beginning of the run | `env`, `subtargets` (ordered list of sub-targets to run) |
| `subtarget_started` | Before each sub-target | `subtarget` |
| `no_handlers` | Sub-target has no declared handlers | `subtarget` |
| `execution_started` | Before each script invocation | `label`, `action`, `cwd` |
| `execution_output_line` | Each line from the script | `label`, `line` |
| `execution_completed` | Script finished | `label`, `action`, `exit_status` |
| `execution_error` | Script could not be launched | `label`, `error` |
| `handler_result` | Summary after a handler completes | `subtarget`, `scope`, `source`, `action`, `service_check`, `runs:[{cwd, exit_status}]`, `exit_status` |
| `handler_warn` | Degraded action (e.g. no destroy handler) | `subtarget`, `scope`, `source`, `message` |
| `finished` | End of the run | `status` (`"ok"` / `"aborted"` / `"error"`), `aborted_at` (sub-target name when aborted, else absent) |
| `plan_handler` | (`--dry-run` only) Handler that would run | `would_run: true`, `subtarget`, `scope`, `source`, `script`, `action`, `required_services`, `service_check_preview` |

**`plan_handler` fields** (emitted only with `--dry-run --json`):

| Field | Type | Meaning |
|-------|------|---------|
| `would_run` | `true` | Always `true`; distinguishes plan events from real-run events |
| `subtarget` | string | Sub-target name (`dependency`, `resource`, `data`) |
| `scope` | string | Handler scope (`workspace`, `feature-environment`, `feature-worktree`) |
| `source` | string | Declaring source (`project` or extension prefix) |
| `script` | string | Path to the script that would be invoked |
| `action` | string | Resolved action (`apply`, `destroy`, or `reset`) |
| `required_services` | list of strings | `required_services` tokens from the handler declaration |
| `service_check_preview` | string or null | Comma-separated owning scopes that would be checked/started; `null` when no `required_services` |

**`service_check` field values in `handler_result`:**

| Value | Meaning |
|-------|---------|
| `null` | No `required_services` declared for this handler |
| `"skipped"` | `--no-service-check` was passed |
| `"ok"` | All required services were already running |
| `"started:<scope>[,<scope>]"` | Winter started the listed owning scopes before running the handler |

## Doctor probe

`winter doctor` includes a built-in `[provision]` probe that validates every declared `[[provision.*]]` manifest entry — from both `.winter/config.toml` and installed extension `winter-ext.toml` files. It reports one finding per bad entry without aborting other doctor checks:

- `scope` is a known value (`workspace`, `feature-environment`, `feature-worktree`)
- `apply` is present
- `required_services` is only declared on `resource` or `data` (not `dependency`)
- No unknown keys are present

See [doctor.md](./doctor.md) for the full doctor probe contract.

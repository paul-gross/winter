# `winter provision` — environment readiness

For the hub and the rest of the command surface, see [../index.md](../index.md).

```bash
# Full chain — runs dependency → resource → data in order
winter provision alpha

# Multiple envs / glob — PATTERNS is a bare env-name glob, at least one required
winter provision alpha beta                   # full chain, two envs, deterministic order
winter provision 'feature-*'                  # full chain, every env matching the glob

# Sub-targets — run one stage only, via --stage (not a second positional)
winter provision alpha --stage dependency     # install/check dependencies
winter provision alpha --stage resource       # create resources (databases, message-queue vhosts, buckets)
winter provision alpha --stage data           # load baseline state (idempotent)

# Action flags — always require an explicit --stage (or a --name selector, below)
winter provision alpha --stage resource --reset       # destroy + recreate resources
winter provision alpha --stage resource --destroy     # destroy resources only
winter provision alpha --stage resource --seed        # create resources, then run data
winter provision alpha --stage data --reset           # destroy + recreate data
winter provision alpha --stage data --destroy         # delete data only

# Named resource selector — target one named entry instead of a whole stage
winter provision alpha --name workspace.mydb              # apply just that entry
winter provision alpha --name workspace.mydb --reset       # reset just that entry
winter provision alpha --name workspace.mydb --destroy     # destroy just that entry

# Global flags
winter provision alpha --no-service-check     # skip the required_services check entirely
winter provision alpha --json                 # NDJSON event stream (see below)
winter provision alpha --dry-run              # print plan; no commands run, no services started
winter provision alpha --dry-run --json       # structured plan as NDJSON (see below)
```

`winter provision` owns **feature-environment readiness** as a re-runnable lifecycle, decoupled from `winter ws init`.
It reads `[[provision.*]]` handlers declared in the workspace config (`.winter/config.toml`) and in each installed
extension's `winter-ext.toml`, and runs them in a defined order against every env `PATTERNS` matches.

`PATTERNS` is a **bare env-name glob** (see
[patterns.md](./ws/patterns.md#winter-provision--winter-clean--winter-ws-destroy--env-level-patterns) for the shared
grammar with `winter clean` and `winter ws destroy`) — a `/`-qualified pattern is rejected, since provision operates on
a whole env, not an `<env>/<repo>` worktree. At least one `PATTERN` is required. The sub-target is selected with
`--stage`, not a positional — a bare positional after a variadic argument can't be told apart from one more pattern.
Each matched env runs independently against the same handler set; `--json` emits one `started`/`finished`-bracketed
event stream per env, in the same NDJSON stream.

## Relationship to `winter ws init`

`winter ws init` is structural: it creates worktrees, branches, allocates a stable env index, copies git identity,
writes excludes, and fires `on_env_init` hooks. It also runs each repo's `cmd` list — that list is a lightweight
trust/bootstrap step (e.g. `mise trust`, `direnv allow`) rather than full dependency installation. Runtime environment
variables are computed and injected by `winter service` or printed by `winter env <name>`.

Run `winter provision <env>` after `winter ws init` to bring the environment to a working state: install dependencies,
provision resources, and load seed data. For project-specific readiness steps not yet migrated to `[[provision.*]]`
handlers, also follow `workspace:/context/project/project-setup.md`.

## Action vocabulary

Three action flags modify the default behaviour. They are shared across `resource` and `data` sub-targets (and validated
accordingly):

| Invocation        | Behaviour                                                                                                                     |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| bare (no flag)    | **apply** — run `apply` handler; idempotent to baseline. For `data`, apply is wipe-and-reload, not append.                    |
| `--destroy`       | Run the declared `destroy` handler; if none declared, warn and no-op.                                                         |
| `--reset`         | Use the declared `reset` handler if present; else compose destroy + apply when both exist; else warn and degrade to re-apply. |
| `resource --seed` | Apply `resource`, then apply `data`.                                                                                          |

Authors guarantee idempotency; winter tracks no state between runs.

**Flag validation:**

- `--reset` and `--destroy` together are rejected.
- `--seed` is valid only on `resource`, not on `dependency` or `data`; it also cannot be combined with `--name`.
- Any action flag (`--reset`, `--destroy`) requires an explicit sub-target (`--stage`) **or** a `--name` selector — not
  the bare full-chain form.
- `--dry-run` may be combined with any action flag, sub-target, or `--name` selector: it previews what the given
  invocation would do.

## Named resource selector (`--name`)

`--name <scope>.<name>` targets a single named `[[provision.*]]` entry (see
[configuration/provision.md#name-field](../configuration/provision.md#name-field)) for apply (bare), `--destroy`, or
`--reset` — every sibling handler, including others in the same sub-target, is skipped. `--stage` is optional when
`--name` is given; when both are present, the resolved handler must belong to the given `--stage` or the run aborts with
a clear error before anything runs.

`<scope>` is one of three short tokens — **not** the hyphenated `scope` field spelling used in the manifest:

| Selector token | Manifest `scope` value |
| -------------- | ---------------------- |
| `workspace`    | `workspace`            |
| `feature`      | `feature-environment`  |
| `worktree`     | `feature-worktree`     |

```bash
winter provision alpha --name workspace.mydb              # apply just the workspace-scope "mydb" entry
winter provision alpha --name feature.mydb --reset         # reset the feature-environment-scope "mydb" entry
winter provision alpha --name workspace.mydb --destroy     # destroy just that entry
```

An unresolvable selector — malformed shape, unknown scope token, or no entry with that name in that scope — **aborts
with a hard error naming the selector**; there is no skip-with-warning fallback (mirrors the `project` field's
missing-worktree hard error in [configuration/provision.md#project-field](../configuration/provision.md#project-field)).
`--dry-run` and `--json` plan output reflect the narrowed selection: only the resolved handler's `plan_handler` event(s)
appear.

## Manifest schema

The `[[provision.*]]` shape an author declares is owned by [configuration/provision.md](../configuration/provision.md).
This page covers running the handlers it declares.

## Scope and ordering

### Sub-target order

When the bare `winter provision <env>` full-chain form is used, sub-targets run in this fixed order:

```text
dependency → resource → data
```

A handler apply failure in any sub-target aborts the remaining sub-targets (failure is non-zero exit from any command in
the handler).

A sub-target with no declared handlers is a no-op; provision reports that no handlers are declared for it.

### Handler order within a sub-target

Within a sub-target, handlers run substrate-first by scope, with workspace-config handlers before extension handlers
within the same scope:

```text
workspace (config) → workspace (extensions) →
feature-environment (config) → feature-environment (extensions) →
feature-worktree (config) → feature-worktree (extensions)
```

The working directory and the environment variables each scope's handler receives are part of the manifest contract —
see
[configuration/provision.md#scope-working-directory-and-environment](../configuration/provision.md#scope-working-directory-and-environment).

## Service check (`required_services`)

When a `resource` or `data` handler declares `required_services`, winter checks those services are running before
executing the handler.

A `required_services` token must be scoped as `workspace/<service>` or `<current-env>/<service>`. A foreign env
reference (e.g. `beta/postgres` when provisioning `alpha`) is rejected.

**Without `--no-service-check`:**

- Each declared service is checked via `winter service status` (running-state, not health — health is
  observability-only).
- Any services that are not running are started by bringing up their owning scope: `winter service up workspace` or
  `winter service up <env>`.
- Started services are left running after provision completes.

**With `--no-service-check`:** the service check is skipped entirely. Use this when the service is known to be up or
when running in an environment without a registered orchestrator.

**Missing orchestrator:** if `required_services` is declared but no service orchestrator is registered in the workspace,
`winter provision` exits non-zero with a clean error message. Cross-link: see [service.md](./service.md) for the service
contract, including how orchestrators are registered.

**Static lint counterpart:** `winter lint` runs a `required-services` core check that validates `required_services`
entries against the merged service catalog from all bound providers — ahead of any provisioning run. The lint check
validates catalog membership (does any provider declare this service name?) while `winter provision`'s runtime service
check additionally enforces that the service is currently running in the correct env scope. Use both: lint catches typos
and missing catalog entries before any env is provisioned; the runtime check ensures services are actually up before
scripts execute. See [configuration/lint.md#built-in-core-checks](../configuration/lint.md#built-in-core-checks) for the
`required-services` lint check description.

## `--dry-run`

`--dry-run` prints the ordered list of handlers that **would** run without executing any commands or starting any
service:

- Per-handler output: sub-target, scope, source, the commands that would run (joined with `&&` for display when
  multiple), resolved action (apply / destroy / reset — or `clean` when this same preview contract is run via
  [`winter clean`](./clean.md)), resolved `project` cwd (when set), and which `required_services` it would check (if
  any).
- A sub-target with no declared handlers is reported as a no-op.
- No mutation occurs: no commands run, no `winter service up` calls are made.
- See the flag validation rules under [Action vocabulary](#action-vocabulary), above, for what `--dry-run` can be
  combined with; it previews that specific path without executing it.

`--dry-run --json` emits the same NDJSON stream as a real run (see below), replacing `execution_*` and `handler_result`
events with `plan_handler` events — one per resolved action in plan order. (A handler with `--reset` that has no `reset`
field but does have a `destroy` field emits two events: a `destroy` then an `apply`.) The `plan_handler` event includes
a `project` key (`null` or the project name) so the resolved cwd is visible in the structured output.

**`--destroy` and `--reset` diverge from their real runs on warnings, but not the same way.** A real `--destroy` run
emits a `handler_warn` for a handler with no declared `destroy`; the `--dry-run` plan is silent for that handler instead
— it is simply absent from the `plan_handler` events, with no matching warn. A `--destroy --dry-run --json` consumer can
still tell the two "nothing to run" cases apart, though: an empty sub-target (no handler matched at all) emits
`no_handlers`, while a handler that matched but has no `destroy` script emits neither `no_handlers` nor `plan_handler`
for that handler.

A real `--reset` run emits a `handler_warn` for a handler with neither `reset` nor `destroy` declared, then runs `apply`
as the degraded action. The `--dry-run` plan for that same handler shows the degrade — a `plan_handler` event with
`action: "apply"` — it just omits the accompanying `handler_warn`.

`clean` does not share either gap: an explicit `--name` selector naming a handler with no declared `clean` emits a
`handler_warn` in the `--dry-run` plan too, matching what a real `clean` run does — see
[clean.md § Named entry selector (`--name`)](./clean.md#named-entry-selector---name).

## `--json` output

`--json` emits NDJSON, one JSON object per line. This event stream is shared with [`winter clean`](./clean.md) —
`IProvisionReporter` is the same reporter for both verbs, so the event vocabulary and field shapes below are one table,
not two; the `started` event's `action` field reads `"clean"` only for a `winter clean` run, never for
`winter provision` itself. The event stream:

| `type`                  | When emitted                                                                                                                   | Key fields                                                                                                                                                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `started`               | Beginning of the run                                                                                                           | `env`, `subtargets` (ordered list of sub-targets to run), `action` (run verb: `apply` / `destroy` / `reset` / `clean`)                                                                                                                          |
| `subtarget_started`     | Before each sub-target                                                                                                         | `subtarget`                                                                                                                                                                                                                                     |
| `no_handlers`           | Sub-target has no declared handlers — for a `clean` run, also fires when the sub-target has handlers but none declares `clean` | `subtarget`                                                                                                                                                                                                                                     |
| `execution_started`     | Before each command sequence invocation (one per cwd)                                                                          | `label`, `action`, `cwd`                                                                                                                                                                                                                        |
| `execution_output_line` | Each line from the running command                                                                                             | `label`, `line`                                                                                                                                                                                                                                 |
| `execution_completed`   | Command sequence finished for this cwd                                                                                         | `label`, `action`, `exit_status`                                                                                                                                                                                                                |
| `execution_error`       | Command could not be launched                                                                                                  | `label`, `error`                                                                                                                                                                                                                                |
| `handler_result`        | Summary after a handler completes                                                                                              | `subtarget`, `scope`, `source`, `action`, `service_check`, `runs:[{cwd, exit_status}]`, `exit_status`                                                                                                                                           |
| `handler_warn`          | Degraded action (e.g. no destroy handler)                                                                                      | `subtarget`, `scope`, `source`, `message`                                                                                                                                                                                                       |
| `finished`              | End of the run                                                                                                                 | `status` (`"ok"` / `"aborted"` / `"error"` — for a `clean` run, `"error"` means one or more handlers failed and the run continued to completion, not that it stopped), `aborted_at` (always present; sub-target name when aborted, else `null`) |
| `plan_handler`          | (`--dry-run` only) Handler that would run                                                                                      | `would_run: true`, `subtarget`, `scope`, `source`, `commands`, `action`, `required_services`, `service_check_preview`                                                                                                                           |

**`plan_handler` fields** (emitted only with `--dry-run --json`):

| Field                   | Type            | Meaning                                                                                                                                      |
| ----------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `would_run`             | `true`          | Always `true`; distinguishes plan events from real-run events                                                                                |
| `subtarget`             | string          | Sub-target name (`dependency`, `resource`, `data`)                                                                                           |
| `scope`                 | string          | Handler scope (`workspace`, `feature-environment`, `feature-worktree`)                                                                       |
| `source`                | string          | Declaring source (`project` or extension prefix)                                                                                             |
| `commands`              | list of strings | Ordered list of shell commands that would run (each via `sh -c`)                                                                             |
| `action`                | string          | Resolved action (`apply`, `destroy`, `reset`, or `clean`)                                                                                    |
| `required_services`     | list of strings | `required_services` tokens from the handler declaration                                                                                      |
| `service_check_preview` | string or null  | Comma-separated owning scopes that would be checked/started; `null` when no `required_services`                                              |
| `project`               | string or null  | Resolved project name when `project` is declared; `null` otherwise. Identifies the worktree subdir `<env>/<project>/` that would be the cwd. |

**`service_check` field values in `handler_result`:**

| Value                         | Meaning                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| `null`                        | No `required_services` declared for this handler                   |
| `"skipped"`                   | `--no-service-check` was passed                                    |
| `"ok"`                        | All required services were already running                         |
| `"started:<scope>[,<scope>]"` | Winter started the listed owning scopes before running the handler |

## Doctor probe

`winter doctor` includes a built-in `[provision]` probe that validates every declared `[[provision.*]]` manifest entry —
from both `.winter/config.toml` and installed extension `winter-ext.toml` files — against the manifest rules in
[configuration/provision.md](../configuration/provision.md#validation). It reports one finding per bad entry without
aborting other doctor checks.

See [doctor.md](./doctor.md) for the full doctor probe contract.

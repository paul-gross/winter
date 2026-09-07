# Provision handlers

The `[[provision.*]]` manifest — the handlers `winter provision` runs to bring a feature environment to a working state.
This page owns the declarable shape and its validation rules; for running the command (actions, ordering, service check,
output), see [../usage/provision.md](../usage/provision.md). The same manifest also backs `winter clean`, which runs
each handler's declared `clean` command instead of `apply`/`destroy`/`reset` — see
[../usage/clean.md](../usage/clean.md) for that command's surface.

Handlers are declared with the **same shape** in two places — the workspace config (`.winter/config.toml`) and each
installed extension's `winter-ext.toml` — as an array-of-tables under one of the three sub-targets: `dependency`,
`resource`, `data`.

## Workspace config (`.winter/config.toml`)

```toml
[[provision.dependency]]
scope = "feature-worktree"
# Single inline command (string form):
apply = "uv sync && mise trust"

[[provision.resource]]
scope            = "workspace"
# Array form — commands run in order; stop at first non-zero exit:
apply            = ["createdb myapp", "psql myapp -f schema.sql"]
destroy          = "dropdb --if-exists myapp"
clean            = "rm -rf .cache/myapp-dump"
required_services = ["workspace/postgres"]

[[provision.data]]
scope            = "feature-environment"
apply            = "$WINTER_WORKSPACE_DIR/.winter/config/provision/seed.sh"
reset            = "$WINTER_WORKSPACE_DIR/.winter/config/provision/reseed.sh"
required_services = ["workspace/postgres"]
```

## Extension manifest (`winter-ext.toml`)

Extensions declare the same shape under `[[provision.*]]` in their `winter-ext.toml`; a command that invokes a script in
the extension locates it via `$WINTER_EXT_DIR`:

```toml
[[provision.dependency]]
scope = "feature-worktree"
apply = "npm ci"

[[provision.resource]]
scope   = "workspace"
apply   = ["createdb myapp_ext", "psql myapp_ext -f schema.sql"]
destroy = "dropdb --if-exists myapp_ext"
```

## Per-entry fields

| Field               | Required | Meaning                                                                                                                                                                                                                                                                                                         |
| ------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scope`             | yes      | Where the handler runs (see [Scope: working directory and environment](#scope-working-directory-and-environment) below). One of `workspace`, `feature-environment`, `feature-worktree`.                                                                                                                         |
| `apply`             | yes      | Inline shell command (string) or list of inline shell commands (array), run via `sh -c`.                                                                                                                                                                                                                        |
| `destroy`           | no       | Inline shell command (string) or list (array) run by `--destroy`. If absent, `--destroy` warns and no-ops.                                                                                                                                                                                                      |
| `reset`             | no       | Inline shell command (string) or list (array) run by `--reset`. If absent, winter composes destroy + apply when both exist; otherwise warns and degrades to re-apply.                                                                                                                                           |
| `clean`             | no       | Inline shell command (string) or list (array) that removes this handler's build output, caches, or other artifacts a `.gitignore` entry would hide. If absent, the handler contributes nothing to a `clean` run — no error. The runtime behavior is [../usage/clean.md#what-runs](../usage/clean.md#what-runs). |
| `required_services` | no       | Services that must be running before this handler executes (valid only on `resource` and `data` — rejected on `dependency`). The runtime check is [../usage/provision.md#service-check-required_services](../usage/provision.md#service-check-required_services).                                               |
| `project`           | no       | Project repo name (must be a declared `[[project_repository]]`). Valid only on `feature-environment` scope. When set, the handler's cwd is `<workspace>/<env>/<project>/` instead of the env root. See [Project field](#project-field) below.                                                                   |
| `name`              | no       | Identifies this entry for single-handler targeting (`winter provision <env> --name <scope>.<name>`, or `winter clean <env> --name <scope>.<name>`). Must be unique within its `scope` grouping — see [Name field](#name-field) below.                                                                           |

**Sub-targets:** `dependency`, `resource`, `data`. Unknown sub-target keys (e.g. `[[provision.custom]]`) are rejected.
Unknown per-entry keys are also rejected.

## Command execution semantics

Each command (string, or each element of an array) runs via `sh -c "<command>"`. This means:

- Shell constructs work: `&&`, `||`, pipes (`|`), `$VAR` expansion, globs, and subshells.
- **Array elements run in declaration order.** Execution within a scope stops at the first non-zero exit; that exit code
  is the handler's result for that scope.
- For `feature-worktree` scope the full command sequence runs once per project worktree; each worktree is an independent
  execution (a failure in one worktree does not automatically skip others — the service layer owns that policy).
- There is no path-escape guard, no `is_file` check, and no executable-bit check. To invoke a script, write it as a
  command and locate it via an environment variable (below) — and either mark it executable (`chmod +x`) or invoke it
  through an interpreter (`sh $WINTER_WORKSPACE_DIR/…/seed.sh`).

## Scope: working directory and environment

`scope` selects where a handler runs, and with it the working directory and the environment variables the handler
receives.

### Working directory by scope

| Scope                 | Working directory | Notes                                                                          |
| --------------------- | ----------------- | ------------------------------------------------------------------------------ |
| `workspace`           | workspace root    | `<workspace>/`                                                                 |
| `feature-environment` | env root          | `<workspace>/<env>/` — or `<workspace>/<env>/<project>/` when `project` is set |
| `feature-worktree`    | per-repo worktree | `<workspace>/<env>/<repo>/` — runs ONCE PER PROJECT WORKTREE in the env        |

### Environment variables

All handlers receive `WINTER_WORKSPACE_DIR` plus the four extension-identity vars. `feature-environment` and
`feature-worktree` handlers additionally receive the env-var trio:

| Var                     | Meaning                                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `WINTER_WORKSPACE_DIR`  | Absolute path to the workspace root                                                                        |
| `WINTER_EXT_DIR`        | Absolute path to the extension repo (workspace root for project-source handlers)                           |
| `WINTER_EXT_PREFIX`     | The extension's resolved symlink prefix (`"project"` for project-source handlers)                          |
| `WINTER_EXT_CONFIG_DIR` | Absolute path to the extension's writable config directory                                                 |
| `WINTER_SERVICE_PREFIX` | The resolved workspace-level service-orchestration namespace prefix. Workspace-invariant — always present. |
| `WINTER_ENV`            | The env name (`alpha`, `beta`, …) — feature-environment/feature-worktree only                              |
| `WINTER_ENV_INDEX`      | The persisted port-offset index for this env — feature-environment/feature-worktree only                   |
| `WINTER_PORT_BASE`      | `base_port + ports_per_env * WINTER_ENV_INDEX` — feature-environment/feature-worktree only                 |

`workspace`-scope handlers receive the four base vars above but not the trio (same pattern as `on_workspace_reconcile`
hooks — see [extensions.md](./extensions.md#hook-env-var-contract)).

## Project field

`project` targets a `feature-environment` handler at a specific repo worktree rather than the env root. Use it when the
handler's tooling lives in one repo (e.g. a migration runner, a seed script) and you want a validated cwd rather than
hand-rolling `cd <repo>` inside the inline command.

```toml
[[provision.data]]
scope   = "feature-environment"
project = "web"
apply   = "bundle exec rails db:seed"
```

**Rules:**

- `project` is only valid on `feature-environment` scope. Declaring it on `workspace` or `feature-worktree` is a
  `ConfigError` at parse time and a `[provision]` doctor finding.
- The value must be the exact name of a declared `[[project_repository]]`. An undeclared name is a `ConfigError` at
  parse time and a `[provision]` doctor finding.
- If `project` is set and the named worktree does not exist in the target env at provision time, `winter provision`
  **aborts with a hard error** naming the missing project and env. There is no skip-with-warning fallback — use
  `winter ws init <env>` to create the worktree first.
- `--dry-run` / `--json` plan output shows the resolved `project` value so you can verify the target before running.

## Name field

`name` identifies an entry so it can be targeted individually — apply, `--destroy`, `--reset`, or `winter clean` —
instead of running the whole stage it belongs to. Declaring `name` is optional; entries without it remain reachable only
via whole-stage actions.

```toml
[[provision.resource]]
scope   = "workspace"
name    = "postgres-db"
apply   = ["createdb myapp", "psql myapp -f schema.sql"]
destroy = "dropdb --if-exists myapp"

[[provision.resource]]
scope   = "workspace"
name    = "message-queue"
apply   = "rabbitmqadmin declare vhost name=myapp"
destroy = "rabbitmqadmin delete vhost name=myapp"
```

**Rules:**

- `name` must be a non-empty string.
- `name` must be unique within its `scope` grouping — across every sub-target (`dependency`/`resource`/`data`) declared
  in the same manifest source (workspace `.winter/config.toml`, or one extension's `winter-ext.toml`). The same short
  name may be reused under a *different* scope without conflict (e.g. a `workspace`-scope `mydb` and a
  `feature-environment`-scope `mydb` coexist). A collision is a `ConfigError` at parse time and a `[provision]` doctor
  finding.
- Targeting a named entry uses the scope-qualified selector `<scope>.<name>` on `winter provision` — see
  [../usage/provision.md#named-resource-selector---name](../usage/provision.md#named-resource-selector---name) — or on
  `winter clean` — see [../usage/clean.md#named-entry-selector---name](../usage/clean.md#named-entry-selector---name).

## Validation

The following values are rejected at parse time (`ConfigError`) and flagged by the doctor `[provision]` probe:

- An empty string (`""`)
- An empty list (`[]`)
- A list containing any non-string or empty-string element
- A value that is neither a string nor a list
- `project` on `workspace` or `feature-worktree` scope
- `project` naming a repo not declared in `[[project_repository]]`
- `name` that is not a non-empty string
- `name` duplicating another entry's name within the same `scope` grouping

The full doctor `[provision]` probe is [../usage/provision.md#doctor-probe](../usage/provision.md#doctor-probe).
